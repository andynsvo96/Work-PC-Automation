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

    def test_failed_shipping_queue_uses_concise_color_message_and_keeps_details(self):
        detailed_message = (
            "SanMar color could not be selected for ST404 / Black Triad So: "
            "SanMar color 'Black Triad So' was not found."
        )
        task = {
            "id": "shipping-task-1",
            "label": "Shipping Bypasser Order 5036695",
            "status": "failed",
            "queue_mode": "normal",
            "task_type": "crm.shipping_bypasser",
            "message": "Shipping Bypasser processed 1 order(s). 1 order(s) need attention.",
            "result_context": {
                "report": {
                    "order_details": [{
                        "order_id": "5036695",
                        "success": False,
                        "outcome": "sanmar_color_not_found",
                        "message": detailed_message,
                    }],
                },
            },
        }

        payload = server._automation_queue_task_payload(task)

        self.assertEqual(payload["message"], "Stock color mismatch detected.")
        self.assertEqual(
            payload["result_context"]["report"]["order_details"][0]["message"],
            detailed_message,
        )

    def test_outside_limit_failure_names_missing_salesforce_template(self):
        task = {
            "task_type": "crm.mass_emailer",
            "status": "failed",
            "message": (
                "Sheet scanner order failed: Outside limit cancel template was not selectable "
                "in Salesforce. Tried: [AUTO] Outside Limit Cancel"
            ),
            "result_context": {"report": {"order_details": []}},
        }

        payload = server._automation_queue_task_payload(task)

        self.assertEqual(
            payload["message"],
            "Salesforce template selection failed: [AUTO] Outside Limit Cancel.",
        )
        self.assertNotIn("needs attention", payload["message"].lower())

    def test_unknown_specific_failure_is_preserved_instead_of_generic_task_label(self):
        task = {
            "task_type": "crm.mass_emailer",
            "status": "failed",
            "message": "Sheet scanner order failed: Customer email address is blank.",
            "result_context": {"report": {"order_details": []}},
        }

        payload = server._automation_queue_task_payload(task)

        self.assertEqual(payload["message"], "Customer email address is blank.")

    def test_generic_failure_reports_that_the_cause_was_not_recorded(self):
        task = {
            "task_type": "crm.mass_emailer",
            "status": "failed",
            "message": "Sheets Scanner needs attention.",
            "result_context": {"report": {"order_details": []}},
        }

        payload = server._automation_queue_task_payload(task)

        self.assertEqual(payload["message"], "Failure reason was not recorded.")
        self.assertNotIn("needs attention", payload["message"].lower())

    def test_main_processing_queue_reads_nested_step_error_message(self):
        task = {
            "task_type": "crm.processing",
            "status": "failed",
            "message": "Automate Processing completed with partial success. Needs attention: Order Goods.",
            "result_context": {
                "report": {
                    "step_results": [
                        {
                            "key": "order_goods",
                            "success": False,
                            "errors": [
                                {
                                    "order_id": "5067329",
                                    "status": "Needs attention",
                                    "outcome": "stock_unlock_not_confirmed",
                                    "message": (
                                        "Stock Auto Ordering Unlocked was not confirmed: "
                                        "CRM still shows 'Locked for Auto Ordering'."
                                    ),
                                }
                            ],
                        }
                    ]
                }
            },
        }

        payload = server._automation_queue_task_payload(task)

        self.assertEqual(
            payload["message"],
            "Stock Auto Ordering Unlocked was not confirmed: CRM still shows 'Locked for Auto Ordering'.",
        )

    def test_shipping_result_context_captures_worker_details_for_view_errors(self):
        detailed_payload = {
            "success": False,
            "message": "One order needs attention.",
            "order_ids": ["5036695"],
            "report": [{
                "order_id": "5036695",
                "success": False,
                "outcome": "sanmar_color_not_found",
                "message": "SanMar color was not found.",
            }],
        }
        task = {
            "task_type": "crm.shipping_bypasser",
            "status_fn": lambda: {"runtime": {"payload": detailed_payload}},
            "result_context": {},
        }

        context = server._automation_queue_result_context(task, False, detailed_payload["message"])

        self.assertEqual(context["report"]["order_details"][0]["outcome"], "sanmar_color_not_found")

    def test_stock_extension_failure_names_stage_cause_and_completed_actions(self):
        result = {
            "order_id": "4705293",
            "failed_stage": "salesforce_email",
            "error": "Salesforce recipient verification failed immediately before Send.",
            "activity": {
                "sales_note_saved": True,
                "email_send_attempted": False,
                "email_sent": False,
                "slack_send_attempted": False,
                "slack_sent": False,
                "status_applied": False,
            },
        }

        message = server._stock_issue_extension_failure_message(result)

        self.assertIn("Salesforce email failed", message)
        self.assertIn("recipient verification failed", message)
        self.assertIn("Sales Note saved", message)
        self.assertIn("email not sent", message)
        self.assertIn("Slack not sent", message)
        self.assertIn("Issue - Stock not applied", message)
        self.assertNotIn("needs attention", message)

    def test_stock_extension_partial_send_warns_before_retry(self):
        result = {
            "failed_stage": "slack",
            "error": "Required Slack notification failed.",
            "activity": {
                "sales_note_saved": True,
                "email_send_attempted": True,
                "email_sent": True,
                "slack_send_attempted": True,
                "slack_sent": False,
                "status_applied": False,
            },
        }

        message = server._stock_issue_extension_failure_message(result)

        self.assertIn("Slack notification failed", message)
        self.assertIn("email sent", message)
        self.assertIn("Slack send attempted but not confirmed", message)
        self.assertIn("Check the attempted send before retrying", message)

    def test_stock_extension_legacy_failure_message_recovers_diagnostics(self):
        original = (
            "Stock Extension stopped at salesforce_email: Salesforce template [AUTO] STOCK - Extension "
            "did not load with required [STOCK] and [DAYS] placeholders.. Recovery state: "
            "email_sent=False, email_send_attempted=False, slack_sent=False, "
            "slack_send_attempted=False, status_applied=False."
        )

        message = server._stock_issue_extension_failure_message({}, original)

        self.assertIn("Salesforce email failed", message)
        self.assertIn("required [STOCK] and [DAYS] placeholders", message)
        self.assertIn("Sales Note saved", message)
        self.assertIn("email not sent", message)
        self.assertIn("Slack not sent", message)
        self.assertIn("Issue - Stock not applied", message)

    def test_stock_extension_result_context_populates_queue_and_view_errors(self):
        detailed_payload = {
            "success": False,
            "order_id": "4705293",
            "failed_stage": "crm_status",
            "error": "Issue - Stock could not be confirmed in CRM.",
            "activity": {
                "sales_note_saved": True,
                "email_send_attempted": True,
                "email_sent": True,
                "slack_send_attempted": True,
                "slack_sent": True,
                "status_applied": False,
            },
        }
        task = {
            "id": "stock-extension-task-1",
            "label": "Stock Issue - Extension Required Order 4705293",
            "status": "failed",
            "queue_mode": "normal",
            "task_type": "crm.stock_issue_extension",
            "task_arguments": {"order_id": "4705293"},
            "status_fn": lambda: {"runtime": {"payload": detailed_payload}},
            "message": "Stock Issue - Extension Required needs attention.",
            "result_context": {},
        }

        context = server._automation_queue_result_context(task, False, task["message"])
        task["result_context"] = context
        queue_payload = server._automation_queue_task_payload(task)
        error_row = queue_payload["result_context"]["report"]["order_details"][0]

        self.assertEqual(context["stock_issue_extension"], detailed_payload)
        self.assertEqual(error_row["order_id"], "4705293")
        self.assertEqual(error_row["outcome"], "crm_status")
        self.assertIn("Issue - Stock status update failed", error_row["message"])
        self.assertIn("email sent", queue_payload["message"])
        self.assertIn("Slack sent", queue_payload["message"])
        self.assertIn("Issue - Stock not applied", queue_payload["message"])
        self.assertNotIn("needs attention", queue_payload["message"])

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
