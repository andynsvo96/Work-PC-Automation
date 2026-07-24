import unittest

import server


class ChromeExtensionBridgeTests(unittest.TestCase):
    ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

    def setUp(self):
        self.previous_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = True
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()

    def tearDown(self):
        server.APP_PIN_REQUIRED = self.previous_required

    def test_status_is_available_without_app_session_to_a_chrome_extension(self):
        response = self.client.get(
            "/api/extension/bridge/status",
            headers={"Origin": self.ORIGIN},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], self.ORIGIN)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.get_json()["protocol"], server.CHROME_EXTENSION_BRIDGE_PROTOCOL)

    def test_status_allows_a_loopback_extension_fetch_without_origin(self):
        response = self.client.get(
            "/api/extension/bridge/status",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_status_rejects_web_origins_and_non_loopback_clients(self):
        web_response = self.client.get(
            "/api/extension/bridge/status",
            headers={"Origin": "https://example.com"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        remote_response = self.client.get(
            "/api/extension/bridge/status",
            headers={"Origin": self.ORIGIN},
            environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
        )

        self.assertEqual(web_response.status_code, 403)
        self.assertEqual(remote_response.status_code, 403)

    def test_only_valid_chrome_extension_origins_are_accepted(self):
        valid = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        self.assertTrue(server._is_chrome_extension_origin(valid))
        self.assertFalse(server._is_chrome_extension_origin("chrome-extension://not-an-extension-id"))
        self.assertFalse(server._is_chrome_extension_origin("https://example.com"))

    def test_pairing_issues_a_token_and_requires_it_for_order_controls(self):
        previous_required = server.APP_PIN_REQUIRED
        server.APP_PIN_REQUIRED = False
        try:
            pair_response = self.client.post(
                "/api/extension/bridge/pair",
                json={},
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(pair_response.status_code, 200)
            token = pair_response.get_json()["token"]

            rejected = self.client.get(
                "/api/extension/bridge/process-order/status",
                headers={"Origin": self.ORIGIN},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(rejected.status_code, 401)

            invalid_order = self.client.post(
                "/api/extension/bridge/process-order",
                json={"order_id": "not-an-order"},
                headers={"Origin": self.ORIGIN, "Authorization": f"Bearer {token}"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(invalid_order.status_code, 409)
            self.assertFalse(invalid_order.get_json()["success"])
        finally:
            server.APP_PIN_REQUIRED = previous_required


if __name__ == "__main__":
    unittest.main()
