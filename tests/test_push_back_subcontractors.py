"""Push Back deadline/save regression tests; no CRM or supplier requests."""

import sys
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workers"))
import crm_push_back as worker
from shipping_bypasser_subcontractors import subcontractor_order_type


class SubcontractorPushBackTests(unittest.TestCase):
    def order(self, **changes):
        return {
            "order_type": subcontractor_order_type("WSA PROMOTIONS LLC"),
            "subcontractor": "WSA PROMOTIONS LLC (IA)",
            "production_date": date(2026, 11, 30), "due_date": date(2026, 12, 7),
            "latest_ship_date": date(2026, 12, 3), "latest_ship_date_error": "", **changes,
        }

    def row(self):
        return {"orderId": "5000001", "colorLabel": "tan"}

    def mocks(self, stack, order):
        stack.enter_context(mock.patch.object(worker, "_publish_status"))
        stack.enter_context(mock.patch.object(worker, "_open_and_read_order", return_value=order))
        stack.enter_context(mock.patch.object(worker, "_page_indicates_push_back_stock_already_ordered", return_value=False))
        stack.enter_context(mock.patch.object(worker, "_wait_for_push_back_stock_confirmation", return_value=True))
        current = dict(order)
        def save(_driver, _order_id, target, **_kwargs):
            current["production_date"] = target
            return target
        change = stack.enter_context(mock.patch.object(worker, "_change_crm_production_date", side_effect=save))
        stack.enter_context(mock.patch.object(worker, "_extract_order_data", side_effect=lambda *_: dict(current)))
        stock = stack.enter_context(mock.patch.object(worker, "_run_order_goods_with_push_back_status", return_value=[{
            "success": True, "outcome": "auto_order_succeeded", "message": "Ordered",
        }]))
        return change, stock, current

    def test_free_all_urls_and_cli_do_not_fall_back_to_rush(self):
        for mode in ("free", "all"):
            with self.subTest(mode=mode), mock.patch.object(worker, f"CRM_PUSH_BACK_{mode.upper()}_URL", ""):
                self.assertEqual(worker._list_url_for_filter(mode), "")
                self.assertEqual(worker.parse_args(["--processing-filter", mode]).processing_filter, mode)
                with mock.patch.object(worker, "_build_crm_session_driver") as browser:
                    with self.assertRaisesRegex(RuntimeError, f"CRM_PUSH_BACK_{mode.upper()}_URL is empty"):
                        worker._run_batch_with_mode(True, processing_filter=mode)
                    browser.assert_not_called()

    def test_production_one_day_before_latest_ship_is_allowed_and_due_date_preserved(self):
        for mode in ("free", "all"):
            with self.subTest(mode=mode), ExitStack() as stack:
                order = self.order(latest_ship_date=date(2026, 12, 2))
                change, stock, _ = self.mocks(stack, order)
                result = worker._run_order_with_driver(mock.Mock(), self.row(), mode, "https://crm.example/report")
            self.assertTrue(result["success"], result)
            self.assertEqual(result["saved_production_date"], "2026-12-01")
            self.assertEqual(result["due_date"], "2026-12-07")
            self.assertEqual(change.call_args.kwargs, {"expected_due_date": date(2026, 12, 7)})
            stock.assert_called_once()

    def test_saved_latest_ship_overrides_row_customer_due_date(self):
        with ExitStack() as stack:
            change, _, _ = self.mocks(stack, self.order())
            row = {**self.row(), "rowText": "Production Date: 2026-11-30 Due Date: 2026-12-01"}
            result = worker._run_order_with_driver(mock.Mock(), row, "free", "https://crm.example/report")
        self.assertTrue(result["success"], result)
        change.assert_called_once()

    def test_same_or_later_deadline_and_missing_newest_deadline_stop_without_saving(self):
        for fields in (
            {"latest_ship_date": date(2026, 12, 1)},
            {"latest_ship_date": date(2026, 11, 30)},
            {"latest_ship_date": None, "latest_ship_date_error": "Newest LATEST SHIP DATE is invalid."},
            {"due_date": None},
        ):
            with self.subTest(fields=fields), ExitStack() as stack:
                change, stock, _ = self.mocks(stack, self.order(**fields))
                result = worker._run_order_with_driver(mock.Mock(), self.row(), "all", "https://crm.example/report")
                self.assertFalse(result["success"])
                self.assertTrue(result["manual_review_required"])
                change.assert_not_called()
                stock.assert_not_called()

    def test_no_purchase_plan_retries_only_before_latest_ship_date(self):
        with ExitStack() as stack:
            change, stock, _ = self.mocks(stack, self.order())
            stock.return_value = [{"success": False, "outcome": "auto_order_no_purchase_plan", "message": "No plan"}]
            result = worker._run_order_with_driver(mock.Mock(), self.row(), "free", "https://crm.example/report")
        self.assertFalse(result["success"])
        self.assertIn("LATEST SHIP DATE 2026-12-03", result["message"])
        self.assertEqual([call.args[2] for call in change.call_args_list], [date(2026, 12, 1), date(2026, 12, 2)])
        self.assertEqual(stock.call_count, 2)
        self.assertEqual(result["due_date"], "2026-12-07")

    def test_changed_saved_destination_due_date_deadline_or_production_stops_before_stock(self):
        for fields in (
            {"due_date": date(2026, 12, 8)},
            {"latest_ship_date": date(2026, 12, 4)},
            {"subcontractor": "Unknown Vendor"},
            {"production_date": date(2026, 11, 30)},
        ):
            with self.subTest(fields=fields), ExitStack() as stack:
                order = self.order()
                _, stock, _ = self.mocks(stack, order)
                stack.enter_context(mock.patch.object(worker, "_change_crm_production_date_with_retry", return_value=(date(2026, 12, 1), 0, None)))
                stack.enter_context(mock.patch.object(worker, "_extract_order_data", return_value=self.order(**{"production_date": date(2026, 12, 1), **fields})))
                with self.assertRaises(RuntimeError):
                    worker._run_order_with_driver(mock.Mock(), self.row(), "all", "https://crm.example/report")
                stock.assert_not_called()

    def test_save_retry_refuses_changed_due_date_or_newest_note(self):
        for fields in ({"due_date": date(2026, 12, 8)}, {"latest_ship_date": date(2026, 12, 4)}):
            with self.subTest(fields=fields), ExitStack() as stack:
                order = self.order()
                stack.enter_context(mock.patch.object(worker, "_extract_order_data", return_value=order))
                change = stack.enter_context(mock.patch.object(worker, "_change_crm_production_date", side_effect=RuntimeError("Save interrupted")))
                stack.enter_context(mock.patch.object(worker, "_refresh_and_read_order_for_save_retry", return_value=self.order(**fields)))
                with self.assertRaises(RuntimeError):
                    worker._change_crm_production_date_with_retry(mock.Mock(), "5000001", date(2026, 12, 1), "free", "https://crm.example/report", expected_order=order)
                change.assert_called_once()

    def test_dry_run_does_not_save_and_max_rush_remains_excluded(self):
        with ExitStack() as stack:
            change, stock, _ = self.mocks(stack, self.order())
            result = worker._run_order_with_driver(mock.Mock(), self.row(), "free", "https://crm.example/report", dry_run=True)
            self.assertTrue(result["success"])
            change.assert_not_called()
            self.assertTrue(stock.call_args.kwargs["dry_run"])
            stock.reset_mock()
            skipped = worker._run_order_with_driver(mock.Mock(), {**self.row(), "colorLabel": "lime_green"}, "all", "https://crm.example/report")
            self.assertTrue(skipped["success"])
            stock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
