"""Offline native Angular/CRM product replay; never opens a customer order.

Optional local assets are kept under ignored runtime/debug/all_tabs_stock_vendor
or ALL_TABS_STOCK_VENDOR_DIR: angular.js (1.2), ui-bootstrap.js (legacy typeahead),
order-design-item.js and native-stock-controller.js. The latter installs the CRM
catalog loader/color watcher/native Apply functions as installNativeStockControls.
No cached customer responses, credentials, or third-party scripts are committed.
"""

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
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select, WebDriverWait


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "crm-order-dark-mode-extension"
ASSETS = ("angular.js", "ui-bootstrap.js", "order-design-item.js", "native-stock-controller.js")


class AllTabsStockBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        vendor_dir = Path(os.environ.get("ALL_TABS_STOCK_VENDOR_DIR", ROOT / "runtime/debug/all_tabs_stock_vendor"))
        if not all((vendor_dir / name).is_file() for name in ASSETS):
            raise unittest.SkipTest("Native stock replay assets are not available; set ALL_TABS_STOCK_VENDOR_DIR.")
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda path: tuple(
                    int(number) for number in next(part for part in path.parts if part.count(".") == 3).split(".")
                )))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver for offline browser tests.")
        fixture = (ROOT / "tests/fixtures/all_tabs_stock_crm.html").read_bytes()
        vendor = {"/vendor/" + name: (vendor_dir / name).read_bytes() for name in ASSETS}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript" if path in vendor else "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(vendor.get(path, fixture))

            def log_message(self, *_args):
                pass

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.httpd.shutdown)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.artifacts = ROOT / "runtime/debug/all_tabs_stock_tests"
        cls.artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=cls.artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for arg in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1200,1050"):
            options.add_argument(arg)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def inject(self):
        for name in ("all-tabs.css", "dark-mode.css"):
            self.driver.execute_script("const style = document.createElement('style'); style.textContent = arguments[0]; document.head.append(style);",
                                       (EXTENSION / name).read_text(encoding="utf-8"))
        self.driver.execute_script("\n".join((EXTENSION / name).read_text(encoding="utf-8")
                                            for name in ("all-tabs-model.js", "all-tabs.js")))

    def show_order(self, specs=None):
        self.driver.get(self.base_url + "/order/1234567")
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return typeof fixtureReset === 'function';"))
        self.driver.execute_script("fixtureReset(arguments[0]);", specs)
        self.inject()
        self.driver.find_element(By.CSS_SELECTOR, ".edit-btn").click()
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-all-tabs-header"))
        self.driver.find_element(By.ID, "crm-all-tabs-header").click()

    def choose_product(self, style, group="s0", wait_for_colors=True):
        control = self.driver.find_element(By.ID, "crm-all-tabs-stock-product-" + group)
        control.click()
        control.send_keys(Keys.CONTROL, "a")
        control.send_keys(style)
        popup = "[data-stock-group='" + group + "'] [typeahead-popup] a"
        WebDriverWait(self.driver, 5).until(lambda d: any(option.text == style for option in d.find_elements(By.CSS_SELECTOR, popup)))
        next(option for option in self.driver.find_elements(By.CSS_SELECTOR, popup) if option.text == style).click()
        if wait_for_colors:
            WebDriverWait(self.driver, 5).until(lambda d: len(Select(d.find_element(By.ID, "crm-all-tabs-stock-color-" + group)).options) > 0)

    def status(self):
        return self.driver.find_element(By.ID, "crm-all-tabs-status").text

    def test_stock_picker_reads_native_inactive_product_scopes_without_clicks_or_waits(self):
        self.driver.get(self.base_url + '/order/1234567')
        WebDriverWait(self.driver, 5).until(lambda d: d.execute_script("return typeof fixtureReset === 'function';"))
        self.driver.execute_script("""
          fixtureReset();
          for (const button of document.querySelectorAll('#main-header-design-tabs button')) {
            button.append(document.createTextNode(' Design Previews'));
          }
          window.scanClicks = 0;
          const original = orderScope.onDesignTabSelect;
          orderScope.onDesignTabSelect = index => { scanClicks++; original(index); };
        """)
        source = (EXTENSION / 'content.js').read_text(encoding='utf-8')
        scan = (source[source.index('function stockIssueCleanText'):source.index('const EXTRA_PRINT_AREAS')]
                + source[source.index('function stockIssueDetectedSizes'):source.index('function createStockIssueDialogShell')])
        self.driver.execute_script('\n'.join((EXTENSION / name).read_text(encoding='utf-8')
                                             for name in ('order-scan-reader.js', 'order-scan-bridge.js')) + scan + """
          window.stockIssueDelay = () => {throw new Error('Unexpected fixed wait');};
          window.runLoadedScan = scanAllStockIssueProducts;
        """)
        result = self.driver.execute_async_script("""
          const done = arguments[0], started = performance.now();
          runLoadedScan().then(products => done({products, milliseconds: performance.now() - started}),
            error => done({error: error.message}));
        """)
        self.assertNotIn('error', result)
        self.assertEqual(len(result['products']), 1)
        row = result['products'][0]
        self.assertEqual(row['style'], 'NF1270')
        self.assertEqual(row['description'], 'A4 Reversible Mesh Tank')
        self.assertEqual(row['available_sizes'], ['L', 'M'])
        self.assertEqual(row['total_quantity'], 4)
        self.assertEqual(row['tab_numbers'], [1, 2, 3, 4])
        self.assertEqual(self.driver.execute_script('return [scanClicks, orderScope.activeDesignIndex];'), [0, 1])
        self.assertLess(result['milliseconds'], 750)

    def resource(self):
        return self.driver.execute_script("return angular.copy(resource);")

    def apply(self):
        control = WebDriverWait(self.driver, 5).until(lambda d: d.find_element(By.ID, "crm-all-tabs-apply")
                                                     if d.find_element(By.ID, "crm-all-tabs-apply").is_enabled() else False)
        control.click()

    def test_native_product_and_color_save_reload_preserve_each_tabs_sizes_prices_and_artwork(self):
        self.show_order()
        before = self.resource()
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, "[data-stock-group]")), 1)
        self.choose_product("ST350")
        Select(self.driver.find_element(By.ID, "crm-all-tabs-stock-color-s0")).select_by_visible_text("WHITE")
        self.assertEqual(self.resource(), before, "Staged native selection must not change real stock")
        self.apply()
        self.assertIn("Applied 4 stock changes", self.status())
        self.assertEqual(self.driver.execute_script("return nativeChangeCalls;"), 4)
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.driver.refresh()
        after = self.resource()
        for i, design in enumerate(after["designs"]):
            value = design["designItems"][0]
            previous = before["designs"][i]["designItems"][0]
            self.assertEqual((value["style"], value["color"]), ("ST350", "WHITE"))
            self.assertEqual(value["id"], previous["id"])
            self.assertEqual([(size["sizeCode"], size["quantity"], size["pricePerPiece"], size["namesList"])
                              for size in value["sizes"] if size["quantity"]],
                             [(size["sizeCode"], size["quantity"], size["pricePerPiece"], size["namesList"])
                              for size in previous["sizes"] if size["quantity"]])
            self.assertEqual(design["printAreas"], before["designs"][i]["printAreas"])

    def test_current_color_is_preselected_and_sizes_total_only_the_matching_tabs(self):
        self.show_order([{"style": "G500", "size": "S", "quantity": 10},
                         {"style": "ST350", "color": "BLACK", "size": "L"},
                         {"style": "ST350", "color": "BLACK", "size": "XL", "quantity": 2},
                         {"style": "ST350", "color": "WHITE", "size": "L", "quantity": 9}])
        before = self.resource()
        self.assertEqual(Select(self.driver.find_element(By.ID, "crm-all-tabs-stock-color-s1")).first_selected_option.text, "BLACK")
        card = self.driver.find_element(By.CSS_SELECTOR, "[data-stock-group='s1']")
        self.assertEqual([cell.text for cell in card.find_elements(By.CSS_SELECTOR, "[data-stock-size]")], ["L", "XL"])
        self.assertEqual([cell.text for cell in card.find_elements(By.CSS_SELECTOR, "[data-stock-quantity]")], ["1", "2"])
        self.assertEqual(len(card.find_elements(By.CSS_SELECTOR, "input")), 1, "Only the native product field is editable")
        self.assertNotIn("43.89", card.text)
        self.assertEqual(self.resource(), before)
        self.driver.save_screenshot(str(self.artifacts / "shared-stock-totals.png"))

    def test_current_color_and_totals_survive_rerenders_and_color_only_edits(self):
        self.show_order()
        select = lambda: Select(self.driver.find_element(By.ID, "crm-all-tabs-stock-color-s0"))
        self.assertEqual(select().first_selected_option.text, "BLACK/WHITE")
        self.assertEqual([cell.text for cell in self.driver.find_elements(By.CSS_SELECTOR, "[data-stock-quantity]")], ["2", "2"])
        self.assertIn("A4 Reversible Mesh Tank", self.driver.find_element(By.CSS_SELECTOR, ".crm-all-tabs-stock-description").text)
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, ".crm-all-tabs-stock-image").is_displayed())
        select().select_by_visible_text("NAVY/WHITE")
        self.driver.find_element(By.ID, "crm-all-tabs-remove-g0").click()
        self.assertEqual(select().first_selected_option.text, "NAVY/WHITE")
        self.driver.find_element(By.ID, "crm-all-tabs-remove-g0").click()
        self.apply()
        self.assertEqual(select().first_selected_option.text, "NAVY/WHITE")
        self.assertEqual([cell.text for cell in self.driver.find_elements(By.CSS_SELECTOR, "[data-stock-quantity]")], ["2", "2"])
        self.assertEqual([design["designItems"][0]["color"] for design in self.resource()["designs"]], ["NAVY/WHITE"] * 4)

    def test_missing_sizes_warn_with_tab_numbers_and_block_stock_and_print_changes(self):
        self.show_order()
        before = self.resource()
        Select(self.driver.find_element(By.ID, "crm-all-tabs-location-g0")).select_by_value("back")
        self.choose_product("LIMITED")
        WebDriverWait(self.driver, 5).until(lambda d: "missing sizes" in self.status())
        self.assertIn("L (tabs 1, 4)", self.status())
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.assertEqual(self.resource(), before)
        self.assertEqual(self.driver.execute_script("return nativeChangeCalls;"), 0)
        self.driver.save_screenshot(str(self.artifacts / "missing-sizes.png"))
        self.choose_product("ST350")
        self.apply()
        self.assertEqual([design["printAreas"][0]["description"] for design in self.resource()["designs"]], ["Back"] * 4)

    def test_mixed_order_only_lists_the_repeated_product_and_color(self):
        self.show_order([{"style": "G500", "size": "S"}, {"style": "ST350", "color": "BLACK", "size": "M"},
                         {"style": "ST350", "color": "BLACK", "size": "L"}, {"style": "ST350", "color": "WHITE", "size": "XL"}])
        before = self.resource()
        cards = self.driver.find_elements(By.CSS_SELECTOR, "[data-stock-group]")
        self.assertEqual(len(cards), 1)
        self.assertIn("ST350 — BLACK", cards[0].text)
        self.assertIn("Affects tabs 2, 3", cards[0].text)
        Select(self.driver.find_element(By.ID, "crm-all-tabs-stock-color-s1")).select_by_visible_text("WHITE")
        self.apply()
        after = self.resource()
        self.assertEqual(after["designs"][0], before["designs"][0])
        self.assertEqual(after["designs"][3], before["designs"][3])
        self.assertEqual([after["designs"][i]["designItems"][0]["color"] for i in (1, 2)], ["WHITE", "WHITE"])

    def test_pending_native_picker_survives_navigation_and_print_panel_rerenders(self):
        self.show_order()
        self.choose_product("ST350")
        self.driver.find_element(By.ID, "crm-all-tabs-remove-g0").click()
        self.assertEqual(self.driver.find_element(By.ID, "crm-all-tabs-stock-product-s0").get_attribute("value"), "ST350")
        self.driver.find_elements(By.CSS_SELECTOR, "#main-header-design-tabs button[ng-repeat]")[0].click()
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.assertEqual(self.driver.execute_script("return saveCount;"), 0)
        self.assertIn("Apply or discard", self.status())
        self.assertEqual(self.driver.find_element(By.ID, "crm-all-tabs-stock-product-s0").get_attribute("value"), "ST350")
        self.driver.find_element(By.ID, "crm-all-tabs-discard").click()
        self.assertEqual(self.driver.find_element(By.ID, "crm-all-tabs-stock-product-s0").get_attribute("value"), "NF1270")

    def test_native_failures_and_later_print_failures_roll_back_combined_edits(self):
        for flag in ("window.failNativeAt = 2", "window.failArea = true"):
            self.show_order()
            before = self.resource()
            self.choose_product("ST350")
            Select(self.driver.find_element(By.ID, "crm-all-tabs-location-g0")).select_by_value("back")
            self.driver.execute_script(flag)
            self.apply()
            self.assertIn("rolled back", self.status())
            self.assertEqual(self.resource(), before)

    def test_typed_product_text_blocks_apply_and_a_reset_keeps_other_pending_edits(self):
        self.show_order()
        Select(self.driver.find_element(By.ID, "crm-all-tabs-location-g0")).select_by_value("back")
        control = self.driver.find_element(By.ID, "crm-all-tabs-stock-product-s0")
        control.send_keys(Keys.CONTROL, "a")
        control.send_keys("NO CATALOG MATCH")
        self.assertIn("CRM's suggestions", self.status())
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.driver.find_element(By.CSS_SELECTOR, ".crm-all-tabs-stock-reset").click()
        self.apply()
        self.assertEqual(self.driver.execute_script("return nativeChangeCalls;"), 0)

    def test_a_native_disabled_product_field_blocks_every_stock_write(self):
        self.show_order()
        before = self.resource()
        self.choose_product("ST350")
        self.driver.execute_script("document.querySelectorAll('.tab-pane input[ng-model=\"formItem.selectedCatalogItemStyle\"]')[1].disabled = true;")
        self.apply()
        self.assertIn("CRM disabled stock editing on tab 2", self.status())
        self.assertEqual(self.driver.execute_script("return nativeChangeCalls;"), 0)
        self.assertEqual(self.resource(), before)

    def test_native_loading_disables_apply_and_each_draft_is_destroyed_on_exit(self):
        self.show_order()
        self.driver.execute_script("window.catalogDelay = 900;")
        self.choose_product("ST350", wait_for_colors=False)
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-stock-color-s0").is_enabled())
        self.apply()
        self.assertIn("Applied 4 stock changes", self.status())
        self.driver.find_element(By.ID, "cancel-edit").click()
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-all-tabs-panel"))
        self.assertEqual([design["designItems"][0]["style"] for design in self.resource()["designs"]], ["NF1270"] * 4)

    def test_dark_mode_narrow_layout_and_printing_keep_native_controls_scoped(self):
        self.show_order()
        self.driver.execute_script("document.documentElement.classList.add('crm-order-dark-mode');")
        self.driver.set_window_size(510, 1050)
        try:
            self.choose_product("LIMITED")
            WebDriverWait(self.driver, 5).until(lambda d: "missing sizes" in self.status())
            self.assertEqual(self.driver.execute_script("return getComputedStyle(document.getElementById('crm-all-tabs-panel')).backgroundColor;"),
                             "rgb(37, 37, 38)")
            self.assertLessEqual(self.driver.execute_script("return document.documentElement.scrollWidth;"),
                                 self.driver.execute_script("return innerWidth;") + 1)
            self.driver.save_screenshot(str(self.artifacts / "missing-sizes-dark-narrow.png"))
            self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
            self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-panel").is_displayed())
            self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, ".tab-pane.active").is_displayed())
        finally:
            self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})
            self.driver.set_window_size(1200, 1050)
