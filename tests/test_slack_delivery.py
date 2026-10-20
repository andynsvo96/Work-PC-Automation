"""Slack confirmation and retry regressions, without contacting Slack or Paycom."""

from contextlib import ExitStack
import copy
from datetime import datetime
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
import slack_team
import server


class SlackRetryTests(unittest.TestCase):
    def test_worker_does_not_fall_back_or_rotate_after_an_uncertain_send(self):
        with (
            mock.patch.dict(os.environ, {"AUTOMATION_HEADLESS": "1"}),
            mock.patch.object(slack_team, "SLACK_FORCE_HEADLESS", False),
            mock.patch.object(slack_team, "_resolve_day_message", return_value={"message": "signing on"}),
            mock.patch.object(slack_team, "_run_once", return_value=(False, slack_team.SEND_UNCONFIRMED_MESSAGE, False)) as run,
            mock.patch.object(slack_team, "log_automation_event"),
            mock.patch.object(slack_team, "record_slack_day_message_use") as rotate,
        ):
            ok, message = slack_team.run("in")
        self.assertFalse(ok)
        self.assertIn("delivery unconfirmed", message)
        run.assert_called_once()
        rotate.assert_not_called()

    def test_server_never_retries_uncertain_or_missing_worker_results(self):
        for message in (
            slack_team.SEND_UNCONFIRMED_MESSAGE,
            "Slack-in timed out after 150 seconds.",
            "Slack-in failed (exit code 1).",
            "Slack-in exited without a completion result.",
            "Slack-in was force-stopped.",
        ):
            with (
                self.subTest(message=message),
                mock.patch.object(server, "_run_automation_script", return_value=(False, message)) as run,
                mock.patch.object(server.time, "sleep") as sleep,
            ):
                ok, result = server._run_slack_action_with_retry("in", retries=2)
            self.assertFalse(ok)
            self.assertIn("delivery unconfirmed", result)
            run.assert_called_once_with(
                server.SLACK_SCRIPT, "in", "Slack", extra_args=None,
                timeout=server.SLACK_SCRIPT_TIMEOUT_SECONDS, require_result=True,
            )
            sleep.assert_not_called()

    def test_server_retries_pre_submit_failure_but_stops_on_uncertain_retry(self):
        with (
            mock.patch.object(server, "_run_automation_script", side_effect=[
                (False, "Unable to place message text in Slack composer."),
                (False, slack_team.SEND_UNCONFIRMED_MESSAGE),
            ]) as run,
            mock.patch.object(server, "_automation_stop_is_blocking", return_value=False),
            mock.patch.object(server.time, "sleep"),
        ):
            ok, _ = server._run_slack_action_with_retry("in", retries=3)
        self.assertFalse(ok)
        self.assertEqual(run.call_count, 2)

    def test_unconfirmed_send_never_records_successful_post_history(self):
        for failure in (None, RuntimeError("browser disconnected after clicking Send")):
            driver = mock.Mock()
            composer = mock.Mock()
            with (
                self.subTest(failure=failure),
                mock.patch.object(slack_team, "kill_stale_chrome"),
                mock.patch.object(slack_team, "build_chrome_driver", return_value=driver),
                mock.patch.object(slack_team, "open_channel_and_find_composer", return_value=(composer, "", "fixture")),
                mock.patch.object(slack_team, "set_composer_message", return_value=True),
                mock.patch.object(slack_team, "submit_message", return_value=False, side_effect=failure),
                mock.patch.object(slack_team, "safe_take_screenshot"),
                mock.patch.object(slack_team, "safe_driver_quit"),
                mock.patch.object(slack_team, "record_slack_post") as record,
            ):
                ok, message, retryable = slack_team._run_once("in", "signing on", "fixture", "fixture-profile", True)
            self.assertFalse(ok)
            self.assertFalse(retryable)
            self.assertIn("delivery unconfirmed", message)
            record.assert_not_called()

    def test_typing_failure_never_injects_editor_dom_text(self):
        driver = mock.Mock()
        composer = mock.Mock()
        composer.send_keys.side_effect = RuntimeError("keyboard unavailable")
        self.assertFalse(slack_team.set_composer_message(driver, composer, "signing on"))
        driver.execute_script.assert_not_called()

    def test_repeat_queue_stops_uncertain_sends_and_keeps_safe_pre_submit_retries(self):
        for ok, message, expected_status in (
            (False, slack_team.SEND_UNCONFIRMED_MESSAGE, "failed"),
            (False, "Work clock-in completed. Slack failed. Paycom punch was recorded.", "failed"),
            (False, "Unable to place message text in Slack composer.", "idle"),
            (True, "Slack post verified.", "idle"),
        ):
            task = {
                "id": "slack-repeat-fixture", "label": "Work In", "status": "queued",
                "task_type": "communications.work", "queue_mode": "repeat", "repeat_interval_minutes": 0,
                "fn": mock.Mock(return_value=(ok, message)),
            }

            def audit(_name, status, *_args, **_kwargs):
                if status in {"COMPLETED", "FAILED"}:
                    raise StopIteration("End the test after one completed queue iteration")

            with (
                self.subTest(message=message),
                mock.patch.object(server, "automation_queue_tasks", [task]),
                mock.patch.object(server, "automation_queue_current_task_id", None),
                mock.patch.object(server, "_automation_queue_result_context", return_value={}),
                mock.patch.object(server, "log_automation_event", side_effect=audit),
                self.assertRaises(StopIteration),
            ):
                server._automation_queue_worker_loop()
            self.assertEqual(task["status"], expected_status)
            task["fn"].assert_called_once()


class WorkSlackResultTests(unittest.TestCase):
    def run_work(self, action, slack_ok, error=None):
        now = datetime(2026, 10, 20, 12)
        state = server._new_work_state(now.date())
        if action == "out":
            state["active_shift"] = {"date": "2026-10-20", "clock_in_at": "2026-10-20T07:03:00"}
        saves = []
        slack_message = "Slack message verified in the conversation." if slack_ok else slack_team.SEND_UNCONFIRMED_MESSAGE

        def slack(*args, **kwargs):
            self.assertTrue(saves, "The Paycom result must be saved before attempting Slack.")
            if action == "in":
                self.assertIsNotNone(saves[-1]["active_shift"])
            else:
                self.assertIsNone(saves[-1]["active_shift"])
                self.assertIsNotNone(saves[-1]["days"]["2026-10-20"]["clock_out_at"])
            if error:
                raise error
            return slack_ok, slack_message

        with ExitStack() as stack:
            for name, value in {
                "WORK_CLOCK_CAPPED": False,
                "WORK_CLOCK_SYNC_FROM_PAYCOM": False,
                "WORK_CLOCK_RESET_ON_FRIDAY_CLOCK_OUT": False,
            }.items():
                stack.enter_context(mock.patch.object(server, name, value))
            clock = stack.enter_context(mock.patch.object(server, "datetime", wraps=datetime))
            clock.now.return_value = now
            stack.enter_context(mock.patch.object(server, "load_work_state", return_value=state))
            stack.enter_context(mock.patch.object(server, "save_work_state", side_effect=lambda current: saves.append(copy.deepcopy(current))))
            stack.enter_context(mock.patch.object(server, "_infer_active_shift_from_paycom_rows", return_value=(False, "")))
            punch = stack.enter_context(mock.patch.object(server, "_run_clock_action_with_retry", return_value=(True, "Paycom punch completed.")))
            stack.enter_context(mock.patch.object(server, "_run_slack_action_with_retry", side_effect=slack))
            stack.enter_context(mock.patch.object(server, "cancel_auto_clock_out_timer"))
            stack.enter_context(mock.patch.object(server, "_cancel_auto_clock_timer_locked"))
            stack.enter_context(mock.patch.object(server, "refresh_tray_status_from_state"))
            notify = stack.enter_context(mock.patch.object(server, "notify_user"))
            stack.enter_context(mock.patch.object(server, "_audit_result"))
            ok, message = server.run_work(action)
        punch.assert_called_once_with(action, dry_run=False, retries=1, delay_seconds=3)
        return ok, message, notify

    def test_combined_work_failure_retains_punch_and_surfaces_slack_failure(self):
        for action in ("in", "out"):
            with self.subTest(action=action):
                ok, message, notify = self.run_work(action, False)
                self.assertFalse(ok)
                self.assertIn(f"Slack {action} failed", message)
                self.assertIn("Paycom punch was recorded", message)
                self.assertIn(f"use Slack {action.title()} only if needed", message)
                notify.assert_called_once()
                task = {"task_type": "communications.work", "status": "failed", "message": message, "fn": lambda: None}
                self.assertFalse(server._automation_queue_task_is_retryable_order(task))

    def test_combined_work_success_includes_the_slack_confirmation(self):
        for action in ("in", "out"):
            with self.subTest(action=action):
                ok, message, _ = self.run_work(action, True)
                self.assertTrue(ok)
                self.assertIn("Slack message verified in the conversation", message)

    def test_exception_after_punch_still_preserves_state_and_blocks_punch_retry(self):
        for action in ("in", "out"):
            with self.subTest(action=action):
                ok, message, _ = self.run_work(action, False, error=RuntimeError("worker stopped unexpectedly"))
                self.assertFalse(ok)
                self.assertIn("Paycom punch was recorded", message)
                self.assertIn("worker stopped unexpectedly", message)
                task = {"task_type": "communications.work", "status": "failed", "message": message, "fn": lambda: None}
                self.assertFalse(server._automation_queue_task_is_retryable_order(task))

    def test_queue_blocks_slack_only_retry_for_uncertain_delivery(self):
        task = {"task_type": "communications.slack", "status": "failed", "message": slack_team.SEND_UNCONFIRMED_MESSAGE, "fn": lambda: None}
        self.assertFalse(server._automation_queue_task_is_retryable_order(task))

    def test_canceled_work_that_completed_its_punch_cannot_repeat_it(self):
        for action in ("in", "out"):
            task = {"task_type": "communications.work", "status": "canceled", "message": f"Work clock-{action} completed. Slack verified.", "fn": lambda: None}
            self.assertFalse(server._automation_queue_task_is_retryable_order(task))


if __name__ == "__main__":
    unittest.main()
