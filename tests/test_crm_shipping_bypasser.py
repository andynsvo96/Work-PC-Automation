import sys
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


if __name__ == "__main__":
    unittest.main()
