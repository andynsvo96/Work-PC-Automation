import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import automation_runtime

WORKERS_DIR = Path(__file__).resolve().parents[1] / "workers"
if str(WORKERS_DIR) not in sys.path:
    sys.path.insert(0, str(WORKERS_DIR))

import paycom_hours


class BrowserProfileRuntimeTests(unittest.TestCase):
    def test_persistent_profile_is_isolated_by_operating_system(self):
        profile = os.path.join(automation_runtime.SCRIPT_DIR, "slack_chrome_profile")

        mac_profile = automation_runtime.resolve_automation_profile_path(
            profile,
            system_name="Darwin",
            profiles_root="/tmp/automation-profiles",
        )
        windows_profile = automation_runtime.resolve_automation_profile_path(
            profile,
            system_name="Windows",
            profiles_root="/tmp/automation-profiles",
        )

        self.assertEqual(mac_profile, "/tmp/automation-profiles/macos/slack_chrome_profile")
        self.assertEqual(windows_profile, "/tmp/automation-profiles/windows/slack_chrome_profile")
        self.assertNotEqual(mac_profile, windows_profile)

    def test_legacy_profile_switch_is_an_immediate_rollback(self):
        profile = os.path.join(automation_runtime.SCRIPT_DIR, "chrome_profile")
        with mock.patch.dict(os.environ, {automation_runtime.LEGACY_PROFILE_FALLBACK_ENV: "1"}):
            resolved = automation_runtime.resolve_automation_profile_path(
                profile,
                system_name="Darwin",
                profiles_root="/tmp/automation-profiles",
            )

        self.assertEqual(resolved, os.path.abspath(profile))

    def test_external_and_temporary_profiles_are_not_remapped(self):
        external = automation_runtime.resolve_automation_profile_path(
            "/tmp/chrome_profile",
            system_name="Darwin",
            profiles_root="/tmp/automation-profiles",
        )
        generated = automation_runtime.resolve_automation_profile_path(
            os.path.join(automation_runtime.SCRIPT_DIR, "runtime", "generated_profiles", "chrome_profile_worker_1"),
            system_name="Darwin",
            profiles_root="/tmp/automation-profiles",
        )

        self.assertEqual(external, "/tmp/chrome_profile")
        self.assertTrue(generated.endswith("runtime/generated_profiles/chrome_profile_worker_1"))

    def test_parallel_workers_keep_using_a_legacy_profile_until_the_new_one_is_set_up(self):
        legacy_profile = os.path.join(automation_runtime.SCRIPT_DIR, "chrome_profile_crm")
        with mock.patch.object(automation_runtime.os.path, "isdir", side_effect=lambda path: path == legacy_profile):
            resolved = automation_runtime.resolve_existing_automation_profile_path(
                legacy_profile,
                system_name="Darwin",
                profiles_root="/tmp/automation-profiles",
            )

        self.assertEqual(resolved, legacy_profile)

    @mock.patch.object(automation_runtime, "psutil", None)
    @mock.patch.object(automation_runtime.shutil, "which", return_value="/bin/ps")
    @mock.patch.object(automation_runtime.subprocess, "run")
    def test_posix_process_fallback_reads_exact_profile_command_lines(self, mock_run, _mock_which):
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout=" 100 /Applications/Google Chrome --user-data-dir=/tmp/one\n 200 /bin/zsh\n",
        )

        entries = automation_runtime._collect_chrome_process_entries_with_posix_ps()

        self.assertEqual(entries, [("100", "/Applications/Google Chrome --user-data-dir=/tmp/one"), ("200", "/bin/zsh")])


class PaycomHumanVerificationTests(unittest.TestCase):
    def test_headless_hcaptcha_reports_a_visible_retry_instead_of_a_bad_code(self):
        driver = mock.Mock()
        code_input = mock.Mock()
        code_input.is_selected.return_value = False
        verify_button = mock.Mock()

        with mock.patch.object(paycom_hours, "_visible_two_factor_code_input", return_value=code_input), \
             mock.patch.object(paycom_hours, "_wait_for_two_factor_code", return_value="123456"), \
             mock.patch.object(paycom_hours, "find_visible", side_effect=[None, verify_button]), \
             mock.patch.object(paycom_hours, "_has_paycom_captcha_challenge", return_value=True), \
             mock.patch.object(paycom_hours, "_remove_file_quietly"), \
             mock.patch.object(paycom_hours, "WebDriverWait") as wait:
            wait.return_value.until.side_effect = paycom_hours.TimeoutException()
            ok, message = paycom_hours.complete_paycom_two_factor(driver, allow_interactive_wait=False)

        self.assertFalse(ok)
        self.assertIn("hCaptcha", message)
        self.assertIn("visible browser", message)

    def test_visible_hcaptcha_waits_for_operator_completion(self):
        driver = mock.Mock()
        with mock.patch.object(paycom_hours, "_has_paycom_captcha_challenge", side_effect=[True, False]), \
             mock.patch.object(paycom_hours, "_visible_two_factor_code_input", return_value=None), \
             mock.patch.object(paycom_hours, "write_status_payload") as status, \
             mock.patch.object(paycom_hours, "WebDriverWait") as wait, \
             mock.patch.object(paycom_hours.time, "sleep"):
            wait.return_value.until.return_value = True
            ok, message = paycom_hours._wait_for_paycom_hcaptcha_completion(driver)

        self.assertTrue(ok)
        self.assertEqual(message, "")
        self.assertEqual(status.call_args.kwargs["stage"], "human_verification_required")


if __name__ == "__main__":
    unittest.main()
