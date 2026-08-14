import unittest
from unittest import mock

import server


class PaycomCredentialConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.previous_pin_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = False
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()
        self.version_patch = mock.patch(
            "server._automation_version_block_reason", return_value=None
        )
        self.version_patch.start()

    def tearDown(self):
        self.version_patch.stop()
        server.APP_PIN_REQUIRED = self.previous_pin_required

    def test_launcher_runs_existing_windows_credential_command(self):
        with (
            mock.patch.object(server, "normalize_os_name", return_value="windows"),
            mock.patch.object(server.os.path, "isfile", return_value=True),
            mock.patch.object(server.subprocess, "Popen") as popen,
        ):
            server._launch_paycom_credential_configuration()

        popen.assert_called_once_with(
            [
                server._resolve_console_python(),
                server.WINDOWS_CREDENTIAL_MANAGER_SCRIPT,
                "set",
                "paycom",
            ],
            cwd=server.SCRIPT_DIR,
            creationflags=getattr(server.subprocess, "CREATE_NEW_CONSOLE", 0),
        )

    def test_route_only_opens_configuration_without_paycom_sync(self):
        with (
            mock.patch.object(server, "normalize_os_name", return_value="windows"),
            mock.patch.object(server, "_launch_paycom_credential_configuration") as launch,
            mock.patch.object(server, "_sync_paycom_hours_into_work_state") as sync,
            mock.patch.object(server, "log_automation_result"),
        ):
            response = self.client.post("/automation/paycom-credential-configure")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertIn("No Paycom login", response.get_json()["message"])
        launch.assert_called_once_with()
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
