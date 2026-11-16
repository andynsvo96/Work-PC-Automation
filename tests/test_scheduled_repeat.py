"""Delayed repeating queue runs retain their timing and existing serialization."""

from contextlib import ExitStack
from datetime import datetime, timedelta
import unittest
from unittest import mock

import server


class Clock(datetime):
    value = datetime(2026, 11, 16, 14, 0)

    @classmethod
    def now(cls, tz=None):
        return cls.value if tz is None else cls.value.astimezone(tz)


class ScheduledRepeatTests(unittest.TestCase):
    def setUp(self):
        Clock.value = datetime(2026, 11, 16, 14, 0)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (("datetime", Clock), ("automation_queue_tasks", []), ("automation_queue_current_task_id", None)):
            self.stack.enter_context(mock.patch.object(server, name, value))
        for name in ("_ensure_automation_queue_worker", "log_automation_event"):
            self.stack.enter_context(mock.patch.object(server, name))
        self.stack.enter_context(mock.patch.object(server, "_automation_version_block_reason", return_value=""))
        self.stack.enter_context(mock.patch.object(server, "_persist_automation_queue_locked", return_value=True))
        self.stack.enter_context(mock.patch.object(server, "_automation_queue_result_context", return_value={}))

    def enqueue(self, **options):
        return server.enqueue_automation("Scheduled repeat", "Processing", lambda: (True, "Finished"), **{
            "queue_mode": "repeat", "repeat_interval_minutes": 30, "scheduled_for": "15:00", **options,
        })

    def run_once(self):
        def audit(_name, status, *_args, **_kwargs):
            if status in {"COMPLETED", "FAILED", "CANCELED"}:
                raise StopIteration
        with mock.patch.object(server, "log_automation_event", side_effect=audit), self.assertRaises(StopIteration):
            server._automation_queue_worker_loop()

    def test_waits_for_initial_schedule_then_repeats_at_the_selected_interval(self):
        ok, message, payload = self.enqueue()
        self.assertTrue(ok, message)
        self.assertEqual(payload["status"], "idle")
        self.assertEqual(payload["idle_reason"], "scheduled_wait")
        self.assertEqual(payload["next_run_at"], "2026-11-16T15:00:00")
        self.assertIn("3:00 PM", payload["message"])
        self.assertEqual(server._automation_queue_promote_due_idle_locked(), [])
        Clock.value = datetime(2026, 11, 16, 15, 0)
        self.run_once()
        task = server.automation_queue_tasks[0]
        self.assertEqual(task["run_count"], 1)
        self.assertEqual(task["status"], "idle")
        self.assertEqual(task["idle_reason"], "repeat_wait")
        self.assertEqual(task["next_run_at"], "2026-11-16T15:30:00")
        Clock.value += timedelta(minutes=30)
        self.run_once()
        self.assertEqual(task["run_count"], 2)
        self.assertEqual(task["next_run_at"], "2026-11-16T16:00:00")
        self.assertEqual(len(task["run_history"]), 2)

    def test_scheduled_repeat_keeps_its_cadence_after_a_long_run_without_catching_up(self):
        self.enqueue()
        task = server.automation_queue_tasks[0]
        def work():
            Clock.value = datetime(2026, 11, 16, 16, 12)
            return True, "Finished long run"
        task["fn"] = mock.Mock(side_effect=work)
        Clock.value = datetime(2026, 11, 16, 15, 0)
        self.run_once()
        self.assertEqual(task["next_run_at"], "2026-11-16T16:30:00")
        task["fn"].assert_called_once()

    def test_rejects_missing_invalid_current_and_past_times_without_starting_a_worker(self):
        for mode in ("scheduled", "repeat"):
            for value in ("", "bad", "13:59", "14:00", "2026-11-15T15:00:00"):
                with self.subTest(mode=mode, time=value):
                    ok, _, task = self.enqueue(queue_mode=mode, scheduled_for=value)
                    self.assertFalse(ok)
                    self.assertIsNone(task)
        self.assertFalse(self.enqueue(queue_mode="scheduled", scheduled_for=None)[0])
        self.assertEqual(server.automation_queue_tasks, [])
        server._ensure_automation_queue_worker.assert_not_called()

    def test_repeat_only_still_starts_immediately_and_counts_interval_from_completion(self):
        ok, message, task = self.enqueue(scheduled_for=None)
        self.assertTrue(ok, message)
        self.assertEqual(task["status"], "queued")
        self.run_once()
        self.assertEqual(server.automation_queue_tasks[0]["next_run_at"], "2026-11-16T14:30:00")

    def test_combined_mode_cannot_start_immediately_when_its_scheduled_time_is_missing(self):
        with mock.patch.object(server, "APP_PIN_REQUIRED", False):
            for endpoint in ("/crm/process", "/crm/mass-emailer"):
                with self.subTest(endpoint=endpoint):
                    response = server.app.test_client().post(endpoint, json={
                        "advanced_mode": "scheduled_repeat", "repeat_interval_minutes": 30,
                    })
                    self.assertFalse(response.json["success"])
                    self.assertIsNone(response.json["queue_task"])
        self.assertEqual(server.automation_queue_tasks, [])
        server._ensure_automation_queue_worker.assert_not_called()

    def test_zero_minute_repeat_waits_for_schedule_then_repeats_immediately(self):
        self.enqueue(repeat_interval_minutes=0)
        self.assertEqual(server.automation_queue_tasks[0]["status"], "idle")
        Clock.value = datetime(2026, 11, 16, 15, 0)
        self.run_once()
        self.assertEqual(server.automation_queue_tasks[0]["next_run_at"], "2026-11-16T15:00:00")

    def test_cancellation_before_the_start_never_runs(self):
        self.enqueue()
        task = server.automation_queue_tasks[0]
        self.assertTrue(server.cancel_automation_queue_task(task["id"])[0])
        Clock.value = datetime(2026, 11, 16, 15, 30)
        self.assertEqual(server._automation_queue_promote_due_idle_locked(), [])
        self.assertEqual(task["status"], "canceled")
        self.assertEqual(task["run_count"], 0)


if __name__ == "__main__":
    unittest.main()
