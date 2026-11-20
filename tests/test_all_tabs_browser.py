"""All Tabs UI against sanitized CRM markup; never opens a live order."""

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
from selenium.webdriver.support.ui import Select, WebDriverWait


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "crm-order-dark-mode-extension"


class AllTabsBrowserTests(unittest.TestCase):
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
        fixture = (ROOT / "tests/fixtures/all_tabs_crm.html").read_bytes()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'<iframe src="/app#/order/1234567" style="width:100%;height:900px"></iframe>'
                                 if urlsplit(self.path).path == "/framed" else fixture)

            def log_message(self, *_args):
                pass

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.httpd.shutdown)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.artifacts = ROOT / "runtime/debug/all_tabs_tests"
        cls.artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=cls.artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for arg in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1200,1050"):
            options.add_argument(arg)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def inject(self):
        for name in ("all-tabs.css", "dark-mode.css"):
            self.driver.execute_script("const style = document.createElement('style'); style.textContent = arguments[0]; document.head.append(style);",
                                       (EXTENSION / name).read_text(encoding="utf-8"))
        self.driver.execute_script("\n".join((EXTENSION / name).read_text(encoding="utf-8")
                                            for name in ("all-tabs-model.js", "all-tabs.js")))

    def show_order(self, layout=None, route="/order/1234567", selected=1):
        self.driver.get(self.base_url + route)
        self.driver.execute_script("fixtureReset(arguments[0], arguments[1]);", layout, selected)
        self.inject()

    def edit(self):
        self.driver.find_element(By.CSS_SELECTOR, ".edit-btn").click()
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-all-tabs-header"))

    def open_all(self):
        self.driver.find_element(By.ID, "crm-all-tabs-header").click()
        WebDriverWait(self.driver, 5).until(lambda d: d.find_element(By.ID, "crm-all-tabs-panel").is_displayed())

    def select(self, field, value):
        Select(self.driver.find_element(By.ID, "crm-all-tabs-" + field)).select_by_value(value)

    def status(self):
        return self.driver.find_element(By.ID, "crm-all-tabs-status").text

    def locations(self):
        return self.driver.execute_script("return resource.designs.map(d => d.printAreas.map(a => a.description));")

    def test_fake_tabs_precede_real_tabs_and_preserve_selection_and_native_counts(self):
        self.show_order()
        self.assertEqual(self.driver.find_elements(By.ID, "crm-all-tabs-header"), [])
        self.edit()
        self.assertEqual(self.driver.execute_script("return orderScope.activeDesignIndex;"), 1)
        self.assertEqual(self.driver.execute_script("return document.querySelector('#main-header-design-tabs .btn-group').firstElementChild.id;"), "crm-all-tabs-header")
        self.assertEqual(self.driver.execute_script("return document.querySelector('#order-design-details .nav-tabs').firstElementChild.id;"), "crm-all-tabs-detail")
        self.assertEqual(self.driver.execute_script("return [resource.designs.length, orderScope.designTabs.length];"), [3, 3])
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-panel").is_displayed())
        self.open_all()
        self.assertEqual(self.driver.execute_script("return orderScope.activeDesignIndex;"), 1)
        self.assertFalse(self.driver.find_element(By.CSS_SELECTOR, ".tab-pane.active").is_displayed())
        self.driver.find_elements(By.CSS_SELECTOR, "#main-header-design-tabs button[ng-repeat]")[1].click()
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-panel").is_displayed())
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, ".tab-pane.active").is_displayed())
        self.driver.find_element(By.CSS_SELECTOR, "#crm-all-tabs-detail button").click()
        self.assertTrue(self.driver.find_element(By.ID, "crm-all-tabs-panel").is_displayed())

    def test_existing_extension_scanner_sees_only_real_tabs_and_native_selection(self):
        self.show_order()
        self.edit()
        self.open_all()
        source = (EXTENSION / "content.js").read_text(encoding="utf-8")
        helpers = source[source.index("function stockIssueCleanText("):source.index("function stockIssueDelay(")]
        scan = self.driver.execute_script(helpers + """
            const tabs = visibleStockIssueDesignTabs();
            return {tabs: tabs.map(tab => tab.tabNumber), active: activeStockIssueDesignTabNumber(tabs)};
        """)
        self.assertEqual(scan, {"tabs": [1, 2, 3], "active": 2})

    def test_mixed_methods_are_kept_until_explicitly_changed(self):
        self.show_order([["Front"], ["Front"]])
        self.driver.execute_script("resource.designs[1].printAreas[0].printMethodDescription = 'Embroidery';")
        self.edit()
        self.open_all()
        self.assertIn("mixed", Select(self.driver.find_element(By.ID, "crm-all-tabs-method-g0")).first_selected_option.text)
        self.select("location-g0", "back")
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.assertEqual(self.driver.execute_script("return resource.designs.map(d => d.printAreas[0].printMethodDescription);"),
                         ["HD Digital", "Embroidery"])
        self.select("method-g0", "screen printing")
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.assertEqual(self.driver.execute_script("return resource.designs.map(d => d.printAreas[0].printMethodDescription);"),
                         ["Screen Printing", "Screen Printing"])

    def test_busy_crm_disables_bulk_writes_and_preserves_the_draft(self):
        self.show_order()
        self.edit()
        self.open_all()
        self.select("location-g0", "side left")
        self.driver.execute_script("orderScope.savingOrder = true;")
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-location-g0").is_enabled())
        self.assertEqual(self.locations(), [["Front"], ["Front", "Back"], ["Front-Left Chest"]])
        self.driver.execute_script("orderScope.savingOrder = false;")
        WebDriverWait(self.driver, 5).until(lambda d: d.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.assertEqual(Select(self.driver.find_element(By.ID, "crm-all-tabs-location-g0")).first_selected_option.get_attribute("value"), "side left")

    def test_native_panel_rebuild_retains_pending_changes_and_first_position(self):
        self.show_order()
        self.edit()
        self.open_all()
        self.select("location-g0", "side left")
        self.driver.execute_script("""
            const replacement = document.createElement('div');
            replacement.className = 'tab-content';
            document.querySelector('#order-design-details .tab-content').replaceWith(replacement);
            renderNative();
            document.querySelector('#main-header-design-tabs .btn-group').append(document.querySelector('#crm-all-tabs-header'));
            document.querySelector('#order-design-details .nav-tabs').append(document.querySelector('#crm-all-tabs-detail'));
        """)
        WebDriverWait(self.driver, 5).until(lambda d: d.find_elements(By.ID, "crm-all-tabs-location-g0"))
        self.assertTrue(self.driver.find_element(By.ID, "crm-all-tabs-panel").is_displayed())
        self.assertEqual(Select(self.driver.find_element(By.ID, "crm-all-tabs-location-g0")).first_selected_option.get_attribute("value"), "side left")
        self.assertEqual(self.driver.execute_script("return document.querySelector('#main-header-design-tabs .btn-group').firstElementChild.id;"), "crm-all-tabs-header")
        self.assertEqual(self.driver.execute_script("return document.querySelector('#order-design-details .nav-tabs').firstElementChild.id;"), "crm-all-tabs-detail")

    def test_union_swaps_subset_edits_and_native_save_survive_refresh(self):
        self.show_order()
        before = self.driver.execute_script("return structuredClone(resource);")
        self.edit()
        self.open_all()
        self.assertEqual([card.get_attribute("data-source-area") for card in self.driver.find_elements(By.CSS_SELECTOR, "[data-source-area]")],
                         ["front", "back", "front-left chest"])
        self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, "[data-source-area='back'] .crm-all-tabs-affected").text, "Affects tabs 2")
        self.select("location-g0", "front-left chest")
        self.select("location-g1", "side left")
        self.select("location-g2", "front")
        self.assertEqual(self.locations(), [["Front"], ["Front", "Back"], ["Front-Left Chest"]])
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.assertEqual(self.locations(), [["Front-Left Chest"], ["Front-Left Chest", "Side Left"], ["Front"]])
        self.assertIn("Save Order", self.status())
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-all-tabs-header"))
        self.driver.refresh()
        self.inject()
        self.assertEqual(self.locations(), [["Front-Left Chest"], ["Front-Left Chest", "Side Left"], ["Front"]])
        after = self.driver.execute_script("return resource;")
        for i, design in enumerate(after["designs"]):
            self.assertEqual(design["designItems"], before["designs"][i]["designItems"])
            for j, area in enumerate(design["printAreas"]):
                for field in ("notes", "designFiles", "width", "height"):
                    self.assertEqual(area[field], before["designs"][i]["printAreas"][j][field])

    def test_collision_blocks_apply_but_allows_completing_a_swap(self):
        self.show_order([["Front", "Front-Left Chest"], ["Front", "Front-Left Chest"]])
        self.edit()
        self.open_all()
        self.select("location-g0", "front-left chest")
        self.assertIn("tabs 1, 2", self.status())
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.select("location-g1", "front")
        self.assertTrue(self.driver.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.assertEqual(self.locations(), [["Front-Left Chest", "Front"], ["Front-Left Chest", "Front"]])

    def test_additions_preview_only_missing_tabs_and_require_a_method(self):
        self.show_order()
        self.edit()
        self.open_all()
        self.driver.find_element(By.ID, "crm-all-tabs-add").click()
        addition = self.driver.find_element(By.CSS_SELECTOR, "[data-addition]").get_attribute("data-addition")
        self.select("location-" + addition, "back")
        self.assertIn("tabs 1, 3", self.driver.find_element(By.CSS_SELECTOR, "[data-addition-targets]").text)
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.select("method-" + addition, "embroidery")
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.assertEqual(self.locations(), [["Front", "Back"], ["Front", "Back"], ["Front-Left Chest", "Back"]])
        self.assertEqual(self.driver.execute_script("return resource.designs.map(d => d.printAreas.at(-1).printMethodDescription);"),
                         ["Embroidery", "HD Digital", "Embroidery"])
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.driver.refresh()
        self.assertEqual(self.locations(), [["Front", "Back"], ["Front", "Back"], ["Front-Left Chest", "Back"]])

    def test_pending_changes_survive_tab_navigation_and_block_native_save_until_discarded(self):
        self.show_order()
        self.edit()
        self.open_all()
        self.select("location-g0", "side left")
        self.driver.find_elements(By.CSS_SELECTOR, "#main-header-design-tabs button[ng-repeat]")[0].click()
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.assertIn("Apply or discard", self.status())
        self.assertEqual(self.driver.execute_script("return saveCount;"), 0)
        self.assertEqual(self.locations(), [["Front"], ["Front", "Back"], ["Front-Left Chest"]])
        self.driver.find_element(By.ID, "crm-all-tabs-discard").click()
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.assertEqual(self.driver.execute_script("return saveCount;"), 1)

    def test_native_cancel_removes_all_tabs_and_drops_applied_unsaved_edits(self):
        self.show_order()
        self.edit()
        self.open_all()
        self.select("location-g0", "side left")
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.driver.find_element(By.ID, "cancel-edit").click()
        WebDriverWait(self.driver, 5).until(lambda d: not d.find_elements(By.ID, "crm-all-tabs-header"))
        self.assertEqual(self.locations(), [["Front"], ["Front", "Back"], ["Front-Left Chest"]])
        self.assertFalse(self.driver.execute_script("return document.documentElement.classList.contains('crm-all-tabs-view');"))

    def test_rejected_native_save_keeps_applied_edits_without_repeating_them(self):
        self.show_order()
        self.edit()
        self.open_all()
        self.select("location-g0", "side left")
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.driver.execute_script("window.rejectSave = true;")
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.assertTrue(self.driver.execute_script("return orderScope.editMode;"))
        self.assertEqual(self.locations(), [["Side Left"], ["Side Left", "Back"], ["Front-Left Chest"]])
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-apply").is_enabled())
        self.driver.execute_script("window.rejectSave = false;")
        self.driver.find_element(By.CSS_SELECTOR, ".save-btn").click()
        self.driver.refresh()
        self.assertEqual(self.locations(), [["Side Left"], ["Side Left", "Back"], ["Front-Left Chest"]])

    def test_stale_native_edits_and_native_failures_do_not_partially_apply(self):
        self.show_order()
        self.edit()
        self.open_all()
        self.select("location-g0", "side left")
        self.driver.execute_script("resource.designs[0].printAreas[0].description = 'Back';")
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.assertIn("changed", self.status())
        self.assertEqual(self.locations()[1], ["Front", "Back"])
        self.driver.find_element(By.ID, "crm-all-tabs-discard").click()
        self.select("location-g0", "side left")
        before = self.locations()
        self.driver.execute_script("nativeFailure = true;")
        self.driver.find_element(By.ID, "crm-all-tabs-apply").click()
        self.assertIn("rolled back", self.status())
        self.assertEqual(self.locations(), before)

    def test_scope_routes_embedded_frames_and_printing(self):
        for route in ("/report/967", "/order/short", "/app#/reports/orders"):
            self.show_order(route=route)
            self.driver.execute_script("orderScope.editModeOn();")
            self.assertEqual(self.driver.find_elements(By.ID, "crm-all-tabs-header"), [])
        self.show_order(route="/app#/order/1234567")
        self.edit()
        self.open_all()
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-header").is_displayed())
        self.assertFalse(self.driver.find_element(By.ID, "crm-all-tabs-panel").is_displayed())
        self.assertTrue(self.driver.find_element(By.CSS_SELECTOR, ".tab-pane.active").is_displayed())
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})
        self.driver.get(self.base_url + "/framed")
        self.inject()
        self.assertEqual(self.driver.find_elements(By.ID, "crm-all-tabs-header"), [])
        self.driver.switch_to.frame(0)
        self.inject()
        self.edit()
        self.open_all()
        self.driver.switch_to.default_content()

    def test_main_world_document_start_loads_before_angular_and_mounts_once(self):
        source = "\n".join((EXTENSION / name).read_text(encoding="utf-8")
                           for name in ("all-tabs-model.js", "all-tabs.js"))
        script_id = self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})["identifier"]
        try:
            self.show_order()
            self.edit()
            self.open_all()
            self.assertEqual(len(self.driver.find_elements(By.ID, "crm-all-tabs-header")), 1)
            self.assertEqual(len(self.driver.find_elements(By.ID, "crm-all-tabs-detail")), 1)
            self.assertEqual(self.driver.execute_script("return typeof window.CRMAllTabsModel.plan;"), "function")
        finally:
            self.driver.execute_cdp_cmd("Page.removeScriptToEvaluateOnNewDocument", {"identifier": script_id})

    def test_light_and_dark_layout_have_no_horizontal_overflow(self):
        self.show_order()
        self.edit()
        self.open_all()
        for dark in (False, True):
            self.driver.execute_script("document.documentElement.classList.toggle('crm-order-dark-mode', arguments[0]);", dark)
            self.driver.save_screenshot(str(self.artifacts / ("all-tabs-dark.png" if dark else "all-tabs-light.png")))
            self.assertTrue(self.driver.execute_script("return document.documentElement.scrollWidth <= innerWidth;"))
        self.driver.set_window_size(550, 900)
        self.assertTrue(self.driver.execute_script("return document.documentElement.scrollWidth <= innerWidth;"))
        self.driver.set_window_size(1200, 1050)


if __name__ == "__main__":
    unittest.main()
