import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import server
from runtime_paths import DEBUG_DIR


class QueuePersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="queue-persistence-", dir=DEBUG_DIR)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "automation_queue.json"
        patches = ExitStack()
        self.addCleanup(patches.close)
        for name, value in {
            "AUTOMATION_QUEUE_STATE_FILE": str(self.path),
            "automation_queue_tasks": [],
            "automation_queue_current_task_id": None,
            "automation_queue_persistence_enabled": False,
            "automation_queue_persistence_error": None,
        }.items():
            patches.enter_context(mock.patch.object(server, name, value))
        patches.enter_context(mock.patch.object(server, "_ensure_automation_queue_worker"))
        patches.enter_context(mock.patch.object(server, "_automation_version_block_reason", return_value=""))
        patches.enter_context(mock.patch.object(server, "log_automation_event"))
        patches.enter_context(mock.patch.object(server, "_automation_queue_result_context", return_value={"report": {"message": "Saved result"}}))
        server.restore_automation_queue()

    def enqueue(self, label="Slack In", **options):
        options.setdefault("task_type", "communications.slack")
        options.setdefault("task_arguments", {"action": "in"})
        ok, message, payload = server.enqueue_automation(label, "Automation", lambda: (True, "Done"), **options)
        self.assertTrue(ok, message)
        return next(task for task in server.automation_queue_tasks if task["id"] == payload["id"])

    def restart(self):
        server.automation_queue_persistence_enabled = False
        server.automation_queue_tasks = []
        server.automation_queue_current_task_id = None
        server.restore_automation_queue()
        return server.automation_queue_tasks

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))["tasks"]

    @staticmethod
    def stop_after_run(_name, status, *_args, **_kwargs):
        if status in {"COMPLETED", "FAILED", "CANCELED"}:
            raise StopIteration("One queue iteration completed")

    def run_once(self):
        with (
            mock.patch.object(server, "log_automation_event", side_effect=self.stop_after_run),
            self.assertRaises(StopIteration),
        ):
            server._automation_queue_worker_loop()

    def test_enqueue_and_restore_preserve_order_identity_and_arguments(self):
        first = self.enqueue()
        second = self.enqueue("Work Out", task_type="communications.work", task_arguments={"action": "out", "automatic": False})
        first["non_persisted_private_value"] = "must not be saved"
        self.assertTrue(server.reorder_automation_queue([second["id"], first["id"]])[0])
        saved = self.saved()
        self.assertEqual([row["id"] for row in saved], [second["id"], first["id"]])
        for row in saved:
            self.assertNotIn("fn", row)
            self.assertNotIn("status_fn", row)
            self.assertNotIn("non_persisted_private_value", row)
        with mock.patch.object(server, "run_work", return_value=(True, "Clocked out")) as run:
            restored = self.restart()
            self.assertEqual([task["id"] for task in restored], [second["id"], first["id"]])
            run.assert_not_called()
            self.assertEqual(restored[0]["fn"](), (True, "Clocked out"))
            run.assert_called_once_with(action="out", automatic=False)
        self.assertEqual(server.get_automation_queue_payload()["mode"], "local")
        self.assertEqual(server.restore_automation_queue(), 2)

    def test_history_retains_more_than_old_limits_until_manually_cleared(self):
        task = self.enqueue()
        server.automation_queue_tasks[:] = [
            {**task, "id": f"finished-{index}", "status": "completed", "message": f"Result {index}"}
            for index in range(65)
        ]
        waiting = self.enqueue("Waiting")
        scheduled = self.enqueue("Scheduled", queue_mode="scheduled", scheduled_for=(datetime.now() + timedelta(hours=1)).isoformat())
        self.restart()
        payload = server.get_automation_queue_payload()
        self.assertEqual(len(payload["history"]), 65)
        self.assertEqual(len(payload["tasks"]), 67)
        self.assertEqual(payload["history"][-1]["message"], "Result 64")
        self.assertTrue(server.delete_automation_queue_task("finished-0")[0])
        self.assertNotIn("finished-0", {row["id"] for row in self.restart()})
        self.assertTrue(server.clear_finished_automation_queue_tasks()[0])
        self.assertEqual({row["id"] for row in self.restart()}, {waiting["id"], scheduled["id"]})

    def test_cancel_and_retry_survive_restarts_in_same_entry(self):
        task = self.enqueue()
        self.assertTrue(server.cancel_automation_queue_task(task["id"])[0])
        restored = self.restart()[0]
        self.assertEqual(restored["status"], "canceled")
        self.assertTrue(server.get_automation_queue_payload()["tasks"][0]["retryable"])
        self.assertTrue(server.retry_automation_queue_task(task["id"])[0])
        restored = self.restart()[0]
        self.assertEqual(restored["id"], task["id"])
        self.assertEqual(restored["status"], "queued")
        self.assertEqual(restored["result_context"]["retry_count"], 1)
        self.assertTrue(server.cancel_all_automation_queue_tasks()[0])
        self.assertEqual(self.restart()[0]["status"], "canceled")

    def test_schedules_repeat_timing_and_run_history_survive(self):
        due = (datetime.now() + timedelta(hours=2)).isoformat()
        scheduled = self.enqueue("Scheduled", queue_mode="scheduled", scheduled_for=due)
        repeated = self.enqueue("Repeated", queue_mode="repeat", repeat_interval_minutes=0, automation_signature={"action": "in"}, advanced_summary="Repeat immediately")
        repeated.update(status="idle", next_run_at=due, idle_reason="repeat_wait", run_count=3, run_history=[{"run_number": 3, "message": "Last result"}])
        server._persist_automation_queue_locked()
        restored = self.restart()
        self.assertEqual(restored[0]["scheduled_for"], scheduled["scheduled_for"])
        self.assertEqual(restored[0]["next_run_at"], due)
        self.assertEqual(restored[1]["run_history"], repeated["run_history"])
        self.assertEqual(restored[1]["run_count"], 3)
        self.assertEqual(restored[1]["repeat_interval_minutes"], 0)
        self.assertEqual(restored[1]["automation_signature"], {"action": "in"})
        self.assertEqual(restored[1]["advanced_summary"], "Repeat immediately")
        self.assertEqual(server._automation_queue_promote_due_idle_locked(), [])

    def test_interrupted_tasks_are_retained_without_automatic_replay(self):
        communication = self.enqueue()
        crm = self.enqueue("Order Goods Order 5010529", task_type="crm.order_goods", task_arguments={"order_id": "5010529", "dry_run": False}, queue_mode="repeat")
        for task in (communication, crm):
            task.update(status="running", started_at=(datetime.now() - timedelta(minutes=2)).isoformat(), message="Previous progress", result_context={"report": {"message": "Original details"}})
        server._persist_automation_queue_locked()
        restored = self.restart()
        self.assertTrue(all(task["status"] == "failed" for task in restored))
        self.assertTrue(all(task["restart_interrupted"] for task in restored))
        self.assertTrue(all("Interrupted by server restart" in task["message"] for task in restored))
        self.assertEqual(restored[1]["result_context"]["report"]["message"], "Original details")
        self.assertFalse(server._automation_queue_task_is_retryable_order(restored[0]))
        self.assertFalse(server.retry_automation_queue_task(communication["id"])[0])
        self.assertEqual(server.get_automation_queue_payload()["queued_count"], 0)
        self.assertTrue(all(task["status"] == "failed" for task in self.restart()))

    def test_every_manual_extension_request_restores_exact_runner_and_selections(self):
        request = {
            "days": 5, "products": [{"design_item_ids": ["123"], "affected_sizes": ["XL"]}],
            "sleeves": [{"tab_number": 2, "quantity": 25, "left": "ink", "reverse": True}],
            "ink_price": "2.66", "embroidery_price": None, "reverse_price": "5.00",
            "colors": ["Red", "Navy"], "sizes": ["L", "XL"],
            "designs": {"selected_design_ids": ["order-design:901"], "all_designs": [{"order_design_id": "901"}]},
        }
        expected = []
        with mock.patch.object(server, "run_crm_extension_manual_order_run_queued", return_value=(True, "Done")) as run:
            for key, automation in server.CRM_EXTENSION_MANUAL_ORDER_AUTOMATIONS.items():
                with self.subTest(automation=key), mock.patch.dict(automation, {"request_validator": lambda _data: request}):
                    ok, message, payload = server.queue_crm_extension_manual_order_run("5010529", key, reason="Original reason", request_payload=request)
                    self.assertTrue(ok, message)
                    expected.append((payload["id"], key, request if automation.get("structured_request") else None))
            original_arguments = {task["id"]: task["task_arguments"] for task in server.automation_queue_tasks}
            restored = self.restart()
            run.assert_not_called()
            for task, (task_id, key, request_data) in zip(restored, expected):
                with self.subTest(automation=key):
                    self.assertEqual(task["id"], task_id)
                    self.assertEqual(task["task_arguments"], original_arguments[task_id])
                    self.assertNotIn("restore_error", task)
                    task["fn"]()
                    run.assert_called_with(order_id="5010529", automation_key=key, reason="Original reason", request_data=request_data)

    def test_processing_retry_preserves_targeted_plan_and_custom_orders(self):
        arguments = {"processing_filter": "custom", "custom_input_type": "order_ids", "custom_order_ids": ["5010529", "5010526"], "order_goods_enabled": True}
        task = self.enqueue("Main Automation", task_type="crm.processing", task_arguments=arguments)
        task.update(status="failed", result_context={"report": {"step_results": [{"key": "order_goods", "success": False, "errors": [{"order_id": "5010529", "message": "Timed out"}]}]}})
        server._persist_automation_queue_locked()
        self.restart()
        self.assertTrue(server.retry_automation_queue_task(task["id"])[0])
        with mock.patch.object(server, "run_crm_processing_run_queued", return_value=(True, "Done")) as run:
            restored = self.restart()[0]
            restored["fn"]()
            run.assert_called_once_with(**arguments, retry_plan={"order_goods": ["5010529"]})
        self.assertEqual(restored["result_context"]["retry_count"], 1)

    def test_standard_task_types_restore_their_original_arguments(self):
        cases = [
            ("communications.paycom_clock", "run_clock", {"action": "in", "dry_run": True}),
            ("communications.slack", "run_slack", {"action": "out"}),
            ("communications.slack_lunch", "start_slack_lunch_break", {"force_test_url": True}),
            ("communications.work", "run_work", {"action": "in", "automatic": False}),
            ("communications.work_sync", "run_work_sync", {}),
            ("communications.automatic_work_out", "_run_automatic_work_clock_out_queued", {}),
            ("communications.slack_lunch_return", "_run_slack_lunch_return_queued", {"return_message": "back", "force_test_url": True, "scheduled_at": "2026-10-23T13:00:00", "day_name": "Friday"}),
            ("system.power", "_run_system_power_task", {"action": "sleep"}),
            ("crm.stock_unlocker", "run_crm_run_queued", {"dry_run": True}),
            ("crm.address_validator", "run_crm_address_run_queued", {"dry_run": False, "order_id": "5010529", "parallel_workers": 1}),
            ("crm.product_separator", "run_crm_product_separator_run_queued", {"dry_run": True, "list_mode": "all", "parallel_workers": 2}),
            ("crm.order_goods", "run_crm_order_goods_run_queued", {"dry_run": False, "order_id": "5010529"}),
            ("crm.shipping_bypasser", "run_crm_shipping_bypasser_run_queued", {"dry_run": False, "order_id": "5010529", "allow_low_stock_buffer": True}),
            ("crm.push_back", "run_crm_push_back_run_queued", {"dry_run": True, "processing_filter": "rush"}),
            ("crm.auto_splitter", "run_crm_auto_splitter_run_queued", {"dry_run": False, "order_target": "5010529", "minimum_tabs": 10}),
            ("crm.mass_emailer", "run_crm_mass_emailer_run_queued", {"action": "process_queue", "dry_run": True, "retry_errors": True}),
            ("crm.processing", "run_crm_processing_run_queued", {"processing_filter": "custom", "custom_list_url": "https://crm.test/report", "address_validator_enabled": True}),
            ("crm.extension_order", "run_crm_extension_order_run_queued", {"order_id": "5010529", "shipping_too_expensive": True}),
            ("crm.sheet_scanner_order", "run_crm_sheet_scanner_order_queued", {"order_id": "5010529", "process": "copyright_cancel", "reason": "Original reason", "designs": {"selected_design_ids": ["order-design:901"]}, "delete_sheet_row": True, "sheet_row_number": 8}),
        ]
        for task_type, name, arguments in cases:
            with self.subTest(task_type=task_type), mock.patch.object(server, name, autospec=True, return_value=(True, "Done")) as run:
                self.enqueue(task_type, task_type=task_type, task_arguments=arguments)
                restored = self.restart()[-1]
                self.assertNotIn("restore_error", restored)
                run.assert_not_called()
                self.assertEqual(restored["fn"](), (True, "Done"))
                run.assert_called_once_with(**arguments)

    def test_hours_target_and_development_requests_restore_without_changing_scope(self):
        self.enqueue("Hours target", task_type="communications.work_hours_target", task_arguments={"scope": "weekly", "target_hours": 40, "expected_clock_in_at": "2026-10-23T07:00:00"})
        self.enqueue("Test suite", task_type="development.test_suite", task_arguments={"selected_tests": ["slack_in"]})
        with (
            mock.patch.object(server, "run_work", return_value=(True, "Done")) as work,
            mock.patch.object(server, "run_automation_test_suite", return_value=(True, "Done", {"results": []})) as development,
        ):
            restored = self.restart()
            restored[0]["fn"]()
            self.assertEqual(restored[1]["fn"](), (True, "Done"))
            work.assert_called_once_with("out", automatic=False, expected_clock_in_at="2026-10-23T07:00:00")
            development.assert_called_once_with(["slack_in"])

    def test_canceling_scheduled_power_does_not_resurrect_it(self):
        power = self.enqueue("Sleep", task_type="system.power", task_arguments={"action": "sleep"}, queue_mode="scheduled", scheduled_for=(datetime.now() + timedelta(hours=1)).isoformat())
        power["category"] = "System Power"
        waiting = self.enqueue("Waiting")
        with mock.patch.object(server, "cancel_power_countdown"):
            self.assertTrue(server.cancel_scheduled_power_tasks()[0])
        self.assertEqual([task["id"] for task in self.restart()], [waiting["id"]])

    def test_worker_checkpoints_before_execution_and_after_completion(self):
        task = self.enqueue()

        def run():
            self.assertEqual(self.saved()[0]["status"], "running")
            self.assertEqual(self.saved()[0]["run_count"], 1)
            return True, "Confirmed completion"

        task["fn"] = run
        self.run_once()
        self.assertEqual(self.saved()[0]["status"], "completed")
        self.assertEqual(self.saved()[0]["message"], "Confirmed completion")
        self.assertEqual(self.restart()[0]["result_context"]["report"]["message"], "Saved result")

    def test_worker_restores_due_task_and_persists_repeat_wait(self):
        task = self.enqueue(queue_mode="scheduled", scheduled_for=(datetime.now() - timedelta(minutes=1)).isoformat())
        with mock.patch.object(server, "run_slack", return_value=(True, "Confirmed")) as run:
            self.restart()
            self.run_once()
            run.assert_called_once_with(action="in")
        self.assertEqual(self.restart()[0]["status"], "completed")
        self.assertEqual(server.automation_queue_tasks[0]["id"], task["id"])
        repeated = self.enqueue("Repeated", queue_mode="repeat", repeat_interval_minutes=7)
        self.run_once()
        restored = self.restart()[1]
        self.assertEqual(restored["id"], repeated["id"])
        self.assertEqual(restored["status"], "idle")
        self.assertEqual(restored["run_count"], 1)
        self.assertEqual(restored["run_history"][0]["message"], "Done")
        self.assertGreater(datetime.fromisoformat(restored["next_run_at"]), datetime.now())

    def test_restored_queue_respects_safe_sync_startup_lock(self):
        self.enqueue()
        self.restart()
        with (
            mock.patch.object(server, "_automation_version_block_reason", return_value="Update required"),
            mock.patch.object(server.automation_queue_condition, "wait", side_effect=StopIteration),
            mock.patch.object(server, "run_slack") as run,
            self.assertRaises(StopIteration),
        ):
            server._automation_queue_worker_loop()
        run.assert_not_called()
        self.assertEqual(self.saved()[0]["status"], "queued")

    def test_unknown_or_invalid_runner_stays_visible_for_review(self):
        self.enqueue("Unsupported", task_type="unknown.runner")
        self.enqueue("Missing action", task_type="communications.work", task_arguments={})
        for task in self.restart():
            self.assertEqual(task["status"], "failed")
            self.assertIsNone(task["fn"])
            self.assertIn("cannot be restored safely", task["restore_error"])
            self.assertFalse(server.retry_automation_queue_task(task["id"])[0])

    def test_corrupt_or_incompatible_state_is_preserved(self):
        for contents in ("{broken", '{"version":2,"tasks":[]}', '{"version":1,"tasks":[{}]}'):
            with self.subTest(contents=contents):
                self.path.write_text(contents, encoding="utf-8")
                server.automation_queue_persistence_enabled = False
                with self.assertRaisesRegex(RuntimeError, "preserved for recovery"):
                    server.restore_automation_queue()
                self.assertEqual(self.path.read_text(encoding="utf-8"), contents)

    def test_failed_atomic_save_preserves_previous_state_and_does_not_start_work(self):
        task = self.enqueue()
        before = self.path.read_text(encoding="utf-8")
        task["fn"] = mock.Mock()
        with (
            mock.patch.object(server.os, "replace", side_effect=OSError("Disk unavailable")),
            mock.patch.object(server.automation_queue_condition, "wait", side_effect=StopIteration),
            self.assertLogs(server.logger, level="ERROR"),
            self.assertRaises(StopIteration),
        ):
            server._automation_queue_worker_loop()
        task["fn"].assert_not_called()
        self.assertEqual(task["status"], "queued")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertIn("Check disk space", server.get_automation_queue_payload()["persistence_error"])


if __name__ == "__main__":
    unittest.main()
