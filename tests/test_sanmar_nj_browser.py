"""Optional browser verification against a local fixture; never visits vendor/CRM sites."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
import crm_shipping_bypasser as worker
from shipping_bypasser_subcontractors import SUBCONTRACTOR_DESTINATIONS, subcontractor_order_type


@unittest.skipUnless(os.environ.get("SANMAR_FIXTURE_CHROMEDRIVER"), "Set SANMAR_FIXTURE_CHROMEDRIVER to run local browser fixtures")
class NjLocalBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        profile_root = ROOT / "runtime" / "generated_profiles"
        profile_root.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="nj_checkout_fixture_", dir=profile_root)
        assert Path(cls.temp.name).resolve().is_relative_to(profile_root.resolve())
        options = webdriver.ChromeOptions()
        if os.name == "nt":
            options.add_argument("--no-sandbox")  # Isolated offline fixture profile only.
        options.add_argument("--disable-gpu")
        if os.environ.get("SANMAR_FIXTURE_CHROME"):
            options.binary_location = os.environ["SANMAR_FIXTURE_CHROME"]
        for option in ("--headless=new", "--disable-background-networking", "--disable-sync", "--no-first-run", "--no-default-browser-check"):
            options.add_argument(option)
        options.add_argument(f"--user-data-dir={cls.temp.name}/chrome")
        service_args = ["--disable-build-check"] if os.environ.get("SANMAR_FIXTURE_IGNORE_DRIVER_VERSION") == "1" else []
        service = Service(os.environ["SANMAR_FIXTURE_CHROMEDRIVER"], service_args=service_args, log_output=str(Path(cls.temp.name) / "chromedriver.log"))
        try:
            cls.driver = webdriver.Chrome(service=service, options=options)
        except Exception:
            cls.temp.cleanup()
            raise
        cls.fixture = (ROOT / "tests" / "fixtures" / "sanmar_nj_checkout.html").as_uri()

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.temp.cleanup()

    def setUp(self):
        self.driver.get(self.fixture + "?page=crm")
        self.driver.execute_script("localStorage.clear();")
        self.driver.refresh()

    def _xxs_product_line(self):
        product = {"index": 1, "product_id": "SXU003", "color": "Navy", "quantities": {"XXS": 1, "XS": 2, "M": 1, "2XL": 3}}
        return {"product": product, "search_id": "SXU003", "quantities": worker._sanmar_quantities_for_product(product)}

    def test_current_inventory_reuses_exact_style_and_preserves_a903_color(self):
        self.driver.get((ROOT / "tests" / "fixtures" / "sanmar_product_search.html").as_uri())
        self.assertTrue(worker._search_sanmar_product(self.driver, "A903"))
        self.assertEqual(self.driver.execute_script("return window.searchSubmissions;"), 0)
        self.assertEqual(worker._sanmar_selected_color_label(self.driver), "Duck Brown")
        self.assertTrue(worker._cart_color_matches("Duck Brown", "DuckBrown"))
        self.assertEqual(worker._sanmar_quantities_for_product({"quantities": {"ONE SIZE": 1}}), {"OSFA": 1})
        self.assertEqual(worker._sanmar_inventory(self.driver), [{"warehouse": "Dallas, TX", "stock": {"OSFA": 232}}])

    def test_other_style_cannot_reuse_companion_or_underlying_product_header(self):
        fixture = (ROOT / "tests" / "fixtures" / "sanmar_product_search.html").as_uri()
        self.driver.get(fixture + "?style=PC54")
        state = worker._sanmar_active_style_matches(self.driver, ["A903"])
        self.assertFalse(state["success"])
        self.assertEqual(state["candidates"], ["PC54"])
        def local_search(driver, url, label=None):
            self.assertEqual(url, "https://www.sanmar.com/search?text=A903")
            driver.get(fixture + "?style=A903")
        with mock.patch.object(worker, "safe_get_with_partial_load", side_effect=local_search) as navigate:
            self.assertTrue(worker._search_sanmar_product(self.driver, "A903"))
        navigate.assert_called_once()
        self.assertEqual(self.driver.execute_script("return window.searchSubmissions;"), 0)
        self.assertTrue(worker._sanmar_active_style_matches(self.driver, ["A903"])["success"])

    def test_result_card_opens_product_link_instead_of_unrelated_button(self):
        fixture = (ROOT / "tests" / "fixtures" / "sanmar_product_search.html").as_uri()
        self.driver.get(fixture + "?results=1")
        self.assertTrue(worker._click_sanmar_product_result(self.driver, ["A903"]))
        self.assertEqual(self.driver.execute_script("return window.productResultClicks;"), 1)
        self.assertEqual(self.driver.execute_script("return window.unrelatedClicks;"), 0)

    def test_xxs_inventory_and_quantity_entry_keep_adjacent_sizes_separate(self):
        fixture = (ROOT / "tests" / "fixtures" / "sanmar_xxs_sizes.html").as_uri()
        for layout in ("drawer", "legacy"):
            with self.subTest(layout=layout), mock.patch.object(worker.time, "sleep"):
                self.driver.get(fixture + "?page=inventory&layout=" + layout)
                inventory = worker._sanmar_inventory(self.driver)
                self.assertEqual(inventory, [
                    {"warehouse": "Robbinsville, NJ", "stock": {"XXS": 0, "XS": 80, "M": 90, "2XL": 100}},
                    {"warehouse": "Dallas, TX", "stock": {"XXS": 30, "XS": 40, "M": 50, "2XL": 60}},
                ])
                line = dict(self._xxs_product_line(), inventory=inventory)
                warehouse, plan = worker._choose_warehouse_plan([line], "inhouse")
                self.assertEqual(warehouse, "Dallas, TX")
                self.assertEqual(plan["mode"], "single_warehouse")
                worker._fill_sanmar_quantities(self.driver, warehouse, line["quantities"])
                values = self.driver.execute_script("return Object.fromEntries(Array.from(document.querySelectorAll('input')).map(input => [input.id, input.value]));")
                self.assertEqual(values, {
                    "qty-0-XXS": "0", "qty-0-XS": "0", "qty-0-M": "0", "qty-0-2XL": "0",
                    "qty-1-XXS": "1", "qty-1-XS": "2", "qty-1-M": "1", "qty-1-2XL": "3",
                })
                self.assertEqual(self.driver.execute_script("return window.quantityChanges;"), {
                    "qty-1-XXS": "1", "qty-1-XS": "2", "qty-1-M": "1", "qty-1-2XL": "3",
                })

    def test_xxs_cart_reading_and_validation_across_supported_layouts(self):
        fixture = (ROOT / "tests" / "fixtures" / "sanmar_xxs_sizes.html").as_uri()
        for layout in ("table", "desktop", "mobile", "multiple", "labels", "cards"):
            with self.subTest(layout=layout), mock.patch.object(worker.time, "sleep"):
                self.driver.get(fixture + "?page=cart&layout=" + layout)
                lines = worker._read_sanmar_cart_lines(self.driver)
                self.assertEqual(lines, [
                    {"style": "SXU003", "color": "Navy", "size": size, "quantity": quantity, "warehouse": "Dallas, TX"}
                    for size, quantity in self._xxs_product_line()["quantities"].items()
                ])
                validation = worker._validate_sanmar_cart_contents(self.driver, [self._xxs_product_line()], warehouse="Dallas, TX", cart_lines=lines)
                self.assertTrue(validation["success"], validation.get("issues"))
                lines[0]["size"] = "XS"
                validation = worker._validate_sanmar_cart_contents(self.driver, [self._xxs_product_line()], warehouse="Dallas, TX", cart_lines=lines)
                self.assertFalse(validation["success"])
                self.assertIn("Expected SXU003 size XXS qty 1, but cart has 0.", validation["issues"])

    def test_real_pickup_selection_and_customer_po_for_inhouse_nj_group(self):
        with mock.patch.object(worker.time, "sleep"):
            self.driver.get(self.fixture + "?page=checkout")
            shipping = worker._select_shipping_destination(self.driver, "inhouse", worker.NJ_WAREHOUSE)
            self.assertEqual(shipping["ship_mode"], "pickup")
            fulfillment = self.driver.execute_script("return window.fixtureFulfillment;")
            self.assertTrue(fulfillment["pickup"])
            self.assertEqual(fulfillment["warehouse"], "Robbinsville, NJ")
            self.assertEqual(worker._fill_review_and_submit(self.driver, "H-CJVillafra302", dry_run=True, shipping_requirements={"order_type": "inhouse", "shipping": shipping}), "dry_run_review_ready")
            self.assertEqual(self.driver.execute_script("return document.getElementById('po').value;"), "H-CJVillafra302")
            self.assertNotIn("UPS", self.driver.execute_script("return document.body.innerText;"))

    def test_real_mach6_nj_single_and_multi_boxes_select_ups_with_same_po(self):
        for keys, names in (("nj", [worker.NJ_WAREHOUSE]), ("va,nj", ["Richmond, VA", worker.NJ_WAREHOUSE])):
            with self.subTest(warehouses=names), mock.patch.object(worker.time, "sleep"):
                self.driver.get(self.fixture + "?page=checkout&warehouses=" + keys)
                multi = len(names) > 1
                shipping = worker._select_shipping_destination(self.driver, "mach6", None if multi else names[0], multi_warehouse=multi)
                eta = worker._select_ups_eta_for_shipping_plan(self.driver, "mach6", warehouse=None if multi else names[0], selected_warehouses=names, multi_warehouse=multi)
                self.assertEqual(eta["eta"], worker.datetime(2026, 10, 6).date())
                worker._click_sanmar_button(self.driver, r"Proceed\s+To\s+Payment")
                fulfillment = self.driver.execute_script("return window.fixtureFulfillment;")
                self.assertFalse(fulfillment["pickup"])
                self.assertEqual(fulfillment["address"], "Mach 6 Manufacturing")
                self.assertEqual(fulfillment["methods"], [{"warehouse": name, "method": "ups"} for name in names])
                self.assertEqual(worker._fill_review_and_submit(self.driver, "H-Mach6Test", dry_run=True, shipping_requirements={"order_type": "mach6", "shipping": shipping}), "dry_run_review_ready")
                self.assertEqual(self.driver.execute_script("return document.getElementById('po').value;"), "H-Mach6Test")
                self.assertEqual(worker._fill_review_and_submit(self.driver, "H-Mach6Test", shipping_requirements={"order_type": "mach6", "shipping": shipping}), "submitted")

    def test_real_mach6_review_blocks_changed_delivery_method(self):
        with mock.patch.object(worker.time, "sleep"):
            self.driver.get(self.fixture + "?page=checkout")
            shipping = worker._select_shipping_destination(self.driver, "mach6", worker.NJ_WAREHOUSE)
            self.driver.execute_script("document.getElementById('pickup').click(); document.getElementById('warehouse').value = 'nj';")
            worker._click_sanmar_button(self.driver, r"Proceed\s+To\s+Payment")
            with self.assertRaisesRegex(RuntimeError, "UPS shipping was not confirmed"):
                worker._fill_review_and_submit(self.driver, "H-Mach6Test", shipping_requirements={"order_type": "mach6", "shipping": shipping})
            self.assertNotIn("Thank You", self.driver.execute_script("return document.body.innerText;"))

    def test_real_subcontractor_banner_classification(self):
        for name, expected in (("Mach 6 Manufacturing (CA)", "mach6"), ("Mach6 Manufacturing (CA)", "mach6"), ("Other Manufacturing", "unsupported_subcontractor")):
            with self.subTest(name=name):
                self.driver.get(self.fixture + "?page=crm")
                self.driver.execute_script("const banner = document.createElement('div'); banner.style.background = '#ff00a0'; banner.textContent = 'Subcontractor: ' + arguments[0] + ' Preferred File Types: Raster PNG'; document.body.prepend(banner);", name)
                order = worker._extract_order_data(self.driver, "5000001")
                self.assertEqual(order["order_type"], expected)
                self.assertTrue(order["subcontractor_banner_present"])

    def test_all_reviewed_subcontractors_select_the_exact_saved_address_without_pickup(self):
        entries = list(SUBCONTRACTOR_DESTINATIONS.values())
        for index, (order_type, destination) in enumerate(SUBCONTRACTOR_DESTINATIONS.items()):
            with self.subTest(subcontractor=destination["crm_name"]), mock.patch.object(worker.time, "sleep"):
                self.driver.get(self.fixture + "?page=checkout")
                self.driver.execute_script("window.fixtureSetSavedAddresses(arguments[0]);", entries)
                shipping = worker._select_shipping_destination(self.driver, order_type, worker.NJ_WAREHOUSE)
                self.assertEqual(shipping, {"ship_mode": "ship", "address": destination["name"]})
                self.assertFalse(self.driver.execute_script("return document.querySelector('#pickup').checked;"))
                self.assertEqual(self.driver.execute_script("return document.querySelector('input[name=shippingAddress]:checked').value;"), str(index))

    def test_changed_or_duplicate_saved_addresses_stop_before_confirming_address(self):
        order_type = subcontractor_order_type("COSMIC PRINTING")
        expected = SUBCONTRACTOR_DESTINATIONS[order_type]
        variants = [{**expected, field: "Wrong value"} for field in ("name", "line1", "line2", "city", "state", "zip")]
        for entries in [[entry] for entry in variants] + [[expected, expected]]:
            with self.subTest(entries=entries), mock.patch.object(worker.time, "sleep"):
                self.driver.get(self.fixture + "?page=checkout")
                self.driver.execute_script("window.fixtureSetSavedAddresses(arguments[0]);", entries)
                with self.assertRaisesRegex(RuntimeError, "saved address.*missing, changed, or ambiguous"):
                    worker._select_shipping_destination(self.driver, order_type, worker.NJ_WAREHOUSE)
                self.assertEqual(self.driver.execute_script("return document.querySelector('#shipping-methods').textContent;"), "")

    def test_subcontractor_nj_and_split_shipping_verify_full_review_address_and_original_po(self):
        for crm_name in ("DESIGNER T'S", "SEW NICE EMBROIDERY & GIFTS", "PRINT WORKS"):
            order_type = subcontractor_order_type(crm_name)
            destination = SUBCONTRACTOR_DESTINATIONS[order_type]
            for keys, names in (("nj", [worker.NJ_WAREHOUSE]), ("va,nj", ["Richmond, VA", worker.NJ_WAREHOUSE])):
                with self.subTest(subcontractor=crm_name, warehouses=names), mock.patch.object(worker.time, "sleep"):
                    self.driver.get(self.fixture + "?page=checkout&warehouses=" + keys)
                    self.driver.execute_script("window.fixtureSetSavedAddresses(arguments[0]);", [destination])
                    multi = len(names) > 1
                    shipping = worker._select_shipping_destination(self.driver, order_type, None if multi else names[0], multi_warehouse=multi)
                    eta = worker._select_ups_eta_for_shipping_plan(self.driver, order_type, warehouse=None if multi else names[0], selected_warehouses=names, multi_warehouse=multi)
                    self.assertEqual(eta["eta"], worker.datetime(2026, 10, 6).date())
                    worker._click_sanmar_button(self.driver, r"Proceed\s+To\s+Payment")
                    state = self.driver.execute_script("return window.fixtureFulfillment;")
                    self.assertFalse(state["pickup"])
                    self.assertEqual(state["address"], destination["name"])
                    self.assertEqual(state["methods"], [{"warehouse": name, "method": "ups"} for name in names])
                    self.assertEqual(worker._fill_review_and_submit(self.driver, "5681151-1", dry_run=True, shipping_requirements={"order_type": order_type, "shipping": shipping}), "dry_run_review_ready")
                    self.assertEqual(self.driver.execute_script("return document.getElementById('po').value;"), "5681151-1")

    def test_billing_address_cannot_confirm_a_changed_shipping_destination(self):
        order_type = subcontractor_order_type("DESIGNER T'S")
        with mock.patch.object(worker.time, "sleep"):
            self.driver.get(self.fixture + "?page=checkout")
            self.driver.execute_script("window.fixtureSetSavedAddresses(arguments[0]);", [SUBCONTRACTOR_DESTINATIONS[order_type]])
            shipping = worker._select_shipping_destination(self.driver, order_type, worker.NJ_WAREHOUSE)
            worker._select_ups_eta_for_shipping_plan(self.driver, order_type, warehouse=worker.NJ_WAREHOUSE, selected_warehouses=[worker.NJ_WAREHOUSE])
            worker._click_sanmar_button(self.driver, r"Proceed\s+To\s+Payment")
            self.driver.execute_script("document.querySelector('#ship-to').innerHTML = '<h2>Ship To</h2><p>Wrong vendor, 99 Wrong Rd, Wrong City, NJ 08030</p>'; document.body.insertAdjacentHTML('beforeend', '<section><h2>Billing Address</h2><p>Designer Ts Inc, 20 Creek Rd, Brooklawn, NJ 08030-2748</p></section>');")
            with self.assertRaisesRegex(RuntimeError, "review did not confirm.*shipping address"):
                worker._fill_review_and_submit(self.driver, "5681151-1", shipping_requirements={"order_type": order_type, "shipping": shipping})
            self.assertNotIn("Thank You", self.driver.execute_script("return document.body.innerText;"))

    def test_saved_production_notes_reader_ignores_other_notes_and_unsaved_drafts(self):
        self.driver.execute_script("document.body.insertAdjacentHTML('afterbegin', `<div class='panel-heading'><span class='vendor'>Subcontractor: DESIGNER T'S (NJ)</span></div><section>Sales Notes LATEST SHIP DATE 12/9</section><textarea>LATEST SHIP DATE 12/8</textarea>`); window.fixtureSetProductionNotes(arguments[0]);", [
            {"timestamp": "Nov 24th, 2026 @ 09:18 AM", "text": "LATEST SHIP DATE - 12/3***"},
            {"timestamp": "Nov 23rd, 2026 @ 04:50 PM", "text": "LATEST SHIP DATE 12/2"},
            {"timestamp": "Nov 25, 2026 @ 09:00 AM", "text": "Approved"},
        ])
        order = worker._extract_order_data(self.driver, "5000001")
        self.assertEqual(order["order_type"], subcontractor_order_type("DESIGNER T'S"))
        self.assertEqual(order["latest_ship_date"], worker.datetime(2026, 12, 3).date())
        self.assertEqual(order["due_date"], worker.datetime(2026, 12, 7).date())

    def test_production_date_save_preserves_the_due_date_in_real_dom(self):
        with mock.patch.object(worker, "_wait_for_order_goods_page_ready", return_value=True), mock.patch.object(worker, "_wait_for_crm_shipping_method_selection"), mock.patch.object(worker, "_acknowledge_crm_production_date_warning", return_value=False), mock.patch.object(worker.time, "sleep"):
            saved = worker._change_crm_production_date(self.driver, "5000001", worker.datetime(2026, 12, 1).date(), expected_due_date=worker.datetime(2026, 12, 7).date())
        self.assertEqual(saved, worker.datetime(2026, 12, 1).date())
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture_due');"), "2026-12-07")
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture_production');"), "2026-12-01")

    def test_add_only_rows_do_not_match_original_po_guards(self):
        self.driver.execute_script("localStorage.setItem('fixture_rows', JSON.stringify(['ADD-H-CJVillafra302']));")
        self.driver.refresh()
        self.assertFalse(worker._crm_manual_order_row_exists(self.driver, "H-CJVillafra302"))
        self.assertFalse(worker._crm_stock_order_yellow_visual_exists(self.driver, "H-CJVillafra302"))
        self.assertTrue(worker._crm_manual_order_row_exists(self.driver, "ADD-H-CJVillafra302", vendor_name="Sanmar"))

    def test_original_order_and_explicit_add_box_save_distinct_pos_and_persist_note(self):
        note = "tab 1: Add box PO ADD-H-CJVillafra302\nAdd box for Navy PC54 for size XL (5 pc) from VA."
        with mock.patch.object(worker, "_wait_for_order_goods_page_ready"), mock.patch.object(worker.time, "sleep"):
            self.assertEqual(worker._record_crm_manual_order(self.driver, "5000001", "H-CJVillafra302"), "recorded")
            self.assertEqual(worker._record_crm_manual_order(self.driver, "5000001", "ADD-H-CJVillafra302", add_box=True), "recorded")
            self.assertEqual(json.loads(self.driver.execute_script("return localStorage.getItem('fixture_clicks');")), ["order-goods", "add-box"])
            self.assertEqual(worker._append_crm_production_note(self.driver, "5000001", note), "recorded")
            worker._verify_crm_production_note(self.driver, "5000001", note)
            self.assertEqual(worker._append_crm_production_note(self.driver, "5000001", note), "already_present")
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture_notes');"), note)
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture_sales_notes');"), "Existing sales note")

    def test_note_retry_recovers_failed_save_and_preserves_existing_production_and_sales_notes(self):
        note = "Add box PO ADD-H-CJVillafra302 from TX."
        existing = "Existing production note"
        self.driver.execute_script("localStorage.setItem('fixture_notes', arguments[0]); localStorage.setItem('fixture_note_save_failures', '1');", existing)
        self.driver.refresh()
        verify = worker._verify_crm_production_note
        with (
            mock.patch.object(worker, "_wait_for_order_goods_page_ready"),
            mock.patch.object(worker, "_publish_status"),
            mock.patch.object(worker.time, "sleep"),
            mock.patch.object(worker, "_verify_crm_production_note", side_effect=lambda *args: verify(*args, timeout=0)),
        ):
            self.assertEqual(worker._save_and_verify_crm_production_note(self.driver, "5000001", note), "recorded")
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture_notes');"), existing + "\n" + note)
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture_sales_notes');"), "Existing sales note")

    def test_note_retry_does_not_duplicate_a_note_that_persisted_after_stale_verification(self):
        note = "Add box PO ADD-H-CJVillafra302 from TX."
        verify = worker._verify_crm_production_note
        checks = []

        def stale_first_check(*args):
            checks.append(True)
            if len(checks) == 1:
                raise worker._CrmProductionNotePersistenceError("Stale CRM view")
            return verify(*args, timeout=0)

        with (
            mock.patch.object(worker, "_wait_for_order_goods_page_ready"),
            mock.patch.object(worker, "_publish_status"),
            mock.patch.object(worker.time, "sleep"),
            mock.patch.object(worker, "_verify_crm_production_note", side_effect=stale_first_check),
        ):
            self.assertEqual(worker._save_and_verify_crm_production_note(self.driver, "5000001", note), "already_present")
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('fixture_notes');"), note)

    def test_ambiguous_production_field_does_not_save_to_sales_notes(self):
        self.driver.execute_script("document.querySelector('#notes').parentElement.insertAdjacentHTML('beforeend', '<textarea>Second possible production field</textarea>');")
        with mock.patch.object(worker.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Production Notes textarea was not found"):
                worker._append_crm_production_note(self.driver, "5000001", "New note")
        self.assertIsNone(self.driver.execute_script("return localStorage.getItem('fixture_notes');"))
        self.assertEqual(self.driver.execute_script("return document.getElementById('sales-note-input').value;"), "Existing sales note")

    def test_split_order_only_orders_tabs_without_source_stock_records(self):
        fixture = (ROOT / "tests" / "fixtures" / "crm_split_stock_tabs.html").as_uri()
        for layout in ("table", "grid"):
            with (
                self.subTest(layout=layout),
                mock.patch.object(worker, "_open_target_order", side_effect=lambda *args, **kwargs: self.driver.get(fixture + "?layout=" + layout)),
                mock.patch.object(worker, "_require_crm_order_ready_once_with_refresh"),
                mock.patch.object(worker, "refresh_if_crm_challenge_attempts_exceeded", return_value=False),
                mock.patch.object(worker, "_nj_stock_order_known", return_value=False),
                mock.patch.object(worker, "_saved_shipping_bypass_confirmation", return_value=None),
                mock.patch.object(worker, "_historical_shipping_bypass_po_exists", return_value=False),
                mock.patch.object(worker, "_publish_status"),
                mock.patch.object(worker.time, "sleep"),
                mock.patch.object(worker, "_process_open_order", side_effect=lambda *args, **kwargs: worker._result("5000001", True, "shipping_bypass_ordered", "Fixture purchase")) as process,
            ):
                results = worker._run_order_with_drivers(self.driver, object(), "5000001")
                self.assertEqual([call.kwargs["stock_tab_index"] for call in process.call_args_list], [4, 6])
                self.assertEqual([item["stock_tab_index"] for item in results if item["outcome"] == "already_stock_ordered"], [1, 2, 3, 5, 7, 8, 9])
                for item in results:
                    if item["outcome"] == "already_stock_ordered":
                        self.assertNotEqual(item["order"]["active_panel_stock_orders"][0]["po"], item["order"]["po"])

    def test_active_stock_records_ignore_hidden_tabs_notes_and_modal_drafts(self):
        fixture = (ROOT / "tests" / "fixtures" / "crm_split_stock_tabs.html").as_uri()
        self.driver.get(fixture)
        with mock.patch.object(worker.time, "sleep"), mock.patch.object(worker, "refresh_if_crm_challenge_attempts_exceeded", return_value=False):
            worker._activate_stock_tab(self.driver, 3)
            order = worker._extract_order_data(self.driver, "5000001")
        self.assertEqual(order["active_panel_stock_orders"], [])
        self.assertFalse(order["active_panel_stock_ordered"])

    def test_active_stock_record_does_not_depend_on_yellow_background(self):
        fixture = (ROOT / "tests" / "fixtures" / "crm_split_stock_tabs.html").as_uri()
        self.driver.get(fixture)
        self.driver.execute_script("document.querySelectorAll('.stock-row').forEach(row => row.style.background = 'rgb(30,30,30)');")
        with mock.patch.object(worker, "refresh_if_crm_challenge_attempts_exceeded", return_value=False):
            order = worker._extract_order_data(self.driver, "5000001")
        self.assertEqual(order["active_panel_stock_orders"], [{"vendor": "Sanmar", "po": "H-Source501-I501"}])
        self.assertTrue(order["active_panel_stock_ordered"])


if __name__ == "__main__":
    unittest.main()
