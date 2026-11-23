"""Ordering-only dispatch and isolation, without contacting CRM."""

import copy
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_order_goods as core
import crm_order_goods_native as native
import crm_order_goods_only as only


class OrderingOnlyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = ROOT / "runtime/debug/order_goods_tests"
        folder.mkdir(parents=True, exist_ok=True)
        temp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="only-", dir=folder))
        self.stack.enter_context(mock.patch.object(native, "RECEIPT_DIR", Path(temp)))
        self.stack.enter_context(mock.patch.object(only.time, "sleep"))
        self.events = []
        self.records = {}
        self.driver = mock.Mock()
        self.driver.current_window_handle = "source"
        self.driver.switch_to.window.side_effect = lambda handle: setattr(self.driver, "current_window_handle", handle)
        self.driver.close.side_effect = lambda: self.events.append(("close", self.driver.current_window_handle))
        self.stack.enter_context(mock.patch.object(only, "_open_tab", side_effect=self.open_tab))
        self.transport = only._read
        self.read = self.stack.enter_context(mock.patch.object(only, "_read", side_effect=self.response))
        self.stack.enter_context(mock.patch.object(core, "_publish_status"))
        self.stack.enter_context(mock.patch.object(core, "_unlock_current_order_for_auto_ordering", side_effect=AssertionError("must not unlock")))
        self.stack.enter_context(mock.patch.object(core, "_wait_for_auto_order_feedback", side_effect=AssertionError("must not inspect errors")))

    def open_tab(self, driver, url):
        order_id = url.rsplit("/", 1)[-1]
        self.events.append(("open", order_id))
        return order_id

    def response(self, driver, order_id, action="inspect", **options):
        self.events.append((action, order_id))
        design = {"id": "101", "tab": 1, "fingerprint": "sizes", "eligible": True}
        if action == "inspect":
            return {"ready": True, "designs": [design], "history": []}
        if action == "start":
            saved = native._load(order_id)
            self.assertEqual(saved["entries"][0]["state"], "submitted")
            self.records[order_id] = copy.deepcopy(options["previous"])
        else:
            record = self.records[order_id]
            record["phase"] = "confirmed"
            record["entries"][0]["state"] = "confirmed"
        return {"ready": True, "record": copy.deepcopy(self.records[order_id])}

    def test_open_all_then_submit_all_before_reading_any_response_and_do_not_refresh(self):
        results = only.order_window(core, self.driver, ["5000001", "5000002", "5000001"])
        self.assertEqual(self.events[:2], [("open", "5000001"), ("open", "5000002")])
        starts = [i for i, event in enumerate(self.events) if event[0] == "start"]
        statuses = [i for i, event in enumerate(self.events) if event[0] == "status"]
        self.assertLess(max(starts), min(statuses))
        self.assertTrue(all(row["success"] and row["outcome"] == "order_goods_submitted" for row in results))
        self.driver.refresh.assert_not_called()
        self.assertEqual(self.driver.current_window_handle, "source")
        self.assertEqual(len([event for event in self.events if event[0] == "close"]), 2)
        self.assertIsNone(native.recovery_block("5000001", submission_only=True))
        self.assertIsNotNone(native.recovery_block("5000001"), "other workflows retain their saved-stock verification guard")
        self.assertEqual(list(native.RECEIPT_DIR.glob("*.lock")), [])

    def test_confirmed_receipt_skips_same_stock_and_new_quantities_are_eligible(self):
        only.order_window(core, self.driver, ["5000001"])
        self.events.clear()
        skipped = only.order_window(core, self.driver, ["5000001"])
        self.assertEqual(skipped[0]["outcome"], "order_goods_skipped")
        self.assertNotIn(("start", "5000001"), self.events)
        original = self.response
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if len(args) == 2 or args[2] == "inspect":
                result["designs"][0]["fingerprint"] = "new quantities"
            return result
        self.read.side_effect = changed
        self.assertEqual(only.order_window(core, self.driver, ["5000001"])[0]["outcome"], "order_goods_submitted")

    def test_explicit_native_failure_is_only_a_submission_receipt_without_error_investigation(self):
        original = self.response
        def failure(driver, order_id, action="inspect", **kwargs):
            response = original(driver, order_id, action, **kwargs)
            if action == "status":
                response["record"]["phase"] = "review"
                response["record"]["entries"][0].update(state="review", failureConfirmed=True)
            return response
        self.read.side_effect = failure
        results = only.order_window(core, self.driver, ["5000001"])
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["outcome"], "order_goods_submitted")
        self.assertIn("not checked", results[0]["message"])

    def test_unknown_start_result_blocks_a_later_run_without_replaying_the_request(self):
        original = self.response
        def lost(driver, order_id, action="inspect", **kwargs):
            result = original(driver, order_id, action, **kwargs)
            if action == "start":
                raise ConnectionError("Lost browser connection")
            return result
        self.read.side_effect = lost
        results = only.order_window(core, self.driver, ["5000001"])
        self.assertTrue(any(not row["success"] for row in results))
        self.assertTrue(all(not row.get("request_submitted") for row in results))
        self.assertIsNotNone(native.recovery_block("5000001"))
        self.events.clear()
        only.order_window(core, self.driver, ["5000001"])
        self.assertEqual(self.events, [])

    def test_dry_run_never_writes_receipts_or_submits(self):
        results = only.order_window(core, self.driver, ["5000001"], dry_run=True)
        self.assertEqual(results[0]["outcome"], "order_goods_ready")
        self.assertEqual(list(native.RECEIPT_DIR.iterdir()), [])
        self.assertFalse(any(action == "start" for action, _ in self.events))

    def test_receipt_storage_failure_prevents_all_native_requests(self):
        with mock.patch.object(native, "_save", side_effect=OSError("Storage failed")):
            results = only.order_window(core, self.driver, ["5000001"])
        self.assertFalse(results[0]["success"])
        self.assertNotIn(("start", "5000001"), self.events)

    def test_native_write_is_never_repeated_to_discover_an_iframe(self):
        self.driver.execute_script.return_value = {"ready": False, "notSubmitted": True}
        result = self.transport(self.driver, "5000001", "start", entries=[])
        self.assertTrue(result["notSubmitted"])
        self.driver.execute_script.assert_called_once()
        self.driver.find_elements.assert_not_called()

    def test_batch_reuses_one_browser_and_deduplicates_each_list_pass(self):
        excluded = []
        def collect(*args, **kwargs):
            excluded.append(set(kwargs["exclude_order_ids"]))
            return [] if excluded[-1] else ["5000001", "5000001", "5000002"]
        with mock.patch.object(core, "_build_crm_session_driver", return_value=self.driver) as build, \
             mock.patch.object(core, "_collect_batch_order_ids_with_driver", side_effect=collect), \
             mock.patch.object(core, "safe_driver_quit") as quit_driver:
            result = only.run_with_mode(core, True, parallel_workers=8, list_url="https://crm.example/report")
        self.assertTrue(result["success"])
        self.assertEqual(result["order_ids"], ["5000001", "5000002"])
        self.assertEqual(result["submitted_request_count"], 2)
        self.assertEqual(excluded, [set(), {"5000001", "5000002"}])
        build.assert_called_once()
        quit_driver.assert_called_once()
        self.assertEqual(result["browser_count"], 1)


if __name__ == "__main__":
    unittest.main()
