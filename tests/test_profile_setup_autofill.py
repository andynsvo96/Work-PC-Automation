import unittest
from unittest import mock

import profile_setup_autofill as setup
from credential_store import CredentialNotFoundError, PaycomCredential, StoredCredential
from windows_credentials import (
    CRM_CREDENTIAL_TARGET,
    SALESFORCE_CREDENTIAL_TARGET,
    SANMAR_CREDENTIAL_TARGET,
    SLACK_CREDENTIAL_TARGET,
)


class _Element:
    def __init__(self, element_id=None):
        self.values = []
        self.cleared = 0
        self.id = element_id

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def clear(self):
        self.cleared += 1

    def send_keys(self, value):
        self.values.append(value)

    def get_attribute(self, name):
        if name == "value" and self.values:
            return self.values[-1]
        return ""


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

    def test_each_standard_setup_uses_its_own_platform_credential(self):
        for service, target in (
            ("crm", CRM_CREDENTIAL_TARGET),
            ("sanmar", SANMAR_CREDENTIAL_TARGET),
            ("salesforce", SALESFORCE_CREDENTIAL_TARGET),
        ):
            with self.subTest(service=service), mock.patch.object(
                setup,
                "read_credential",
                return_value=StoredCredential(target, f"{service}-user", "secret"),
            ) as read:
                self.assertEqual(
                    setup._credential_values(service),
                    (f"{service}-user", "secret", ""),
                )
                read.assert_called_once_with(target)

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
        quit_driver.assert_called_once_with(driver, profile_path="/tmp/profile", keep_browser_open=True)

    def test_prefill_waits_for_password_that_renders_after_username(self):
        username = _Element("username")
        password = _Element("password")
        driver = mock.Mock()
        detected = [username, None, username, password]
        with mock.patch.object(setup, "_credential_values", return_value=("user", "pass", "")), mock.patch.object(
            setup, "build_chrome_driver", return_value=driver
        ), mock.patch.object(setup, "safe_get_with_partial_load"), mock.patch.object(
            setup, "safe_driver_quit"
        ), mock.patch.object(
            setup, "_first_visible", side_effect=detected
        ), mock.patch.object(
            setup, "is_chrome_profile_in_use", return_value=False
        ), mock.patch.object(setup.time, "sleep"):
            result = setup.open_and_prefill_setup_profile("slack", "/tmp/profile", "https://example.test", wait_seconds=1)

        self.assertEqual(result.fields_filled, ("username", "password"))
        self.assertEqual(username.values, ["user"])
        self.assertEqual(password.values, ["pass"])

    def test_fill_replaces_a_different_browser_saved_value(self):
        field = _Element("username")
        field.values.append("old-user")

        self.assertTrue(setup._fill(field, "windows-user"))
        self.assertEqual(field.cleared, 1)
        self.assertEqual(field.values[-1], "windows-user")

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

    def test_native_setup_uses_plain_chrome_without_webdriver_flags(self):
        with mock.patch.object(setup, "is_chrome_profile_in_use", return_value=False), mock.patch.object(
            setup.subprocess, "Popen"
        ) as popen, mock.patch.object(setup.os, "makedirs"), mock.patch.object(setup.sys, "platform", "darwin"):
            result = setup.open_native_setup_profile(
                "paycom",
                "/tmp/paycom-profile",
                "https://example.test/login",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            )

        args = popen.call_args.args[0]
        self.assertEqual(args[:4], ["open", "-na", "Google Chrome", "--args"])
        self.assertIn("--user-data-dir=/tmp/paycom-profile", args)
        self.assertFalse(any("webdriver" in arg.lower() for arg in args))
        self.assertFalse(any("remote-debugging" in arg.lower() for arg in args))
        self.assertFalse(any("mock-keychain" in arg.lower() for arg in args))
        self.assertFalse(result.credential_available)

    def test_windows_native_setup_keeps_paycom_in_dedicated_profile(self):
        with mock.patch.object(setup, "is_chrome_profile_in_use", return_value=False), mock.patch.object(
            setup.subprocess, "Popen"
        ) as popen, mock.patch.object(setup.os, "makedirs"), mock.patch.object(setup.sys, "platform", "win32"):
            setup.open_native_setup_profile(
                "paycom",
                r"C:\Automation\chrome_profile",
                "https://example.test/login",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            )

        args = popen.call_args.args[0]
        self.assertEqual(args[0], r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        self.assertIn(r"--user-data-dir=C:\Automation\chrome_profile", args)
        self.assertIn("--profile-directory=Default", args)
        self.assertFalse(any("webdriver" in arg.lower() for arg in args))
        self.assertFalse(any("remote-debugging" in arg.lower() for arg in args))


if __name__ == "__main__":
    unittest.main()
