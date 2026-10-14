import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import automation_runtime

WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import paycom_clock


def policy_block():
    error = OSError("An Application Control policy has blocked this file")
    error.winerror = 4551
    return error


class ChromeDriverPolicyTests(unittest.TestCase):
    def test_policy_block_reports_actual_executable_for_both_session_types(self):
        for attached in (False, True):
            error = policy_block()
            with (
                self.subTest(attached=attached),
                tempfile.TemporaryDirectory() as folder,
                mock.patch.object(automation_runtime.ChromeDriverManager, "install", return_value="downloaded-driver"),
                mock.patch.object(automation_runtime, "Service", return_value=mock.Mock(path="selected-driver")),
                mock.patch.object(automation_runtime.webdriver, "Chrome", side_effect=error) as chrome,
                mock.patch.object(automation_runtime, "install_mailto_click_guard") as guard,
                self.assertRaises(automation_runtime.ChromeDriverPolicyError) as raised,
            ):
                if attached:
                    automation_runtime.build_attached_chrome_driver()
                else:
                    automation_runtime.build_chrome_driver(os.path.join(folder, "profile"))

            message = str(raised.exception)
            self.assertIn("WinError 4551", message)
            self.assertIn('Driver: "selected-driver"', message)
            self.assertIn("Windows Security > App & browser control", message)
            self.assertIs(raised.exception.__cause__, error)
            self.assertFalse(paycom_clock._is_retryable_exception(raised.exception))
            chrome.assert_called_once()
            guard.assert_not_called()

    def test_unrelated_os_errors_are_preserved(self):
        error = OSError("Unrelated launch failure")
        with (
            mock.patch.object(automation_runtime.ChromeDriverManager, "install", return_value="downloaded-driver"),
            mock.patch.object(automation_runtime, "Service"),
            mock.patch.object(automation_runtime.webdriver, "Chrome", side_effect=error),
            self.assertRaises(OSError) as raised,
        ):
            automation_runtime._start_chrome_driver(mock.Mock())
        self.assertIs(raised.exception, error)

    def test_clock_policy_block_stops_before_navigation_or_punch(self):
        error = automation_runtime.ChromeDriverPolicyError(
            "Windows Application Control blocked ChromeDriver (WinError 4551)."
        )
        with (
            mock.patch.object(paycom_clock, "kill_stale_chrome"),
            mock.patch.object(paycom_clock, "build_chrome_driver", side_effect=error) as build_driver,
            mock.patch.object(paycom_clock, "safe_get_with_partial_load") as navigate,
            mock.patch.object(paycom_clock, "find_punch_button") as punch,
            mock.patch.object(paycom_clock, "write_result") as result,
            mock.patch.object(paycom_clock, "log_automation_event"),
            mock.patch.object(paycom_clock, "paycom_headless_mode_enabled", return_value=True),
            self.assertRaises(SystemExit) as raised,
        ):
            paycom_clock.run("in", dry_run=False)

        self.assertEqual(raised.exception.code, 1)
        build_driver.assert_called_once()
        navigate.assert_not_called()
        punch.assert_not_called()
        result.assert_called_once_with(False, f"Clock-in failed: ChromeDriverPolicyError: {error}")


if __name__ == "__main__":
    unittest.main()
