import json
from pathlib import Path
import unittest
from unittest import mock

from workers import crm_stock_issue_color as stock_color


ROOT = Path(__file__).resolve().parents[1]


def product(style="DM130", description="District Perfect Tri Tee", color="Red"):
    return {
        "tab_number": 1,
        "design_item_id": "design-item-8206660",
        "style": style,
        "description": description,
        "color": color,
        "total_quantity": 4,
    }


class StockIssueColorFormattingTests(unittest.TestCase):
    def test_uses_confirmed_live_salesforce_template_name(self):
        self.assertEqual(stock_color.SALESFORCE_TEMPLATE, "[AUTO] STOCK - Color")

    def test_suggested_color_grammar(self):
        self.assertEqual(stock_color.format_suggested_colors(["Navy"]), "Navy")
        self.assertEqual(stock_color.format_suggested_colors(["Navy", "Black"]), "Navy or Black")
        self.assertEqual(
            stock_color.format_suggested_colors(["Navy", "Black", "White"]),
            "Navy, Black, or White",
        )

    def test_colors_can_be_entered_as_comma_separated_plain_text(self):
        self.assertEqual(
            stock_color.normalize_suggested_colors(" Navy, Black, navy "),
            ["Navy", "Black"],
        )

    def test_request_requires_products_and_safe_colors(self):
        for colors, products in (([], [product()]), (["Navy"], []), (["<b>Navy</b>"], [product()])):
            with self.subTest(colors=colors, products=products):
                with self.assertRaises(stock_color.StockIssueColorError):
                    stock_color.normalize_request(colors, products)

    def test_stock_and_sales_note_text_include_selected_product(self):
        self.assertEqual(
            stock_color.format_email_stock_text([product()]),
            "DM130 District Perfect Tri Tee in the color Red",
        )
        self.assertEqual(
            stock_color.format_sales_note(["Navy", "Black"], [product()]),
            "No stock for DM130 in Red - suggested Navy or Black\nEmailed Txted",
        )

    def test_template_signature_matches_current_live_email(self):
        state = {
            "subject": "RushOrderTees Order #[ORDER-NUMBER] - URGENT Stock Issue",
            "body": (
                "Thank you for placing your order with RushOrderTees! We're reaching out regarding a minor "
                "inventory issue with your order. Unfortunately, the [STOCK] is currently out of stock and will "
                "not be available in time to meet your scheduled due date. To help avoid a delay, we can switch "
                "the item to an available color, such as [COLOR]. Please reply to this email or call us at "
                "(800) 620-1233 to let us know if you approve the color change. If you'd prefer to review other "
                "available options, we'll be happy to go over them with you. Thank you for trusting the "
                "RushOrderTees.com team."
            ),
        }

        self.assertEqual(stock_color._stock_color_template_signature_error(state), "")

    def test_template_signature_allows_same_phrase_without_optional_comma(self):
        state = {
            "subject": "RushOrderTees Order #[ORDER-NUMBER] - URGENT Stock Issue",
            "body": (
                "The [STOCK] is currently out of stock. We can switch the item to an available color such as "
                "[COLOR]. Please let us know if you approve the color change. Thank you for trusting the "
                "RushOrderTees.com team."
            ),
        }

        self.assertEqual(stock_color._stock_color_template_signature_error(state), "")

    def test_template_signature_still_requires_color_offer_phrase(self):
        state = {
            "subject": "RushOrderTees Order #[ORDER-NUMBER] - URGENT Stock Issue",
            "body": (
                "The [STOCK] is currently out of stock. Choose [COLOR]. Please let us know if you approve the "
                "color change. Thank you for trusting the RushOrderTees.com team."
            ),
        }

        self.assertIn("available color", stock_color._stock_color_template_signature_error(state))

    def test_body_replacement_requires_both_placeholders(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"[STOCK]": 1, "[COLOR]": 1, "unwrapped_links": 0}

        result = stock_color._replace_stock_color_placeholders(
            driver,
            "DM130 District Perfect Tri Tee in the color Red",
            "Navy or Black",
        )

        self.assertEqual(result["[STOCK]"], 1)
        self.assertEqual(driver.execute_script.call_args.args[1:], (
            "DM130 District Perfect Tri Tee in the color Red",
            "Navy or Black",
            "[STOCK]",
            "[COLOR]",
        ))

    def test_exact_template_selector_does_not_accept_the_search_input(self):
        driver = mock.Mock()
        driver.execute_script.return_value = None

        self.assertFalse(stock_color._click_exact_stock_color_template(driver))
        selector_script = driver.execute_script.call_args.args[0]
        self.assertIn("input|textarea|select|option", selector_script)
        self.assertNotIn("el.textContent || el.value", selector_script)


class StockIssueColorSourceContractTests(unittest.TestCase):
    def test_extension_and_bridge_carry_color_workflow_data(self):
        content = (ROOT / "crm-order-dark-mode-extension" / "content.js").read_text(encoding="utf-8")
        bridge = (ROOT / "crm-order-dark-mode-extension" / "bridge.js").read_text(encoding="utf-8")
        background = (ROOT / "crm-order-dark-mode-extension" / "background.js").read_text(encoding="utf-8")
        manifest = json.loads((ROOT / "crm-order-dark-mode-extension" / "manifest.json").read_text(encoding="utf-8"))

        self.assertIn('key: "stock_issue_color", label: "Suggest Different Color"', content)
        self.assertIn('key: "stock_issue_size", label: "Suggest Different Size"', content)
        self.assertIn("validateStockIssueSuggestedColors", content)
        self.assertIn("validateStockIssueSuggestedSizes", content)
        self.assertIn("colors: inputValidation.colors", content)
        self.assertIn("sizes: inputValidation.sizes", content)
        self.assertIn("colors: structuredData.colors", bridge)
        self.assertIn("sizes: structuredData.sizes", bridge)
        self.assertIn("colors: message.colors", background)
        self.assertIn("sizes: message.sizes", background)
        self.assertIn("{ surfacePageErrors: false }", content)
        self.assertIn("if (response && response.success)", content)
        self.assertEqual(manifest["version"], "1.5.1")


if __name__ == "__main__":
    unittest.main()
