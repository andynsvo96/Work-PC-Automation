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
        self.audit_event_patch = mock.patch("server.log_automation_event")
        self.audit_result_patch = mock.patch("server.log_automation_result")
        self.audit_event_patch.start()
        self.audit_result_patch.start()

    def tearDown(self):
        self.audit_result_patch.stop()
        self.audit_event_patch.stop()
        self.version_patch.stop()
        server.APP_PIN_REQUIRED = self.previous_pin_required

    def test_repair_route_saves_credential_without_contacting_paycom(self):
        with mock.patch.object(server, "normalize_os_name", return_value="windows"), mock.patch.object(
            server, "_run_paycom_credential_repair_dialog", return_value=("saved", "saved")
        ), mock.patch.object(server, "_sync_paycom_hours_into_work_state") as sync:
            response = self.client.post("/automation/paycom-credential-repair")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["credential_saved"])
        self.assertFalse(payload["sync_attempted"])
        self.assertIn("No Paycom login", payload["message"])
        sync.assert_not_called()

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
