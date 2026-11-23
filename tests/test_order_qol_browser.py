"""Preview retention and native Order All with local markup and stubbed CRM services."""

import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "crm-order-dark-mode-extension"


class OrderQolBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            paths = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if paths:
                driver_path = str(max(paths, key=lambda path: tuple(int(value) for value in
                    next(part for part in path.parts if part.count(".") == 3).split("."))))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver for offline QOL browser checks.")
        fixture = (ROOT / "tests/fixtures/order_qol_crm.html").read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(fixture)

            def log_message(self, *_args):
                pass

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.httpd.shutdown)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.artifacts = ROOT / "runtime/debug/order_qol_tests"
        cls.artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=cls.artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for arg in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1200,1200"):
            options.add_argument(arg)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def show(self, route="/order/1234567"):
        self.driver.get("about:blank")
        self.driver.get(self.url + route)
        self.driver.execute_script("sessionStorage.clear(); fixtureResetStock();")
        self.inject()

    def inject(self, automation=True):
        for name in ("all-tabs.css", "order-qol.css", "dark-mode.css"):
            self.driver.execute_script("const style = document.createElement('style'); style.textContent = arguments[0]; document.head.append(style);",
                                       (EXTENSION / name).read_text(encoding="utf-8"))
        scripts = (["content.js"] if automation else []) + ["all-tabs-model.js", "all-tabs.js", "order-all-batch.js", "order-all.js", "order-edit-previews.js"]
        self.driver.execute_script("\n".join((EXTENSION / name).read_text(encoding="utf-8") for name in scripts))

    def wait_for_button(self):
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#crm-order-all-header button"))
        return self.driver.find_element(By.CSS_SELECTOR, "#crm-order-all-header button")

    def edit(self):
        self.driver.find_element(By.CSS_SELECTOR, ".edit-btn").click()
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-edit-design-previews"))

    def test_automation_refresh_guard_preserves_native_edits_notes_and_dialogs(self):
        self.show()
        script = (EXTENSION / "order-refresh.mjs").read_text(encoding="utf-8").replace("export function", "function")
        def safe():
            return self.driver.execute_script(script + "\nreturn canRefreshOrderPage('1234567');")
        self.assertTrue(safe())
        self.edit()
        self.assertFalse(safe())
        self.driver.execute_script("orderScope.saveOrder(); resource.addSalesNotes = 'Keep this draft';")
        self.assertFalse(safe())
        self.driver.execute_script("resource.addSalesNotes = ''; const dialog = document.createElement('div'); dialog.id = 'refresh-dialog'; dialog.setAttribute('role', 'dialog'); dialog.textContent = 'Open input'; document.body.append(dialog);")
        self.assertFalse(safe())
        self.driver.execute_script("document.getElementById('refresh-dialog').remove();")
        self.assertTrue(safe())

    def test_previews_remain_at_bottom_and_follow_highlighted_design_without_model_edits(self):
        self.show()
        original = self.driver.execute_script("return structuredClone(resource);")
        self.assertEqual(self.driver.find_elements(By.ID, "crm-edit-design-previews"), [])
        self.edit()
        panel = self.driver.find_element(By.ID, "crm-edit-design-previews")
        self.assertTrue(panel.is_displayed())
        self.assertEqual(self.driver.execute_script("return document.querySelector('#view-orders-index').lastElementChild.id;"), panel.get_attribute("id"))
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, ".crm-edit-previews-caption").text, "Highlighted tab 2")
        images = self.driver.find_elements(By.CSS_SELECTOR, ".crm-edit-previews-grid img")
        self.assertEqual(len(images), 5)
        self.assertEqual(self.driver.execute_script("return [...document.querySelectorAll('.crm-edit-previews-grid a')].filter(a => !a.hidden).length;"), 4)
        for image in images:
            self.assertIn("src=20001_", image.get_attribute("src"))
            self.assertIn("width=395", image.get_attribute("src"))
        self.driver.find_elements(By.CSS_SELECTOR, "#main-header-design-tabs button[ng-repeat]")[2].click()
        WebDriverWait(self.driver, 5).until(lambda d: d.find_element(By.CSS_SELECTOR, ".crm-edit-previews-caption").text.endswith("3"))
        self.assertIn("src=20002_f", self.driver.find_element(By.CSS_SELECTOR, ".crm-edit-previews-grid img").get_attribute("src"))
        self.assertEqual(self.driver.execute_script("return resource;"), original)
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-edit-design-previews"))
        self.assertTrue(self.driver.find_element(By.ID, "native-preview").is_displayed())

    def test_preview_preserves_approval_vendor_transfer_and_deleted_design_restrictions(self):
        self.show()
        self.edit()
        for expression in ("resource.features.approved = true", "orderScope.transfersOnly = true",
                           "fixtureVendor = true", "resource.designs[1].crudAction = 'd'", "orderScope.user = null"):
            self.driver.execute_script(expression + "; document.body.classList.toggle('changed');")
            WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-edit-design-previews"))
            self.driver.execute_script("resource.features.approved = false; orderScope.transfersOnly = false; fixtureVendor = false; delete resource.designs[1].crudAction; orderScope.user = {isAny: role => fixtureVendor && role === 'vendor'}; document.body.classList.toggle('changed');")
            WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-edit-design-previews"))
        self.driver.execute_script("fixtureVendor = true; resource.features.releasedToVendor = 1;")
        self.assertTrue(self.driver.find_element(By.ID, "crm-edit-design-previews").is_displayed())

    def test_previews_load_directly_in_edit_mode_and_stay_visible_in_all_tabs(self):
        self.show("/order/1234567?edit=1")
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-edit-design-previews"))
        self.driver.find_element(By.ID, "crm-all-tabs-header").click()
        self.assertTrue(self.driver.find_element(By.ID, "crm-edit-design-previews").is_displayed())
        self.assertTrue(self.driver.find_element(By.ID, "crm-all-tabs-panel").is_displayed())
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, ".crm-edit-previews-caption").text, "Highlighted tab 2")
        self.assertEqual(self.driver.execute_script("return [orderScope.activeDesignIndex, resource.designs.length];"), [1, 3])

    def test_order_all_submits_all_native_requests_in_one_click_without_automation(self):
        self.show()
        vendor = self.driver.find_element(By.CSS_SELECTOR, '.tab-pane.active tr[ng-repeat="provider in providers"] > :first-child')
        self.assertEqual(vendor.tag_name, "th")
        self.assertEqual(vendor.text, "Sanmar / S&S Activewear")
        button = self.wait_for_button()
        self.assertIn("tabs 2, 3", button.get_attribute("title"))
        self.assertEqual(self.driver.execute_script("return document.querySelector('#main-header-design-tabs .btn-group').lastElementChild.id;"), "crm-order-all-header")
        self.assertEqual(self.driver.execute_script("return document.querySelector('#order-design-details .nav-tabs').lastElementChild.id;"), "crm-order-all-detail")
        self.assertEqual(self.driver.execute_script("return resource.designs.length;"), 3)
        self.driver.save_screenshot(str(self.artifacts / "order-all-available.png"))
        button.click()
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return nativeCalls.length;") == 2)
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), ["101", "102"])
        self.assertEqual(self.driver.execute_script("return queueCalls;"), [])
        self.assertTrue(button.get_property("disabled"))
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, "#crm-order-all-detail button").get_property("disabled"))
        self.assertEqual(self.driver.execute_script("return [nativeGoodsClicks, orderScope.activeDesignIndex];"), [0, 1])
        self.driver.execute_script("renderFixture();")
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.assertEqual(self.driver.execute_script("return nativeCalls.length;"), 2)
        self.driver.execute_script("pendingNative.get('101').resolve({succeeded:true,supplier:'Sanmar'});")
        WebDriverWait(self.driver, 5).until(lambda d: "1/2" in d.find_element(By.ID, "crm-order-all-result").text)
        self.assertEqual(self.driver.execute_script("return nativeReloads;"), 0)
        self.driver.execute_script("pendingNative.get('102').resolve({succeeded:true,supplier:'Sanmar'});")
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return nativeReloads;") == 1)
        self.assertIn("confirmed stock ordered on tabs 2, 3", self.driver.find_element(By.ID, "crm-order-all-result").text)
        self.driver.refresh()
        self.inject(automation=False)
        self.assertEqual(self.driver.find_elements(By.ID, "crm-order-all-header"), [])
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), ["101", "102"])

    def test_order_all_accepts_older_vendor_data_cells_and_excludes_manual_order(self):
        self.show()
        self.driver.execute_script("fixtureVendorCellTag = 'td'; renderFixture();")
        button = self.wait_for_button()
        self.assertIn("tabs 2, 3", button.get_attribute("title"))
        vendor = self.driver.find_element(By.CSS_SELECTOR, '.tab-pane.active tr[ng-repeat="provider in providers"] > :first-child')
        self.assertEqual(vendor.tag_name, "td")
        self.driver.execute_script("fixtureStock.disabled = [true,true,true]; renderFixture();")
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-order-all-header"))
        # Manual Order remains enabled; it must not qualify as automatic stock.
        manual = self.driver.find_elements(By.CSS_SELECTOR, '.tab-pane.active tr[ng-repeat="provider in providers"]')[-1]
        self.assertTrue(manual.find_element(By.CSS_SELECTOR, "button").is_enabled())

    def test_order_all_hides_when_no_eligible_stock_and_returns_for_new_stock(self):
        self.show()
        self.wait_for_button()
        self.driver.execute_script("fixtureStock.ordered = [true,true,true]; renderFixture();")
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-order-all-header"))
        self.driver.execute_script("resource.stockStatus = 'Ordered'; fixtureStock.ordered[0] = false; renderFixture();")
        self.assertIn("tabs 1", self.wait_for_button().get_attribute("title"))
        self.driver.execute_script("fixtureStock.disabled[0] = true; renderFixture();")
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-order-all-header"))
        self.driver.execute_script("fixtureStock.disabled[0] = false; fixtureStock.hidden[0] = true; renderFixture();")
        self.assertEqual(self.driver.find_elements(By.ID, "crm-order-all-header"), [])

    def test_order_all_is_absent_in_edit_mode_and_on_non_order_routes(self):
        self.show()
        self.wait_for_button()
        self.edit()
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-order-all-header"))
        for route in ("/report/967", "/order/short", "/app#/reports/orders"):
            self.show(route)
            self.assertEqual(self.driver.find_elements(By.ID, "crm-order-all-header"), [])
            self.driver.execute_script("orderScope.editModeOn();")
            self.assertEqual(self.driver.find_elements(By.ID, "crm-edit-design-previews"), [])
        self.show("/app#/order/1234567")
        self.wait_for_button()
        self.edit()

    def test_refresh_keeps_unconfirmed_error_and_allows_manual_retry_without_auto_submission(self):
        self.show()
        self.wait_for_button().click()
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return nativeCalls.length;") == 2)
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.driver.execute_script("document.querySelector('#crm-order-all-header').remove(); renderFixture();")
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.assertEqual(self.driver.execute_script("return nativeCalls.length;"), 2)
        self.driver.refresh()
        self.inject(automation=False)
        self.assertFalse(self.wait_for_button().get_property("disabled"))
        self.assertIn("page changed before CRM confirmed", self.driver.find_element(By.ID, "crm-order-all-result").text)
        self.assertEqual(self.driver.execute_script("return nativeCalls.length;"), 2)
        self.wait_for_button().click()
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), ["101", "102", "101", "102"])

    def test_native_failure_has_bold_tab_and_manual_retry_skips_confirmed_stock(self):
        self.show()
        self.wait_for_button().click()
        self.driver.execute_script("pendingNative.get('101').resolve({succeeded:true,supplier:'Sanmar'}); pendingNative.get('102').resolve({succeeded:false,errors:['Not enough stock']});")
        WebDriverWait(self.driver, 5).until(lambda d: "Not enough stock" in d.find_element(By.ID, "crm-order-all-result").text)
        result = self.driver.find_element(By.ID, "crm-order-all-result")
        self.assertEqual(result.find_element(By.CSS_SELECTOR, "strong").text, "Tab 3:")
        self.assertNotIn("Check the stock on the listed tabs before ordering again.", result.text)
        self.assertFalse(self.wait_for_button().get_property("disabled"))
        self.assertIn("tabs 3", self.wait_for_button().get_attribute("title"))
        self.assertEqual(self.driver.execute_script("return [nativeCalls.length, queueCalls.length, nativeReloads];"), [2, 0, 0])
        self.driver.save_screenshot(str(self.artifacts / "order-all-retry.png"))
        self.wait_for_button().click()
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), ["101", "102", "102"])
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.driver.execute_script("pendingNative.get('102').resolve({succeeded:true,supplier:'Sanmar'});")
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return nativeReloads;") == 1)

    def test_each_failed_tab_has_its_own_line_and_error_text_is_escaped(self):
        self.show()
        self.wait_for_button().click()
        self.driver.execute_script("pendingNative.get('101').resolve({succeeded:false,errors:['Not enough stock']}); pendingNative.get('102').resolve({succeeded:false,errors:['<img src=x onerror=alert(1)>']});")
        WebDriverWait(self.driver, 5).until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#crm-order-all-result .crm-order-all-error")) == 2)
        rows = self.driver.find_elements(By.CSS_SELECTOR, "#crm-order-all-result .crm-order-all-error")
        self.assertEqual([row.find_element(By.CSS_SELECTOR, "strong").text for row in rows], ["Tab 2:", "Tab 3:"])
        self.assertIn("<img src=x onerror=alert(1)>", rows[1].text)
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, "#crm-order-all-result img"), [])
        self.assertFalse(self.wait_for_button().get_property("disabled"))
        self.wait_for_button().click()
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), ["101", "102", "101", "102"])

    def test_rejected_native_requests_can_be_retried_manually(self):
        self.show()
        self.wait_for_button().click()
        self.driver.execute_script("pendingNative.get('101').reject(new Error('Lost response')); pendingNative.get('102').reject(new Error('Lost response'));")
        WebDriverWait(self.driver, 5).until(lambda d: not self.wait_for_button().get_property("disabled"))
        self.assertIn("No confirmed CRM response.", self.driver.find_element(By.ID, "crm-order-all-result").text)
        self.assertEqual(self.driver.execute_script("return nativeCalls.length;"), 2)
        self.wait_for_button().click()
        self.assertEqual(self.driver.execute_script("return nativeCalls.length;"), 4)

    def test_native_order_all_works_with_no_automation_content_script(self):
        self.driver.get("about:blank")
        self.driver.get(self.url + "/order/1234567")
        self.driver.execute_script("sessionStorage.clear(); fixtureResetStock(); delete window.chrome;")
        self.inject(automation=False)
        self.wait_for_button().click()
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), ["101", "102"])
        self.assertEqual(self.driver.execute_script("return typeof queueManualOrderAutomation;"), "undefined")

    def test_native_locked_flag_is_respected_even_before_dom_disabled_updates(self):
        self.show()
        self.wait_for_button()
        self.driver.execute_script("resource.featureFlags.stockOrderingLocked = true; document.body.classList.toggle('locked');")
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-order-all-header"))
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), [])

    def test_native_ordering_waits_for_other_inventory_loading_or_requests(self):
        self.show()
        self.wait_for_button()
        self.driver.execute_script("inventoryScopes[0].placingOrder = true; renderFixture();")
        WebDriverWait(self.driver, 5).until(lambda d: self.wait_for_button().get_property("disabled"))
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), [])
        self.driver.execute_script("inventoryScopes[0].placingOrder = false; renderFixture();")
        WebDriverWait(self.driver, 5).until(lambda d: not self.wait_for_button().get_property("disabled"))

    def test_confirmed_stock_cannot_repeat_but_new_quantities_can_be_ordered(self):
        self.show()
        self.driver.execute_script("nativeMode = 'success';")
        self.wait_for_button().click()
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return nativeReloads;") == 1)
        self.driver.refresh()
        self.inject(automation=False)
        self.driver.execute_script("fixtureStock.ordered[1] = false; resource.designs[1].designItems[0].pricePerPiece = '90.00'; renderFixture();")
        self.assertEqual(self.driver.find_elements(By.ID, "crm-order-all-header"), [])
        self.driver.execute_script("resource.designs[1].designItems[0].quantity = 2; renderFixture();")
        WebDriverWait(self.driver, 5).until(lambda d: not self.wait_for_button().get_property("disabled"))
        self.wait_for_button().click()
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), ["101", "102", "101"])
        self.assertEqual(self.driver.execute_script("return queueCalls;"), [])

    def test_native_targets_cannot_escape_the_current_order_or_provider(self):
        self.show()
        self.driver.execute_script("""
            inventoryScopes[1].order = () => ({getResource: () => ({id:'7654321'})});
            inventoryScopes[2].providers[0].id = 210;
            renderFixture();
        """)
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-order-all-header"))
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), [])

    def test_an_already_submitted_older_automation_cannot_be_duplicated(self):
        self.show()
        self.wait_for_button()
        self.driver.execute_script("sessionStorage.setItem('crm-order-process-active:1234567','1'); document.body.classList.toggle('previous-task');")
        WebDriverWait(self.driver, 5).until(lambda d: self.wait_for_button().get_property("disabled"))
        self.assertIn("earlier automation", self.wait_for_button().get_attribute("title"))
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), [])

    def test_preview_layout_dark_mode_and_print(self):
        self.show()
        self.edit()
        for dark in (False, True):
            self.driver.execute_script("document.documentElement.classList.toggle('crm-order-dark-mode', arguments[0]);", dark)
            self.driver.save_screenshot(str(self.artifacts / ("preview-dark.png" if dark else "preview-light.png")))
            self.assertTrue(self.driver.execute_script("return document.documentElement.scrollWidth <= innerWidth;"))
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        self.assertFalse(self.driver.find_element(By.ID, "crm-edit-design-previews").is_displayed())
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.wait_for_button()
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        self.assertFalse(self.driver.find_element(By.ID, "crm-order-all-header").is_displayed())
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})


if __name__ == "__main__":
    unittest.main()
