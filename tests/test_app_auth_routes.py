import unittest

import server
from app_security import create_app_security


class AppAuthRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous_required = server.APP_PIN_REQUIRED
        self.previous_config = server.app_security_config
        self.previous_error = server.app_security_initialization_error
        self.previous_secret = server.app.secret_key
        server.APP_PIN_REQUIRED = True
        server.app_security_config = create_app_security("123456")
        server.app_security_initialization_error = None
        server.app.secret_key = server.app_security_config.session_secret
        server.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False)
        server.app_login_attempts.clear()
        self.client = server.app.test_client()

    def tearDown(self):
        server.APP_PIN_REQUIRED = self.previous_required
        server.app_security_config = self.previous_config
        server.app_security_initialization_error = self.previous_error
        server.app.secret_key = self.previous_secret

    def test_pin_login_and_csrf_protection(self):
        response = self.client.get("/api/node-runtime")
        self.assertEqual(response.status_code, 401)

        response = self.client.post("/api/auth/login", json={"pin": "123456"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Expires=", response.headers.get("Set-Cookie", ""))
        self.assertEqual(
            server.app.config["PERMANENT_SESSION_LIFETIME"],
            server.timedelta(days=server.APP_TRUSTED_DEVICE_DAYS),
        )
        token = response.get_json()["csrf_token"]

        response = self.client.post("/api/queue/cancel-all")
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/queue/cancel-all", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)

    def test_legacy_get_actions_are_blocked_after_login(self):
        self.client.post("/api/auth/login", json={"pin": "123456"})
        response = self.client.get("/work/in")
        self.assertEqual(response.status_code, 405)

    def test_clipboard_peer_endpoint_requires_valid_machine_signature(self):
        response = self.client.get("/api/clipboard/peer/status")
        self.assertEqual(response.status_code, 401)

        path = "/api/clipboard/peer/status"
        headers = server.clipboard_peer_authenticator.headers("GET", path, b"")
        response = self.client.get(path, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])

    def test_app_update_requires_login_and_csrf(self):
        response = self.client.post("/api/app/update")
        self.assertEqual(response.status_code, 401)

        login = self.client.post("/api/auth/login", json={"pin": "123456"})
        self.assertEqual(login.status_code, 200)
        response = self.client.post("/api/app/update")
        self.assertEqual(response.status_code, 403)

if __name__ == "__main__":
    unittest.main()
