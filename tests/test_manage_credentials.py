import argparse
import unittest
from unittest import mock

import manage_credentials
from credential_store import PaycomCredential


class ManageCredentialsTests(unittest.TestCase):
    def test_paycom_set_verifies_complete_saved_credential(self):
        options = argparse.Namespace(service="paycom", username="paycom-user", json_file=None)
        with mock.patch.object(
            manage_credentials.getpass,
            "getpass",
            side_effect=["correct-password", "0123"],
        ), mock.patch.object(manage_credentials, "write_credential") as write, mock.patch.object(
            manage_credentials,
            "read_paycom_credential",
            return_value=PaycomCredential("paycom-user", "correct-password", "0123"),
        ):
            result = manage_credentials.command_set(options)

        self.assertEqual(result, 0)
        write.assert_called_once()

    def test_paycom_set_rejects_a_mismatched_readback(self):
        options = argparse.Namespace(service="paycom", username="paycom-user", json_file=None)
        with mock.patch.object(
            manage_credentials.getpass,
            "getpass",
            side_effect=["correct-password", "0123"],
        ), mock.patch.object(manage_credentials, "write_credential"), mock.patch.object(
            manage_credentials,
            "read_paycom_credential",
            return_value=PaycomCredential("paycom-user", "old-password", "0123"),
        ):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                manage_credentials.command_set(options)


if __name__ == "__main__":
    unittest.main()
