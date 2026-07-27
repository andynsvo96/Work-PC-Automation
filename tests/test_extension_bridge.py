import unittest

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


if __name__ == "__main__":
    unittest.main()
