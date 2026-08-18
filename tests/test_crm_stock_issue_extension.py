import json
from pathlib import Path
import unittest
from unittest import mock

from workers import crm_stock_issue_extension as stock_extension


ROOT = Path(__file__).resolve().parents[1]


def product(style="DM130", description="Triblend T-Shirts", color="Red", tab=1, item=1, quantity=2):
    return {
        "tab_number": tab,
        "design_item_id": f"design-item-{item}",
        "style": style,
        "description": description,
        "color": color,
        "total_quantity": quantity,
    }


class StockIssueExtensionFormattingTests(unittest.TestCase):
    def test_duplicate_style_description_and_color_across_tabs_is_one_choice(self):
        normalized = stock_extension.normalize_selected_products(
            [
                product(tab=1, item=100, quantity=2),
                product(style="dm130", color="red", tab=2, item=200, quantity=3),
            ]
        )

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["tab_numbers"], [1, 2])
        self.assertEqual(normalized[0]["design_item_ids"], ["design-item-100", "design-item-200"])
        self.assertEqual(normalized[0]["total_quantity"], 5)

    def test_different_colors_remain_selectable_and_group_in_email(self):
        products = [
            product(color="Red", item=1),
            product(color="Black", item=2),
            product(color="Green", item=3),
        ]

        self.assertEqual(len(stock_extension.normalize_selected_products(products)), 3)
        self.assertEqual(
            stock_extension.format_email_stock_text(products),
            "DM130 Triblend T-Shirts in the color Red, Black, or Green",
        )

    def test_two_colors_use_and(self):
        text = stock_extension.format_email_stock_text(
            [product(color="Red", item=1), product(color="Black", item=2)]
        )

        self.assertEqual(text, "DM130 Triblend T-Shirts in the color Red and Black")

    def test_sales_note_includes_colors_and_uses_group_verb_agreement(self):
        one_product = [product(color="Red", item=1), product(color="Black", item=2)]
        multiple_products = one_product + [
            product(style="PC54", description="Core Cotton Tee", color="Navy", item=3)
        ]

        self.assertEqual(
            stock_extension.format_sales_note(5, one_product),
            "DM130 in Red and Black needs 5-day(s) extension\nEmailed Txted",
        )
        self.assertEqual(
            stock_extension.format_sales_note(5, multiple_products),
            "DM130 in Red and Black and PC54 in Navy need 5-day(s) extension\nEmailed Txted",
        )

    def test_slack_message_is_exact_and_has_no_days(self):
        message = stock_extension.format_slack_message("5043020")

        self.assertEqual(
            message,
            "https://crm2.legacy.printfly.com/order/5043020 - Rush Order needs extension",
        )
        self.assertNotIn("day", message.lower())

    def test_request_validation_rejects_unsafe_or_incomplete_input(self):
        invalid_requests = [
            (0, [product()]),
            (-1, [product()]),
            (1.5, [product()]),
            ("five", [product()]),
            (5, []),
            (5, [product(description="<b>Unsafe</b>")]),
            (5, [product(quantity=0)]),
        ]

        for days, products in invalid_requests:
            with self.subTest(days=days, products=products):
                with self.assertRaises(stock_extension.StockIssueExtensionError):
                    stock_extension.normalize_request(days, products)


class StockIssueExtensionWorkflowTests(unittest.TestCase):
    def _base_patches(self):
        driver = mock.Mock()
        driver.current_window_handle = "crm"
        driver.switch_to = mock.Mock()
        return driver, [
            mock.patch.object(stock_extension.shared, "_open_driver", return_value=driver),
            mock.patch.object(stock_extension.shared, "safe_get_with_partial_load"),
            mock.patch.object(stock_extension.shared, "_login_to_crm_if_needed"),
            mock.patch.object(stock_extension.shared, "_switch_to_crm_app_frame"),
            mock.patch.object(stock_extension.shared, "_wait_for_order_scope"),
            mock.patch.object(stock_extension.shared, "_wait_for_crm_contact_info", return_value={"email": "customer@example.com"}),
            mock.patch.object(stock_extension.shared, "_activate_crm_context"),
            mock.patch.object(stock_extension.shared, "safe_driver_quit"),
            mock.patch.object(stock_extension.shared, "safe_take_screenshot"),
            mock.patch.object(stock_extension.shared, "_profile_path", return_value="profile"),
        ]

    def test_slack_failure_prevents_issue_stock_status(self):
        driver, patches = self._base_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], \
             mock.patch.object(stock_extension, "_append_sales_note", return_value={"updated": True}), \
             mock.patch.object(stock_extension, "_prepare_and_send_salesforce_email", return_value={"sent": True}), \
             mock.patch.object(stock_extension, "_send_required_slack", side_effect=RuntimeError("Slack unavailable")), \
             mock.patch.object(stock_extension.shared, "_apply_order_status") as apply_status:
            with self.assertRaises(stock_extension.StockIssueExtensionError) as raised:
                stock_extension.process_stock_issue_extension_order("5043020", 5, [product()])

        apply_status.assert_not_called()
        self.assertEqual(raised.exception.result["failed_stage"], "slack")
        self.assertFalse(raised.exception.result["activity"]["status_applied"])
        self.assertIs(driver, driver)

    def test_success_order_is_note_email_slack_then_status(self):
        _driver, patches = self._base_patches()
        sequence = []

        def note(*_args, **_kwargs):
            sequence.append("note")
            return {"updated": True}

        def email(*_args, **_kwargs):
            sequence.append("email")
            return {"sent": True}

        def slack(*_args, **_kwargs):
            sequence.append("slack")
            return {"sent": True}

        def status(*_args, **_kwargs):
            sequence.append("status")
            return {"status_applied": True}

        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], \
             mock.patch.object(stock_extension, "_append_sales_note", side_effect=note), \
             mock.patch.object(stock_extension, "_prepare_and_send_salesforce_email", side_effect=email), \
             mock.patch.object(stock_extension, "_send_required_slack", side_effect=slack), \
             mock.patch.object(stock_extension.shared, "_apply_order_status", side_effect=status):
            result = stock_extension.process_stock_issue_extension_order("5043020", 5, [product()])

        self.assertTrue(result["success"])
        self.assertEqual(sequence, ["note", "email", "slack", "status"])

    def test_existing_exact_sales_note_is_not_duplicated(self):
        driver = mock.Mock()
        note = "DM130 in Red needs 5-day(s) extension\nEmailed Txted"
        with mock.patch.object(stock_extension.shared, "_order_scope", return_value=f"Older note\n{note}"), \
             mock.patch.object(stock_extension.shared, "_save_order_and_wait") as save:
            result = stock_extension._append_sales_note(driver, note)

        self.assertFalse(result["updated"])
        self.assertTrue(result["already_present"])
        save.assert_not_called()

    def test_final_recipient_check_requires_exact_to_and_no_cc_or_bcc(self):
        driver = mock.Mock()
        good_state = {"to": ["customer@example.com"], "cc": [], "bcc": []}
        with mock.patch.object(stock_extension, "_read_recipient_state", return_value=good_state), \
             mock.patch.object(
                 stock_extension.shared,
                 "_read_salesforce_email_state",
                 return_value={"from": f"Orders <{stock_extension.shared.SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL}>"},
             ):
            verified = stock_extension._verify_final_recipients(driver, " Customer@Example.com ")

        self.assertEqual(verified["to"], ["customer@example.com"])

        invalid_states = [
            {"to": [], "cc": [], "bcc": []},
            {"to": ["other@example.com"], "cc": [], "bcc": []},
            {"to": ["customer@example.com", "other@example.com"], "cc": [], "bcc": []},
            {"to": ["customer@example.com"], "cc": ["other@example.com"], "bcc": []},
            {"to": ["customer@example.com"], "cc": [], "bcc": ["other@example.com"]},
        ]
        for state in invalid_states:
            with self.subTest(state=state), mock.patch.object(
                stock_extension, "_read_recipient_state", return_value=state
            ):
                with self.assertRaises(stock_extension.StockIssueExtensionError):
                    stock_extension._verify_final_recipients(driver, "customer@example.com")

    def test_final_recipient_check_rejects_non_orders_sender(self):
        driver = mock.Mock()
        with mock.patch.object(
            stock_extension,
            "_read_recipient_state",
            return_value={"to": ["customer@example.com"], "cc": [], "bcc": []},
        ), mock.patch.object(
            stock_extension.shared,
            "_read_salesforce_email_state",
            return_value={"from": "Agent <agent@example.com>"},
        ):
            with self.assertRaises(stock_extension.StockIssueExtensionError):
                stock_extension._verify_final_recipients(driver, "customer@example.com")


class StockIssueExtensionSourceContractTests(unittest.TestCase):
    def test_extension_contains_stock_control_all_tab_scan_and_structured_bridge(self):
        content = (ROOT / "crm-order-dark-mode-extension" / "content.js").read_text(encoding="utf-8")
        bridge = (ROOT / "crm-order-dark-mode-extension" / "bridge.js").read_text(encoding="utf-8")
        background = (ROOT / "crm-order-dark-mode-extension" / "background.js").read_text(encoding="utf-8")
        manifest = json.loads((ROOT / "crm-order-dark-mode-extension" / "manifest.json").read_text(encoding="utf-8"))

        self.assertIn('key: "stock_issue_extension", label: "Extension Required"', content)
        self.assertIn('label: "Stock Issue"', content)
        self.assertIn("scanAllStockIssueProducts", content)
        self.assertIn("finally {", content)
        self.assertIn("clickStockIssueDesignTab(originalTabNumber)", content)
        self.assertIn("#design-items-list [id^='design-item-']", content)
        self.assertIn("totalQuantity <= 0", content)
        self.assertIn("deduplicateStockIssueProducts", content)
        self.assertIn("days: structuredData.days", bridge)
        self.assertIn("products: structuredData.products", bridge)
        self.assertIn("days: message.days", background)
        self.assertIn("products: message.products", background)
        self.assertEqual(manifest["version"], "1.3.0")


if __name__ == "__main__":
    unittest.main()
