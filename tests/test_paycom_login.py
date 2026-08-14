import sys
import unittest
from pathlib import Path
from unittest import mock


WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import paycom_login


class _Field:
    def __init__(self, value=""):
        self.value = value
        self.sent = []

    def get_attribute(self, name):
        return self.value if name == "value" else ""

    def clear(self):
        self.value = ""

    def send_keys(self, value):
        self.value = value
        self.sent.append(value)


class _Button:
    def __init__(self):
        self.clicked = False

    def click(self):
        self.clicked = True


class PaycomLoginTests(unittest.TestCase):
    def test_no_login_fields_makes_no_click(self):
        with mock.patch.object(paycom_login, "find_visible") as find_visible:
            submitted = paycom_login.submit_paycom_login(mock.Mock(), (None, None, None))
        self.assertFalse(submitted)
        find_visible.assert_not_called()

    def test_trusted_session_mode_never_submits_login(self):
        fields = (_Field("saved-user"), _Field("saved-password"), _Field("1234"))
        with mock.patch.object(paycom_login, "find_visible") as find_visible, mock.patch.object(
            paycom_login, "read_paycom_credential"
        ) as read_credential:
            with self.assertRaises(paycom_login.PaycomTrustedSessionRequiredError):
                paycom_login.submit_paycom_login(
                    mock.Mock(),
                    fields,
                    allow_credential_submission=False,
                )

        find_visible.assert_not_called()
        read_credential.assert_not_called()

    def test_browser_autofill_is_used_without_reading_os_credential(self):
        fields = (_Field("saved-user"), _Field("saved-password"), _Field("1234"))
        button = _Button()
        with mock.patch.object(paycom_login, "find_visible", return_value=button), mock.patch.object(
            paycom_login, "read_paycom_credential"
        ) as read_credential, mock.patch.object(paycom_login, "WebDriverWait") as wait:
            submitted = paycom_login.submit_paycom_login(mock.Mock(), fields)
        self.assertTrue(submitted)
        self.assertTrue(button.clicked)
        read_credential.assert_not_called()
        wait.assert_called_once()

    def test_empty_fields_are_filled_from_os_credential(self):
        fields = (_Field(), _Field(), _Field())
        button = _Button()
        credential = mock.Mock(username="paycom-user", password="secret", pin="0123")
        with mock.patch.object(paycom_login, "_wait_for_browser_autofill"), mock.patch.object(
            paycom_login, "read_paycom_credential", return_value=credential
        ), mock.patch.object(paycom_login, "find_visible", return_value=button), mock.patch.object(
            paycom_login, "WebDriverWait"
        ):
            submitted = paycom_login.submit_paycom_login(mock.Mock(), fields)
        self.assertTrue(submitted)
        self.assertEqual([field.value for field in fields], ["paycom-user", "secret", "0123"])

    def test_login_form_without_submit_button_fails_clearly(self):
        fields = (_Field("saved-user"), _Field("saved-password"), _Field("1234"))
        with mock.patch.object(paycom_login, "find_visible", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Log In button"):
                paycom_login.submit_paycom_login(mock.Mock(), fields)


if __name__ == "__main__":
    unittest.main()
