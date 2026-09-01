import unittest
from decimal import Decimal
from pathlib import Path

from workers import crm_sleeve_prints


class SleevePrintsRequestTests(unittest.TestCase):
    def test_normalize_request_accepts_per_tab_mixed_sleeve_methods(self):
        request = crm_sleeve_prints.normalize_request(
            [
                {"tab_number": 1, "quantity": 5, "left": "ink", "right": "embroidery"},
                {"tab_number": 2, "quantity": 33, "left": "", "right": "ink"},
            ],
            "5",
            "12.5",
        )

        self.assertEqual(request["ink_price"], "5.00")
        self.assertEqual(request["embroidery_price"], "12.50")
        self.assertEqual(request["sleeves"][0]["right"], "embroidery")

    def test_normalize_request_rejects_duplicate_tabs_and_unused_custom_prices(self):
        with self.assertRaisesRegex(crm_sleeve_prints.SleevePrintsError, "selected more than once"):
            crm_sleeve_prints.normalize_request(
                [
                    {"tab_number": 1, "quantity": 1, "left": "ink", "right": ""},
                    {"tab_number": 1, "quantity": 1, "left": "", "right": "embroidery"},
                ]
            )
        with self.assertRaisesRegex(crm_sleeve_prints.SleevePrintsError, "without an embroidery sleeve"):
            crm_sleeve_prints.normalize_request(
                [{"tab_number": 1, "quantity": 1, "left": "ink", "right": ""}],
                embroidery_price="15.00",
            )


class SleevePrintsPricingTests(unittest.TestCase):
    def test_ink_pricing_tiers_include_exact_100_quantity_boundary(self):
        self.assertEqual(crm_sleeve_prints._ink_price_for_quantity(1), Decimal("8.00"))
        self.assertEqual(crm_sleeve_prints._ink_price_for_quantity(10), Decimal("7.00"))
        self.assertEqual(crm_sleeve_prints._ink_price_for_quantity(20), Decimal("6.00"))
        self.assertEqual(crm_sleeve_prints._ink_price_for_quantity(99), Decimal("6.00"))
        self.assertEqual(crm_sleeve_prints._ink_price_for_quantity(100), Decimal("5.00"))

    def test_live_plan_combines_unique_ink_tab_quantities_and_charges_each_sleeve(self):
        request = crm_sleeve_prints.normalize_request(
            [
                {"tab_number": 1, "quantity": 1, "left": "ink", "right": ""},
                {"tab_number": 2, "quantity": 1, "left": "ink", "right": "ink"},
                {"tab_number": 3, "quantity": 1, "left": "", "right": "embroidery"},
            ]
        )
        state = {
            "designs": [
                {"tab_number": 1, "quantity": 5, "print_areas": [{"method": "HD Digital"}]},
                {"tab_number": 2, "quantity": 20, "print_areas": [{"method": "Screen Printing"}]},
                {"tab_number": 3, "quantity": 7, "print_areas": []},
            ]
        }

        plan = crm_sleeve_prints._build_live_plan(request, state)

        self.assertEqual(plan["ink_quantity"], 25)
        self.assertEqual(plan["ink_price"], Decimal("6.00"))
        self.assertEqual(plan["embroidery_price"], Decimal("15.00"))
        self.assertEqual(plan["selections"][0]["surcharge"], Decimal("6.00"))
        self.assertEqual(plan["selections"][1]["surcharge"], Decimal("12.00"))
        self.assertEqual(plan["selections"][2]["surcharge"], Decimal("15.00"))
        self.assertEqual(plan["selections"][1]["ink_method"], "Screen Printing")

    def test_live_plan_defaults_to_hd_digital_and_reports_mixed_existing_methods(self):
        request = crm_sleeve_prints.normalize_request(
            [{"tab_number": 1, "quantity": 1, "left": "ink", "right": ""}]
        )
        state = {
            "designs": [{
                "tab_number": 1,
                "quantity": 10,
                "print_areas": [{"method": "HD Digital"}, {"method": "Screen Printing"}],
            }]
        }

        plan = crm_sleeve_prints._build_live_plan(request, state)

        self.assertEqual(plan["selections"][0]["ink_method"], "HD Digital")
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("both HD Digital and Screen Printing", plan["warnings"][0])

    def test_sales_note_and_email_text_are_exact_for_single_and_combined_requests(self):
        ink = [{"left": "ink", "right": ""}]
        embroidery = [{"left": "", "right": "embroidery"}]
        combined = [{"left": "ink", "right": "embroidery"}]

        self.assertEqual(
            crm_sleeve_prints.format_sales_note(ink, Decimal("5"), None),
            "Sleeve prints\nPriced at $5.00 per sleeve\nEmailed Txted",
        )
        self.assertEqual(
            crm_sleeve_prints.format_sales_note(embroidery, None, Decimal("15")),
            "Sleeve embroidery\nPriced at $15.00 per sleeve\nEmailed Txted",
        )
        self.assertEqual(
            crm_sleeve_prints.format_sales_note(combined, Decimal("5"), Decimal("15")),
            "Sleeve prints and embroidery\nPriced at $5.00 for ink prints and $15.00 for embroidery per sleeve\nEmailed Txted",
        )
        self.assertEqual(crm_sleeve_prints._format_request_text(combined), "sleeve print and embroidery")
        self.assertEqual(
            crm_sleeve_prints._format_cost_text(combined, Decimal("5"), Decimal("15")),
            "$5.00 for sleeve prints and $15.00 for embroidery",
        )

    def test_matching_sales_note_identifies_a_retry_without_repricing(self):
        note = "Sleeve prints\nPriced at $5.00 per sleeve\nEmailed Txted"
        self.assertTrue(crm_sleeve_prints._crm_note_exists({"sales_notes": f"Earlier note\n{note}"}, note))
        self.assertFalse(crm_sleeve_prints._crm_note_exists({"sales_notes": "Earlier note"}, note))


class SleevePrintsExtensionUiTests(unittest.TestCase):
    def test_sleeve_prints_is_a_manual_process_with_inline_prefilled_prices(self):
        content = (
            Path(__file__).resolve().parents[1] / "crm-order-dark-mode-extension" / "content.js"
        ).read_text(encoding="utf-8")

        manual_start = content.index("const MANUAL_ORDER_AUTOMATIONS")
        reachout_start = content.index("const REACHOUT_ORDER_AUTOMATIONS")
        self.assertIn('key: "sleeve_prints", label: "Sleeve Prints"', content[manual_start:reachout_start])
        self.assertNotIn('key: "sleeve_prints", label: "Sleeve Prints"', content[reachout_start:content.index("const STOCK_ISSUE_AUTOMATIONS")])
        self.assertIn("const priceWrap = document.createElement", content)
        self.assertIn("Price per sleeve — calculated from", content)
        self.assertIn("priceInput.value = Number(price).toFixed(2)", content)
        self.assertNotIn("const pricing = document.createElement", content)


if __name__ == "__main__":
    unittest.main()
