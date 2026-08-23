import subprocess
import unittest
from unittest import mock

import server


class CredentialSetupTerminalTests(unittest.TestCase):
    def setUp(self):
        self.previous_pin_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = False
        server.app.config.update(TESTING=True)

    def tearDown(self):
        server.APP_PIN_REQUIRED = self.previous_pin_required

    def test_windows_launches_interactive_powershell_for_selected_service(self):
        with mock.patch.object(server, "normalize_os_name", return_value="windows"), mock.patch.object(
            server.subprocess, "Popen"
        ) as popen:
            store_name = server._launch_credential_setup_terminal("paycom")

        self.assertEqual(store_name, "Windows Credential Manager")
        command = popen.call_args.args[0]
        self.assertEqual(command[:5], ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit", "-Command"])
        self.assertIn("manage_credentials.py", command[5])
        self.assertIn("paycom", command[5])
        self.assertEqual(
            popen.call_args.kwargs["creationflags"],
            getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )

    def test_macos_launches_terminal_for_keychain_setup(self):
        with mock.patch.object(server, "normalize_os_name", return_value="macos"), mock.patch.object(
            server.subprocess, "Popen"
        ) as popen:
            store_name = server._launch_credential_setup_terminal("crm")

        self.assertEqual(store_name, "macOS Keychain")
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "osascript")
        self.assertIn('tell application "Terminal"', command)
        self.assertTrue(any("manage_credentials.py" in part and "crm" in part for part in command))

    def test_unknown_service_is_rejected_before_launch(self):
        with mock.patch.object(server.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                server._launch_credential_setup_terminal("not-a-service")
        popen.assert_not_called()

    def test_endpoint_returns_selected_store_without_exposing_credentials(self):
        with mock.patch.object(
            server, "_launch_credential_setup_terminal", return_value="Windows Credential Manager"
        ) as launch:
            response = server.app.test_client().post(
                "/automation/credential-setup",
                json={"service": "slack"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["service"], "slack")
        self.assertEqual(payload["credential_store"], "Windows Credential Manager")
        self.assertNotIn("password", payload)
        launch.assert_called_once_with("slack")


if __name__ == "__main__":
    unittest.main()
