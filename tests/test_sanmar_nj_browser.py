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


@unittest.skipUnless(os.environ.get("SANMAR_FIXTURE_CHROMEDRIVER"), "Set SANMAR_FIXTURE_CHROMEDRIVER to run local browser fixtures")
class NjLocalBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        profile_root = ROOT / "runtime" / "generated_profiles"
        profile_root.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="nj_checkout_fixture_", dir=profile_root)
        assert Path(cls.temp.name).resolve().is_relative_to(profile_root.resolve())
        options = webdriver.ChromeOptions()
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

    def test_real_pickup_selection_and_customer_po_for_both_destinations(self):
        for destination in ("inhouse", "mach6"):
            with self.subTest(destination=destination), mock.patch.object(worker.time, "sleep"):
                self.driver.get(self.fixture + "?page=checkout")
                shipping = worker._select_shipping_destination(self.driver, destination, worker.NJ_WAREHOUSE)
                self.assertEqual(shipping["ship_mode"], "pickup")
                self.assertEqual(self.driver.execute_script("return window.fixtureFulfillment;"), {"pickup": True, "warehouse": "Robbinsville, NJ"})
                self.assertEqual(worker._fill_review_and_submit(self.driver, "H-CJVillafra302", dry_run=True), "dry_run_review_ready")
                self.assertEqual(self.driver.execute_script("return document.getElementById('po').value;"), "H-CJVillafra302")
                self.assertNotIn("UPS", self.driver.execute_script("return document.body.innerText;"))

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


if __name__ == "__main__":
    unittest.main()
