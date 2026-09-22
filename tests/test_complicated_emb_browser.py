"""Offline Chrome checks for the reachout feedback picker."""
import os
import tempfile
import unittest
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

ROOT = Path(__file__).resolve().parents[1]


class ComplicatedEmbBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda p: p.stat().st_mtime))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver available.")
        options = webdriver.ChromeOptions()
        for arg in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1000,900"):
            options.add_argument(arg)
        cls.profile = tempfile.TemporaryDirectory(prefix="emb-popup-test-", dir=ROOT / "tmp")
        cls.addClassCleanup(cls.profile.cleanup)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def show_popup(self):
        self.driver.get("about:blank")
        source = (ROOT / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        choices = source[source.index("const REACHOUT_ORDER_AUTOMATIONS"):source.index("const STOCK_ISSUE_AUTOMATIONS")]
        popup = source[source.index("function showOrderAutomationConfirmation"):source.index("function createOrderProcessMenuControl")]
        self.driver.execute_script('''
            window.queued = [];
            window.queueManualOrderAutomation = (...args) => window.queued.push(args[0].key);
        ''' + choices + popup + '''
            window.embChoice = REACHOUT_ORDER_AUTOMATIONS[0];
            showOrderAutomationConfirmation(window.embChoice, null, null);
        ''')

    def test_choices_queue_immediately_and_back_abandons(self):
        for label, expected in (("Yes", ["complicated_emb_feedback"]), ("No", ["complicated_emb_to_hdd"]), ("Back", [])):
            with self.subTest(label=label):
                self.show_popup()
                self.assertEqual(self.driver.execute_script("return window.embChoice.label"), "Complicated EMB")
                dialog = self.driver.find_element(By.CSS_SELECTOR, '[role="dialog"]')
                self.assertEqual(dialog.get_attribute("aria-label"), "Feedback required?")
                self.assertNotIn("Queue task", dialog.text)
                self.assertFalse(dialog.find_elements(By.TAG_NAME, "textarea"))
                self.assertEqual(self.driver.execute_script("return window.queued"), [])
                dialog.find_element(By.XPATH, f'.//button[text()="{label}"]').click()
                self.assertEqual(self.driver.execute_script("return window.queued"), expected)
                self.assertFalse(self.driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]'))


if __name__ == "__main__":
    unittest.main()
