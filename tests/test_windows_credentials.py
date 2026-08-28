import json
import os
import sys
import unittest
import uuid
from unittest import mock

import windows_credentials


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from windows_credentials import (
    WindowsCredentialError,
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
        try:
            write_windows_credential(self.target, "availability-probe", "safe")
        except WindowsCredentialError as exc:
            self.skipTest(f"Windows Credential Manager is unavailable in this logon session: {exc}")
        delete_windows_credential(self.target, missing_ok=True)

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


class WindowsCredentialWriteTests(unittest.TestCase):
    def test_write_clears_duplicate_versions_and_uses_enterprise_persistence(self):
        api = mock.Mock()
        api.CredDeleteW.side_effect = [True, True, False]
        api.CredWriteW.return_value = True
        with mock.patch.object(windows_credentials, "_advapi32", return_value=api), mock.patch.object(
            windows_credentials.ctypes, "get_last_error", return_value=windows_credentials._ERROR_NOT_FOUND
        ):
            write_windows_credential("WorkAutomation/Test", "test-user", "safe")

        self.assertEqual(api.CredDeleteW.call_count, 3)
        written = api.CredWriteW.call_args.args[0]._obj
        self.assertEqual(written.Persist, windows_credentials._CRED_PERSIST_ENTERPRISE)

    def test_delete_reports_missing_when_no_version_exists(self):
        api = mock.Mock()
        api.CredDeleteW.return_value = False
        with mock.patch.object(windows_credentials, "_advapi32", return_value=api), mock.patch.object(
            windows_credentials.ctypes, "get_last_error", return_value=windows_credentials._ERROR_NOT_FOUND
        ):
            self.assertFalse(delete_windows_credential("WorkAutomation/Missing", missing_ok=True))

    def test_write_falls_back_for_a_local_only_windows_account(self):
        api = mock.Mock()
        api.CredDeleteW.return_value = False
        api.CredWriteW.side_effect = [False, True]
        with mock.patch.object(windows_credentials, "_advapi32", return_value=api), mock.patch.object(
            windows_credentials.ctypes,
            "get_last_error",
            side_effect=[
                windows_credentials._ERROR_NOT_FOUND,
                windows_credentials._ERROR_NO_SUCH_LOGON_SESSION,
            ],
        ):
            write_windows_credential("WorkAutomation/Test", "test-user", "safe")

        self.assertEqual(api.CredWriteW.call_count, 2)
        written = api.CredWriteW.call_args.args[0]._obj
        self.assertEqual(written.Persist, windows_credentials._CRED_PERSIST_LOCAL_MACHINE)


if __name__ == "__main__":
    unittest.main()
