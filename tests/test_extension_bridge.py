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


if __name__ == "__main__":
    unittest.main()
