"""Preview retention and queued Order All with local markup and stubbed services."""

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
        self.driver.execute_script("sessionStorage.clear();")
        self.inject()

    def inject(self):
        for name in ("all-tabs.css", "order-qol.css", "dark-mode.css"):
            self.driver.execute_script("const style = document.createElement('style'); style.textContent = arguments[0]; document.head.append(style);",
                                       (EXTENSION / name).read_text(encoding="utf-8"))
        self.driver.execute_script("\n".join((EXTENSION / name).read_text(encoding="utf-8")
            for name in ("content.js", "all-tabs-model.js", "all-tabs.js", "order-all.js", "order-edit-previews.js")))

    def wait_for_button(self):
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#crm-order-all-header button"))
        return self.driver.find_element(By.CSS_SELECTOR, "#crm-order-all-header button")

    def edit(self):
        self.driver.find_element(By.CSS_SELECTOR, ".edit-btn").click()
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-edit-design-previews"))

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

    def test_order_all_is_after_last_real_tab_and_queues_only_current_order_once(self):
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
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return queueCalls.length;") == 1)
        self.assertEqual(self.driver.execute_script("return queueCalls[0].orderId;"), "1234567")
        self.assertEqual(self.driver.execute_script("return queueCalls[0].automation;"), "order_goods")
        self.assertTrue(button.get_property("disabled"))
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, "#crm-order-all-detail button").get_property("disabled"))
        self.assertEqual(self.driver.execute_script("return [nativeGoodsClicks, orderScope.activeDesignIndex];"), [0, 1])
        self.driver.execute_script("renderFixture();")
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.assertEqual(self.driver.execute_script("return queueCalls.length;"), 1)

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

    def test_uncertain_queue_acceptance_blocks_repeat_clicks_after_rebuild(self):
        self.show()
        self.driver.execute_script("queueMode = 'uncertain';")
        self.wait_for_button().click()
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return queueCalls.length;") == 1)
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.assertIn("may already be queued", self.wait_for_button().get_attribute("title"))
        self.driver.execute_script("document.querySelector('#crm-order-all-header').remove(); renderFixture();")
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.assertEqual(self.driver.execute_script("return queueCalls.length;"), 1)
        self.driver.refresh()
        self.inject()
        self.assertTrue(self.wait_for_button().get_property("disabled"))
        self.assertIn("may already be queued", self.wait_for_button().get_attribute("title"))

    def test_known_queue_rejection_shows_error_and_allows_retry(self):
        self.show()
        self.driver.execute_script("queueMode = 'reject';")
        self.wait_for_button().click()
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return queueCalls.length;") == 1)
        WebDriverWait(self.driver, 5).until(lambda d: not self.wait_for_button().get_property("disabled"))
        self.assertIn("not reachable", self.driver.find_element(By.ID, "crm-order-automation-result").text)
        self.driver.execute_script("queueMode = 'success';")
        self.wait_for_button().click()
        self.assertEqual(self.driver.execute_script("return queueCalls.length;"), 2)

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
