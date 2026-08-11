import tempfile
import unittest
from unittest import mock

from workers import salesforce_verification


class SalesforceVerificationHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.request_dir_patch = mock.patch.object(
            salesforce_verification,
            "REQUEST_DIR",
            self.temp_dir.name,
        )
        self.request_dir_patch.start()

    def tearDown(self):
        self.request_dir_patch.stop()
        self.temp_dir.cleanup()

    def test_code_round_trip_removes_code_from_public_payload_and_disk_after_consume(self):
        request = salesforce_verification.create_request(worker_slot=2, order_id="4600001")

        submitted = salesforce_verification.submit_code(request["request_id"], "123-456")
        self.assertNotIn("verification_code", submitted)

        code, status = salesforce_verification.consume_submitted_code(request["request_id"])
        self.assertEqual(code, "123456")
        self.assertEqual(status, "processing")
        stored = salesforce_verification.get_request(request["request_id"])
        self.assertEqual(stored["status"], "processing")
        self.assertNotIn("verification_code", stored)

    def test_pending_list_exposes_worker_and_order_without_code(self):
        request = salesforce_verification.create_request(worker_slot=7, order_id="4700001")

        pending = salesforce_verification.list_pending_requests()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["request_id"], request["request_id"])
        self.assertEqual(pending[0]["worker_slot"], 7)
        self.assertEqual(pending[0]["order_id"], "4700001")
        self.assertNotIn("verification_code", pending[0])

    def test_rejects_non_six_digit_code(self):
        request = salesforce_verification.create_request(worker_slot=1)

        with self.assertRaisesRegex(ValueError, "6-digit"):
            salesforce_verification.submit_code(request["request_id"], "12345")

    def test_cancel_releases_waiting_worker(self):
        request = salesforce_verification.create_request(worker_slot=1)

        salesforce_verification.cancel_request(request["request_id"])
        code, status = salesforce_verification.consume_submitted_code(request["request_id"])

        self.assertIsNone(code)
        self.assertEqual(status, "canceled")


if __name__ == "__main__":
    unittest.main()
