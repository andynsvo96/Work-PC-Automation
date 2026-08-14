import json
import unittest
from unittest import mock

import credential_store


class CredentialStoreTests(unittest.TestCase):
    def test_keyring_round_trip_payload(self):
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = json.dumps({"username": "andy", "secret": "safe"})
        with mock.patch.object(credential_store.os, "name", "posix"), mock.patch.object(
            credential_store, "keyring", fake_keyring
        ), mock.patch.object(credential_store, "_is_macos", return_value=False):
            value = credential_store.read_credential("WorkAutomation/Test")
        self.assertEqual(value.username, "andy")
        self.assertEqual(value.secret, "safe")

    def test_write_uses_fixed_keyring_account(self):
        fake_keyring = mock.Mock()
        with mock.patch.object(credential_store.os, "name", "posix"), mock.patch.object(
            credential_store, "keyring", fake_keyring
        ), mock.patch.object(credential_store, "_is_macos", return_value=False):
            credential_store.write_credential("WorkAutomation/Test", "andy", "safe")
        args = fake_keyring.set_password.call_args.args
        self.assertEqual(args[:2], ("WorkAutomation/Test", credential_store.KEYRING_ACCOUNT))
        self.assertEqual(json.loads(args[2]), {"username": "andy", "secret": "safe"})

    def test_missing_optional_credential_returns_none(self):
        fake_keyring = mock.Mock()
        fake_keyring.get_password.return_value = None
        with mock.patch.object(credential_store.os, "name", "posix"), mock.patch.object(
            credential_store, "keyring", fake_keyring
        ), mock.patch.object(credential_store, "_is_macos", return_value=False):
            self.assertIsNone(credential_store.read_credential("WorkAutomation/Missing", required=False))

    def test_macos_reads_native_keychain_without_keyring(self):
        native_api = mock.Mock()
        native_api.read.return_value = json.dumps({"username": "andy", "secret": "safe"})
        fake_keyring = mock.Mock()
        with mock.patch.object(credential_store.os, "name", "posix"), mock.patch.object(
            credential_store, "_is_macos", return_value=True
        ), mock.patch.object(credential_store, "keyring", fake_keyring), mock.patch.object(
            credential_store, "_get_macos_keychain_api", return_value=native_api
        ):
            value = credential_store.read_credential("WorkAutomation/Test")
        self.assertEqual(value.username, "andy")
        self.assertEqual(value.secret, "safe")
        native_api.read.assert_called_once_with("WorkAutomation/Test")
        fake_keyring.get_password.assert_not_called()

    def test_macos_write_uses_native_security_framework(self):
        native_api = mock.Mock()
        with mock.patch.object(credential_store.os, "name", "posix"), mock.patch.object(
            credential_store, "_is_macos", return_value=True
        ), mock.patch.object(
            credential_store, "_get_macos_keychain_api", return_value=native_api
        ):
            credential_store.write_credential("WorkAutomation/Test", "andy", "safe")
        target, native_payload = native_api.write.call_args.args
        self.assertEqual(target, "WorkAutomation/Test")
        self.assertEqual(json.loads(native_payload), {"username": "andy", "secret": "safe"})

    def test_macos_missing_optional_credential_returns_none(self):
        native_api = mock.Mock()
        native_api.read.return_value = None
        with mock.patch.object(credential_store.os, "name", "posix"), mock.patch.object(
            credential_store, "_is_macos", return_value=True
        ), mock.patch.object(
            credential_store, "_get_macos_keychain_api", return_value=native_api
        ):
            self.assertIsNone(credential_store.read_credential("WorkAutomation/Missing", required=False))

    def test_macos_delete_uses_native_keychain(self):
        native_api = mock.Mock()
        native_api.delete.return_value = True
        with mock.patch.object(credential_store.os, "name", "posix"), mock.patch.object(
            credential_store, "_is_macos", return_value=True
        ), mock.patch.object(
            credential_store, "_get_macos_keychain_api", return_value=native_api
        ):
            removed = credential_store.delete_credential("WorkAutomation/Test")
        self.assertTrue(removed)
        native_api.delete.assert_called_once_with("WorkAutomation/Test")

    def test_windows_reads_native_credential_without_calling_keyring(self):
        fake_keyring = mock.Mock()
        native = credential_store.StoredCredential("WorkAutomation/Test", "andy", "safe")
        with mock.patch.object(credential_store.os, "name", "nt"), mock.patch.object(
            credential_store, "keyring", fake_keyring
        ), mock.patch.object(credential_store, "_legacy_windows_read", return_value=native):
            value = credential_store.read_credential("WorkAutomation/Test")
        self.assertEqual(value, native)
        fake_keyring.get_password.assert_not_called()

    def test_windows_writes_native_credential_without_calling_keyring(self):
        fake_keyring = mock.Mock()
        with mock.patch.object(credential_store.os, "name", "nt"), mock.patch.object(
            credential_store, "keyring", fake_keyring
        ), mock.patch("windows_credentials.write_windows_credential") as write_native:
            credential_store.write_credential("WorkAutomation/Test", "andy", "safe")
        write_native.assert_called_once_with("WorkAutomation/Test", "andy", "safe")
        fake_keyring.set_password.assert_not_called()

    def test_invalid_json_secret_is_rejected(self):
        with mock.patch.object(
            credential_store,
            "read_credential",
            return_value=credential_store.StoredCredential("target", "user", "not json"),
        ):
            with self.assertRaises(credential_store.CredentialStoreError):
                credential_store.read_json_credential("target")

    def test_reads_complete_paycom_credential(self):
        secret = credential_store.build_paycom_secret("correct horse", "0123")
        with mock.patch.object(
            credential_store,
            "read_credential",
            return_value=credential_store.StoredCredential(
                credential_store.PAYCOM_CREDENTIAL_TARGET, "paycom-user", secret
            ),
        ):
            value = credential_store.read_paycom_credential()
        self.assertEqual(value.username, "paycom-user")
        self.assertEqual(value.password, "correct horse")
        self.assertEqual(value.pin, "0123")

    def test_rejects_legacy_pin_only_paycom_credential(self):
        with mock.patch.object(
            credential_store,
            "read_credential",
            return_value=credential_store.StoredCredential(
                credential_store.PAYCOM_CREDENTIAL_TARGET, "PIN", "0123"
            ),
        ):
            with self.assertRaises(credential_store.CredentialStoreError):
                credential_store.read_paycom_credential()

    def test_paycom_uses_complete_native_credential_when_primary_is_stale(self):
        stale = credential_store.StoredCredential(
            credential_store.PAYCOM_CREDENTIAL_TARGET, "paycom-user", "old-password"
        )
        native = credential_store.StoredCredential(
            credential_store.PAYCOM_CREDENTIAL_TARGET,
            "paycom-user",
            credential_store.build_paycom_secret("new-password", "0123"),
        )
        with mock.patch.object(credential_store, "read_credential", return_value=stale), mock.patch.object(
            credential_store, "_legacy_windows_read", return_value=native
        ):
            value = credential_store.read_paycom_credential()
        self.assertEqual(value.username, "paycom-user")
        self.assertEqual(value.password, "new-password")
        self.assertEqual(value.pin, "0123")

if __name__ == "__main__":
    unittest.main()
