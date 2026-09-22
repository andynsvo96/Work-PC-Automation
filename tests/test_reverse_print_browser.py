"""Offline browser checks. Set CHROMEDRIVER to a local driver when needed."""
import os
import tempfile
from pathlib import Path
import unittest

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from workers import crm_reverse_prints as reverse

ROOT = Path(__file__).resolve().parents[1]


class ReversePrintBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda p: tuple(int(n) for n in next(part for part in p.parts if part.count('.') == 3).split('.'))))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver available for offline browser tests.")
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--disable-background-networking")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1000,900")
        cls.profile = tempfile.TemporaryDirectory(prefix="reverse-print-test-", dir=ROOT / "tmp")
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        try:
            cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        except Exception:
            cls.profile.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.profile.cleanup()

    def test_real_style_sub_picker_apply_vendor_and_retry(self):
        driver = self.driver
        driver.get((ROOT / "tests/fixtures/reverse_print_crm.html").as_uri())
        selections = [{"tab_number": 1, "reverse_unit_price": "14.00"}]
        mutation = reverse.apply_reverse_prints(driver, selections)
        self.assertTrue(reverse.verify_reverse_prints(driver, mutation))
        self.assertEqual(driver.execute_script("return events"), ["clone", "pick:Style Sub", "apply", "pick:Sanmar", "pick:Style Sub", "apply", "pick:Sanmar"])
        clone = reverse.read_designs(driver)[1]
        self.assertEqual([x["color"] for x in clone["items"]], ["Deep Red/ Black", "White"])
        # Simulate a previously saved, partially completed clone and retry.
        driver.execute_script("resource.designs[1].designItems[0].sizes[2].quantity=4;resource.designs[1].designItems[1].sizes[2].pricePerPiece='33.09';events=[]")
        retry = reverse.apply_reverse_prints(driver, selections)
        self.assertFalse(retry["jobs"][0]["created"])
        self.assertTrue(reverse.verify_reverse_prints(driver, retry))
        self.assertEqual(driver.execute_script("return events"), [])
        self.assertEqual(len(reverse.read_designs(driver)), 2)
        driver.execute_script("resource.designs[1].designItems[0].styleSubs[0]={vendor:'',style:'',color:'',description:''};events=[]")
        incomplete = reverse.apply_reverse_prints(driver, selections)
        self.assertTrue(reverse.verify_reverse_prints(driver, incomplete))
        self.assertEqual(driver.execute_script("return events"), ["pick:Sanmar"])

    def test_popup_exclusive_categories_and_shared_reverse_price(self):
        driver = self.driver
        driver.get("about:blank")
        content = (ROOT / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        functions = content[content.index("const EXTRA_PRINT_AREAS"):content.index("function stockIssueDetectedSizes")]
        driver.execute_script('''
window.visibleStockIssueDesignTabs=()=>[{tabNumber:1,quantity:8},{tabNumber:2,quantity:2},{tabNumber:3,quantity:1000}];
window.queueManualOrderAutomation=async (...args)=>{window.queued=args[4];return {success:true}};
''' + functions + '\nshowSleevePrintsDialog({},null,null);')
        def control(label):
            return driver.find_element(By.CSS_SELECTOR, f'[aria-label="{label}"]')
        self.assertFalse(control("Reversible Prints for tab 1").is_displayed())
        for tab in (1, 2):
            control(f"Include design tab {tab}").click()
            control(f"Reversible Prints for tab {tab}").click()
        self.assertFalse(control("Print location for tab 1").is_displayed())
        self.assertFalse(control("Print method for tab 1").is_displayed())
        self.assertEqual(control("Reversible ink price per area").get_attribute("value"), "7.00")
        price = control("Reversible ink price per area")
        price.send_keys(Keys.CONTROL, "a")
        price.send_keys("4.25")
        self.assertEqual(price.get_attribute("value"), "4.25")
        self.assertEqual(len([x for x in driver.find_elements(By.CSS_SELECTOR, 'input[inputmode="decimal"]') if x.is_displayed()]), 1)
        control("Sleeve Prints for tab 1").click()
        self.assertFalse(control("Reversible Prints for tab 1").is_selected())
        self.assertTrue(control("Print location for tab 1").is_displayed())
        control("Reversible Prints for tab 1").click()
        driver.find_element(By.XPATH, "//button[text()='Queue task']").click()
        payload = driver.execute_script("return window.queued")
        self.assertEqual(payload["reverse_price"], 4.25)
        self.assertEqual(payload["sleeves"], [{"tab_number": 1, "quantity": 8, "reverse": "ink"}, {"tab_number": 2, "quantity": 2, "reverse": "ink"}])

    def test_popup_shared_ink_price_quantity_changes_and_validation(self):
        from selenium.webdriver.support.ui import Select
        driver = self.driver
        driver.get("about:blank")
        content = (ROOT / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        functions = content[content.index("const EXTRA_PRINT_AREAS"):content.index("function stockIssueDetectedSizes")]
        driver.execute_script('''
window.visibleStockIssueDesignTabs=()=>[{tabNumber:1,quantity:19},{tabNumber:2,quantity:1},{tabNumber:3,quantity:1000}];
window.queueManualOrderAutomation=async (...args)=>{window.queued=args[4];return {success:true}};
''' + functions + '\nshowSleevePrintsDialog({},null,null);')
        def control(label):
            return driver.find_element(By.CSS_SELECTOR, f'[aria-label="{label}"]')
        def visible_prices():
            return [x for x in driver.find_elements(By.CSS_SELECTOR, 'input[inputmode="decimal"]') if x.is_displayed()]
        self.assertEqual(len(visible_prices()), 0)
        for tab in (1, 2):
            control(f"Include design tab {tab}").click()
            control(f"Sleeve Prints for tab {tab}").click()
            Select(control(f"Print location for tab {tab}")).select_by_value("right")
        price = control("Sleeve / side ink price per area")
        self.assertEqual(len(visible_prices()), 1)
        self.assertEqual(price.get_attribute("value"), "6.00")
        self.assertIn("applies to tabs 1, 2", price.find_element(By.XPATH, "..").text)
        control("Include design tab 2").click()
        self.assertEqual(price.get_attribute("value"), "7.00")
        control("Include design tab 2").click()
        control("Side Prints for tab 2").click()
        Select(control("Print location for tab 2")).select_by_value("both")
        self.assertEqual(price.get_attribute("value"), "6.00")
        self.assertEqual(len(visible_prices()), 1)
        price.send_keys(Keys.CONTROL, "a")
        price.send_keys("bad")
        queue = driver.find_element(By.XPATH, "//button[text()='Queue task']")
        self.assertFalse(queue.is_enabled())
        price.send_keys(Keys.CONTROL, "a")
        price.send_keys("4.25")
        Select(control("Print method for tab 2")).select_by_value("embroidery")
        self.assertEqual(len(visible_prices()), 2)
        self.assertEqual(control("Embroidery price per area").get_attribute("value"), "15.00")
        Select(control("Print method for tab 2")).select_by_value("ink")
        self.assertEqual(len(visible_prices()), 1)
        self.assertEqual(price.get_attribute("value"), "4.25")
        queue.click()
        payload = driver.execute_script("return window.queued")
        self.assertEqual(payload["ink_price"], 4.25)
        self.assertEqual(payload["sleeves"], [{"tab_number": 1, "quantity": 19, "right": "ink"}, {"tab_number": 2, "quantity": 1, "side_left": "ink", "side_right": "ink"}])

    def test_bulk_vendor_from_isolated_inventory_panel(self):
        driver = self.driver
        driver.get((ROOT / "tests/fixtures/reverse_print_crm.html").as_uri())
        driver.execute_script("resource.designs[0].designItems.forEach(item=>delete item.vendorName);window.inventoryVendors=[['Sanmar (Bulk)', 'sanmar']];render();")
        self.assertEqual([item["vendor"] for item in reverse.read_designs(driver)[0]["items"]], ["Sanmar", "Sanmar"])
        mutation = reverse.apply_reverse_prints(driver, [{"tab_number": 1, "reverse_unit_price": "14.00"}])
        self.assertTrue(reverse.verify_reverse_prints(driver, mutation))
        self.assertEqual(driver.execute_script("return events.filter(e=>e.startsWith('pick:Sanmar'))"), ["pick:Sanmar", "pick:Sanmar"])
        # A different tab's vendor cannot fill a missing original vendor.
        driver.execute_script("window.inventoryVendors=[[],['Sanmar (Bulk)']];render();")
        self.assertEqual(reverse.read_designs(driver)[0]["items"][0]["vendor"], "")
        driver.execute_script("window.inventoryVendors=[['Sanmar (Bulk)','Other Vendor']];render();")
        self.assertEqual(reverse.read_designs(driver)[0]["items"][0]["vendor"], "")


if __name__ == "__main__":
    unittest.main()
