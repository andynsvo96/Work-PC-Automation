import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_product_separator  # noqa: E402


class ProductSeparatorBoxNoteTests(unittest.TestCase):
    def test_header_only_stock_does_not_create_note_when_order_needs_stock(self):
        scan = {
            "order_stock_status": {"state": "need_to_order"},
            "tabs": [{
                "tab_number": 1,
                "tab_name": "H-Test001",
                "needs_split": True,
                "products": [
                    {"product_name": "Adult Tee", "group": "adult_general"},
                    {"product_name": "Youth Tee", "group": "youth"},
                ],
                "stock": {
                    "state": "ordered_header_only",
                    "stock_status_ordered": True,
                    "has_po_row": False,
                    "manual_order_rows": [],
                },
            }],
        }

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertEqual(plan["production_notes"], [])
        self.assertEqual(plan["split_tabs"][0]["production_note_if_stock_ordered"], "")


if __name__ == "__main__":
    unittest.main()
