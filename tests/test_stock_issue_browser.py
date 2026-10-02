"""Offline Chrome checks for stock selections; no CRM or messaging connections."""

import os
import tempfile
import unittest
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[1]


def product(color="Black", sizes=None):
    return {
        "style": "5040", "description": "Bayside Performance T-Shirts", "color": color,
        "available_sizes": ["S", "M", "L", "XL"] if sizes is None else sizes,
        "tab_numbers": [1], "design_item_ids": ["design-item-1"], "total_quantity": 12,
    }


class StockIssueBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda path: tuple(
                    int(number) for number in next(part for part in path.parts if part.count(".") == 3).split(".")
                )))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver available for offline browser tests.")
        artifact_dir = ROOT / "runtime/debug/stock_issue_tests"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifact_dir)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for arg in (
            "--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1000,900",
        ):
            options.add_argument(arg)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)
        source = (ROOT / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        cls.functions = (
            source[source.index("function stockIssueCleanText"):source.index("const EXTRA_PRINT_AREAS")]
            + source[source.index("function stockIssueDetectedSizes"):source.index("function readComplicatedEmbMethods")]
        )

    def show_dialog(self, products=None, workflow="stock_issue_extension", pending=False):
        self.driver.get("about:blank")
        self.driver.execute_script("""
            window.queued = [];
            window.queueManualOrderAutomation = (...args) => {
                window.queued.push({key: args[0].key, data: args[4]});
                if (arguments[2]) return new Promise(resolve => window.finishQueue = resolve);
                return Promise.resolve({success: true});
            };
        """ + self.functions + """
            showStockIssueProductDialog(arguments[0], {key: arguments[1], label: 'Stock Issue'}, null, null);
        """, [product()] if products is None else products, workflow, pending)

    def queue_button(self):
        return self.driver.find_element(By.XPATH, '//button[text()="Queue task" or text()="Queuing…"]')

    def size(self, product_index, size_index):
        return self.driver.find_element(By.ID, f"crm-stock-issue-size-{product_index}-{size_index}")

    def select_product(self, index):
        self.driver.find_element(By.CSS_SELECTOR, f'tbody > tr > td > input[value="{index}"]').click()

    def enter_days(self, days="5"):
        self.driver.find_element(By.ID, "crm-stock-issue-days").send_keys(days)

    def queued_data(self):
        return self.driver.execute_script("return window.queued[0].data")

    def test_all_sizes_start_selected_and_are_sent_to_the_queue(self):
        self.show_dialog()
        self.assertTrue(all(self.size(0, index).is_selected() for index in range(4)))
        self.assertFalse(self.queue_button().is_enabled())
        self.enter_days()
        self.assertTrue(self.queue_button().is_enabled())
        self.queue_button().click()
        self.assertEqual(self.queued_data()["products"][0]["affected_sizes"], ["S", "M", "L", "XL"])
        self.assertEqual(self.queued_data()["days"], 5)
        self.assertFalse(self.driver.find_elements(By.ID, "crm-stock-issue-dialog"))

    def test_clear_requires_a_new_size_selection_and_queues_only_that_size(self):
        self.show_dialog()
        self.enter_days()
        self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Clear sizes for 5040 in Black"]').click()
        self.assertFalse(any(self.size(0, index).is_selected() for index in range(4)))
        self.assertFalse(self.queue_button().is_enabled())
        self.assertIn("Select at least one affected size", self.driver.find_element(By.ID, "crm-stock-issue-validation").text)
        self.size(0, 3).click()
        self.assertTrue(self.queue_button().is_enabled())
        self.queue_button().click()
        self.assertEqual(self.queued_data()["products"][0]["affected_sizes"], ["XL"])

    def test_multiple_products_keep_independent_sizes_when_toggled(self):
        self.show_dialog([product(), product(color="Navy", sizes=["M", "L"])])
        self.enter_days()
        self.assertFalse(self.queue_button().is_enabled())
        self.select_product(0)
        self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Clear sizes for 5040 in Black"]').click()
        self.size(0, 3).click()
        self.select_product(1)
        self.assertTrue(all(self.size(1, index).is_selected() for index in range(2)))
        self.size(1, 1).click()
        self.select_product(0)
        self.select_product(0)
        self.assertTrue(self.size(0, 3).is_selected())
        self.assertFalse(self.size(0, 0).is_selected())
        self.queue_button().click()
        selected = self.queued_data()["products"]
        self.assertEqual([(item["color"], item["affected_sizes"]) for item in selected], [("Black", ["XL"]), ("Navy", ["M"])])

    def test_unselected_product_is_excluded_and_missing_sizes_block_selection(self):
        self.show_dialog([product(), product(color="Navy", sizes=[])])
        self.enter_days()
        self.select_product(0)
        self.assertTrue(self.queue_button().is_enabled())
        self.select_product(1)
        self.assertFalse(self.queue_button().is_enabled())
        self.assertIn("No order sizes could be detected", self.driver.find_element(By.ID, "crm-stock-issue-dialog").text)
        self.select_product(1)
        self.queue_button().click()
        self.assertEqual(len(self.queued_data()["products"]), 1)
        self.assertEqual(self.queued_data()["products"][0]["color"], "Black")

    def test_selections_are_frozen_while_queuing_and_restored_on_failure(self):
        self.show_dialog(pending=True)
        self.enter_days()
        self.queue_button().click()
        self.assertTrue(all(not element.is_enabled() for element in self.driver.find_elements(By.TAG_NAME, "input")))
        clear = self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Clear sizes for 5040 in Black"]')
        self.assertFalse(clear.is_enabled())
        self.driver.execute_script("window.finishQueue({success: false, message: 'Local test failure'});")
        WebDriverWait(self.driver, 5).until(lambda _: self.queue_button().is_enabled())
        self.assertTrue(clear.is_enabled())
        self.assertTrue(all(self.size(0, index).is_selected() for index in range(4)))
        self.assertIn("Local test failure", self.driver.find_element(By.ID, "crm-stock-issue-validation").text)
        self.assertEqual(len(self.driver.execute_script("return window.queued")), 1)

    def test_other_stock_dialogs_keep_their_existing_selection_defaults(self):
        self.show_dialog(workflow="stock_issue_size")
        self.assertFalse(any(self.size(0, index).is_selected() for index in range(4)))
        self.assertFalse(self.driver.find_elements(By.XPATH, '//button[text()="Clear"]'))
        self.show_dialog([product(sizes=["XL"])], workflow="stock_issue_size")
        self.assertTrue(self.size(0, 0).is_selected())
        self.show_dialog(workflow="stock_issue_color")
        self.assertFalse(self.driver.find_elements(By.CSS_SELECTOR, '[id^="crm-stock-issue-size-"]'))

    def test_scanner_detects_ordered_sizes_and_unions_matching_products(self):
        self.driver.get((ROOT / "tests/fixtures/stock_issue_crm.html").as_uri())
        scanned = self.driver.execute_script(self.functions + "return scanCurrentStockIssueDesignTab(1);")
        self.assertEqual(scanned[0]["available_sizes"], ["S", "XL"])
        self.assertEqual(scanned[1]["available_sizes"], ["M"])
        combined = self.driver.execute_script(self.functions + "return deduplicateStockIssueProducts(arguments[0]);", scanned)
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["available_sizes"], ["S", "XL", "M"])
        self.assertEqual(combined[0]["total_quantity"], 4)


if __name__ == "__main__":
    unittest.main()
