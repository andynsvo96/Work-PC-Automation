import copy
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))

import crm_reorder as worker
from flask import Flask
from routes.reorder_routes import register_reorder_routes


def product(**changes):
    return {"index": 1, "product_id": "G500", "product_name": "Gildan Cotton T-Shirt", "color": "Black", "quantities": {"L": 1, "XL": 1, "2XL": 1}, **changes}


def tab(**changes):
    return {"tab_index": 1, "design_tab_id": "design-fixture", "tab_label": "H-Fixture 1 - QTY: 3", "vendor": "sanmar", "red_records": [{"vendor": "Sanmar", "po": "H-Fixture-OLD", "vendor_order_number": "1234", "assigned": False}], "products": [product()], "valid_records": [], **changes}


class ReorderPlanningTests(unittest.TestCase):
    def test_sanmar_cart_status_uses_the_has_items_flag(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"hasItems": False, "text": "Your shopping cart is empty"}
        with mock.patch.object(worker.bypass, "safe_get_with_partial_load"):
            worker._empty(driver, "sanmar")
            driver.execute_script.return_value = {"hasItems": True, "text": "Checkout"}
            with self.assertRaisesRegex(RuntimeError, "cart already contains stock"):
                worker._empty(driver, "sanmar")

    def test_stock_is_aggregated_once_across_designs_with_same_numeric_po(self):
        products = worker.aggregate_products([tab(), tab(tab_index=2)], "sanmar")
        self.assertEqual(products[0]["quantities"], {"L": 2, "XL": 2, "2XL": 2})
        self.assertEqual(worker.aggregate_products([], "sanmar"), [])

    def test_all_shipping_classes_extend_dates_and_preserve_existing_later_dates(self):
        eta = date(2026, 11, 5)
        for shipping_class in ("free", "rush", "", None):
            with self.subTest(shipping_class=shipping_class):
                production, due = worker.date_changes({"shipping_class": shipping_class, "production_date": date(2026, 11, 2), "due_date": date(2026, 11, 4)}, eta)
                self.assertEqual((production, due), (eta, date(2026, 11, 6)))
                self.assertEqual(worker.date_changes({"shipping_class": shipping_class, "production_date": date(2026, 11, 9), "due_date": date(2026, 11, 12)}, eta), (date(2026, 11, 9), date(2026, 11, 12)))

    def test_arrival_on_due_date_moves_both_dates_and_weekends_roll_forward(self):
        order = {"shipping_class": "rush", "production_date": date(2026, 11, 3), "due_date": date(2026, 11, 4)}
        self.assertEqual(worker.date_changes(order, date(2026, 11, 4)), (date(2026, 11, 4), date(2026, 11, 5)))
        self.assertEqual(worker.date_changes(order, date(2026, 11, 6)), (date(2026, 11, 6), date(2026, 11, 9)))
        self.assertEqual(worker.date_changes(order, date(2026, 11, 7)), (date(2026, 11, 9), date(2026, 11, 10)))

    def test_sanmar_reorder_accepts_exact_low_stock_and_still_rejects_shortages(self):
        p = product(quantities={"L": 1})
        warehouse = worker.bypass.NJ_WAREHOUSE
        inventory = [{"warehouse": warehouse, "stock": {"L": 1}}]
        with ExitStack() as stack:
            for name in ("_search_sanmar_product", "_select_sanmar_color"):
                stack.enter_context(mock.patch.object(worker.bypass, name))
            available = stack.enter_context(mock.patch.object(worker.bypass, "_wait_for_sanmar_inventory", return_value=inventory))
            stack.enter_context(mock.patch.object(worker.bypass, "_sanmar_closed_warehouses", return_value=set()))
            groups = worker.sanmar_groups(mock.Mock(), [p])
            self.assertEqual(groups[0]["lines"][0]["quantities"], {"L": 1})
            self.assertEqual(groups[0]["lines"][0]["warehouse"], warehouse)
            # The shared default used by ordinary Shipping Bypasser is unchanged.
            self.assertIsNone(worker.bypass._choose_warehouse_plan([{"quantities": {"L": 1}, "inventory": inventory}], "inhouse")[1])
            available.return_value = [{"warehouse": warehouse, "stock": {"L": 0}}]
            with self.assertRaisesRegex(RuntimeError, "all required stock quantities"):
                worker.sanmar_groups(mock.Mock(), [p])

    def test_sanmar_reorder_can_split_exact_low_stock_across_warehouses(self):
        p = product(quantities={"L": 3})
        with ExitStack() as stack:
            for name in ("_search_sanmar_product", "_select_sanmar_color"):
                stack.enter_context(mock.patch.object(worker.bypass, name))
            stack.enter_context(mock.patch.object(worker.bypass, "_wait_for_sanmar_inventory", return_value=[{"warehouse": worker.bypass.NJ_WAREHOUSE, "stock": {"L": 2}}, {"warehouse": "Richmond, VA", "stock": {"L": 1}}]))
            stack.enter_context(mock.patch.object(worker.bypass, "_sanmar_closed_warehouses", return_value=set()))
            groups = worker.sanmar_groups(mock.Mock(), [p])
        self.assertEqual(len(groups), 2)
        self.assertEqual({line["warehouse"]: line["quantities"] for group in groups for line in group["lines"]}, {worker.bypass.NJ_WAREHOUSE: {"L": 2}, "Richmond, VA": {"L": 1}})

    def test_missing_production_date_can_be_assigned_but_missing_due_cannot(self):
        self.assertEqual(worker.date_changes({"shipping_class": "free", "due_date": date(2026, 11, 6)}, date(2026, 11, 5)), (date(2026, 11, 5), date(2026, 11, 6)))
        with self.assertRaisesRegex(RuntimeError, "due date"):
            worker.date_changes({}, date(2026, 11, 5))

    def test_only_exact_philly_alias_is_inhouse(self):
        for name in ("M6 Print and Ship to Philly (PA)", "M6 Print and Ship to Philly"):
            order = {"order_type": "inhouse", "subcontractor_banner_present": True, "subcontractor": name}
            self.assertIsNone(worker.bypass._production_destination_error(order))
        self.assertIsNotNone(worker.bypass._production_destination_error({"order_type": "inhouse", "subcontractor": "Mach 6 Manufacturing (CA)"}))

    def test_nj_only_is_prepared_as_pickup_and_numeric_po(self):
        driver = mock.Mock()
        line = {"warehouse": worker.bypass.NJ_WAREHOUSE, "search_id": "5000", "product": product(), "quantities": {"L": 1}}
        group = {"vendor": "sanmar", "lines": [line], "plan": {"warehouses": [worker.bypass.NJ_WAREHOUSE]}}
        with ExitStack() as stack:
            for name in ("_search_sanmar_product", "_select_sanmar_color", "_wait_for_text"):
                stack.enter_context(mock.patch.object(worker.bypass, name))
            inventory = stack.enter_context(mock.patch.object(worker.bypass, "_wait_for_sanmar_inventory", return_value=[{"warehouse": worker.bypass.NJ_WAREHOUSE, "stock": {"L": 1}}]))
            stack.enter_context(mock.patch.object(worker.bypass, "_sanmar_closed_warehouses", return_value=set()))
            prepare = stack.enter_context(mock.patch.object(worker.bypass, "_prepare_sanmar_shipping_plan", return_value={"success": True, "shipping": {"ship_mode": "pickup"}}))
            review = stack.enter_context(mock.patch.object(worker.bypass, "_fill_review_and_submit"))
            worker.prepare_group(driver, group, {"order_id": "5000001"})
            inventory.return_value = [{"warehouse": worker.bypass.NJ_WAREHOUSE, "stock": {"L": 0}}]
            with self.assertRaisesRegex(RuntimeError, "no longer available"):
                worker.prepare_group(driver, group, {"order_id": "5000001"})
            self.assertEqual(prepare.call_count, 1)
        self.assertTrue(prepare.call_args.kwargs["nj_pickup_order"])
        self.assertEqual(review.call_args.args[1], "5000001")
        self.assertTrue(review.call_args.kwargs["dry_run"])


class ReorderStockScanTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("_open_target_order", "_require_crm_order_ready_once_with_refresh", "_activate_stock_tab"):
            self.stack.enter_context(mock.patch.object(worker.bypass, name))
        self.stack.enter_context(mock.patch.object(worker.bypass, "_find_stock_tabs", return_value=[{"label": "H-Fixture 1 - QTY: 1 View Proofs"}]))
        self.stack.enter_context(mock.patch.object(worker.bypass, "_extract_order_data", return_value={"products": [product()]}))
        self.stack.enter_context(mock.patch.object(worker.time, "sleep"))

    @staticmethod
    def state(assigned=False, *, ready=True):
        return {"ready": ready, "design_tab_id": "design-fixture", "records": [
            {"id": "old-stock", "vendor": "Sanmar", "po": "H-Fixture-OLD", "assigned": assigned},
        ]}

    def test_late_inventory_rows_cannot_be_reported_as_no_reorder_needed(self):
        driver = mock.Mock()
        driver.execute_script.side_effect = [
            {"ready": False, "design_tab_id": "design-fixture", "records": []},
            self.state(ready=False), self.state(), self.state(),
        ]
        _order, tabs = worker.scan_order(driver, "5000001")
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["vendor"], "sanmar")
        self.assertEqual(tabs[0]["red_records"][0]["po"], "H-Fixture-OLD")

    def test_empty_or_unreadable_inventory_stops_for_review_instead_of_success(self):
        for state in ({"ready": False, "records": []}, self.state(assigned=None, ready=False)):
            driver = mock.Mock()
            driver.execute_script.return_value = state
            with self.subTest(state=state), self.assertRaisesRegex(RuntimeError, "Reorder requires review"):
                worker._read_rows(driver, timeout=0)

    def test_destination_must_finish_changing_before_the_scan_is_accepted(self):
        driver = mock.Mock()
        driver.execute_script.side_effect = [self.state(assigned=True), self.state(), self.state()]
        _order, tabs = worker.scan_order(driver, "5000001")
        self.assertEqual(len(tabs), 1)
        self.assertFalse(tabs[0]["red_records"][0]["assigned"])

    def test_verified_current_destination_stock_still_needs_no_reorder(self):
        driver = mock.Mock()
        driver.execute_script.return_value = self.state(assigned=True)
        _order, tabs = worker.scan_order(driver, "5000001")
        self.assertEqual(tabs, [])

    def test_non_sanmar_red_stock_stops_before_any_supplier_access(self):
        for supplier in ("S&S Activewear", "SS Activewear", "S and S Activewear", "Local Inventory"):
            with self.subTest(supplier=supplier), mock.patch.object(worker, "load_receipt", return_value=None):
                state = self.state()
                state["records"][0]["vendor"] = supplier
                driver = mock.Mock()
                driver.execute_script.return_value = state
                vendor = mock.Mock()
                with self.assertRaisesRegex(RuntimeError, "SanMar only"):
                    worker.process_order(driver, vendor, "5000001")
                vendor.assert_not_called()

    def test_mixed_sanmar_and_retired_supplier_designs_cannot_partially_purchase(self):
        sanmar = self.state()
        retired = self.state()
        retired["records"][0]["vendor"] = "S&S Activewear"
        driver = mock.Mock()
        driver.execute_script.side_effect = [sanmar, sanmar, retired, retired]
        vendor = mock.Mock()
        with mock.patch.object(worker.bypass, "_find_stock_tabs", return_value=[{"label": "Design 1"}, {"label": "Design 2"}]), mock.patch.object(
            worker, "load_receipt", return_value=None
        ), self.assertRaisesRegex(RuntimeError, "SanMar only"):
            worker.process_order(driver, vendor, "5000001")
        vendor.assert_not_called()




class ReorderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(mock.patch.object(worker, "RECEIPT_DIR", directory))
        self.order = {"order_id": "5000001", "po": "H-Fixture", "production_date": date.today(), "due_date": date.today() + timedelta(days=10), "shipping_class": "free"}
        self.tabs = [tab()]
        self.scan = self.stack.enter_context(mock.patch.object(worker, "scan_order", side_effect=lambda *_: (dict(self.order), copy.deepcopy(self.tabs))))
        self.shipping_class = self.stack.enter_context(mock.patch.object(worker.bypass, "_order_shipping_class_from_text", side_effect=AssertionError("Reorder must not classify shipping charges")))
        self.stack.enter_context(mock.patch.object(worker.bypass, "_ensure_sanmar_logged_in"))
        self.empty = self.stack.enter_context(mock.patch.object(worker, "_empty"))
        self.plan = self.stack.enter_context(mock.patch.object(worker, "sanmar_groups", return_value=[{"vendor": "sanmar", "state": "planned", "plan": {}, "lines": []}]))
        self.prepare = self.stack.enter_context(mock.patch.object(worker, "prepare_group", return_value={"eta": date.today(), "shipping_requirements": {}}))
        self.submit = self.stack.enter_context(mock.patch.object(worker.bypass, "_fill_review_and_submit"))
        self.capture = self.stack.enter_context(mock.patch.object(worker.bypass, "_capture_sanmar_confirmation", return_value={"po_confirmed": True, "po": "5000001", "web_reference": "web123"}))
        self.finish = self.stack.enter_context(mock.patch.object(worker, "finish_crm"))
        self.stack.enter_context(mock.patch.object(worker.bypass, "_change_crm_due_date", side_effect=lambda _driver, _id, value: self.order.update(due_date=value)))
        self.stack.enter_context(mock.patch.object(worker.bypass, "_change_crm_production_date", side_effect=lambda _driver, _id, value: self.order.update(production_date=value)))
        self.vendor = mock.Mock()

    def run_order(self, dry_run=False):
        return worker.process_order(mock.Mock(), self.vendor, "5000001", dry_run=dry_run)

    def test_mixed_records_stop_before_vendor_access(self):
        self.tabs[0]["valid_records"] = [{"po": "5000001", "vendor": "Sanmar", "assigned": True}]
        with self.assertRaisesRegex(RuntimeError, "Mixed"):
            self.run_order()
        self.vendor.assert_not_called(); self.submit.assert_not_called()

    def test_retired_supplier_receipts_stop_without_changing_saved_purchases(self):
        for state in ("planned", "submitting", "uncertain", "confirmed"):
            with self.subTest(state=state):
                receipt = {"version": 1, "order_id": "5000001", "customer_po": "5000001",
                           "source_signature": worker.signature(worker.snapshot(self.tabs)),
                           "groups": [{"vendor": "sanmar", "state": "planned"},
                                      {"vendor": "ssactivewear", "state": state,
                                       "confirmation": {"po": "5000001", "po_confirmed": True}}]}
                worker.save_receipt(receipt)
                path = Path(worker.RECEIPT_DIR, "5000001.json")
                original = path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "SanMar only.*review existing purchases manually"):
                    self.run_order()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(worker.load_receipt("5000001")["groups"][1]["state"], state)
        self.scan.assert_not_called()
        self.vendor.assert_not_called()
        self.empty.assert_not_called()
        self.submit.assert_not_called()
        self.finish.assert_not_called()

    def test_existing_numeric_source_po_stops_before_another_supplier_purchase(self):
        self.tabs[0]['red_records'][0]['po'] = '5000001'
        with self.assertRaisesRegex(RuntimeError, 'Manual Order form rejects duplicate POs'):
            self.run_order()
        self.vendor.assert_not_called()
        self.submit.assert_not_called()
        self.empty.assert_not_called()
        self.assertIsNone(worker.load_receipt('5000001'))

    def test_confirmed_purchase_recovers_approved_crm_alias_without_supplier_access(self):
        self.tabs[0]['red_records'][0]['po'] = '5000001'
        self.tabs[0]['valid_records'] = [{'assigned': True, 'po': '5000001-2', 'vendor': 'Sanmar'}]
        receipt = {'version': 1, 'order_id': '5000001', 'customer_po': '5000001', 'crm_po': '5000001-2',
                   'source_signature': worker.signature(worker.snapshot(self.tabs)),
                   'groups': [{'vendor': 'sanmar', 'state': 'confirmed', 'lines': [], 'review': {'eta': date.today()},
                               'confirmation': {'po': '5000001', 'po_confirmed': True, 'web_reference': 'fixture'}}]}
        worker.save_receipt(receipt)
        result = self.run_order()
        self.assertEqual(result['crm_po'], '5000001-2')
        self.vendor.assert_not_called()
        self.prepare.assert_not_called()
        self.submit.assert_not_called()
        self.finish.assert_called_once()

    def test_invalid_crm_alias_cannot_change_the_supplier_po_or_recovery_order(self):
        receipt = {'version': 1, 'order_id': '5000001', 'customer_po': '5000001',
                   'source_signature': worker.signature(worker.snapshot(self.tabs)), 'groups': []}
        for alias in ('5000002-2', '5000001-0', '5000001-1', 'other'):
            with self.subTest(alias=alias):
                worker.save_receipt({**receipt, 'crm_po': alias})
                with self.assertRaisesRegex(RuntimeError, 'receipt cannot be read'):
                    self.run_order()
        self.vendor.assert_not_called()
        self.submit.assert_not_called()

    def test_no_red_stock_does_not_launch_supplier(self):
        self.tabs.clear()
        self.assertEqual(self.run_order()["outcome"], "no_red_stock")
        self.vendor.assert_not_called()

    def test_dry_run_preflights_without_receipt_purchase_or_crm_record(self):
        self.assertEqual(self.run_order(dry_run=True)["outcome"], "reorder_ready")
        self.assertIsNone(worker.load_receipt("5000001"))
        self.submit.assert_not_called(); self.finish.assert_not_called()

    def test_late_paid_reorder_saves_both_dates_before_purchasing(self):
        self.order.update(shipping_class="rush", production_date=date(2026, 11, 3), due_date=date(2026, 11, 4))
        self.prepare.return_value = {"eta": date(2026, 11, 4), "shipping_requirements": {}}
        def submit(*_args, **_kwargs):
            self.assertEqual(self.order["production_date"], date(2026, 11, 4))
            self.assertEqual(self.order["due_date"], date(2026, 11, 5))
            self.assertEqual(worker.load_receipt("5000001")["groups"][0]["state"], "submitting")
        self.submit.side_effect = submit
        self.assertEqual(self.run_order()["outcome"], "reordered")
        self.submit.assert_called_once()
        self.finish.assert_called_once()
        self.shipping_class.assert_not_called()

    def test_confirmed_purchase_recovers_crm_without_second_purchase(self):
        self.finish.side_effect = [RuntimeError("CRM record save failed"), None]
        with self.assertRaisesRegex(RuntimeError, "CRM record"):
            self.run_order()
        self.assertEqual(worker.load_receipt("5000001")["groups"][0]["state"], "confirmed")
        self.run_order()
        self.assertEqual(self.submit.call_count, 1)
        self.assertEqual(self.submit.call_args.args[1], "5000001")
        self.assertEqual(self.finish.call_count, 2)

    def test_uncertain_submission_is_reserved_before_click_and_never_repeated(self):
        def interrupted(*_args, **_kwargs):
            self.assertEqual(worker.load_receipt("5000001")["groups"][0]["state"], "submitting")
            raise RuntimeError("confirmation timeout")
        self.submit.side_effect = interrupted
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.run_order()
        with self.assertRaisesRegex(RuntimeError, "automatic resubmission is blocked"):
            self.run_order()
        self.assertEqual(self.submit.call_count, 1)
        self.finish.assert_not_called()

    def test_wrong_confirmation_po_does_not_finish_crm_or_allow_retry(self):
        self.capture.return_value = {"po_confirmed": False}
        with self.assertRaisesRegex(RuntimeError, "exact Reorder PO"):
            self.run_order()
        self.finish.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            self.run_order()

    def test_shortened_crm_due_date_during_checkout_prevents_purchase(self):
        calls = 0
        def scan(*_args):
            nonlocal calls
            calls += 1
            order = dict(self.order)
            if calls > 1:
                order["due_date"] = date.today()
            return order, copy.deepcopy(self.tabs)
        self.scan.side_effect = scan
        with self.assertRaisesRegex(RuntimeError, "date changed during checkout"):
            self.run_order()
        self.submit.assert_not_called()

    def test_po_confirmed_flag_cannot_override_a_different_confirmation_po(self):
        self.capture.return_value = {"po_confirmed": True, "po": "5000002"}
        with self.assertRaisesRegex(RuntimeError, "exact Reorder PO"):
            self.run_order()
        self.finish.assert_not_called()

    def test_changed_quantities_block_recovery_of_existing_purchase(self):
        self.run_order()
        self.tabs[0]["products"][0]["quantities"]["L"] = 2
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            self.run_order()
        self.assertEqual(self.submit.call_count, 1)

    def test_all_supplier_preflights_finish_before_first_purchase(self):
        self.plan.return_value *= 2
        self.prepare.side_effect = [{"eta": date.today()}, RuntimeError("second cart failed")]
        with self.assertRaisesRegex(RuntimeError, "second cart"):
            self.run_order()
        self.submit.assert_not_called()

    def test_corrupt_receipt_never_falls_back_to_new_purchase(self):
        Path(worker.RECEIPT_DIR, "5000001.json").write_text('{"incomplete":true}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "receipt cannot be read"):
            self.run_order()
        self.vendor.assert_not_called()


class ReorderCrmRecordingTests(unittest.TestCase):
    def test_crm_alias_records_and_verifies_the_alias_while_preserving_supplier_po(self):
        receipt = {'customer_po': '5000001', 'crm_po': '5000001-2', 'groups': [{
            'vendor': 'sanmar', 'state': 'confirmed', 'confirmation': {'po': '5000001', 'po_confirmed': True, 'web_reference': 'fixture-ref'},
            'lines': [{'product': product(), 'quantities': {'L': 1}, 'warehouse': 'Robbinsville, NJ'}]}]}
        driver = mock.Mock()
        with mock.patch.object(worker, 'verify_record', side_effect=[False, True]) as verify, mock.patch.object(
            worker.bypass, '_record_crm_manual_order'
        ) as record, mock.patch.object(worker.bypass, '_open_target_order'), mock.patch.object(
            worker.bypass, '_require_crm_order_ready_once_with_refresh'
        ), mock.patch.object(worker.bypass, '_save_and_verify_crm_production_note') as note, mock.patch.object(worker, 'save_receipt'):
            worker.finish_crm(driver, '5000001', [tab()], receipt)
        self.assertEqual([c.args[2] for c in verify.call_args_list], ['5000001-2', '5000001-2'])
        record.assert_called_once_with(driver, '5000001', '5000001-2', stock_tab_index=1, vendor_name='Sanmar', add_box=True)
        self.assertIn('PO 5000001 - Sanmar fixture-ref', note.call_args_list[0].args[2])
        self.assertIn('CRM stock PO 5000001-2', note.call_args_list[1].args[2])
        self.assertIn('supplier PO 5000001', note.call_args_list[1].args[2])
        self.assertEqual(receipt['customer_po'], '5000001')
        self.assertTrue(receipt['complete'])


class ReorderRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.enqueue = mock.Mock(return_value=(True, "Queued", {"id": "fixture"}))
        self.runner = mock.Mock()
        register_reorder_routes(self.app, enqueue_automation=self.enqueue, run_reorder=self.runner, get_status=lambda: {})
        self.client = self.app.test_client()

    def test_custom_ids_and_links_queue_one_local_batch_in_input_order(self):
        result = self.client.post("/crm/reorder", json={"processing_filter": "custom", "custom_input_type": "orders", "custom_order_ids": "5000001, https://crm2.legacy.printfly.com/app#/order/5000002 5000001"})
        self.assertTrue(result.json["success"])
        self.enqueue.assert_called_once()
        call = self.enqueue.call_args
        self.assertEqual(call.kwargs["task_arguments"], {"order_ids": ["5000001", "5000002"]})
        self.assertEqual(call.kwargs["task_type"], "crm.reorder_batch")
        self.assertEqual(len(result.json["tasks"]), 1)
        call.args[2]()
        self.runner.assert_called_once_with(order_ids=["5000001", "5000002"])

    def test_queue_rejection_never_leaves_a_partial_batch(self):
        self.enqueue.return_value = (False, "Queue unavailable", None)
        result = self.client.post("/crm/reorder", json={"processing_filter": "custom", "custom_input_type": "orders", "custom_order_ids": "5000001 5000002"})
        self.assertEqual(result.status_code, 409)
        self.enqueue.assert_called_once()
        self.runner.assert_not_called()

    def test_report_input_other_modes_and_malformed_lists_are_rejected_in_full(self):
        base = {"processing_filter": "custom", "custom_input_type": "orders", "custom_order_ids": "5000001"}
        for change in ({"processing_filter": "all"}, {"custom_input_type": "link"}, {"custom_list_url": "https://crm.example/report"}, {"custom_order_ids": "5000001 bad"}):
            with self.subTest(change=change):
                self.assertEqual(self.client.post("/crm/reorder", json={**base, **change}).status_code, 400)
        self.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
