"""Offline Chrome checks for the reachout feedback picker."""
import os
import tempfile
import unittest
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workers"))
import complicated_emb

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

    def show_popup(self, designs=None):
        self.driver.get("about:blank")
        source = (ROOT / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        choices = source[source.index("const REACHOUT_ORDER_AUTOMATIONS"):source.index("const STOCK_ISSUE_AUTOMATIONS")]
        popup = source[source.index("function showOrderAutomationConfirmation"):source.index("function createOrderProcessMenuControl")]
        self.driver.execute_script('''
            window.queued = [];
            window.queueManualOrderAutomation = (...args) => window.queued.push({key: args[0].key, data: args[4]});
            window.scanComplicatedEmbDesigns = async () => arguments[0];
        ''' + choices + popup + '''
            window.embChoice = REACHOUT_ORDER_AUTOMATIONS[0];
            showOrderAutomationConfirmation(window.embChoice, null, null);
        ''', designs or [{"tab_number": 2, "design_name": "Schuti Hats"}])
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.CSS_SELECTOR, 'input[data-emb-tab]'))

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
                self.assertEqual(self.driver.execute_script("return window.queued.map(item => item.key)"), expected)
                if expected:
                    self.assertEqual(self.driver.execute_script("return window.queued[0].data.designs"), [{"tab_number": 2, "design_name": "Schuti Hats"}])
                self.assertFalse(self.driver.find_elements(By.CSS_SELECTOR, '[role="dialog"]'))

    def test_multiple_designs_require_selection_and_share_one_email(self):
        designs = [{"tab_number": 1, "design_name": "A"}, {"tab_number": 3, "design_name": "B"}]
        self.show_popup(designs)
        yes = self.driver.find_element(By.XPATH, '//button[text()="Yes"]')
        no = self.driver.find_element(By.XPATH, '//button[text()="No"]')
        self.assertFalse(yes.is_enabled())
        self.assertFalse(no.is_enabled())
        for checkbox in self.driver.find_elements(By.CSS_SELECTOR, 'input[data-emb-tab]'):
            checkbox.click()
        yes.click()
        self.assertEqual(self.driver.execute_script('return window.queued'), [{"key": "complicated_emb_feedback", "data": {"designs": designs}}])

    def test_rich_text_replacement_preserves_links_and_literal_names(self):
        self.driver.get('about:blank')
        self.driver.execute_script('''document.body.innerHTML = '<div contenteditable="true"><p>Review <b>[DE</b><i>SIGN]</i> and [DESIGN].</p><a href="https://example.com">Keep link</a></div>';''')
        name = 'A & <Hat> $& and B'
        self.assertEqual(complicated_emb.replace_design_placeholder(self.driver, name), 2)
        body = self.driver.find_element(By.CSS_SELECTOR, '[contenteditable]').text
        self.assertEqual(body.count(name), 2)
        self.assertEqual(self.driver.find_element(By.TAG_NAME, 'a').get_attribute('href'), 'https://example.com/')
        self.assertFalse(self.driver.find_elements(By.TAG_NAME, 'hat'))

    def test_method_reader_includes_other_and_mixed_methods(self):
        self.driver.get('about:blank')
        reader = (ROOT / 'crm-order-dark-mode-extension/emb-design-reader.js').read_text(encoding='utf-8')
        rows = self.driver.execute_script('''
          const designs = ['Embroidery', 'HDD', 'EMB', '', 'Screen Printing', 'HD Digital'].map(method => ({printAreas: [{printMethodDescription: method}]}));
          designs.push({printAreas: [{printMethodDescription: 'Embroidery'}, {printMethodDescription: 'Screen Printing'}]});
          designs.push({crudAction: 'd', printAreas: [{printMethodDescription: 'Embroidery'}]});
          designs.push({printAreas: [{crudAction: 'd', printMethodDescription: 'Embroidery'}]});
          designs.push({printAreas: []});
          const scope = {copyOrder() {}, order: {getResource: () => ({designs})}};
          window.angular = {element: () => ({scope: () => scope})};
        ''' + reader + '\nreturn readEmbDesignMethods();')
        self.assertEqual([row['tab_number'] for row in rows if row['eligible']], [1, 2, 3, 5, 6, 7])

    def test_scan_and_worker_include_ink_and_mixed_methods(self):
        self.driver.get('about:blank')
        source = (ROOT / 'crm-order-dark-mode-extension/content.js').read_text(encoding='utf-8')
        helpers = source[source.index('function stockIssueCleanText'):source.index('const EXTRA_PRINT_AREAS')]
        scan = source[source.index('function readComplicatedEmbMethods'):source.index('function showOrderAutomationConfirmation')]
        reader = (ROOT / 'crm-order-dark-mode-extension/emb-design-reader.js').read_text(encoding='utf-8')
        bridge = (ROOT / 'crm-order-dark-mode-extension/emb-design-bridge.js').read_text(encoding='utf-8')
        self.driver.execute_script('''
          document.body.innerHTML = '<div id="main-header-design-tabs"></div><p id="design-detail">Design Name: Hat 2 | admin</p>';
          for (let number = 1; number <= 3; number++) {
            const button = document.createElement('button');
            button.textContent = `${number} - QTY: 20 Design Previews`;
            button.setAttribute('aria-selected', String(number === 2));
            button.onclick = () => {
              for (const other of document.querySelectorAll('button')) other.setAttribute('aria-selected', 'false');
              button.setAttribute('aria-selected', 'true');
              document.getElementById('design-detail').textContent = `Design Name: Hat ${number} | admin`;
            };
            document.getElementById('main-header-design-tabs').append(button);
          }
          const designs = ['Embroidery', 'HDD', 'Embroidery'].map(method => ({printAreas: [{printMethodDescription: method}]}));
          designs[2].printAreas.push({printMethodDescription: 'Screen Printing'});
          const scope = {copyOrder() {}, order: {getResource: () => ({designs})}};
          window.angular = {element: () => ({scope: () => scope})};
        ''' + reader + bridge + helpers + scan + '''
          window.scanResult = null;
          scanComplicatedEmbDesigns(() => {}).then(rows => window.scanResult = rows).catch(error => window.scanResult = {error: error.message});
        ''')
        rows = WebDriverWait(self.driver, 15).until(lambda d: d.execute_script('return window.scanResult'))
        self.assertEqual(rows, [{"tab_number": 1, "design_name": "Hat 1"}, {"tab_number": 2, "design_name": "Hat 2"},
                                {"tab_number": 3, "design_name": "Hat 3"}])
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, 'button[aria-selected="true"]').text, '2 - QTY: 20 Design Previews')
        # Changing a selected design to another ink method before the queued run
        # must still resolve the same tabs and names.
        self.driver.execute_script('''
          window.angular.element(document.body).scope().order.getResource().designs[1]
            .printAreas[0].printMethodDescription = 'Screen Printing';
        ''')
        self.assertEqual(complicated_emb.resolve_designs(self.driver, rows[1:]), rows[1:])


if __name__ == "__main__":
    unittest.main()
