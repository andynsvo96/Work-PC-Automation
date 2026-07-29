import unittest
from unittest import mock

import profile_setup_autofill as setup
from credential_store import CredentialNotFoundError, PaycomCredential, StoredCredential
from windows_credentials import SLACK_CREDENTIAL_TARGET


class _Element:
    def __init__(self):
        self.values = []
        self.cleared = 0

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def clear(self):
        self.cleared += 1

    def send_keys(self, value):
        self.values.append(value)


class ProfileSetupAutofillTests(unittest.TestCase):
    def test_paycom_values_read_from_platform_credential_store(self):
        value = PaycomCredential(username="paycom-user", password="secret", pin="0123")
        with mock.patch.object(setup, "read_paycom_credential", return_value=value):
            self.assertEqual(setup._credential_values("paycom"), ("paycom-user", "secret", "0123"))

    def test_slack_uses_dedicated_platform_credential(self):
        credential = StoredCredential(SLACK_CREDENTIAL_TARGET, "slack@example.test", "secret")
        with mock.patch.object(setup, "read_credential", return_value=credential) as read:
            self.assertEqual(setup._credential_values("slack"), ("slack@example.test", "secret", ""))
        read.assert_called_once_with(SLACK_CREDENTIAL_TARGET)

    def test_prefill_does_not_submit_login(self):
        username, password, pin = _Element(), _Element(), _Element()
        driver = mock.Mock()
        with mock.patch.object(setup, "_credential_values", return_value=("user", "pass", "0123")), mock.patch.object(
            setup, "build_chrome_driver", return_value=driver
        ) as build, mock.patch.object(setup, "safe_get_with_partial_load"), mock.patch.object(
            setup, "safe_driver_quit"
        ) as quit_driver, mock.patch.object(
            setup, "_first_visible", side_effect=[username, password, pin]
        ), mock.patch.object(
            setup, "is_chrome_profile_in_use", return_value=False
        ):
            result = setup.open_and_prefill_setup_profile("paycom", "/tmp/profile", "https://example.test", wait_seconds=1)

        self.assertEqual(result.fields_filled, ("username", "password", "PIN"))
        self.assertEqual(username.values, ["user"])
        self.assertEqual(password.values, ["pass"])
        self.assertEqual(pin.values, ["0123"])
        build.assert_called_once()
        self.assertTrue(build.call_args.kwargs["detach"])
        quit_driver.assert_called_once_with(driver, profile_path="/tmp/profile")

    def test_missing_credential_opens_profile_for_manual_setup(self):
        driver = mock.Mock()
        with mock.patch.object(setup, "_credential_values", side_effect=CredentialNotFoundError("missing")), mock.patch.object(
            setup, "build_chrome_driver", return_value=driver
        ), mock.patch.object(setup, "safe_get_with_partial_load"), mock.patch.object(
            setup, "safe_driver_quit"
        ), mock.patch.object(setup, "is_chrome_profile_in_use", return_value=False):
            result = setup.open_and_prefill_setup_profile("slack", "/tmp/profile", "https://example.test")

        self.assertFalse(result.credential_available)
        self.assertIn("no stored login", result.message)

    def test_open_profile_is_reported_without_starting_another_chrome(self):
        with mock.patch.object(setup, "is_chrome_profile_in_use", return_value=True), mock.patch.object(
            setup, "build_chrome_driver"
        ) as build:
            with self.assertRaises(setup.ChromeProfileInUseError):
                setup.open_and_prefill_setup_profile("paycom", "/tmp/profile", "https://example.test")
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
