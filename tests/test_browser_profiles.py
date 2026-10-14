import os
import socket
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
    @unittest.skipUnless(os.name == "nt", "Windows socket handle inheritance regression")
    def test_chromedriver_does_not_keep_parent_dashboard_port_open(self):
        # Use an inheritable dashboard socket and a harmless Python child in
        # place of ChromeDriver. The port must be reusable while the child is
        # still alive, just as it must be after a dashboard restart.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.set_inheritable(True)
            address = listener.getsockname()
            services = []

            def start_child(*, service, options):
                services.append(service)
                service._start_process(service.path)
                return mock.Mock()

            with (
                mock.patch.object(automation_runtime.ChromeDriverManager, "install", return_value=sys.executable),
                mock.patch.object(automation_runtime.Service, "command_line_args", return_value=[
                    "-c", "import sys; sys.stdin.buffer.read(1)",
                ]),
                mock.patch.object(automation_runtime.webdriver, "Chrome", side_effect=start_child),
            ):
                try:
                    automation_runtime._start_chrome_driver(mock.Mock())
                    child = services[0].process
                    self.assertIsNone(child.poll())
                    listener.close()
                    with socket.socket() as replacement:
                        replacement.bind(address)
                        replacement.listen(1)
                finally:
                    for service in services:
                        child = service.process
                        if child.poll() is None:
                            child.terminate()
                        child.communicate(timeout=5)

    def test_profile_in_use_matches_only_the_exact_chrome_profile(self):
        profile = "/tmp/profile-in-use"
        with mock.patch.object(
            automation_runtime,
            "_collect_chrome_process_entries_with_psutil",
            return_value=[
                ("101", "Google Chrome --user-data-dir=/tmp/profile-in-use"),
                ("102", "Google Chrome --user-data-dir=/tmp/another-profile"),
            ],
        ):
            self.assertTrue(automation_runtime.is_chrome_profile_in_use(profile))

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

        root = os.path.abspath("/tmp/automation-profiles")
        self.assertEqual(mac_profile, os.path.join(root, "macos", "slack_chrome_profile"))
        self.assertEqual(windows_profile, os.path.join(root, "windows", "slack_chrome_profile"))
        self.assertNotEqual(mac_profile, windows_profile)

    def test_paycom_uses_a_new_real_keychain_profile_only_on_macos(self):
        profile = os.path.join(automation_runtime.SCRIPT_DIR, "chrome_profile")
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

        self.assertEqual(
            mac_profile,
            os.path.join(
                os.path.abspath("/tmp/automation-profiles"),
                "macos",
                automation_runtime.MACOS_PAYCOM_KEYCHAIN_PROFILE,
            ),
        )
        self.assertEqual(
            windows_profile,
            os.path.join(os.path.abspath("/tmp/automation-profiles"), "windows", "chrome_profile"),
        )
        self.assertNotEqual(mac_profile, windows_profile)

    @mock.patch.object(automation_runtime, "Service", return_value=mock.Mock())
    @mock.patch.object(automation_runtime.ChromeDriverManager, "install", return_value="/tmp/chromedriver")
    @mock.patch.object(automation_runtime.webdriver, "Chrome")
    def test_macos_paycom_driver_uses_real_keychain_switches(self, mock_chrome, _mock_install, _mock_service):
        driver = mock.Mock()
        mock_chrome.return_value = driver
        profile = os.path.join(
            automation_runtime.PLATFORM_PROFILE_ROOT,
            "macos",
            automation_runtime.MACOS_PAYCOM_KEYCHAIN_PROFILE,
        )

        with mock.patch.object(automation_runtime.sys, "platform", "darwin"):
            automation_runtime.build_chrome_driver(profile)

        options = mock_chrome.call_args.kwargs["options"]
        excluded = options.experimental_options["excludeSwitches"]
        self.assertIn("use-mock-keychain", excluded)
        self.assertIn("password-store", excluded)

    @mock.patch.object(automation_runtime, "Service", return_value=mock.Mock())
    @mock.patch.object(automation_runtime.ChromeDriverManager, "install", return_value="/tmp/chromedriver")
    @mock.patch.object(automation_runtime.webdriver, "Chrome")
    def test_windows_paycom_driver_keeps_existing_switch_behavior(self, mock_chrome, _mock_install, _mock_service):
        mock_chrome.return_value = mock.Mock()
        profile = os.path.join(
            automation_runtime.PLATFORM_PROFILE_ROOT,
            "windows",
            "chrome_profile",
        )

        with mock.patch.object(automation_runtime.sys, "platform", "win32"):
            automation_runtime.build_chrome_driver(profile)

        options = mock_chrome.call_args.kwargs["options"]
        excluded = options.experimental_options["excludeSwitches"]
        self.assertNotIn("use-mock-keychain", excluded)
        self.assertNotIn("password-store", excluded)

    def test_legacy_profile_switch_is_an_immediate_rollback(self):
        profile = os.path.join(automation_runtime.SCRIPT_DIR, "chrome_profile")
        with mock.patch.dict(os.environ, {automation_runtime.LEGACY_PROFILE_FALLBACK_ENV: "1"}):
            resolved = automation_runtime.resolve_automation_profile_path(
                profile,
                system_name="Darwin",
                profiles_root="/tmp/automation-profiles",
            )

        self.assertEqual(resolved, automation_runtime._normalize_profile_path_for_match(profile))

    def test_local_config_can_keep_this_machine_on_its_existing_profiles(self):
        profile = os.path.join(automation_runtime.SCRIPT_DIR, "chrome_profile")
        with mock.patch.object(automation_runtime, "_legacy_profile_fallback_enabled", return_value=True):
            resolved = automation_runtime.resolve_automation_profile_path(profile)

        self.assertEqual(resolved, automation_runtime._normalize_profile_path_for_match(profile))

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

        self.assertEqual(external, automation_runtime._normalize_profile_path_for_match("/tmp/chrome_profile"))
        self.assertEqual(
            generated,
            automation_runtime._normalize_profile_path_for_match(
                os.path.join(
                    automation_runtime.SCRIPT_DIR,
                    "runtime",
                    "generated_profiles",
                    "chrome_profile_worker_1",
                )
            ),
        )

    def test_parallel_workers_keep_using_a_legacy_profile_until_the_new_one_is_set_up(self):
        legacy_profile = os.path.join(automation_runtime.SCRIPT_DIR, "chrome_profile_crm")
        normalized_legacy = automation_runtime._normalize_profile_path_for_match(legacy_profile)
        with mock.patch.object(
            automation_runtime.os.path,
            "isdir",
            side_effect=lambda path: automation_runtime._normalize_profile_path_for_match(path) == normalized_legacy,
        ):
            resolved = automation_runtime.resolve_existing_automation_profile_path(
                legacy_profile,
                system_name="Darwin",
                profiles_root="/tmp/automation-profiles",
            )

        self.assertEqual(resolved, normalized_legacy)

    @mock.patch.object(automation_runtime, "psutil", None)
    @mock.patch.object(automation_runtime.shutil, "which", return_value="/bin/ps")
    @mock.patch.object(automation_runtime.subprocess, "run")
    def test_posix_process_fallback_reads_exact_profile_command_lines(self, mock_run, _mock_which):
        mock_run.return_value = mock.Mock(
            returncode=0,
            stdout=" 100 /Applications/Google Chrome --user-data-dir=/tmp/one\n 200 /bin/zsh\n",
        )

        with mock.patch.object(automation_runtime.os, "name", "posix"):
            entries = automation_runtime._collect_chrome_process_entries_with_posix_ps()

        self.assertEqual(entries, [("100", "/Applications/Google Chrome --user-data-dir=/tmp/one"), ("200", "/bin/zsh")])


if __name__ == "__main__":
    unittest.main()
