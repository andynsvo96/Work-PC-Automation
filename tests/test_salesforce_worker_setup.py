import sys
import types
import unittest
from unittest import mock

import server


class SalesforceWorkerSetupTests(unittest.TestCase):
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

    def test_status_lists_each_configured_worker(self):
        saved_state = {
            "workers": {
                "1": {
                    "last_test_success": True,
                    "last_tested_at": "2026-08-10T08:00:00-04:00",
                    "message": "Salesforce Worker 1 is connected.",
                },
                "2": {
                    "last_test_success": False,
                    "message": "Salesforce Worker 2 requires login or 2FA.",
                },
                "3": {
                    "last_test_success": True,
                    "message": "Stale result for a removed profile.",
                },
            }
        }
        with (
            mock.patch("server._salesforce_worker_count", return_value=3),
            mock.patch("server._load_salesforce_worker_setup_state", return_value=saved_state),
            mock.patch("server._salesforce_worker_profile_path", side_effect=lambda slot: f"profile-{slot}"),
            mock.patch("server.os.path.isdir", side_effect=lambda path: path != "profile-3"),
            mock.patch("server.is_chrome_profile_in_use", return_value=False),
        ):
            response = self.client.get("/api/salesforce-worker-setup")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["worker_count"], 3)
        self.assertEqual(
            [worker["status"] for worker in payload["workers"]],
            ["connected", "login_required", "not_initialized"],
        )

    def test_verification_status_returns_the_oldest_pending_request(self):
        pending = [
            {"request_id": "request-one", "worker_slot": 2, "order_id": "4600001"},
            {"request_id": "request-two", "worker_slot": 4, "order_id": "4600002"},
        ]
        with mock.patch("server.list_pending_salesforce_verification_requests", return_value=pending):
            response = self.client.get("/api/salesforce-verification")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["request"], pending[0])
        self.assertEqual(payload["requests"], pending)

    def test_verification_submit_passes_code_to_waiting_worker_without_echoing_it(self):
        public_request = {"request_id": "request-one", "status": "submitted", "worker_slot": 2}
        with mock.patch(
            "server.submit_salesforce_verification_code",
            return_value=public_request,
        ) as submit_code:
            response = self.client.post(
                "/api/salesforce-verification/submit",
                json={"request_id": "request-one", "code": "123456"},
            )

        self.assertEqual(response.status_code, 200)
        submit_code.assert_called_once_with("request-one", "123456")
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertNotIn("123456", response.get_data(as_text=True))

    def test_verification_submit_reports_invalid_code(self):
        with mock.patch(
            "server.submit_salesforce_verification_code",
            side_effect=ValueError("Enter the 6-digit Salesforce verification code."),
        ):
            response = self.client.post(
                "/api/salesforce-verification/submit",
                json={"request_id": "request-one", "code": "12345"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])

    def test_setup_opens_the_selected_persistent_worker_profile(self):
        result = types.SimpleNamespace(fields_filled=("username", "password"))
        with (
            mock.patch("server._salesforce_worker_count", return_value=3),
            mock.patch("server._ensure_salesforce_worker_profile", return_value="worker-profile") as ensure_profile,
            mock.patch("server.open_and_prefill_setup_profile", return_value=result) as open_setup,
            mock.patch("server._update_salesforce_worker_setup_state") as update_state,
        ):
            response = self.client.post("/automation/salesforce-worker-setup", json={"worker": 2})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        ensure_profile.assert_called_once_with(2)
        open_setup.assert_called_once_with(
            "salesforce",
            "worker-profile",
            "https://printfly.lightning.force.com/lightning/",
        )
        self.assertIsNone(update_state.call_args.kwargs["last_test_success"])

    def test_connection_test_is_read_only_and_marks_connected(self):
        driver = object()
        fake_worker = types.SimpleNamespace(
            _is_salesforce_login_page=mock.Mock(return_value=False),
            _is_salesforce_login_approval_page=mock.Mock(return_value=False),
            _is_salesforce_verification_code_page=mock.Mock(return_value=False),
            _is_salesforce_authenticated_page=mock.Mock(return_value=True),
        )
        with (
            mock.patch("server._salesforce_worker_count", return_value=3),
            mock.patch("server._salesforce_worker_profile_path", return_value="worker-profile"),
            mock.patch("server.os.path.isdir", return_value=True),
            mock.patch("server._wait_for_salesforce_worker_profile_available", return_value=True),
            mock.patch("server.build_chrome_driver", return_value=driver) as build_driver,
            mock.patch("server.safe_get_with_partial_load") as open_page,
            mock.patch("server.safe_driver_quit") as quit_driver,
            mock.patch("server._update_salesforce_worker_setup_state") as update_state,
            mock.patch.dict(sys.modules, {"crm_copyright_cancel": fake_worker}),
        ):
            response = self.client.post("/automation/salesforce-worker-test", json={"worker": 1})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertTrue(payload["connected"])
        build_driver.assert_called_once()
        open_page.assert_called_once_with(
            driver,
            "https://printfly.lightning.force.com/lightning/",
            "Salesforce Worker 1 test",
        )
        self.assertTrue(update_state.call_args.kwargs["last_test_success"])
        quit_driver.assert_called_once_with(driver, profile_path="worker-profile")

    def test_connection_test_reports_verification_code_without_submitting_login(self):
        driver = object()
        fake_worker = types.SimpleNamespace(
            _is_salesforce_login_page=mock.Mock(return_value=False),
            _is_salesforce_login_approval_page=mock.Mock(return_value=True),
            _is_salesforce_verification_code_page=mock.Mock(return_value=True),
            _is_salesforce_authenticated_page=mock.Mock(return_value=False),
            _click_salesforce_saved_username=mock.Mock(),
            _fill_salesforce_login_with_autofill=mock.Mock(),
            _click_salesforce_login_with_selenium=mock.Mock(),
        )
        with (
            mock.patch("server._salesforce_worker_count", return_value=3),
            mock.patch("server._salesforce_worker_profile_path", return_value="worker-profile"),
            mock.patch("server.os.path.isdir", return_value=True),
            mock.patch("server._wait_for_salesforce_worker_profile_available", return_value=True),
            mock.patch("server.build_chrome_driver", return_value=driver),
            mock.patch("server.safe_get_with_partial_load"),
            mock.patch("server.safe_driver_quit"),
            mock.patch("server._update_salesforce_worker_setup_state"),
            mock.patch.dict(sys.modules, {"crm_copyright_cancel": fake_worker}),
        ):
            response = self.client.post("/automation/salesforce-worker-test", json={"worker": 1})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["connected"])
        self.assertTrue(payload["verification_code_required"])
        self.assertIn("6-digit verification code", payload["message"])
        fake_worker._click_salesforce_saved_username.assert_not_called()
        fake_worker._fill_salesforce_login_with_autofill.assert_not_called()
        fake_worker._click_salesforce_login_with_selenium.assert_not_called()

    def test_connection_test_reports_login_without_submitting_credentials(self):
        driver = object()
        fake_worker = types.SimpleNamespace(
            _is_salesforce_login_page=mock.Mock(return_value=True),
            _is_salesforce_login_approval_page=mock.Mock(return_value=False),
            _is_salesforce_verification_code_page=mock.Mock(return_value=False),
            _is_salesforce_authenticated_page=mock.Mock(return_value=False),
            _fill_salesforce_login_with_autofill=mock.Mock(),
            _click_salesforce_login_with_selenium=mock.Mock(),
        )
        with (
            mock.patch("server._salesforce_worker_count", return_value=3),
            mock.patch("server._salesforce_worker_profile_path", return_value="worker-profile"),
            mock.patch("server.os.path.isdir", return_value=True),
            mock.patch("server._wait_for_salesforce_worker_profile_available", return_value=True),
            mock.patch("server.build_chrome_driver", return_value=driver),
            mock.patch("server.safe_get_with_partial_load"),
            mock.patch("server.safe_driver_quit"),
            mock.patch("server._update_salesforce_worker_setup_state"),
            mock.patch.dict(sys.modules, {"crm_copyright_cancel": fake_worker}),
        ):
            response = self.client.post("/automation/salesforce-worker-test", json={"worker": 1})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["connected"])
        self.assertIn("will not submit credentials", payload["message"])
        fake_worker._fill_salesforce_login_with_autofill.assert_not_called()
        fake_worker._click_salesforce_login_with_selenium.assert_not_called()

    def test_connection_test_refuses_to_interrupt_an_open_profile(self):
        with (
            mock.patch("server._salesforce_worker_count", return_value=3),
            mock.patch("server._salesforce_worker_profile_path", return_value="worker-profile"),
            mock.patch("server.os.path.isdir", return_value=True),
            mock.patch("server._wait_for_salesforce_worker_profile_available", return_value=False),
            mock.patch("server.build_chrome_driver") as build_driver,
        ):
            response = self.client.post("/automation/salesforce-worker-test", json={"worker": 1})

        self.assertEqual(response.status_code, 409)
        self.assertIn("still open or in use", response.get_json()["message"])
        build_driver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
