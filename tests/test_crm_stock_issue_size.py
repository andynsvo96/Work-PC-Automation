import unittest
from unittest import mock

from workers import crm_stock_issue_size as stock_size


def product():
    return {
        "style": "DM130", "description": "District Perfect Tri Tee", "color": "Red",
        "available_sizes": ["Small", "Medium", "Large"], "affected_sizes": ["Medium", "Large"],
    }


class StockIssueSizeTests(unittest.TestCase):
    def test_uses_the_size_template_and_natural_list_grammar(self):
        self.assertEqual(stock_size.SALESFORCE_TEMPLATE, "[AUTO] Stock - Size")
        self.assertEqual(stock_size.format_suggested_sizes(["Small"]), "Small")
        self.assertEqual(stock_size.format_suggested_sizes(["Small", "Medium"]), "Small or Medium")
        self.assertEqual(stock_size.format_suggested_sizes(["Small", "Medium", "Large"]), "Small, Medium, or Large")

    def test_normalizes_safe_comma_separated_sizes(self):
        self.assertEqual(stock_size.normalize_suggested_sizes(" Small, Medium, small "), ["Small", "Medium"])
        with self.assertRaises(stock_size.StockIssueSizeError):
            stock_size.normalize_request(["<b>Small</b>"], [product()])

    def test_selected_product_sizes_are_required_and_used_in_stock_text(self):
        single_size_product = product()
        single_size_product["affected_sizes"] = ["X-Small"]
        self.assertEqual(
            stock_size.format_email_stock_text([single_size_product]),
            "DM130 District Perfect Tri Tee in the color Red for size X-Small",
        )
        three_sizes_product = product()
        three_sizes_product["affected_sizes"] = ["Medium", "Large", "X-Large"]
        self.assertEqual(
            stock_size.format_email_stock_text([three_sizes_product]),
            "DM130 District Perfect Tri Tee in the color Red for sizes Medium, Large, and X-Large",
        )
        missing_selection = product()
        missing_selection.pop("affected_sizes")
        with self.assertRaises(stock_size.StockIssueSizeError):
            stock_size.normalize_request(["Small"], [missing_selection])
        unavailable_selection = product()
        unavailable_selection["affected_sizes"] = ["X-Large"]
        with self.assertRaises(stock_size.StockIssueSizeError):
            stock_size.normalize_request(["Small"], [unavailable_selection])

    def test_sales_note_includes_original_size(self):
        selected = {
            **product(), "style": "M550W", "color": "LIGHT DENIM",
            "affected_sizes": ["small"],
        }
        self.assertEqual(
            stock_size.format_sales_note(["medium"], [selected]),
            "No stock for M550W in LIGHT DENIM for size small - suggested medium\nEmailed Txted",
        )

    def test_sales_note_keeps_affected_sizes_with_each_color(self):
        self.assertEqual(
            stock_size.format_sales_note(["Small"], [
                product(), {**product(), "color": "Blue", "affected_sizes": ["Large"]},
            ]),
            "No stock for DM130 in Red for sizes Medium and Large and "
            "DM130 in Blue for size Large - suggested Small\nEmailed Txted",
        )

    def test_crm_size_codes_are_expanded_in_email_and_sales_note(self):
        for code, name in (
            ("XS", "x-small"), ("S", "small"), ("M", "medium"), ("L", "large"),
            ("XL", "x-large"), ("XXL", "2x-large"), ("2XL", "2x-large"),
            ("3XL", "3x-large"), ("XXXL", "3x-large"), ("6XL", "6x-large"),
            ("XXS", "2x-small"), (" s ", "small"), ("Youth Small", "Youth Small"),
            ("32/34", "32/34"),
        ):
            with self.subTest(code=code):
                selected = {**product(), "affected_sizes": [code]}
                self.assertEqual(
                    stock_size.format_email_stock_text([selected]),
                    f"DM130 District Perfect Tri Tee in the color Red for size {name}",
                )
                self.assertEqual(
                    stock_size.format_sales_note(["3XL"], [selected]),
                    f"No stock for DM130 in Red for size {name} - suggested 3x-large\nEmailed Txted",
                )
                self.assertEqual(stock_size.format_suggested_sizes([code]), name)

    def test_size_display_conversion_preserves_crm_selection_codes(self):
        selected = {**product(), "available_sizes": ["S", "M"], "affected_sizes": ["S"]}
        request = stock_size.normalize_request(["M"], [selected])
        self.assertEqual(request["products"][0]["affected_sizes"], ["S"])
        self.assertEqual(stock_size.format_suggested_sizes(request["sizes"]), "medium")
        self.assertEqual(selected["affected_sizes"], ["S"])

    def test_template_requires_the_size_language_and_placeholder(self):
        state = {
            "subject": "RushOrderTees Order #[ORDER-NUMBER] - URGENT Stock Issue",
            "body": (
                "The [STOCK] is currently out of stock. We can switch the item to an available size such as "
                "[SIZE]. Please let us know if you approve the size change. Thank you for trusting the "
                "RushOrderTees.com team."
            ),
        }
        self.assertEqual(stock_size._stock_size_template_signature_error(state), "")

    def test_replaces_the_size_placeholder(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"[STOCK]": 1, "[SIZE]": 1, "unwrapped_links": 0}
        result = stock_size._replace_stock_size_placeholders(driver, "DM130 in Red", "Small or Medium")
        self.assertEqual(result["[SIZE]"], 1)
        self.assertEqual(driver.execute_script.call_args.args[1:], ("DM130 in Red", "Small or Medium", "[STOCK]", "[SIZE]"))


if __name__ == "__main__":
    unittest.main()
