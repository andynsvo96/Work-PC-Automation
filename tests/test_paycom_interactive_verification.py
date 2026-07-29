import unittest
from unittest import mock
from pathlib import Path
import sys

WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import paycom_hours
import config


class _Body:
    def __init__(self, text):
        self.text = text


class _Driver:
    def __init__(self, text):
        self.body = _Body(text)

    def find_element(self, *_args):
        return self.body


class PaycomInteractiveVerificationTests(unittest.TestCase):
    def test_interactive_handoff_is_enabled_only_on_macos(self):
        with mock.patch.object(paycom_hours, "normalize_os_name", return_value="macos"), mock.patch.dict(
            paycom_hours.os.environ, {"PAYCOM_MAC_INTERACTIVE_VERIFICATION": "1"}, clear=False
        ):
            self.assertTrue(paycom_hours._is_macos_interactive_verification_enabled())
        with mock.patch.object(paycom_hours, "normalize_os_name", return_value="windows"):
            self.assertFalse(paycom_hours._is_macos_interactive_verification_enabled())

    def test_macos_paycom_can_reuse_a_trusted_session_headlessly(self):
        with mock.patch.object(paycom_hours, "normalize_os_name", return_value="macos"), mock.patch.dict(
            paycom_hours.os.environ,
            {"AUTOMATION_HEADLESS": "1", "PAYCOM_MAC_INTERACTIVE_VERIFICATION": "1"},
            clear=False,
        ):
            self.assertTrue(paycom_hours.paycom_headless_mode_enabled())

    def test_windows_paycom_keeps_headless_preference(self):
        with mock.patch.object(paycom_hours, "normalize_os_name", return_value="windows"), mock.patch.dict(
            paycom_hours.os.environ,
            {"AUTOMATION_HEADLESS": "1"},
            clear=False,
        ):
            self.assertTrue(paycom_hours.paycom_headless_mode_enabled())

    def test_macos_headless_login_is_attempted_before_visible_challenge_fallback(self):
        with mock.patch.object(paycom_hours, "normalize_os_name", return_value="macos"):
            self.assertFalse(paycom_hours.should_defer_paycom_login_to_visible(True, True))
            self.assertFalse(paycom_hours.should_defer_paycom_login_to_visible(True, False))
            self.assertFalse(paycom_hours.should_defer_paycom_login_to_visible(False, True))

    def test_hours_url_targets_read_only_timecard_view(self):
        self.assertTrue(config.PAYCOM_HOURS_URL.endswith("/timecard/WEB02#!timecard-view"))
        self.assertEqual(
            paycom_hours.resolve_paycom_hours_target_url(
                "https://www.paycomonline.net/v4/ee/web.php/timecard/WEB02"
            ),
            "https://www.paycomonline.net/v4/ee/web.php/timecard/WEB02#!timecard-view",
        )

    def test_login_redirect_is_forced_back_to_required_timecard_view(self):
        driver = mock.Mock()
        driver.current_url = "https://www.paycomonline.net/v4/ee/web.php/timeclock/WEB04"
        target = "https://www.paycomonline.net/v4/ee/web.php/timecard/WEB02#!timecard-view"
        with mock.patch.object(paycom_hours, "safe_get_with_partial_load") as navigate, mock.patch.object(
            paycom_hours, "wait_for_paycom_timecard_view", return_value=True
        ):
            ready = paycom_hours.ensure_paycom_timecard_view(
                driver,
                target,
                force_navigation=True,
            )
        self.assertTrue(ready)
        navigate.assert_called_once_with(driver, target, "required Web Time Sheet page")

    def test_time_clock_url_is_not_accepted_as_timecard_view(self):
        self.assertFalse(
            paycom_hours.is_paycom_timecard_view_url(
                "https://www.paycomonline.net/v4/ee/web.php/timeclock/WEB04"
            )
        )
        self.assertTrue(
            paycom_hours.is_paycom_timecard_view_url(
                "https://www.paycomonline.net/v4/ee/web.php/timecard/WEB02#!timecard-view"
            )
        )

    def test_captcha_page_is_treated_as_interactive_verification(self):
        driver = _Driver("hCaptcha: Drag the vial to the empty slot it fits into")
        self.assertTrue(paycom_hours.is_paycom_interactive_verification_page(driver))

    def test_wait_returns_when_operator_completes_verification(self):
        driver = mock.Mock()
        with mock.patch.object(paycom_hours, "is_paycom_interactive_verification_page", side_effect=[True, False]), mock.patch.object(
            paycom_hours.time, "sleep"
        ) as sleep:
            success, message = paycom_hours.wait_for_paycom_interactive_verification(driver, timeout_seconds=60)
        self.assertTrue(success)
        self.assertEqual(message, "")
        sleep.assert_called_once_with(1)

    def test_timeout_is_bounded_to_a_safe_range(self):
        with mock.patch.dict(paycom_hours.os.environ, {"PAYCOM_MAC_INTERACTIVE_VERIFICATION_TIMEOUT": "9999"}, clear=False):
            self.assertEqual(paycom_hours._interactive_verification_timeout_seconds(), 600)


if __name__ == "__main__":
    unittest.main()
