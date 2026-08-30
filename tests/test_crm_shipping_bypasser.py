import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"
for path in (ROOT, WORKERS_DIR):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

import crm_shipping_bypasser  # noqa: E402
import shipping_bypasser_mappings  # noqa: E402


class ShippingBypassProductColorMappingTests(unittest.TestCase):
    def test_repository_mapping_file_supplies_product_and_color_overrides(self):
        self.assertEqual(
            crm_shipping_bypasser.SANMAR_PRODUCT_SEARCH_OVERRIDES["G500VL"]["search_id"],
            "5V00L",
        )
        self.assertEqual(
            crm_shipping_bypasser.SANMAR_PRODUCT_COLOR_ALIASES[("ST404", "BLACKTRIADSO")],
            ["Black Triad Solid"],
        )

    def test_user_mapping_normalizes_product_and_color_ids(self):
        payload = {
            "products": [
                {
                    "crm_product_id": "test-100",
                    "sanmar_product_id": "sm-200",
                    "colors": [
                        {
                            "crm_color_id": "Blue / White",
                            "sanmar_color_id": "Blue/ White",
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mappings.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            products, colors = shipping_bypasser_mappings.build_runtime_indexes(str(path))

        self.assertEqual(products["TEST-100"]["search_id"], "SM-200")
        self.assertEqual(colors[("TEST100", "BLUEWHITE")], ["Blue/ White"])


class ShippingAddressRadioTests(unittest.TestCase):
    def test_waits_for_delayed_address_radio(self):
        driver = mock.Mock()
        radio = object()
        driver.execute_script.side_effect = [None, None, radio]

        with (
            mock.patch.object(crm_shipping_bypasser, "_click_with_fallback") as click,
            mock.patch.object(crm_shipping_bypasser.time, "sleep"),
        ):
            crm_shipping_bypasser._click_radio_near_text(driver, "123 EZ TEES INC")

        self.assertEqual(driver.execute_script.call_count, 3)
        click.assert_called_once_with(driver, radio)

    def test_timeout_preserves_address_name_in_error(self):
        driver = mock.Mock()
        driver.execute_script.return_value = None

        with (
            mock.patch.object(crm_shipping_bypasser.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
            mock.patch.object(crm_shipping_bypasser.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "123 EZ TEES INC"):
                crm_shipping_bypasser._click_radio_near_text(
                    driver,
                    "123 EZ TEES INC",
                    timeout=0.5,
                )


class ShippingBypassSingleCleanupTests(unittest.TestCase):
    def test_worker_exception_cleans_sanmar_cart(self):
        crm_driver = object()
        sanmar_driver = object()

        with (
            mock.patch.object(crm_shipping_bypasser, "_build_crm_session_driver", return_value=crm_driver),
            mock.patch.object(crm_shipping_bypasser, "_build_sanmar_driver", return_value=sanmar_driver),
            mock.patch.object(crm_shipping_bypasser, "_run_order_with_drivers", side_effect=RuntimeError("address failed")),
            mock.patch.object(crm_shipping_bypasser, "_cleanup_after_failed_order", return_value=True) as cleanup,
            mock.patch.object(crm_shipping_bypasser, "safe_take_screenshot"),
            mock.patch.object(crm_shipping_bypasser, "safe_driver_quit"),
            mock.patch.object(crm_shipping_bypasser, "_publish_status"),
        ):
            payload = crm_shipping_bypasser._run_single_with_mode(False, "5039567")

        self.assertFalse(payload["success"])
        self.assertEqual(payload["report"][0]["outcome"], "worker_exception")
        cleanup.assert_called_once_with(sanmar_driver, "5039567", payload["report"])


class ShippingBypassStockBufferTests(unittest.TestCase):
    def _product_lines(self, available, needed=1):
        return [
            {
                "product": {"index": 1, "product_id": "PC54"},
                "quantities": {"M": needed},
                "inventory": [
                    {
                        "warehouse": "Robbinsville, NJ",
                        "stock": {"M": available},
                    }
                ],
            }
        ]

    def test_default_plan_keeps_ten_piece_safety_buffer(self):
        warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
            self._product_lines(available=1),
            "inhouse",
        )

        self.assertIsNone(warehouse)
        self.assertIsNone(plan)

    def test_manual_override_uses_available_stock_without_safety_buffer(self):
        warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
            self._product_lines(available=1),
            "inhouse",
            stock_buffer=0,
        )

        self.assertEqual(warehouse, "Robbinsville, NJ")
        self.assertEqual(plan["warehouses"], ["Robbinsville, NJ"])

    def test_manual_override_still_rejects_actual_stock_shortages(self):
        warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
            self._product_lines(available=1, needed=2),
            "inhouse",
            stock_buffer=0,
        )

        self.assertIsNone(warehouse)
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
