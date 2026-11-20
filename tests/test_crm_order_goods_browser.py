"""Run Order Goods only against a local fixture and isolated Chrome profile."""

import os
import re
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

import crm_order_goods as worker  # noqa: E402


class StockOrderingBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda path: next(
                    (tuple(map(int, part.split('.'))) for part in path.parts if re.fullmatch(r"\d+(?:\.\d+){3}", part)), ()
                )))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver for offline Order Goods verification.")
        artifact_dir = ROOT / "runtime/debug/order_goods_tests"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifact_dir)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for flag in ("--headless=new", "--disable-background-networking", "--disable-sync", "--disable-gpu", "--no-sandbox", "--no-first-run", "--window-size=1200,900"):
            options.add_argument(flag)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)
        cls.fixture_url = (ROOT / "tests/fixtures/crm_order_goods_tabs.html").as_uri()

    def setUp(self):
        self.driver.get(self.fixture_url)
        self.driver.execute_script("sessionStorage.clear()")
        self.driver.refresh()
        status = mock.patch.object(worker, "_publish_status")
        status.start()
        self.addCleanup(status.stop)

    def test_finds_all_designs_with_view_proofs_and_duplicate_labels(self):
        tabs = worker._find_stock_tabs(self.driver)
        self.assertEqual(len(tabs), 4)
        self.assertIn("View Proofs", tabs[1]["label"])
        self.assertEqual(tabs[2]["label"], tabs[3]["label"])

    def test_po_detection_uses_visible_records_and_ignores_notes_and_drafts(self):
        for layout in ("table", "grid"):
            with self.subTest(layout=layout):
                self.driver.get(self.fixture_url + "?layout=" + layout)
                for index, expected in enumerate((True, True, False, False)):
                    self.assertIsNotNone(worker._activate_stock_tab(self.driver, index))
                    self.assertEqual(worker._active_stock_tab_has_purchase_order(self.driver), expected)

    def test_orders_remaining_tabs_and_revisits_new_sizes_on_a_later_run(self):
        # CRM navigation resets the active tab, as it does on the real order.
        with mock.patch.object(worker, "_open_target_order_with_refresh", side_effect=lambda *_a, **_k: self.driver.refresh()):
            results = worker._order_goods_for_all_stock_tabs(self.driver, "5000001")
            self.assertEqual([item["outcome"] for item in results], [
                "already_stock_ordered", "order_goods_clicked", "order_goods_clicked", "order_goods_clicked",
            ])
            self.assertEqual([item["stock_tab_index"] for item in results], [1, 2, 3, 4])
            self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4])

            self.driver.execute_script("addSizeToDesign(4)")
            results = worker._order_goods_for_all_stock_tabs(self.driver, "5000001")
            self.assertEqual([item["outcome"] for item in results], [
                "already_stock_ordered", "already_stock_ordered", "already_stock_ordered", "order_goods_clicked",
            ])
            self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [2, 3, 4, 4])

    def test_dry_run_checks_all_tabs_without_clicking_order_goods(self):
        results = worker._order_goods_for_all_stock_tabs(self.driver, "5000001", dry_run=True)
        self.assertEqual([item["outcome"] for item in results], [
            "already_stock_ordered", "order_goods_ready", "order_goods_ready", "order_goods_ready",
        ])
        self.assertEqual(self.driver.execute_script("return stockFixtureState().clicks"), [])


if __name__ == "__main__":
    unittest.main()
