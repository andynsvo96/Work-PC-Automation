import unittest
from unittest import mock

import server


class PaycomCredentialRepairRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous_pin_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = False
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()
        self.version_patch = mock.patch("server._automation_version_block_reason", return_value=None)
        self.version_patch.start()

    def tearDown(self):
        self.version_patch.stop()
        server.APP_PIN_REQUIRED = self.previous_pin_required

    def test_repair_route_saves_credential_and_runs_read_only_sync(self):
        with mock.patch.object(server, "normalize_os_name", return_value="windows"), mock.patch.object(
            server, "_run_paycom_credential_repair_dialog", return_value=("saved", "saved")
        ), mock.patch.object(
            server,
            "_sync_paycom_hours_into_work_state",
            return_value=(True, "synced", 37.25, 5, 0),
        ) as sync:
            response = self.client.post("/automation/paycom-credential-repair")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["credential_saved"])
        self.assertTrue(payload["sync_success"])
        self.assertEqual(payload["week_hours"], 37.25)
        sync.assert_called_once_with("credential-repair", update_total_hours=True)

    def test_repair_route_reports_saved_credential_when_sync_fails(self):
        with mock.patch.object(server, "normalize_os_name", return_value="windows"), mock.patch.object(
            server, "_run_paycom_credential_repair_dialog", return_value=("saved", "saved")
        ), mock.patch.object(
            server,
            "_sync_paycom_hours_into_work_state",
            return_value=(False, "Paycom challenge required.", None, 0, 0),
        ):
            response = self.client.post("/automation/paycom-credential-repair")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["credential_saved"])
        self.assertFalse(payload["sync_success"])
        self.assertIn("saved", payload["message"])

    def test_repair_route_can_be_cancelled_without_sync(self):
        with mock.patch.object(server, "normalize_os_name", return_value="windows"), mock.patch.object(
            server, "_run_paycom_credential_repair_dialog", return_value=("cancelled", "Canceled.")
        ), mock.patch.object(server, "_sync_paycom_hours_into_work_state") as sync:
            response = self.client.post("/automation/paycom-credential-repair")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["cancelled"])
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
