import sys
import unittest
from pathlib import Path
from unittest import mock

import server
from paycom_access import is_paycom_ip_block, paycom_ip_block_message

WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))
import paycom_clock


DENIED = (
    "Access Denied Reason: Your company has not authorized your IP address "
    "(108.2.113.126) to access this Web Time Clock terminal. "
    "Please contact your HR department for further assistance."
)


class PaycomIPBlockTests(unittest.TestCase):
    def test_detects_restriction_with_any_ip_and_whitespace(self):
        for address in ("108.2.113.126", "203.0.113.9", "2001:db8::1"):
            text = DENIED.replace("108.2.113.126", address).replace("your IP", "your\nIP")
            self.assertTrue(is_paycom_ip_block(text))
            message = paycom_ip_block_message(text)
            self.assertIn(address, message)
            self.assertIn("No punch was recorded", message)
            self.assertFalse(server._is_retryable_clock_failure(message))

    def test_unrelated_denials_do_not_trigger_fallback(self):
        for text in ("Access Denied", "Could not find 'In Day' button", "Login failed"):
            self.assertIsNone(paycom_ip_block_message(text))

    def test_worker_stops_without_punch_or_visible_retry(self):
        driver = mock.Mock()
        driver.current_url = "https://example.test/timeclock"
        driver.find_element.return_value.text = DENIED
        with (
            mock.patch.object(paycom_clock, "kill_stale_chrome"),
            mock.patch.object(paycom_clock, "build_chrome_driver", return_value=driver),
            mock.patch.object(paycom_clock, "safe_get_with_partial_load"),
            mock.patch.object(paycom_clock, "find_paycom_login_fields", return_value=(None, None, None)),
            mock.patch.object(paycom_clock, "submit_paycom_login", return_value=False),
            mock.patch.object(paycom_clock, "is_paycom_interactive_verification_page", return_value=False),
            mock.patch.object(paycom_clock, "is_paycom_login_page", return_value=False),
            mock.patch.object(paycom_clock, "find_punch_button") as punch,
            mock.patch.object(paycom_clock, "safe_driver_quit") as close,
        ):
            ok, message, retry = paycom_clock._run_once("in", False, "test-profile", True)
        self.assertFalse(ok)
        self.assertFalse(retry)
        self.assertTrue(is_paycom_ip_block(message))
        punch.assert_not_called()
        close.assert_called_once()

    def test_work_continues_slack_and_preserves_tracker_on_ip_block(self):
        for action in ("in", "out"):
            for slack_ok in (True, False):
                with (
                    self.subTest(action=action, slack_ok=slack_ok),
                    mock.patch.object(server, "load_work_state", return_value={"active_shift": None, "total_paid_hours": 0}),
                    mock.patch.object(server, "_infer_active_shift_from_paycom_rows", return_value=(False, "")),
                    mock.patch.object(server, "WORK_CLOCK_SYNC_FROM_PAYCOM", False),
                    mock.patch.object(server, "WORK_CLOCK_CAPPED", False),
                    mock.patch.object(server, "_run_clock_action_with_retry", return_value=(False, paycom_ip_block_message(DENIED))),
                    mock.patch.object(server, "_run_slack_action_with_retry", return_value=(slack_ok, "Slack result")) as slack,
                    mock.patch.object(server, "notify_user") as notify,
                    mock.patch.object(server, "save_work_state") as save,
                    mock.patch.object(server, "_audit_result"),
                ):
                    ok, message = server.run_work(action)
                    self.assertFalse(ok)
                    self.assertIn("Slack completed" if slack_ok else "Slack failed", message)
                    slack.assert_called_once_with(action, retries=1, delay_seconds=3)
                    notify.assert_called_once()
                    save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
