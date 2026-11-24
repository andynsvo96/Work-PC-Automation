"""Preview retention and native Order All with local markup and stubbed CRM services."""

import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

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
                route = urlsplit(self.path).path
                data, mime = fixture, "text/html; charset=utf-8"
                if route.startswith("/extension/"):
                    name = route.rsplit("/", 1)[-1]
                    data = (EXTENSION / name).read_bytes()
                    mime = "text/css" if name.endswith(".css") else "application/javascript" if name.endswith(".js") else mime
                    if name == "popup.html":
                        data = data.replace(b'<script src="order-sections.js">', b'<script>window.chrome=parent.chrome;</script><script src="order-sections.js">')
                        data = data.replace(b'<script type="module" src="all-orders-popup.mjs"></script>', b'')
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.end_headers()
                self.wfile.write(data)

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

    def setUp(self):
        self.driver.set_window_size(1200, 1200)
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})

    def show(self, route="/order/1234567"):
        self.driver.get("about:blank")
        self.driver.get(self.url + route)
        self.driver.execute_script("sessionStorage.clear(); localStorage.clear(); fixtureResetStock();")
        self.inject()

    def inject(self, automation=True):
        for name in ("all-tabs.css", "order-qol.css", "dark-mode.css"):
            self.driver.execute_script("const style = document.createElement('style'); style.textContent = arguments[0]; document.head.append(style);",
                                       (EXTENSION / name).read_text(encoding="utf-8"))
        scripts = (["order-sections.js", "content.js"] if automation else []) + ["all-tabs-model.js", "all-tabs.js", "order-all-batch.js", "order-all.js", "order-edit-previews.js"]
        self.driver.execute_script("\n".join((EXTENSION / name).read_text(encoding="utf-8") for name in scripts))

    def wait_for_button(self):
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.CSS_SELECTOR, "#crm-order-all-header button"))
        return self.driver.find_element(By.CSS_SELECTOR, "#crm-order-all-header button")

    def open_hidden_sections_popup(self):
        self.driver.switch_to.default_content()
        self.driver.execute_script("""
          document.getElementById('popup')?.remove();
          const popup = document.createElement('iframe'); popup.id='popup';
          popup.style='position:fixed;top:0;left:0;width:800px;height:600px;z-index:2147483647;border:0';
          popup.src='/extension/popup.html'; document.body.append(popup);
        """)
        wait = WebDriverWait(self.driver, 5)
        self.driver.switch_to.frame(wait.until(lambda d: d.find_element(By.ID, "popup")))
        wait.until(lambda d: d.find_element(By.ID, "hidden-sections-toggle").is_displayed())
        self.driver.find_element(By.ID, "hidden-sections-toggle").click()
        return wait.until(lambda d: d.find_element(By.ID, "hidden-sections-lock").is_enabled()
                          and d.find_element(By.ID, "hidden-sections-lock"))

    def stock_action(self, op, **extra):
        return self.driver.execute_async_script("const done=arguments[arguments.length-1]; CRMOrderAllPage.execute(arguments[0]).then(done);",
            {"op": op, "orderId": "1234567", **extra})

    def test_window_adapter_orders_single_product_stock_without_adding_a_per_order_button(self):
        self.show()
        self.driver.execute_script("resource.designs=resource.designs.slice(0,1); fixtureStock.ordered[0]=false; orderScope.activeDesignIndex=0; nativeMode='success'; renderFixture();")
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-order-all-header"))
        self.assertEqual(self.driver.find_elements(By.ID, "crm-order-all-header"), [])
        read = self.stock_action("inspect")
        self.assertEqual([target['id'] for target in read['targets']], ['100'])
        result = self.stock_action("order", targets=read['targets'])
        self.assertTrue(result['success'], result)
        self.assertEqual(result['phase'], 'confirmed')
        self.assertEqual(self.driver.execute_script("return [nativeCalls,nativeReloads,queueCalls];"), [['100'], 1, []])
        inspected = self.stock_action("inspect")
        self.assertEqual(inspected['targets'], [])
        self.assertEqual([entry['id'] for entry in inspected['confirmed']], ['100'])

    def test_window_adapter_rechecks_edit_mode_and_stock_fingerprints_before_any_submission(self):
        for change in ("orderScope.editMode=true", "resource.designs[2].designItems[0].quantity=2",
                       "resource.addSalesNotes='Keep this pending note'", "inventoryScopes[0].placingOrder=true",
                       "document.body.insertAdjacentHTML('beforeend','<div role=dialog>Keep this dialog</div>')"):
            with self.subTest(change=change):
                self.show()
                targets = self.stock_action("inspect")['targets']
                self.driver.execute_script(change + "; renderFixture();")
                result = self.stock_action("order", targets=targets)
                self.assertFalse(result['success'], result)
                self.assertTrue(result['notSubmitted'])
                self.assertEqual(self.driver.execute_script("return nativeCalls;"), [])

    def test_window_adapter_skips_locked_and_manual_stock_and_does_not_retry_uncertain_receipts(self):
        self.show()
        self.driver.execute_script("resource.featureFlags.stockOrderingLocked=true; renderFixture();")
        self.assertEqual(self.stock_action("inspect")['targets'], [])
        self.driver.execute_script("resource.featureFlags.stockOrderingLocked=false; inventoryScopes[1].providers[0].id=210; renderFixture();")
        read = self.stock_action("inspect")
        self.assertEqual([target['id'] for target in read['targets']], ['102'])
        self.driver.execute_script("inventoryService.orderGoodsSupplynet=()=>Promise.reject(Error('Lost response'));")
        result = self.stock_action("order", targets=read['targets'])
        self.assertEqual(result['phase'], 'review')
        self.assertIn("previous stock request needs review", self.stock_action("inspect")['skipReason'])

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


    def test_bottom_previews_preserve_native_columns_and_drafts_at_different_widths(self):
        self.show()
        original = self.driver.execute_script("""
          window.originalPrintArea = document.querySelector('.tab-pane.active .printarea input');
          window.originalForm = originalPrintArea.closest('[ng-form="designForm"]');
          originalPrintArea.value = 'Keep this draft';
          orderScope.editMode = true; document.body.classList.toggle('changed');
          return structuredClone(resource);
        """)
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-edit-design-previews"))
        for width in (1200, 1600, 700):
            with self.subTest(width=width):
                self.driver.set_window_size(width, 1100)
                self.assertTrue(self.driver.execute_script("""
                  const root = document.getElementById('view-orders-index');
                  const main = document.getElementById('main-content');
                  const page = document.getElementById('content');
                  const preview = document.getElementById('crm-edit-design-previews');
                  return root.lastElementChild === preview
                    && preview.getBoundingClientRect().top >= page.getBoundingClientRect().bottom
                    && getComputedStyle(main).display === 'block'
                    && Math.abs(main.getBoundingClientRect().width - page.getBoundingClientRect().width * (innerWidth > 991 ? .5 : 1)) < 2
                    && originalPrintArea === document.querySelector('.tab-pane.active .printarea input')
                    && originalPrintArea.closest('[ng-form="designForm"]') === originalForm
                    && originalPrintArea.value === 'Keep this draft'
                    && !root.querySelector('.crm-edit-preview-layout, .crm-edit-preview-target')
                    && document.documentElement.scrollWidth <= innerWidth;
                """))
        self.assertEqual(self.driver.execute_script("return resource;"), original)

    def test_automation_menus_queue_separator_then_copyright_reachout_while_working(self):
        self.show()
        wait = WebDriverWait(self.driver, 6)
        wait.until(lambda d: d.find_elements(By.ID, "crm-order-manual-process-button"))
        self.driver.find_element(By.ID, "crm-order-manual-process-button").click()
        self.driver.find_element(By.CSS_SELECTOR, '[data-crm-order-automation="product_separator"]').click()
        wait.until(lambda d: d.execute_script("return queueCalls.length;") == 1)
        self.driver.execute_script("fixtureRuntime = {...fixtureRuntime, queued: false, running: true};")
        wait.until(lambda d: "Working" in d.find_element(By.ID, "crm-order-manual-process-button").text)
        for button in self.driver.find_elements(By.CSS_SELECTOR, '[data-crm-order-process-trigger="true"]'):
            self.assertTrue(button.is_enabled())
        self.driver.find_element(By.ID, "crm-order-reachout-button").click()
        self.driver.find_element(By.CSS_SELECTOR, '[data-crm-order-automation="copyright_reachout"]').click()
        self.driver.find_element(By.ID, "crm-order-automation-reason").send_keys("Protected artwork")
        self.driver.find_element(By.XPATH, '//div[@role="dialog"]//button[text()="Queue task"]').click()
        wait.until(lambda d: d.execute_script("return queueCalls.length;") == 2)
        calls = self.driver.execute_script("return queueCalls;")
        self.assertEqual([(call["automation"], call["orderId"], call["reason"]) for call in calls],
                         [("product_separator", "1234567", ""), ("copyright_reachout", "1234567", "Protected artwork")])
        self.assertTrue(all(button.is_enabled() for button in self.driver.find_elements(By.CSS_SELECTOR, '[data-crm-order-process-trigger="true"]')))
        self.assertEqual(self.driver.execute_script("return nativeCalls;"), [])

    def test_sending_one_automation_leaves_other_actions_available_and_blocks_double_clicks(self):
        self.show()
        self.driver.execute_script("queueMode = 'hold';")
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-order-manual-process-button"))
        self.driver.find_element(By.ID, "crm-order-manual-process-button").click()
        self.driver.find_element(By.CSS_SELECTOR, '[data-crm-order-automation="product_separator"]').click()
        self.driver.find_element(By.ID, "crm-order-manual-process-button").click()
        separator = self.driver.find_element(By.CSS_SELECTOR, '[data-crm-order-automation="product_separator"]')
        self.assertFalse(separator.is_enabled())
        self.driver.execute_script("arguments[0].click();", separator)
        self.driver.find_element(By.CSS_SELECTOR, '[data-crm-order-automation="address_validator"]').click()
        self.assertEqual(self.driver.execute_script("return queueCalls.map(call => call.automation);"), ["product_separator", "address_validator"])
        self.assertTrue(self.driver.find_element(By.ID, "crm-order-reachout-button").is_enabled())
        self.driver.execute_script("fixtureQueueResolvers.splice(0).forEach(resolve => resolve());")
        WebDriverWait(self.driver, 5).until(lambda d: separator.is_enabled())
        self.assertEqual(self.driver.execute_script("return queueCalls.length;"), 2)

    def test_hidden_sections_persist_across_orders_and_restore_from_actual_popup(self):
        self.show()
        wait = WebDriverWait(self.driver, 5)
        ids = ["order-purchase-orders", "order-promos", "order-cartons", "sales-tax-transactions"]
        original = self.driver.execute_script("return structuredClone(resource);")
        for section in ids:
            button = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, f'#{section} .crm-order-section-hide'))
            button.click()
            wait.until(lambda d: not d.find_element(By.ID, section).is_displayed())
        self.assertEqual(self.driver.execute_script("return [resource,fixtureHeaderClicks];"), [original, 0])
        self.driver.refresh()
        self.inject()
        wait.until(lambda d: all(not d.find_element(By.ID, section).is_displayed() for section in ids))
        first = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        try:
            self.driver.get(self.url + "/app#/order/7654321")
            self.inject()
            wait.until(lambda d: all(not d.find_element(By.ID, section).is_displayed() for section in ids))
            self.driver.execute_script("""
              const popup = document.createElement('iframe'); popup.id='popup';
              popup.style='position:fixed;top:0;left:0;width:800px;height:600px;z-index:2147483647;border:0';
              popup.src='/extension/popup.html'; document.body.append(popup);
            """)
            self.driver.switch_to.frame(wait.until(lambda d: d.find_element(By.ID, "popup")))
            wait.until(lambda d: d.find_element(By.ID, "hidden-sections-toggle").is_displayed())
            self.driver.find_element(By.ID, "hidden-sections-toggle").click()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#hidden-sections-list li")) == 4)
            self.assertTrue(self.driver.execute_script("return document.getElementById('hidden-sections-toggle').getBoundingClientRect().right < document.querySelector('.toggle').getBoundingClientRect().left;"))
            self.driver.save_screenshot(str(self.artifacts / "hidden-sections-popup.png"))
            self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Unhide Promos"]').click()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#hidden-sections-list li")) == 3)
            self.driver.switch_to.default_content()
            wait.until(lambda d: d.find_element(By.ID, "order-promos").is_displayed())
            self.driver.switch_to.window(first)
            wait.until(lambda d: d.find_element(By.ID, "order-promos").is_displayed())
            self.assertFalse(self.driver.find_element(By.ID, "order-cartons").is_displayed())
        finally:
            for handle in self.driver.window_handles:
                if handle != first:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
            self.driver.switch_to.window(first)

    def test_hide_controls_preserve_native_visibility_and_print_and_follow_route_changes(self):
        self.show()
        wait = WebDriverWait(self.driver, 5)
        button = wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#order-promos .crm-order-section-hide'))
        self.driver.execute_script("fixtureStorageFail = true;")
        button.click()
        self.assertTrue(self.driver.find_element(By.ID, "order-promos").is_displayed())
        self.assertIn("Could not save", button.get_attribute("title"))
        self.driver.execute_script("fixtureStorageFail = false;")
        button.click()
        wait.until(lambda d: not d.find_element(By.ID, "order-promos").is_displayed())
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        self.assertTrue(self.driver.find_element(By.ID, "order-promos").is_displayed())
        self.assertFalse(button.is_displayed())
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})
        self.driver.execute_script("history.pushState({}, '', '/reports/1234567'); dispatchEvent(new PopStateEvent('popstate'));")
        wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide"))
        self.assertTrue(self.driver.find_element(By.ID, "order-promos").is_displayed())
        self.driver.execute_script("history.pushState({}, '', '/app#/order/7654321'); dispatchEvent(new PopStateEvent('popstate'));")
        wait.until(lambda d: not d.find_element(By.ID, "order-promos").is_displayed())
        self.driver.execute_script("""
          document.getElementById('order-promos').outerHTML = '<section id="order-promos" class="panel ng-hide"><div class="panel-heading">Promos</div></section>';
          chrome.storage.local.set({'crmOrderHiddenSection:order-promos':false});
        """)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, "#order-promos .crm-order-section-hide")) == 1)
        self.assertFalse(self.driver.find_element(By.ID, "order-promos").is_displayed())

    def test_vendor_purchase_order_uses_hide_unhide_and_lock_without_native_actions(self):
        self.show()
        self.assertEqual(self.driver.find_elements(By.ID, "order-vendor-po-panel"), [])
        self.driver.execute_script("fixtureSubcontractor = true; renderFixture();")
        wait = WebDriverWait(self.driver, 5)
        panel = self.driver.find_element(By.ID, "order-vendor-po-panel")
        wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#order-vendor-po-panel .crm-order-section-hide')).click()
        wait.until(lambda d: not panel.is_displayed())
        self.assertTrue(self.driver.find_element(By.ID, "order-purchase-orders").is_displayed())
        self.assertEqual(self.driver.execute_script("return [fixtureHeaderClicks,fixtureVendorPOCalls,queueCalls,nativeCalls];"), [0, 0, [], []])
        self.driver.execute_script("fixtureSubcontractor = false; renderFixture();")
        self.assertEqual(self.driver.find_elements(By.ID, "order-vendor-po-panel"), [])
        lock = self.open_hidden_sections_popup()
        self.assertIn("Vendor Purchase Order", self.driver.find_element(By.ID, "hidden-sections-list").text)
        lock.click()
        wait.until(lambda d: lock.get_attribute("aria-pressed") == "true" and lock.is_enabled())
        self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Unhide Vendor Purchase Order"]').click()
        wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "#hidden-sections-list li"))
        self.driver.switch_to.default_content()
        self.assertEqual(self.driver.find_elements(By.ID, "order-vendor-po-panel"), [])
        self.driver.execute_script("fixtureSubcontractor = true; renderFixture();")
        self.assertTrue(self.driver.find_element(By.ID, "order-vendor-po-panel").is_displayed())
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, "#order-vendor-po-panel .panel-body").is_displayed())
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, "#order-vendor-po-panel .crm-order-section-hide"), [])
        lock = self.open_hidden_sections_popup()
        lock.click()
        wait.until(lambda d: lock.get_attribute("aria-pressed") == "false" and lock.is_enabled())
        self.driver.switch_to.default_content()
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#order-vendor-po-panel .crm-order-section-hide"))
        self.assertEqual(self.driver.execute_script("return [fixtureHeaderClicks,fixtureVendorPOCalls,queueCalls,nativeCalls];"), [0, 0, [], []])
        self.driver.execute_script("document.getElementById('popup').remove(); orderScope.editModeOn();")
        self.assertEqual(self.driver.find_elements(By.ID, "order-vendor-po-panel"), [])
        self.driver.execute_script("orderScope.saveOrder();")
        wait.until(lambda d: d.find_elements(By.CSS_SELECTOR, "#order-vendor-po-panel .crm-order-section-hide"))

    def test_section_hide_lock_persists_across_orders_and_keeps_unhide_available(self):
        self.show()
        wait = WebDriverWait(self.driver, 5)
        original = self.driver.execute_script("return structuredClone(resource);")
        wait.until(lambda d: d.find_element(By.CSS_SELECTOR, '#order-purchase-orders .crm-order-section-hide')).click()
        wait.until(lambda d: not d.find_element(By.ID, "order-purchase-orders").is_displayed())
        lock = self.open_hidden_sections_popup()
        self.assertEqual(lock.get_attribute("aria-pressed"), "false")
        self.assertTrue(self.driver.execute_script("""
          const lock = document.getElementById('hidden-sections-lock').getBoundingClientRect();
          const title = document.querySelector('.hidden-sections-heading h2').getBoundingClientRect();
          const panel = document.getElementById('hidden-sections-panel').getBoundingClientRect();
          return lock.left > title.right && lock.right < panel.right && lock.top < title.bottom;
        """))
        lock.click()
        wait.until(lambda d: lock.get_attribute("aria-pressed") == "true" and lock.is_enabled())
        self.driver.save_screenshot(str(self.artifacts / "hidden-sections-locked.png"))
        self.driver.switch_to.default_content()
        wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide, .crm-order-hideable-heading"))
        self.assertFalse(self.driver.find_element(By.ID, "order-purchase-orders").is_displayed())
        self.assertEqual(self.driver.execute_script("return resource;"), original)
        self.driver.refresh()
        self.inject()
        self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide"), [])
        first = self.driver.current_window_handle
        self.driver.switch_to.new_window("tab")
        try:
            self.driver.get(self.url + "/app#/order/7654321")
            self.inject()
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide"), [])
            self.assertFalse(self.driver.find_element(By.ID, "order-purchase-orders").is_displayed())
            lock = self.open_hidden_sections_popup()
            self.assertEqual(lock.get_attribute("aria-pressed"), "true")
            self.driver.find_element(By.CSS_SELECTOR, '[aria-label="Unhide Purchase Orders"]').click()
            wait.until(lambda d: not d.find_elements(By.CSS_SELECTOR, "#hidden-sections-list li"))
            self.assertEqual(lock.get_attribute("aria-pressed"), "true")
            self.driver.switch_to.default_content()
            wait.until(lambda d: d.find_element(By.ID, "order-purchase-orders").is_displayed())
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide"), [])
            self.driver.execute_script("document.getElementById('order-promos').outerHTML='<section id=order-promos class=panel><div class=panel-heading>Promos</div></section>';")
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, "#order-promos .crm-order-section-hide"), [])
            second = self.driver.current_window_handle
            self.driver.switch_to.window(first)
            wait.until(lambda d: d.find_element(By.ID, "order-purchase-orders").is_displayed())
            self.assertEqual(self.driver.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide"), [])
            lock = self.open_hidden_sections_popup()
            self.assertEqual(lock.get_attribute("aria-pressed"), "true")
            lock.click()
            wait.until(lambda d: lock.get_attribute("aria-pressed") == "false" and lock.is_enabled())
            self.driver.switch_to.default_content()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide")) == 5)
            self.driver.switch_to.window(second)
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide")) == 5)
            self.driver.refresh()
            self.inject()
            wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide")) == 5)
        finally:
            for handle in self.driver.window_handles:
                if handle != first:
                    self.driver.switch_to.window(handle)
                    self.driver.close()
            self.driver.switch_to.window(first)

    def test_section_hide_lock_save_failures_keep_previous_lock_state_and_buttons(self):
        self.show()
        wait = WebDriverWait(self.driver, 5)
        wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide")) == 5)
        lock = self.open_hidden_sections_popup()
        for initially_locked in (False, True):
            with self.subTest(locked=initially_locked):
                self.driver.switch_to.default_content()
                self.driver.execute_script("fixtureStorageFail = true;")
                self.driver.switch_to.frame(self.driver.find_element(By.ID, "popup"))
                lock.click()
                wait.until(lambda d: "Could not save the section lock" in d.find_element(By.ID, "hidden-sections-status").text)
                self.assertEqual(lock.get_attribute("aria-pressed"), str(initially_locked).lower())
                self.assertTrue(lock.is_enabled())
                self.driver.switch_to.default_content()
                self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, ".crm-order-section-hide")), 0 if initially_locked else 5)
                self.assertEqual(self.driver.execute_script("return [queueCalls,nativeCalls];"), [[], []])
                self.driver.execute_script("fixtureStorageFail = false;")
                self.driver.switch_to.frame(self.driver.find_element(By.ID, "popup"))
                if not initially_locked:
                    lock.click()
                    wait.until(lambda d: lock.get_attribute("aria-pressed") == "true" and lock.is_enabled())
        self.driver.switch_to.default_content()

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
        self.assertNotEqual(self.driver.execute_script("return getComputedStyle(document.getElementById('main-content')).display;"), "contents")
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.wait_for_button()
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        self.assertFalse(self.driver.find_element(By.ID, "crm-order-all-header").is_displayed())
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})


if __name__ == "__main__":
    unittest.main()
