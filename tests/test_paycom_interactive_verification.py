import unittest
from unittest import mock
from pathlib import Path
import sys

WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import paycom_hours


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
