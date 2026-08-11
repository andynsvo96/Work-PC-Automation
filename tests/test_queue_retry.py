import unittest
from unittest import mock

import server


class LocalQueueRetryTests(unittest.TestCase):
    @staticmethod
    def _order_task(status="canceled"):
        return {
            "id": "order-task-1",
            "label": "Complicated EMB to HDD Order 5010529",
            "category": "Processing",
            "details": "Single CRM order 5010529",
            "status": status,
            "queue_mode": "normal",
            "task_type": "crm.sheet_scanner_order",
            "task_arguments": {
                "order_id": "5010529",
                "process": "complicated_emb_to_hdd",
            },
            "fn": lambda: (True, "Finished."),
            "success": False,
            "cancel_requested": False,
            "result_context": {},
        }

    def test_canceled_order_retries_in_same_queue_entry(self):
        task = self._order_task()
        tasks = [task]
        with mock.patch.object(server, "automation_queue_tasks", tasks):
            ok, message = server.retry_automation_queue_task(task["id"])

        self.assertTrue(ok)
        self.assertIn("completed CRM work", message)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], "order-task-1")
        self.assertEqual(tasks[0]["status"], "queued")
        self.assertEqual(tasks[0]["task_arguments"]["order_id"], "5010529")
        self.assertTrue(tasks[0]["result_context"]["retrying"])

    def test_completed_order_is_never_retryable(self):
        task = self._order_task(status="completed")
        with mock.patch.object(server, "automation_queue_tasks", [task]):
            ok, message = server.retry_automation_queue_task(task["id"])

        self.assertFalse(ok)
        self.assertIn("canceled or failed", message)
        self.assertEqual(task["status"], "completed")

    def test_cancel_before_start_retains_order_for_retry(self):
        task = self._order_task(status="queued")
        tasks = [task]
        with (
            mock.patch.object(server, "automation_queue_tasks", tasks),
            mock.patch.object(server, "log_automation_event"),
        ):
            ok, _message = server.cancel_automation_queue_task(task["id"])
            payload = server._automation_queue_task_payload(task)

        self.assertTrue(ok)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(task["status"], "canceled")
        self.assertTrue(payload["retryable"])

    def test_main_retry_plan_contains_only_failed_order_rows(self):
        report = {
            "step_results": [
                {
                    "key": "address_validator_batch",
                    "success": False,
                    "order_count": 3,
                    "error_count": 1,
                    "errors": [{"order_id": "5010526", "message": "Timed out"}],
                },
                {
                    "key": "product_separator",
                    "success": True,
                    "order_count": 3,
                    "error_count": 0,
                    "errors": [],
                },
            ]
        }

        self.assertEqual(
            server._crm_processing_retry_plan(report),
            {"address_validator_batch": ["5010526"]},
        )

    def test_main_retry_step_targets_each_failed_order(self):
        calls = []

        def run_step(step_key, processing_filter, processing_state=None, target_order_id=None):
            calls.append((step_key, processing_filter, target_order_id))
            return {"success": True, "message": f"Finished {target_order_id}.", "errors": []}

        with mock.patch.object(server, "_run_crm_processing_step", side_effect=run_step):
            result = server._run_crm_processing_retry_step(
                "order_goods",
                "rush",
                ["5010527", "5010529"],
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            calls,
            [
                ("order_goods", "rush", "5010527"),
                ("order_goods", "rush", "5010529"),
            ],
        )

    def test_main_result_context_records_original_arguments_and_failed_orders(self):
        task = {
            "task_type": "crm.processing",
            "task_arguments": {
                "address_validator_enabled": True,
                "product_separator_enabled": True,
                "processing_filter": "rush",
            },
            "result_context": {},
        }
        state = {
            "last_filter_used": "rush",
            "last_selected_steps": ["address_validator_batch", "product_separator"],
            "last_run_success": False,
            "last_run_message": "One order failed.",
            "last_step_results": [
                {
                    "key": "address_validator_batch",
                    "success": False,
                    "errors": [{"order_id": "5010526", "message": "Timed out"}],
                },
                {"key": "product_separator", "success": True, "errors": []},
            ],
        }

        with mock.patch.object(server, "load_crm_processing_state", return_value=state):
            context = server._automation_queue_result_context(task, False, "One order failed.")

        self.assertEqual(context["original_arguments"], task["task_arguments"])
        self.assertEqual(
            server._crm_processing_retry_plan(context["report"]),
            {"address_validator_batch": ["5010526"]},
        )

    def test_order_goods_targeted_retry_never_uses_the_full_list(self):
        payload = {
            "success": True,
            "order_count": 1,
            "order_results": [{"order_id": "5010529", "success": True}],
        }
        with (
            mock.patch.object(server, "_crm_processing_mode_list_url_for_step", return_value="https://crm.test/list"),
            mock.patch.object(server, "_saved_crm_automation_parallel_workers", return_value=1),
            mock.patch.object(server, "_start_crm_order_goods_runtime") as start_runtime,
            mock.patch.object(server, "_execute_crm_order_goods_worker", return_value=(True, "Finished.", payload)) as execute,
            mock.patch.object(server, "_persist_crm_order_goods_run_result", return_value={}),
            mock.patch.object(server, "_finish_crm_order_goods_runtime"),
        ):
            result = server._run_crm_processing_step(
                "order_goods",
                "rush",
                target_order_id="5010529",
            )

        self.assertTrue(result["success"])
        self.assertEqual(start_runtime.call_args.kwargs["order_id"], "5010529")
        self.assertIsNone(start_runtime.call_args.kwargs["list_url"])
        self.assertEqual(execute.call_args.kwargs["order_id"], "5010529")
        self.assertIsNone(execute.call_args.kwargs["list_url"])

    def test_stock_unlock_target_url_is_restricted_to_one_order(self):
        url = server._crm_processing_targeted_list_url(
            "https://crm.test/orders?status=locked&id%5Blow%5D=4000000&id%5Bhigh%5D=5999999",
            "5010529",
        )

        self.assertIn("status=locked", url)
        self.assertIn("id%5Blow%5D=5010529", url)
        self.assertIn("id%5Bhigh%5D=5010529", url)
        self.assertIn("_orderIds=5010529", url)


if __name__ == "__main__":
    unittest.main()
