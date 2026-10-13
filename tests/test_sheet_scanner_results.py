import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_copyright_cancel as scanner  # noqa: E402


class SheetScannerEmailResultTests(unittest.TestCase):
    def setUp(self):
        audit = mock.patch("automation_runtime.log_automation_result")
        audit.start()
        self.addCleanup(audit.stop)
        artifacts = ROOT / "runtime" / "debug"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(self.temp_dir.cleanup)
        self.args = SimpleNamespace(
            order_id="5408963",
            order_url="",
            process="oversize_emb_to_hdd",
            reason="",
            emb_designs_json="",
            dry_run=False,
            visible=False,
            attach_browser=False,
            debugger_address="127.0.0.1:9222",
            login_wait_seconds=0,
            skip_refund_click=False,
            skip_from_selection=False,
            skip_ready_verify=False,
            keep_browser_open=False,
            keep_browser_open_on_error=False,
            delete_sheet_row=True,
            sheet_row_number=2,
            retry_errors=False,
            limit=0,
            parallel_workers=2,
            result_file=str(Path(self.temp_dir.name) / "result.json"),
        )
        self.details = {
            "order_id": self.args.order_id,
            "process": self.args.process,
            "dry_run": False,
            "crm_action": {"sales_note": {"updated": True}},
            "salesforce": scanner._prepare_and_maybe_send_salesforce_email(
                mock.Mock(),
                "crm-tab",
                self.args.order_id,
                "buyer@example.test",
                dry_run=False,
                process=scanner.OVERSIZE_EMB_TO_HDD_PROCESS,
                contact={"salesforce_label": " Salesforce   Contact "},
            ),
        }

    def read_result(self):
        return json.loads(Path(self.args.result_file).read_text(encoding="utf-8"))

    def test_contact_skip_fails_order_and_preserves_retry_row_and_crm_details(self):
        with mock.patch.object(scanner, "process_single_order", return_value=self.details), \
             mock.patch.object(scanner, "_find_retried_sheet_queue_row") as find_row, \
             mock.patch.object(scanner, "_clear_retried_sheet_queue_row") as clear_row:
            exit_code = scanner.run_process_order(self.args)

        payload = self.read_result()
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["message"], "Sheet scanner order failed: " + self.details["salesforce"]["message"])
        self.assertEqual(payload["order_id"], "5408963")
        self.assertEqual(payload["crm_action"], self.details["crm_action"])
        self.assertEqual(payload["salesforce"], self.details["salesforce"])
        self.assertIsNone(payload.get("cleared_sheet_row"))
        find_row.assert_called_once()
        clear_row.assert_not_called()

    def test_contact_skip_fails_email_recovery(self):
        with mock.patch.object(scanner, "send_salesforce_email_single_order", return_value=self.details):
            exit_code = scanner.run_send_email_order(self.args)

        payload = self.read_result()
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["success"])
        self.assertIn(self.details["salesforce"]["message"], payload["message"])
        self.assertFalse(payload["salesforce"]["sent"])
        self.assertEqual(payload["order_id"], "5408963")

    def test_contact_skip_fails_dry_run_without_sheet_changes(self):
        self.args.dry_run = True
        self.details["dry_run"] = True
        self.details["salesforce"]["dry_run"] = True
        with mock.patch.object(scanner, "process_single_order", return_value=self.details), \
             mock.patch.object(scanner, "_find_retried_sheet_queue_row") as find_row, \
             mock.patch.object(scanner, "_clear_retried_sheet_queue_row") as clear_row:
            exit_code = scanner.run_process_order(self.args)

        self.assertEqual(exit_code, 1)
        self.assertFalse(self.read_result()["success"])
        find_row.assert_not_called()
        clear_row.assert_not_called()

    def test_supported_outcomes_still_complete_successfully(self):
        cases = [
            (False, {"sent": True, "activity_verified": True}),
            (False, {"sent": False, "skipped": True, "skip_reason": "matching_email_already_sent"}),
            (True, {"sent": False, "dry_run": True}),
            (False, None),  # Non-email workflows retain their existing success.
        ]
        for dry_run, salesforce in cases:
            with self.subTest(dry_run=dry_run, salesforce=salesforce):
                self.args.dry_run = dry_run
                details = {"order_id": "5408963", "dry_run": dry_run, "salesforce": salesforce}
                with mock.patch.object(scanner, "process_single_order", return_value=details), \
                     mock.patch.object(scanner, "_find_retried_sheet_queue_row"), \
                     mock.patch.object(scanner, "_clear_retried_sheet_queue_row", return_value=2) as clear_row:
                    exit_code = scanner.run_process_order(self.args)

                self.assertEqual(exit_code, 0)
                self.assertTrue(self.read_result()["success"])
                if dry_run:
                    clear_row.assert_not_called()
                else:
                    clear_row.assert_called_once()

    def test_batch_contact_skip_is_an_error_and_other_orders_still_complete(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                self.args.dry_run = dry_run
                worksheet = mock.Mock(title="Sheet1")
                rows = [
                    scanner.QueueRow(2, "5408963", "5408963", scanner.OVERSIZE_EMB_TO_HDD_ISSUE_TYPE, "", "oversize_emb_to_hdd", ""),
                    scanner.QueueRow(3, "5408964", "5408964", scanner.OVERSIZE_EMB_TO_HDD_ISSUE_TYPE, "", "oversize_emb_to_hdd", ""),
                ]
                results = [
                    (rows[0], {**self.details, "success": True, "message": "Sheet scanner automation complete."}, None),
                    (rows[1], {"order_id": "5408964", "salesforce": {"sent": True}}, None),
                ]
                with mock.patch.object(scanner, "_scan_queue_rows", return_value=(mock.Mock(title="Queue"), worksheet, [], rows, [])), \
                     mock.patch.object(scanner, "_run_sheet_scanner_parallel_rows", return_value=(results, 2)), \
                     mock.patch.object(scanner, "_write_missing_reason_errors", return_value=0), \
                     mock.patch.object(scanner, "_write_sheet_error") as write_error, \
                     mock.patch.object(scanner, "_clear_sheet_queue_row") as clear_row, \
                     mock.patch.object(scanner, "_publish_status"):
                    exit_code = scanner.run_process_queue(self.args)

                payload = self.read_result()
                self.assertEqual(exit_code, 1)
                self.assertFalse(payload["success"])
                self.assertEqual(payload["message"], "Processed 1 sheet scanner row(s); 1 failed.")
                self.assertEqual([row["order_id"] for row in payload["processed"]], ["5408964"])
                self.assertEqual(len(payload["failures"]), 1)
                failure = payload["failures"][0]
                self.assertEqual(failure["order_id"], "5408963")
                self.assertFalse(failure["success"])
                self.assertEqual(failure["error"], self.details["salesforce"]["message"])
                self.assertEqual(failure["message"], self.details["salesforce"]["message"])
                self.assertEqual(failure["crm_action"], self.details["crm_action"])
                if dry_run:
                    write_error.assert_not_called()
                    clear_row.assert_not_called()
                else:
                    write_error.assert_called_once_with(worksheet, [], 2, self.details["salesforce"]["message"])
                    clear_row.assert_called_once_with(worksheet, 3)


if __name__ == "__main__":
    unittest.main()
