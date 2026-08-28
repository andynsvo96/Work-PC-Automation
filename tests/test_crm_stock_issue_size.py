import unittest
from unittest import mock

from workers import crm_stock_issue_size as stock_size


def product():
    return {"style": "DM130", "description": "District Perfect Tri Tee", "color": "Red"}


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
