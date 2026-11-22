"""Native Angular add-note controls and popup replay; all URLs stay on loopback."""

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


class AllOrdersBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda path: tuple(
                    int(number) for number in next(part for part in path.parts if part.count(".") == 3).split(".")
                )))
        angular = ROOT / "runtime/debug/all_tabs_stock_vendor/angular.js"
        if not driver_path or not angular.is_file():
            raise unittest.SkipTest("Local ChromeDriver and the ignored Angular fixture asset are required.")

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                route = urlsplit(self.path).path
                mime = "text/html; charset=utf-8"
                if route.startswith("/extension/"):
                    name = route.rsplit("/", 1)[-1]
                    path = EXTENSION / name
                    data = path.read_bytes()
                    mime = "text/css" if name.endswith(".css") else "application/javascript" if name.endswith((".js", ".mjs")) else mime
                    if name == "popup.html":
                        data = data.replace(b"<script src=\"popup.js\">", b"<script>window.chrome=parent.fixtureApi;</script><script src=\"popup.js\">")
                elif route == "/vendor/angular.js":
                    data = angular.read_bytes()
                    mime = "application/javascript"
                elif route == "/harness":
                    data = (ROOT / "tests/fixtures/all_orders_popup_harness.html").read_bytes()
                elif route == "/framed":
                    data = b'<iframe id="crm" src="/app#/order/1000002" style="width:100%;height:1000px"></iframe>'
                else:
                    data = (ROOT / "tests/fixtures/all_orders_crm.html").read_bytes()
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
        cls.artifacts = ROOT / "runtime/debug/all_orders_tests"
        cls.artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=cls.artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for arg in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1100,900"):
            options.add_argument(arg)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.driver.set_script_timeout(40)
        cls.addClassCleanup(cls.driver.quit)

    def setUp(self):
        self.driver.switch_to.default_content()
        self.driver.get(self.url + "/order/1000001")
        self.driver.execute_script("localStorage.clear();")
        self.driver.refresh()
        self.wait = WebDriverWait(self.driver, 12)
        self.wait.until(lambda d: d.execute_script("return !!window.fixtureScope;"))

    def execute(self, op, **extra):
        command = {"op": op, "orderId": "1000001", "batchId": "browser-batch", **extra}
        return self.driver.execute_async_script("const done=arguments[arguments.length-1]; CRMAllOrdersPage.execute(arguments[0]).then(done);", command)

    def draft(self):
        return {"changes": {"front": {"location": "Front-Left Chest"}}, "additions": [],
                "notes": {"sales": "New shared Sales Note", "production": "New shared Production Note", "art": "New shared Art Note"}}

    def test_native_single_tab_save_reload_preserves_history_artwork_sizes_and_prices(self):
        before = self.driver.execute_script("return structuredClone(fixtureResource);")
        self.assertTrue(self.execute("begin")["success"])
        applied = self.execute("apply", draft=self.draft())
        self.assertTrue(applied["success"], applied)
        self.assertEqual(self.driver.execute_script("return fixtureSaveCount;"), 0)
        for kind in ("sales", "production", "art"):
            self.assertEqual(self.driver.execute_script("return fixtureResource[arguments[0]+'Notes'];", kind), before[kind + "Notes"])
        self.assertTrue(self.execute("save")["success"])
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script("return !!window.fixtureScope;"))
        self.assertTrue(self.execute("verify", expected=applied["expected"])["success"])
        after = self.driver.execute_script("return fixtureResource;")
        self.assertEqual(after["designs"][0]["designItems"], before["designs"][0]["designItems"])
        area = after["designs"][0]["printAreas"][0]
        self.assertEqual(area["description"], "Front-Left Chest")
        for name in ("notes", "designFiles", "width", "height"):
            self.assertEqual(area[name], before["designs"][0]["printAreas"][0][name])
        for kind in ("sales", "production", "art"):
            self.assertEqual(after[kind + "Notes"], before[kind + "Notes"] + "\n" + self.draft()["notes"][kind])

    def test_reapplying_a_batch_cannot_append_a_second_copy_of_any_note(self):
        self.execute("begin")
        self.assertTrue(self.execute("apply", draft=self.draft())["success"])
        again = self.execute("apply", draft=self.draft())
        self.assertFalse(again["success"])
        self.assertEqual(self.driver.execute_script("return fixtureResource.addSalesNotes;"), "New shared Sales Note")

    def test_missing_native_art_input_blocks_all_changes_before_mutation(self):
        self.execute("begin")
        self.driver.execute_script("fixtureScope.$apply(()=>fixtureScope.disableArt=true);")
        response = self.execute("apply", draft=self.draft())
        self.assertFalse(response["success"])
        self.assertIn("native Add Art Note", response["message"])
        self.assertEqual(self.driver.execute_script("return fixtureChangeCount;"), 0)
        self.assertEqual(self.driver.execute_script("return fixtureResource.addSalesNotes;"), "")

    def test_changed_native_drafts_are_preserved_and_block_apply(self):
        self.execute("begin")
        self.driver.execute_script("fixtureScope.$apply(()=>fixtureResource.addProductionNotes='Manually entered note');")
        response = self.execute("apply", draft=self.draft())
        self.assertFalse(response["success"])
        self.assertIn("changed", response["message"])
        self.assertEqual(self.driver.execute_script("return fixtureResource.addProductionNotes;"), "Manually entered note")

    def test_native_helper_failure_rolls_back_without_touching_saved_notes(self):
        self.execute("begin")
        self.driver.execute_script("window.fixtureFailHelper=true;")
        result = self.execute("apply", draft=self.draft())
        self.assertFalse(result["success"])
        self.assertEqual(self.driver.execute_script("return fixtureResource.designs[0].printAreas[0].description;"), "Front")
        self.assertEqual(self.driver.execute_script("return fixtureResource.addSalesNotes;"), "")

    def test_save_success_without_persisted_notes_is_reported_for_review(self):
        self.execute("begin")
        applied = self.execute("apply", draft=self.draft())
        self.driver.execute_script("window.fixtureDropNotes=true;")
        self.execute("save")
        self.driver.refresh()
        self.wait.until(lambda d: d.execute_script("return !!window.fixtureScope;"))
        response = self.execute("verify", expected=applied["expected"])
        self.assertFalse(response["success"])
        self.assertIn("notes could not be verified", response["message"])

    def test_embedded_order_uses_every_active_internal_tab_and_preserves_back(self):
        self.driver.get(self.url + "/framed")
        self.driver.switch_to.frame("crm")
        self.wait.until(lambda d: d.execute_script("return !!window.fixtureScope;"))
        self.assertTrue(self.execute("begin", orderId="1000002")["success"])
        applied = self.execute("apply", orderId="1000002", draft=self.draft())
        self.assertTrue(applied["success"], applied)
        self.assertEqual(self.driver.execute_script("return fixtureResource.designs.map(d=>d.printAreas.map(a=>a.description));"),
                         [["Front-Left Chest"], ["Front-Left Chest", "Back"]])

    def popup(self):
        self.driver.switch_to.default_content()
        self.driver.switch_to.frame("popup")
        self.wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, ".order-row")) == 2)

    def click_wait(self, button_id, text):
        self.driver.find_element(By.ID, button_id).click()
        self.wait.until(lambda d: text in d.find_element(By.ID, "orders-status").text)

    def test_popup_complete_workflow_and_reopening_preserve_drafts_and_window_scope(self):
        self.driver.get(self.url + "/harness")
        self.wait.until(lambda d: d.execute_script("return !!window.fixtureApi;"))
        self.popup()
        self.click_wait("orders-edit", "Choose your shared edits")
        Select(self.driver.find_element(By.ID, "orders-location-0")).select_by_visible_text("Front-Left Chest")
        for kind in ("sales", "production", "art"):
            self.driver.find_element(By.ID, "orders-note-" + kind).send_keys("Shared " + kind + " note")
        self.driver.switch_to.default_content()
        self.wait.until(lambda d: d.execute_script("return fixtureController.states.get(11)?.draft.notes.art === 'Shared art note';"))
        self.driver.execute_script("reopenPopup();")
        self.popup()
        self.assertEqual(self.driver.find_element(By.ID, "orders-note-sales").get_attribute("value"), "Shared sales note")
        self.assertEqual(Select(self.driver.find_element(By.ID, "orders-location-0")).first_selected_option.text, "Front-Left Chest")
        self.click_wait("orders-preview", "Every included order validates")
        self.driver.save_screenshot(str(self.artifacts / "popup-preview.png"))
        self.click_wait("orders-apply", "Changes are applied")
        self.driver.find_element(By.ID, "orders-save").click()
        # Close the popup while the background coordinator owns the save.
        self.driver.switch_to.default_content()
        self.driver.execute_script("document.getElementById('popup').src='about:blank';")
        self.wait.until(lambda d: d.execute_script("return fixtureController.states.get(11)?.phase === 'complete';"))
        self.driver.execute_script("reopenPopup();")
        self.popup()
        self.assertEqual(len(self.driver.find_elements(By.CSS_SELECTOR, ".order-row.verified")), 2)
        self.driver.save_screenshot(str(self.artifacts / "popup-complete.png"))
        self.driver.switch_to.default_content()
        self.assertEqual(self.driver.execute_script("return document.getElementById('tab-3').contentWindow.fixtureResource.salesNotes;"), "Keep original Sales Note")
        for tab in (1, 2):
            result = self.driver.execute_script("return document.getElementById('tab-'+arguments[0]).contentWindow.fixtureResource;", tab)
            for kind in ("sales", "production", "art"):
                self.assertTrue(result[kind + "Notes"].endswith("Shared " + kind + " note"))

    def test_popup_never_applies_an_old_draft_after_the_new_draft_fails_to_reach_the_worker(self):
        self.driver.get(self.url + "/harness")
        self.wait.until(lambda d: d.execute_script("return !!window.fixtureApi;"))
        self.popup()
        self.click_wait("orders-edit", "Choose your shared edits")
        self.driver.switch_to.default_content()
        self.driver.execute_script("""
          const original=fixtureApi.runtime.sendMessage;
          fixtureApi.runtime.sendMessage=message=>message.action==='draft'
            ? Promise.reject(Error('Fixture draft delivery failed')) : original(message);
        """)
        self.popup()
        Select(self.driver.find_element(By.ID, "orders-location-0")).select_by_visible_text("Front-Left Chest")
        self.wait.until(lambda d: "Fixture draft delivery failed" in d.find_element(By.ID, "orders-status").text)
        self.driver.find_element(By.ID, "orders-apply").click()
        self.wait.until(lambda d: "Fixture draft delivery failed" in d.find_element(By.ID, "orders-status").text)
        self.assertTrue(self.driver.find_element(By.ID, "orders-location-0").is_enabled())
        self.driver.switch_to.default_content()
        for tab in (1, 2):
            self.assertEqual(self.driver.execute_script("return document.getElementById('tab-'+arguments[0]).contentWindow.fixtureChangeCount;", tab), 0)


if __name__ == "__main__":
    unittest.main()
