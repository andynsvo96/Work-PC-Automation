import unittest
from unittest import mock

from app_security import AppSecurityError, create_app_security, load_app_security


class AppSecurityTests(unittest.TestCase):
    def test_pin_hash_verification(self):
        config = create_app_security("123456")
        self.assertTrue(config.verify_pin("123456"))
        self.assertFalse(config.verify_pin("654321"))
        self.assertNotIn("123456", config.pin_hash)
        self.assertGreaterEqual(len(config.session_secret), 32)

    def test_pin_must_be_six_to_twelve_digits(self):
        for invalid in ("12345", "1234567890123", "abcdef"):
            with self.assertRaises(AppSecurityError):
                create_app_security(invalid)

    def test_load_from_keychain_payload(self):
        created = create_app_security("123456")
        payload = type("Credential", (), {"secret": __import__("json").dumps(created.to_dict())})()
        with mock.patch("app_security.read_credential", return_value=payload):
            loaded = load_app_security()
        self.assertTrue(loaded.verify_pin("123456"))


if __name__ == "__main__":
    unittest.main()
