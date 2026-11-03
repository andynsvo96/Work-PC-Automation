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
import ssactivewear_ordering as sxs
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
        self.assertEqual(worker.aggregate_products([tab()], "ssactivewear"), [])

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


class SxsPlanningTests(unittest.TestCase):
    def test_login_input_does_not_type_when_focus_moves_elsewhere(self):
        driver = mock.Mock(current_url=sxs.LOGIN_URL)
        driver.execute_script.return_value = False
        field = mock.Mock()
        self.assertFalse(sxs._fill_login_control(driver, field, 'fixture-password'))
        self.assertEqual(field.click.call_count, 2)
        field.send_keys.assert_not_called()

    def test_ambiguous_login_fields_stop_before_reading_credentials(self):
        driver = mock.Mock(current_url=sxs.LOGIN_URL)
        fields = [mock.Mock(), mock.Mock()]
        for field in fields:
            field.is_displayed.return_value = True
            field.is_enabled.return_value = True
        driver.find_elements.side_effect = lambda by, value: fields if value == 'M_M_zEmailTB' else []
        with mock.patch.object(sxs, 'safe_get_with_partial_load'), mock.patch.object(
            sxs, '_raise_site_error'
        ), mock.patch.object(sxs, 'accept_cookie_banner'), mock.patch.object(sxs, 'read_credential') as read:
            with self.assertRaisesRegex(RuntimeError, 'login form is ambiguous'):
                sxs.ensure_login(driver)
        read.assert_not_called()

    def test_cookie_consent_is_never_clicked_on_another_origin(self):
        driver = mock.Mock(current_url="https://example.com/")
        with mock.patch.object(sxs.bypass, "_click_with_fallback") as click:
            with self.assertRaisesRegex(RuntimeError, "origin"):
                sxs.accept_cookie_banner(driver)
        click.assert_not_called()

    def test_supplier_error_stops_before_saved_credentials_are_read(self):
        error = "ERROR Looks like we're having some problems. Ray ID - aabbccddeeff0011 Client IP - 192.0.2.1"
        for location in ("cart", "login"):
            with self.subTest(location=location):
                driver = mock.Mock(current_url=sxs.LOGIN_URL)
                driver.find_elements.return_value = []
                driver.execute_script.side_effect = [error] if location == "cart" else ["Sign In", error]
                with mock.patch.object(sxs, "safe_get_with_partial_load"), mock.patch.object(sxs, "read_credential") as credentials:
                    with self.assertRaisesRegex(RuntimeError, "S&S returned its website error page") as raised:
                        sxs.ensure_login(driver)
                self.assertIn("aabbccddeeff0011", str(raised.exception))
                self.assertNotIn("192.0.2.1", str(raised.exception))
                credentials.assert_not_called()

    def test_supplier_error_during_a_wait_is_reported_without_timeout(self):
        driver = mock.Mock(current_url=sxs.BASE_URL)
        driver.find_elements.return_value = []
        driver.execute_script.return_value = "Looks like we’re having some problems. Ray ID - aabbccddeeff0011"
        ready = mock.Mock(return_value=False)
        with self.assertRaisesRegex(RuntimeError, "Ray ID: aabbccddeeff0011"):
            sxs._wait(driver, ready, timeout=0)
        ready.assert_not_called()
        driver.execute_script.return_value = "Sign In Welcome to S&S"
        ready.return_value = "ready"
        self.assertEqual(sxs._wait(driver, ready, timeout=0), "ready")

    def test_credentials_and_checkout_require_the_exact_https_supplier_origin(self):
        sxs._require_site(mock.Mock(current_url="https://www.ssactivewear.com/checkout/"))
        for url in ("http://www.ssactivewear.com/checkout", "https://www.ssactivewear.com.example.com/login", "https://example.com/login"):
            with self.subTest(url=url), self.assertRaisesRegex(RuntimeError, "origin"):
                sxs._require_site(mock.Mock(current_url=url))

    def test_comfort_colors_style_from_actual_crm_example(self):
        p = product(product_id="C1717", product_name="Comfort Colors Heavyweight T-Shirt")
        candidates = [{"url": "https://www.ssactivewear.com/p/comfort_colors/1717", "title": "1717 Comfort Colors Garment-Dyed Heavyweight T-Shirt"}]
        self.assertEqual(sxs.select_product(p, candidates), candidates[0])

    def test_tultex_fine_jersey_matches_its_brand_and_style(self):
        p = product(product_id="202", product_name="Tultex Unisex Fine Jersey T-Shirt")
        candidate = {"url": "https://www.ssactivewear.com/p/tultex/202", "title": "202 Tultex Unisex Fine Jersey T-Shirt"}
        other = {"url": "https://www.ssactivewear.com/p/other_brand/202", "title": "202 Other Brand Unisex Fine Jersey T-Shirt"}
        self.assertEqual(sxs.select_product(p, [other, candidate]), candidate)
        with self.assertRaisesRegex(RuntimeError, "unambiguous"):
            sxs.select_product(p, [other])

    def candidates(self):
        return [{"url": "https://www.ssactivewear.com/p/gildan/5000", "title": "5000 Gildan Unisex Heavy Cotton T-Shirt"}, {"url": "https://www.ssactivewear.com/p/bayside/5000", "title": "5000 Bayside Unisex USA-Made Fine Jersey T-Shirt"}]

    def test_style_brand_and_description_distinguish_bayside_from_gildan(self):
        self.assertEqual(sxs.select_product(product(), self.candidates())["url"], self.candidates()[0]["url"])
        with self.assertRaises(RuntimeError):
            sxs.select_product(product(), self.candidates()[1:])

    def test_unknown_brand_and_youth_mismatch_stop_instead_of_guessing(self):
        for p in (product(product_name="Cotton Shirt"), product(product_name="Gildan Youth Cotton Shirt")):
            with self.assertRaises(RuntimeError):
                sxs.select_product(p, self.candidates())

    def line(self, warehouses, quantities=None):
        return {"product": product(), "selected": self.candidates()[0], "quantities": quantities or {"L": 2}, "inventory": [{"warehouse": name, "eta_text": "Est. Delivery: 11/03/2026", "stock": stock} for name, stock in warehouses]}

    def test_reading_preferred_then_complete_other_warehouse_before_split(self):
        line = self.line([("Middleboro, MA", {"L": 10}), ("Reading, PA", {"L": 10})])
        self.assertEqual(sxs.allocate([line])[0]["warehouse"], "Reading, PA")
        line["inventory"][1]["stock"]["L"] = 1
        self.assertEqual(sxs.allocate([line])[0]["warehouse"], "Middleboro, MA")

    def test_sxs_reorder_accepts_one_available_piece_without_an_extra_buffer(self):
        line = self.line([("Reading, PA", {"L": 1})], quantities={"L": 1})
        self.assertEqual(sxs.allocate([line])[0]["quantities"], {"L": 1})
        line["inventory"][0]["stock"]["L"] = 0
        with self.assertRaisesRegex(RuntimeError, "every required size"):
            sxs.allocate([line])

    def test_split_is_used_only_when_no_complete_warehouse_and_never_short_ships(self):
        line = self.line([("Reading, PA", {"L": 1}), ("Middleboro, MA", {"L": 1})])
        allocation = sxs.allocate([line])
        self.assertEqual([x["quantities"]["L"] for x in allocation], [1, 1])
        line["quantities"]["L"] = 3
        with self.assertRaisesRegex(RuntimeError, "every required size"):
            sxs.allocate([line])

    def test_common_warehouse_covers_all_products_before_split(self):
        a = self.line([("Reading, PA", {"L": 2}), ("Middleboro, MA", {"L": 2})])
        b = self.line([("Reading, PA", {"L": 0}), ("Middleboro, MA", {"L": 2})])
        self.assertEqual({x["warehouse"] for x in sxs.allocate([a, b])}, {"Middleboro, MA"})

    def test_cart_requires_exact_product_color_size_quantity_and_warehouse(self):
        line = self.line([("Reading, PA", {"L": 50})]); line["warehouse"] = "Reading, PA"
        row = {"url": "https://www.ssactivewear.com/p/gildan/5000/black-heavy_cotton", "color": "Black", "size": "L", "quantity": 2, "method": "S&S Ship", "warehouse": "Reading, PA"}
        sxs.validate_cart([row], [line])
        for change in ({"quantity": 1}, {"color": "White"}, {"size": "XL"}, {"warehouse": "Reno, NV"}, {"url": "https://www.ssactivewear.com/p/bayside/5000/black"}, {"method": "Pick Up"}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                sxs.validate_cart([{**row, **change}], [line])

    def test_confirmation_requires_supplier_success_reference_and_exact_labelled_po(self):
        text = "Thank you for your order! Order Number: 98765432 PO Number: 5000001"
        url = "https://www.ssactivewear.com/checkout/confirmation"
        self.assertTrue(sxs.confirmation_from_text(text, "5000001", url)["po_confirmed"])
        for altered, location in ((text.replace("5000001", "50000010"), url), ("PO Number: 5000001", url), (text, "https://example.com/confirmation"), (text.replace("PO Number:", "Phone:"), url)):
            self.assertIsNone(sxs.confirmation_from_text(altered, "5000001", location))

    def test_actual_order_confirmation_heading_accepts_line_breaks_and_combined_labels(self):
        url = "https://www.ssactivewear.com/checkout/orderconfirmation?orderguid=fixture"
        for text in ("Order Confirmation: 98765432\nPO Number: 5000001\nWarehouse: Reading, PA", "Order Confirmation: 98765432PO Number: 5000001Warehouse: Reading, PA"):
            with self.subTest(text=text):
                result = sxs.confirmation_from_text(text, "5000001", url)
                self.assertEqual(result["order_number"], "98765432")
                self.assertEqual(result["po"], "5000001")
                self.assertIsNone(sxs.confirmation_from_text(text.replace("5000001", "50000010"), "5000001", url))
                self.assertIsNone(sxs.confirmation_from_text(text + "\nOrder Confirmation: 87654321", "5000001", url))

    def test_split_cart_supports_separate_rows_and_verified_grouped_quantities(self):
        line = self.line([("Reading, PA", {"L": 1}), ("Middleboro, MA", {"L": 1})])
        lines = sxs.allocate([line])
        row = {"url": "https://www.ssactivewear.com/p/gildan/5000/black-heavy_cotton", "color": "Black", "size": "L", "quantity": 1, "method": "S&S Ship", "warehouse": "Reading, PA"}
        sxs.validate_cart([row, {**row, "warehouse": "Middleboro, MA"}], lines)
        grouped = {**row, "quantity": 2, "warehouse": "Multiple warehouses", "allocations": {"Reading, PA": 1, "Middleboro, MA": 1}}
        sxs.validate_cart([grouped], lines)
        with self.assertRaisesRegex(RuntimeError, "match"):
            sxs.validate_cart([{**grouped, "allocations": {"Reading, PA": 2}}], lines)


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

    def test_custom_ids_and_links_queue_distinct_local_order_tasks(self):
        result = self.client.post("/crm/reorder", json={"processing_filter": "custom", "custom_input_type": "orders", "custom_order_ids": "5000001, https://crm2.legacy.printfly.com/app#/order/5000002 5000001"})
        self.assertTrue(result.json["success"])
        self.assertEqual([c.kwargs["task_arguments"] for c in self.enqueue.call_args_list], [{"order_id": "5000001"}, {"order_id": "5000002"}])
        for call in self.enqueue.call_args_list:
            self.assertEqual(call.kwargs["task_type"], "crm.reorder")
            call.args[2]()
        self.runner.assert_has_calls([mock.call(order_id="5000001"), mock.call(order_id="5000002")])

    def test_report_input_other_modes_and_malformed_lists_are_rejected_in_full(self):
        base = {"processing_filter": "custom", "custom_input_type": "orders", "custom_order_ids": "5000001"}
        for change in ({"processing_filter": "all"}, {"custom_input_type": "link"}, {"custom_list_url": "https://crm.example/report"}, {"custom_order_ids": "5000001 bad"}):
            with self.subTest(change=change):
                self.assertEqual(self.client.post("/crm/reorder", json={**base, **change}).status_code, 400)
        self.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
