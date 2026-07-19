import os
import tempfile
import unittest

from configure_clipboard import configure_peer_url, validate_peer_url


class ConfigureClipboardTests(unittest.TestCase):
    def test_rejects_non_tailscale_or_non_https_url(self):
        for value in ("http://mac.example.ts.net:8443", "https://example.com:8443"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_peer_url(value)

    def test_adds_missing_setting_without_changing_other_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("SECRET_VALUE = 'preserve-me'\n")
            configure_peer_url(path, "https://mac.tailnet.ts.net:8443/")
            with open(path, "r", encoding="utf-8") as handle:
                result = handle.read()
        self.assertIn("SECRET_VALUE = 'preserve-me'", result)
        self.assertIn("AUTOMATION_CLIPBOARD_PEER_URL = 'https://mac.tailnet.ts.net:8443'", result)

    def test_replaces_existing_setting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("AUTOMATION_CLIPBOARD_PEER_URL = 'https://old.tailnet.ts.net:8443'\n")
            configure_peer_url(path, "https://new.tailnet.ts.net:8443")
            with open(path, "r", encoding="utf-8") as handle:
                result = handle.read()
        self.assertNotIn("old.tailnet", result)
        self.assertEqual(result.count("AUTOMATION_CLIPBOARD_PEER_URL"), 1)


if __name__ == "__main__":
    unittest.main()
