import json
import tempfile
import unittest
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from unittest import mock

import server
from runtime_paths import DEBUG_DIR


def purchase(vendor="ssactivewear", order_id="5000001"):
    return {"vendor": vendor, "state": "confirmed", "confirmation": {
        "po": order_id, "po_confirmed": True, "order_number": "98765432",
        "url": "https://www.ssactivewear.com/checkout/orderconfirmation?orderguid=fixture",
    }}


class ReorderBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reorder-batch-", dir=DEBUG_DIR)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.json"
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in {
            "AUTOMATION_QUEUE_STATE_FILE": str(self.path),
            "automation_queue_tasks": [], "automation_queue_current_task_id": None,
            "automation_queue_persistence_enabled": False, "automation_queue_persistence_error": None,
            "crm_reorder_runtime": {},
        }.items():
            stack.enter_context(mock.patch.object(server, name, value))
        for name in ("_ensure_automation_queue_worker", "log_automation_event", "_audit_result"):
            stack.enter_context(mock.patch.object(server, name))
        stack.enter_context(mock.patch.object(server, "_automation_version_block_reason", return_value=""))
        stack.enter_context(mock.patch.object(server, "_automation_stop_requested_since", return_value=False))
        stack.enter_context(mock.patch.object(server, "_automation_stop_is_blocking", return_value=False))
        server.restore_automation_queue()
        arguments = {"order_ids": ["5000001", "5000002", "5000003"]}
        ok, message, _ = server.enqueue_automation(
            "Reorder Stock — 3 orders", "Processing", partial(server.run_crm_reorder_batch_queued, **arguments),
            task_type="crm.reorder_batch", task_arguments=arguments, status_fn=server.get_crm_reorder_status_payload,
        )
        self.assertTrue(ok, message)
        self.task = server.automation_queue_tasks[0]

    def run_batch(self):
        self.task["status"] = "running"
        server.automation_queue_current_task_id = self.task["id"]
        result = self.task["fn"]()
        self.task["status"] = "completed" if result[0] else "failed"
        return result

    def rows(self):
        return self.task["result_context"]["report"]["order_details"]

    def test_one_batch_continues_after_failures_and_retains_real_confirmation_links(self):
        results = [
            (True, "Stock reordered", {"groups": [purchase()]}),
            (False, "CRM note did not persist", {"groups": [purchase(order_id="5000002")]}),
            RuntimeError("Fixture browser failed"),
        ]
        with mock.patch.object(server, "_run_script", side_effect=results) as run:
            ok, message = self.run_batch()
        self.assertFalse(ok)
        self.assertIn("1 succeeded; 2 failed", message)
        self.assertEqual([call.args[1] for call in run.call_args_list], [["--order-id", order_id] for order_id in self.task["task_arguments"]["order_ids"]])
        rows = self.rows()
        self.assertEqual([row["status"] for row in rows], ["Success", "Failed", "Failed"])
        self.assertIn("did not persist", rows[1]["message"])
        self.assertEqual(rows[2]["message"], "Fixture browser failed")
        self.assertEqual(rows[0]["purchases"][0]["url"], purchase()["confirmation"]["url"])
        self.assertEqual(rows[1]["purchases"][0]["reference"], "98765432")
        self.assertEqual(json.loads(self.path.read_text())["tasks"][0]["result_context"]["report"]["order_details"], rows)

    def test_retry_keeps_the_same_entry_and_skips_successful_orders(self):
        with mock.patch.object(server, "_run_script", side_effect=[
            (True, "Purchased", {"groups": [purchase()]}), (False, "Needs review", {}),
            (True, "No red stock", {"outcome": "no_red_stock"}),
        ]):
            self.run_batch()
        successful = self.rows()[0].copy()
        self.assertTrue(server.retry_automation_queue_task(self.task["id"])[0])
        self.assertEqual(len(server.automation_queue_tasks), 1)
        with mock.patch.object(server, "_run_script", return_value=(True, "Recovered", {})) as run:
            self.assertTrue(self.run_batch()[0])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[1], ["--order-id", "5000002"])
        self.assertEqual(self.rows()[0], successful)
        self.assertEqual([row["order_id"] for row in self.rows()], ["5000001", "5000002", "5000003"])

    def test_cancel_stops_before_the_next_order_and_retains_finished_results(self):
        def run(*_args, **_kwargs):
            self.task["cancel_requested"] = True
            return True, "Purchased", {"groups": [purchase()]}
        with mock.patch.object(server, "_run_script", side_effect=run) as worker:
            self.assertFalse(self.run_batch()[0])
        self.assertEqual(worker.call_count, 1)
        self.assertEqual([row["status"] for row in self.rows()], ["Success", "Not run", "Not run"])

    def test_restart_restores_checkpoints_and_retry_does_not_repeat_a_finished_purchase(self):
        with mock.patch.object(server, "_run_script", side_effect=[
            (True, "Purchased", {"groups": [purchase()]}), SystemExit("Interrupted worker"),
        ]), self.assertRaises(SystemExit):
            self.run_batch()
        server.automation_queue_persistence_enabled = False
        server.automation_queue_tasks = []
        server.automation_queue_current_task_id = None
        server.restore_automation_queue()
        self.task = server.automation_queue_tasks[0]
        self.assertTrue(self.task["restart_interrupted"])
        self.assertEqual(self.task["status"], "failed")
        self.assertEqual(self.rows()[0]["status"], "Success")
        self.assertTrue(server.retry_automation_queue_task(self.task["id"])[0])
        with mock.patch.object(server, "_run_script", return_value=(True, "Recovered", {})) as run:
            self.assertTrue(self.run_batch()[0])
        self.assertEqual([call.args[1] for call in run.call_args_list], [["--order-id", "5000002"], ["--order-id", "5000003"]])
        self.assertTrue(self.rows()[0]["purchases"])

    def test_queued_report_lists_every_input_and_invalid_input_never_launches_a_worker(self):
        report = server._automation_queue_task_payload(self.task)["result_context"]["report"]
        self.assertEqual([row["status"] for row in report["order_details"]], ["Waiting"] * 3)
        with mock.patch.object(server, "_run_script") as run:
            for ids in ([], ["5000001", "bad"], "https://crm.example/report/1234567"):
                self.assertFalse(server.run_crm_reorder_batch_queued(ids)[0])
            run.assert_not_called()

    def test_batch_holds_the_existing_crm_lock_through_all_orders(self):
        def run(*_args, **_kwargs):
            self.assertFalse(server.crm_lock.acquire(blocking=False))
            return True, "Done", {}
        with mock.patch.object(server, "_run_script", side_effect=run):
            self.assertTrue(self.run_batch()[0])
        self.assertTrue(server.crm_lock.acquire(blocking=False))
        server.crm_lock.release()

    def test_unconfirmed_or_wrong_po_receipts_do_not_produce_purchase_links(self):
        for group in (purchase(order_id="5000002"), {**purchase(), "state": "uncertain"}):
            row = server._crm_reorder_order_result("5000001", False, "Review", {"groups": [group]})
            self.assertEqual(row["purchases"], [])

    def test_interrupted_report_marks_running_and_unstarted_orders_without_losing_successes(self):
        self.task["status"] = "failed"
        finished = server._crm_reorder_order_result("5000001", True, "Purchased", {"groups": [purchase()]})
        running = {**server._crm_reorder_waiting_result("5000002"), "status": "Running"}
        self.task["result_context"] = {"report": server._crm_reorder_batch_report([finished, running, server._crm_reorder_waiting_result("5000003")])}
        rows = server._automation_queue_task_payload(self.task)["result_context"]["report"]["order_details"]
        self.assertEqual([row["status"] for row in rows], ["Success", "Needs review", "Not run"])
        self.assertTrue(rows[0]["purchases"])
        self.assertIsNone(self.rows()[1]["success"], "Display normalization must not mutate saved checkpoints")

    def test_legacy_single_order_retry_report_uses_the_new_confirmed_receipt(self):
        task = {"task_type": "crm.reorder", "task_arguments": {"order_id": "5000001"},
                "result_context": {"retrying": True},
                "status_fn": lambda: {"runtime": {"payload": {"groups": [purchase()]}}}}
        context = server._automation_queue_result_context(task, True, "Recovered")
        self.assertFalse(context["retrying"])
        self.assertTrue(context["retry_completed"])
        self.assertTrue(context["report"]["order_details"][0]["purchases"])


if __name__ == "__main__":
    unittest.main()
