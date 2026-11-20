"""Stock-tab and repeat-run regressions without live CRM or vendor actions."""

import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_order_goods as worker  # noqa: E402


class StockTabOrderingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.driver = mock.Mock()
        self.button = mock.Mock()
        self.button.is_enabled.return_value = True
        for name, value in {
            "_active_stock_tab_has_purchase_order": False,
            "_page_indicates_stock_already_ordered": True,
            "_page_indicates_stock_locked_for_auto_ordering": False,
            "_find_stock_tabs": [{"index": 0}, {"index": 1}],
            "_find_sanmar_order_goods_button": self.button,
            "_recover_missing_order_goods_button_once": None,
            "_wait_for_order_goods_page_ready": True,
        }.items():
            setattr(self, name, self.stack.enter_context(mock.patch.object(worker, name, return_value=value)))
        self.click = self.stack.enter_context(mock.patch.object(worker, "_click_with_fallback"))
        self.unlock = self.stack.enter_context(mock.patch.object(worker, "_unlock_current_order_for_auto_ordering"))
        self.stack.enter_context(mock.patch.object(worker.time, "sleep"))

    def test_order_header_does_not_skip_unordered_design(self):
        result = worker._order_goods_for_open_order(self.driver, "5000001", stock_tab_index=1)
        self.assertEqual(result["outcome"], "order_goods_clicked")
        self.click.assert_called_once_with(self.driver, self.button)

    def test_yellow_po_with_button_orders_new_sizes_on_single_or_multiple_designs(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        for tabs in ([{"index": 0}], [{"index": 0}, {"index": 1}]):
            with self.subTest(tabs=tabs):
                self._find_stock_tabs.return_value = tabs
                self.click.reset_mock()
                result = worker._order_goods_for_open_order(self.driver, "5000001")
                self.assertEqual(result["outcome"], "order_goods_clicked")
                self.click.assert_called_once_with(self.driver, self.button)

    def test_single_ordered_status_with_button_still_orders(self):
        self._find_stock_tabs.return_value = [{"index": 0}]
        result = worker._order_goods_for_open_order(self.driver, "5000001")
        self.assertEqual(result["outcome"], "order_goods_clicked")

    def test_po_without_button_skips_only_that_design_even_for_push_back(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        self._find_sanmar_order_goods_button.return_value = None
        for ignore_status in (False, True):
            with self.subTest(ignore_status=ignore_status):
                result = worker._order_goods_for_open_order(
                    self.driver, "5000001", ignore_already_ordered=ignore_status,
                )
                self.assertEqual(result["outcome"], "already_stock_ordered")
        self.click.assert_not_called()
        self.unlock.assert_not_called()
        self._recover_missing_order_goods_button_once.assert_not_called()

    def test_missing_button_without_po_needs_review_despite_order_header(self):
        self._find_sanmar_order_goods_button.return_value = None
        with self.assertRaises(worker.TimeoutException):
            worker._order_goods_for_open_order(self.driver, "5000001", stock_tab_index=1)
        self.click.assert_not_called()

    def test_dry_run_with_po_and_button_does_not_submit(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        result = worker._order_goods_for_open_order(self.driver, "5000001", dry_run=True)
        self.assertEqual(result["outcome"], "order_goods_ready")
        self.click.assert_not_called()

    def test_unlock_wait_prioritizes_button_over_existing_po(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        state = worker._wait_after_stock_unlock(self.driver, "5000001", timeout=1)
        self.assertEqual(state, "orderable")

    def test_unlock_wait_accepts_po_without_button(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        self._find_sanmar_order_goods_button.return_value = None
        state = worker._wait_after_stock_unlock(self.driver, "5000001", timeout=1)
        self.assertEqual(state, "ordered")

    def test_unlock_wait_requires_unlocked_status_even_with_po_and_button(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        self._page_indicates_stock_locked_for_auto_ordering.return_value = True
        with mock.patch.object(worker.time, "time", side_effect=[0, 0, 2]):
            state = worker._wait_after_stock_unlock(self.driver, "5000001", timeout=1)
        self.assertEqual(state, "locked")

    def test_missing_tab_after_unlock_cannot_use_first_design_stock(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        with mock.patch.object(worker, "_activate_stock_tab", return_value=None):
            state = worker._wait_after_stock_unlock(self.driver, "5000001", stock_tab_index=1, timeout=1)
        self.assertEqual(state, "timeout")
        self._active_stock_tab_has_purchase_order.assert_not_called()
        self._find_sanmar_order_goods_button.assert_not_called()

    def test_old_po_and_remaining_button_do_not_confirm_new_purchase_after_timeout(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        with mock.patch.object(worker, "_read_visible_auto_order_feedback", return_value=None), \
             mock.patch.object(worker.time, "time", side_effect=[0, 0, 2, 2, 2, 4]):
            feedback = worker._wait_for_auto_order_feedback(self.driver, timeout=1, refresh_recheck_seconds=1)
        self.assertEqual(feedback["kind"], "timeout")
        self.driver.refresh.assert_called_once()

    def test_feedback_refresh_restores_submitted_design_before_reading_po(self):
        self._active_stock_tab_has_purchase_order.return_value = True
        self._find_sanmar_order_goods_button.return_value = None
        events = []
        self._active_stock_tab_has_purchase_order.side_effect = lambda *_a: events.append("read_po") or True
        with mock.patch.object(worker, "_activate_stock_tab", side_effect=lambda *_a: events.append("activate") or {"index": 1}) as activate, \
             mock.patch.object(worker, "_read_visible_auto_order_feedback", return_value=None), \
             mock.patch.object(worker.time, "time", side_effect=[0, 0, 2]):
            feedback = worker._wait_for_auto_order_feedback(self.driver, timeout=1, stock_tab_index=1)
        self.assertEqual(feedback["kind"], "ordered")
        self.assertEqual(events, ["activate", "read_po"])
        activate.assert_called_once_with(self.driver, 1)


class StockTabRecoveryTests(unittest.TestCase):
    def test_missing_tab_after_button_recovery_stops_before_reading_first_design(self):
        with mock.patch.object(worker, "_page_indicates_non_vendor_stock_tab", return_value=False), \
             mock.patch.object(worker, "_find_sanmar_order_goods_button", return_value=None) as find_button, \
             mock.patch.object(worker, "_wait_for_order_goods_page_ready", return_value=True), \
             mock.patch.object(worker, "_activate_stock_tab", return_value=None), \
             mock.patch.object(worker, "_publish_status"), \
             mock.patch.object(worker.time, "sleep"):
            with self.assertRaisesRegex(worker.TimeoutException, "requested stock tab"):
                worker._recover_missing_order_goods_button_once(mock.Mock(), "5000001", stock_tab_index=1)
        self.assertEqual(find_button.call_count, 1)


class RepeatStockBatchTests(unittest.TestCase):
    def test_parallel_batches_revisit_history_but_deduplicate_current_run(self):
        exclusions = []

        def collect(*_args, exclude_order_ids, **_kwargs):
            exclusions.append(set(exclude_order_ids))
            return [] if "5000001" in exclude_order_ids else ["5000001"]

        history = {"run_history": [{"automation_key": "order_goods", "order_ids": ["5000001"]}]}
        with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(history))), \
             mock.patch.object(worker, "_publish_status"), \
             mock.patch.object(worker, "_collect_batch_order_ids", side_effect=collect), \
             mock.patch.object(worker, "_clone_profile_for_worker", return_value=(None, str(ROOT))), \
             mock.patch.object(worker, "_order_goods_worker_payload", side_effect=lambda *_a, **_k: {
                 "success": True, "order_ids": ["5000001"],
                 "report": [{"order_id": "5000001", "success": True, "outcome": "order_goods_clicked"}],
             }) as run_order:
            for _ in range(2):
                result = worker._run_parallel_batch_with_mode(
                    True, profile_path=str(ROOT), list_url="https://crm.example/report", parallel_workers=2,
                )
                self.assertTrue(result["success"])
                self.assertEqual(result["order_ids"], ["5000001"])
                self.assertEqual(result["skipped_historical_order_count"], 0)
        self.assertEqual(run_order.call_count, 2)
        self.assertEqual(exclusions, [set(), {"5000001"}, set(), {"5000001"}])


if __name__ == "__main__":
    unittest.main()
