import json
import os
import sys
import unittest
import uuid


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from windows_credentials import (
    credential_exists,
    delete_windows_credential,
    read_json_credential,
    read_windows_credential,
    write_windows_credential,
)


@unittest.skipUnless(os.name == "nt", "Windows Credential Manager is Windows-only")
class WindowsCredentialIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.target = f"WorkAutomation/Test/{uuid.uuid4()}"

    def tearDown(self):
        delete_windows_credential(self.target, missing_ok=True)

    def test_round_trip_unicode_secret(self):
        write_windows_credential(self.target, "test-user", "pässword-✓")
        stored = read_windows_credential(self.target)
        self.assertEqual("test-user", stored.username)
        self.assertEqual("pässword-✓", stored.secret)
        self.assertTrue(credential_exists(self.target))

    def test_json_round_trip(self):
        value = {"client_email": "robot@example.test", "private_key": "private\nkey"}
        write_windows_credential(
            self.target,
            value["client_email"],
            json.dumps(value, separators=(",", ":")),
        )
        self.assertEqual(value, read_json_credential(self.target))


if __name__ == "__main__":
    unittest.main()
