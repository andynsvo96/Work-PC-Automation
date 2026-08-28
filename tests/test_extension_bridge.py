import unittest
from unittest import mock

import server


class ChromeExtensionBridgeTests(unittest.TestCase):
    ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

    def setUp(self):
        self.previous_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = True
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()

    def tearDown(self):
        server.APP_PIN_REQUIRED = self.previous_required

    def test_status_is_available_without_app_session_to_a_chrome_extension(self):
        response = self.client.get(
            "/api/extension/bridge/status",
            headers={"Origin": self.ORIGIN},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], self.ORIGIN)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.get_json()["protocol"], server.CHROME_EXTENSION_BRIDGE_PROTOCOL)

    def test_status_allows_a_loopback_extension_fetch_without_origin(self):
        response = self.client.get(
            "/api/extension/bridge/status",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_status_rejects_web_origins_and_non_loopback_clients(self):
        web_response = self.client.get(
            "/api/extension/bridge/status",
            headers={"Origin": "https://example.com"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        remote_response = self.client.get(
            "/api/extension/bridge/status",
            headers={"Origin": self.ORIGIN},
            environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
        )

        self.assertEqual(web_response.status_code, 403)
        self.assertEqual(remote_response.status_code, 403)

    def test_only_valid_chrome_extension_origins_are_accepted(self):
        valid = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        self.assertTrue(server._is_chrome_extension_origin(valid))
        self.assertFalse(server._is_chrome_extension_origin("chrome-extension://not-an-extension-id"))
        self.assertFalse(server._is_chrome_extension_origin("https://example.com"))

    def test_shipping_cost_feedback_requests_bypass(self):
        self.assertTrue(
            server._crm_extension_order_shipping_cost_detected(
                [{"payload": {"report": [{"outcome": "auto_order_shipment_cost_exceeded"}]}}]
            )
        )
        self.assertFalse(server._crm_extension_order_shipping_cost_detected([{"payload": {"success": True}}]))

    def test_no_purchase_plan_decisions_use_automated_notes_classification(self):
        decisions = server._crm_extension_order_no_purchase_plan_decisions(
            [
                {
                    "order_id": "4917538",
                    "payload": {
                        "report": [
                            {
                                "order_id": "4917538",
                                "outcome": "auto_order_no_purchase_plan",
                                "auto_order_feedback": {
                                    "automated_notes": {
                                        "classification": "push_back",
                                        "note": "The following products are unable to be delivered on time",
                                    }
                                },
                            }
                        ]
                    },
                },
                {
                    "order_id": "4918203",
                    "payload": {
                        "report": [
                            {
                                "order_id": "4918203",
                                "outcome": "auto_order_no_purchase_plan",
                                "auto_order_feedback": {
                                    "automated_notes": {
                                        "classification": "stock_issue",
                                        "note": "The following products do not have available inventory",
                                    }
                                },
                            }
                        ]
                    },
                },
            ]
        )

        self.assertEqual(decisions["4917538"]["classification"], "push_back")
        self.assertEqual(decisions["4918203"]["classification"], "stock_issue")

    def test_no_purchase_plan_decisions_preserve_every_stock_tab(self):
        decisions = server._crm_extension_order_no_purchase_plan_decisions(
            [{
                "order_id": "4924912",
                "payload": {
                    "report": [
                        {
                            "order_id": "4924912",
                            "outcome": "auto_order_no_purchase_plan",
                            "stock_tab_index": 1,
                            "stock_tab_label": "PO 1001",
                            "auto_order_feedback": {"automated_notes": {
                                "classification": "push_back",
                                "note": "The following products are unable to be delivered on time",
                            }},
                        },
                        {
                            "order_id": "4924912",
                            "outcome": "auto_order_no_purchase_plan",
                            "stock_tab_index": 2,
                            "stock_tab_label": "PO 1002",
                            "auto_order_feedback": {"automated_notes": {
                                "classification": "stock_issue",
                                "note": "The following products do not have available inventory",
                            }},
                        },
                    ]
                },
            }]
        )

        decision = decisions["4924912"]
        self.assertEqual(decision["classification"], "stock_issue")
        self.assertEqual(len(decision["tab_decisions"]), 2)
        self.assertIn("PO 1001", decision["note"])
        self.assertIn("PO 1002", decision["note"])

    def test_auto_process_report_result_uses_its_own_quick_report_row(self):
        result = server._crm_extension_order_report_result(
            ["4918203"],
            False,
            "Stock issue: order(s) 4918203 have no available inventory.",
            server.time.monotonic(),
        )

        self.assertEqual(result["key"], "auto_process")
        self.assertEqual(result["order_count"], 1)
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(server._crm_processing_step_label("auto_process"), "Auto-Process")

    def test_auto_process_marks_no_change_steps_as_not_needed(self):
        self.assertTrue(
            server._crm_extension_order_step_not_needed(
                "address_validator",
                "Skipped because the order already showed a valid shipping address before opening edit.",
                {
                    "report": [
                        {
                            "success": True,
                            "outcome": "already_valid_skipped",
                            "resolution": "already_valid",
                        }
                    ]
                },
            )
        )
        self.assertTrue(
            server._crm_extension_order_step_not_needed(
                "product_separator",
                "Product Separator skipped order 4917538: no mixed product tabs detected.",
                {"resolution": "skipped_no_split_needed"},
            )
        )
        self.assertFalse(
            server._crm_extension_order_step_not_needed(
                "address_validator",
                "Address validation completed.",
                {"report": [{"success": True, "outcome": "validated", "resolution": "validated"}]},
            )
        )

    def test_auto_process_queue_message_uses_exact_order_goods_failure(self):
        detail = server._crm_extension_order_order_goods_failure_detail(
            [
                {
                    "success": False,
                    "message": "Order Goods needs attention.",
                    "payload": {
                        "report": [
                            {
                                "success": False,
                                "outcome": "stock_unlock_not_confirmed",
                                "message": "Stock Auto Ordering Unlocked was not confirmed: CRM still shows 'Locked for Auto Ordering'.",
                            }
                        ]
                    },
                }
            ]
        )

        self.assertIn("Stock Auto Ordering Unlocked was not confirmed", detail)

    def test_order_controls_do_not_require_pairing_and_validate_order_id(self):
        previous_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = False
        try:
            status = self.client.get(
                "/api/extension/bridge/process-order/status",
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(status.status_code, 200)

            invalid_order = self.client.post(
                "/api/extension/bridge/process-order",
                json={"order_id": "not-an-order"},
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(invalid_order.status_code, 409)
            self.assertFalse(invalid_order.get_json()["success"])
        finally:
            server.APP_PIN_REQUIRED = previous_required

    def test_order_controls_support_chrome_service_worker_requests_without_origin(self):
        previous_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = False
        try:
            response = self.client.post(
                "/api/extension/bridge/process-order",
                json={"order_id": "not-an-order"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(response.status_code, 409)
            self.assertFalse(response.get_json()["success"])
        finally:
            server.APP_PIN_REQUIRED = previous_required

    def test_manual_order_control_queues_only_the_selected_single_order_automation(self):
        with mock.patch(
            "server.enqueue_automation",
            return_value=(True, "Product Separator queued.", {"id": "task-1", "status": "queued"}),
        ) as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={"order_id": "4917538", "automation": "product_separator"},
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(enqueue.call_args.kwargs["task_type"], "crm.product_separator")
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"]["order_id"], "4917538")
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"]["list_mode"], "all")

    def test_manual_order_control_rejects_unknown_automation(self):
        response = self.client.post(
            "/api/extension/bridge/process-order/manual",
            json={"order_id": "4917538", "automation": "not-real"},
            headers={"Origin": self.ORIGIN},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["success"])

    def test_sheet_scanner_manual_action_requires_a_reason_before_queueing(self):
        with mock.patch("server.enqueue_automation") as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={"order_id": "4917538", "automation": "copyright_cancel", "reason": ""},
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("requires a reason", response.get_json()["message"])
        enqueue.assert_not_called()

    def test_content_violation_manual_action_requires_a_reason_before_queueing(self):
        with mock.patch("server.enqueue_automation") as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={"order_id": "4917538", "automation": "content_violation_cancel", "reason": ""},
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.get_json()["success"])
        self.assertIn("requires a reason", response.get_json()["message"])
        enqueue.assert_not_called()

    def test_content_violation_manual_action_queues_with_its_reason(self):
        with mock.patch(
            "server.enqueue_automation",
            return_value=(True, "Content Violation - Cancel queued.", {"id": "task-3", "status": "queued"}),
        ) as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={
                    "order_id": "4917538",
                    "automation": "content_violation_cancel",
                    "reason": "Policy violation details",
                },
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"], {
            "order_id": "4917538",
            "process": "content_violation_cancel",
            "reason": "Policy violation details",
        })

    def test_sheet_scanner_manual_action_queues_one_order_with_its_reason(self):
        with mock.patch(
            "server.enqueue_automation",
            return_value=(True, "Copyright - Cancel queued.", {"id": "task-2", "status": "queued"}),
        ) as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={
                    "order_id": "4917538",
                    "automation": "copyright_cancel",
                    "reason": "Trademarked logo",
                },
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(enqueue.call_args.kwargs["task_type"], "crm.sheet_scanner_order")
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"], {
            "order_id": "4917538",
            "process": "copyright_cancel",
            "reason": "Trademarked logo",
        })

    def test_auto_splitter_is_available_as_a_single_order_manual_action(self):
        with mock.patch(
            "server.enqueue_automation",
            return_value=(True, "Auto Splitter queued.", {"id": "task-3", "status": "queued"}),
        ) as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={"order_id": "4917538", "automation": "auto_splitter"},
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(enqueue.call_args.kwargs["task_type"], "crm.auto_splitter")
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"]["order_target"], "4917538")
        self.assertFalse(enqueue.call_args.kwargs["task_arguments"]["dry_run"])

    def test_stock_issue_extension_queues_structured_single_order_request(self):
        products = [
            {
                "tab_number": 1,
                "design_item_id": "design-item-8206660",
                "style": "DM130",
                "description": "District Perfect Tri Tee",
                "color": "Red",
                "total_quantity": 4,
            }
        ]
        with mock.patch(
            "server.enqueue_automation",
            return_value=(True, "Stock Extension queued.", {"id": "stock-1", "status": "queued"}),
        ) as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={
                    "order_id": "5043020",
                    "automation": "stock_issue_extension",
                    "days": 5,
                    "products": products,
                },
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(enqueue.call_args.kwargs["task_type"], "crm.stock_issue_extension")
        arguments = enqueue.call_args.kwargs["task_arguments"]
        self.assertEqual(arguments["order_id"], "5043020")
        self.assertEqual(arguments["days"], 5)
        self.assertEqual(arguments["products"][0]["style"], "DM130")
        self.assertEqual(arguments["products"][0]["color"], "Red")
        self.assertFalse(arguments["dry_run"])

    def test_stock_issue_extension_rejects_invalid_days_and_empty_products(self):
        invalid_payloads = [
            {"days": 0, "products": [{"style": "DM130", "description": "Tee", "color": "Red"}]},
            {"days": -2, "products": [{"style": "DM130", "description": "Tee", "color": "Red"}]},
            {"days": 1.5, "products": [{"style": "DM130", "description": "Tee", "color": "Red"}]},
            {"days": 5, "products": []},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), mock.patch("server.enqueue_automation") as enqueue:
                response = self.client.post(
                    "/api/extension/bridge/process-order/manual",
                    json={"order_id": "5043020", "automation": "stock_issue_extension", **payload},
                    headers={"Origin": self.ORIGIN},
                    environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                )

                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.get_json()["success"])
                enqueue.assert_not_called()

    def test_stock_issue_color_queues_normalized_colors_and_products(self):
        products = [{
            "tab_number": 1,
            "design_item_id": "design-item-8206660",
            "style": "DM130",
            "description": "District Perfect Tri Tee",
            "color": "Red",
            "total_quantity": 4,
        }]
        with mock.patch(
            "server.enqueue_automation",
            return_value=(True, "Suggest Different Color queued.", {"id": "stock-color-1", "status": "queued"}),
        ) as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={
                    "order_id": "5043020",
                    "automation": "stock_issue_color",
                    "colors": [" Navy ", "Black", "navy"],
                    "products": products,
                },
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(enqueue.call_args.kwargs["task_type"], "crm.stock_issue_color")
        arguments = enqueue.call_args.kwargs["task_arguments"]
        self.assertEqual(arguments["order_id"], "5043020")
        self.assertEqual(arguments["colors"], ["Navy", "Black"])
        self.assertEqual(arguments["products"][0]["style"], "DM130")
        self.assertFalse(arguments["dry_run"])

    def test_stock_issue_color_rejects_empty_or_unsafe_colors(self):
        product = [{"style": "DM130", "description": "Tee", "color": "Red"}]
        invalid_colors = [[], [""], ["Navy", ""], ["<b>Black</b>"]]
        for colors in invalid_colors:
            with self.subTest(colors=colors), mock.patch("server.enqueue_automation") as enqueue:
                response = self.client.post(
                    "/api/extension/bridge/process-order/manual",
                    json={
                        "order_id": "5043020",
                        "automation": "stock_issue_color",
                        "colors": colors,
                        "products": product,
                    },
                    headers={"Origin": self.ORIGIN},
                    environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                )

                self.assertEqual(response.status_code, 409)
                self.assertFalse(response.get_json()["success"])
                enqueue.assert_not_called()

    def test_stock_issue_size_queues_normalized_sizes_and_products(self):
        products = [{
            "style": "DM130", "description": "District Perfect Tri Tee", "color": "Red",
            "available_sizes": ["Small", "Medium"], "affected_sizes": ["Small"],
        }]
        with mock.patch(
            "server.enqueue_automation",
            return_value=(True, "Suggest Different Size queued.", {"id": "stock-size-1", "status": "queued"}),
        ) as enqueue:
            response = self.client.post(
                "/api/extension/bridge/process-order/manual",
                json={
                    "order_id": "5043020",
                    "automation": "stock_issue_size",
                    "sizes": [" Small ", "Medium", "small"],
                    "products": products,
                },
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(enqueue.call_args.kwargs["task_type"], "crm.stock_issue_size")
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"]["sizes"], ["Small", "Medium"])


if __name__ == "__main__":
    unittest.main()
