"""Native stock receipts and worker routing without live CRM actions."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_order_goods as worker
import crm_order_goods_native as native


class NativeStockReceiptTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime/debug/order_goods_tests"
        folder.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(prefix="receipts-", dir=folder)
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(native, "RECEIPT_DIR", Path(directory.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.record = {
            "version": 1, "orderId": "5000001", "phase": "submitting", "history": [],
            "entries": [{"id": "101", "tab": 1, "fingerprint": "sizes", "state": "submitted"}],
        }

    def test_atomic_receipt_survives_new_worker_and_blocks_before_unlocking(self):
        native._save(self.record)
        self.assertEqual(native._load("5000001"), self.record)
        with mock.patch.object(worker, "_open_target_order_with_refresh") as open_order, \
             mock.patch.object(worker, "_unlock_current_order_for_auto_ordering") as unlock:
            results = worker._run_order_with_driver(mock.Mock(), "5000001")
        self.assertFalse(results[0]["success"])
        open_order.assert_not_called()
        unlock.assert_not_called()
        self.assertEqual(list(native.RECEIPT_DIR.glob("*.tmp")), [])

    def test_known_failure_allows_later_run_while_uncertain_failure_blocks(self):
        entry = self.record["entries"][0]
        entry.update(state="review", failureConfirmed=True)
        native._save(self.record)
        self.assertIsNone(native.recovery_block("5000001"))
        entry["failureConfirmed"] = False
        native._save(self.record)
        self.assertIsNotNone(native.recovery_block("5000001"))

    def test_corrupt_and_wrong_order_receipts_stop_for_review(self):
        for record in ("broken JSON", json.dumps({**self.record, "orderId": "5000002"})):
            with self.subTest(record=record):
                native._path("5000001").write_text(record, encoding="utf-8")
                self.assertIsNotNone(native.recovery_block("5000001"))

    def test_overlapping_worker_lock_prevents_another_run(self):
        with native._lock("5000001"):
            self.assertIsNotNone(native.recovery_block("5000001"))
            driver = mock.Mock()
            results = native.order_stock_tabs(driver, "5000001", [{"label": "tab 1"}])
            self.assertFalse(results[0]["success"])
            driver.execute_script.assert_not_called()
        self.assertIsNone(native.recovery_block("5000001"))

    def test_missing_stock_tabs_cannot_turn_a_native_failure_into_empty_success(self):
        results = native.order_stock_tabs(mock.Mock(), "5000001", [])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])

    def test_new_worker_retains_confirmed_fingerprints(self):
        self.record["entries"][0]["state"] = "confirmed"
        self.record["entries"][0]["verified"] = True
        native._save(self.record)
        loaded = native._load("5000001")
        self.assertEqual(native._history(loaded), [{"id": "101", "fingerprint": "sizes"}])
        self.assertIsNone(native.recovery_block("5000001"))

    def test_confirmed_response_without_saved_stock_verification_blocks_reordering(self):
        self.record["entries"][0]["state"] = "confirmed"
        native._save(self.record)
        self.assertIsNotNone(native.recovery_block("5000001"))

    def test_multi_tab_worker_uses_native_batch_without_activating_tabs(self):
        driver = mock.Mock()
        tabs = [{"index": 0, "label": "tab 1"}, {"index": 1, "label": "tab 2"}]
        with mock.patch.object(worker, "_find_stock_tabs", return_value=tabs), \
             mock.patch.object(worker, "_publish_status"), \
             mock.patch.object(native, "order_stock_tabs", return_value=[{"success": True}]) as order, \
             mock.patch.object(worker, "_activate_stock_tab") as activate, \
             mock.patch.object(worker, "_order_goods_for_open_order") as serial:
            results = worker._order_goods_for_all_stock_tabs(driver, "5000001")
        self.assertTrue(results[0]["success"])
        order.assert_called_once()
        self.assertEqual(order.call_args.args, (driver, "5000001", tabs))
        activate.assert_not_called()
        serial.assert_not_called()

    def test_existing_receipt_stays_in_use_after_order_is_reduced_to_one_tab(self):
        self.record["entries"][0]["state"] = "confirmed"
        native._save(self.record)
        tabs = [{"index": 0, "label": "tab 1"}]
        with mock.patch.object(worker, "_find_stock_tabs", return_value=tabs), \
             mock.patch.object(worker, "_publish_status"), \
             mock.patch.object(native, "order_stock_tabs", return_value=[{"success": True}]) as order:
            worker._order_goods_for_all_stock_tabs(mock.Mock(), "5000001")
        order.assert_called_once()


if __name__ == "__main__":
    unittest.main()
