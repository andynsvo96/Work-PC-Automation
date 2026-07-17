import json
import unittest
from unittest import mock

import credential_store


class CredentialStoreTests(unittest.TestCase):
    def test_keyring_round_trip_payload(self):
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = json.dumps({"username": "andy", "secret": "safe"})
        with mock.patch.object(credential_store, "keyring", fake_keyring):
            value = credential_store.read_credential("WorkAutomation/Test")
        self.assertEqual(value.username, "andy")
        self.assertEqual(value.secret, "safe")

    def test_write_uses_fixed_keyring_account(self):
        fake_keyring = mock.Mock()
        with mock.patch.object(credential_store, "keyring", fake_keyring):
            credential_store.write_credential("WorkAutomation/Test", "andy", "safe")
        args = fake_keyring.set_password.call_args.args
        self.assertEqual(args[:2], ("WorkAutomation/Test", credential_store.KEYRING_ACCOUNT))
        self.assertEqual(json.loads(args[2]), {"username": "andy", "secret": "safe"})

    def test_missing_optional_credential_returns_none(self):
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = None
        with mock.patch.object(credential_store, "keyring", fake_keyring), mock.patch.object(
            credential_store, "_legacy_windows_read", return_value=None
        ):
            self.assertIsNone(credential_store.read_credential("WorkAutomation/Missing", required=False))

    def test_invalid_json_secret_is_rejected(self):
        with mock.patch.object(
            credential_store,
            "read_credential",
            return_value=credential_store.StoredCredential("target", "user", "not json"),
        ):
            with self.assertRaises(credential_store.CredentialStoreError):
                credential_store.read_json_credential("target")


if __name__ == "__main__":
    unittest.main()
