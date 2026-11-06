"""Orders From defaults against isolated Salesforce markup; never opens Salesforce."""

import http.server
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "crm-order-dark-mode-extension"
ORDERS = "Orders <orders@rushordertees.com>"
PERSONAL = "Agent <agent@example.test>"


class SalesforceSenderManifestTests(unittest.TestCase):
    def test_sender_script_is_injected_on_salesforce_without_changing_crm_scope(self):
        manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
        sender, = [entry for entry in manifest["content_scripts"]
                   if "salesforce-email-sender.js" in entry["js"]]
        self.assertEqual(sender["matches"], ["https://*.salesforce.com/*", "https://*.force.com/*"])
        self.assertTrue(sender["all_frames"])
        self.assertEqual(manifest["permissions"], ["storage", "tabs", "webNavigation"])
        for entry in manifest["content_scripts"]:
            if "content.js" in entry["js"]:
                self.assertEqual(entry["matches"], ["https://crm2.legacy.printfly.com/*"])


class SalesforceSenderBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                def version(path):
                    return tuple(int(number) for number in next(
                        part for part in path.parts if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", part)
                    ).split("."))
                driver_path = str(max(candidates, key=version))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver for offline Salesforce sender tests.")
        fixture = (ROOT / "tests/fixtures/salesforce_sender.html").read_bytes()

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
        artifacts = ROOT / "runtime/debug/salesforce_sender_tests"
        artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for argument in ("--headless=new", "--disable-background-networking", "--disable-gpu",
                         "--no-sandbox", "--window-size=1200,1100"):
            options.add_argument(argument)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)
        cls.source = (EXTENSION / "salesforce-email-sender.js").read_text(encoding="utf-8")

    def setUp(self):
        self.driver.get(self.url)

    def tearDown(self):
        self.assertEqual(self.driver.execute_script("return fixture.sends"), 0)
        self.assertEqual(self.driver.execute_script("return fixture.unrelatedClicks"), 0)
        self.assertEqual(self.driver.execute_script("return fixture.recipientClicks"), 0)

    def script(self, expression, *args):
        return self.driver.execute_script(expression, *args)

    def wait(self, expression):
        return WebDriverWait(self.driver, 7).until(lambda _: self.script("return " + expression))

    def settle(self, milliseconds=300):
        self.driver.execute_async_script("setTimeout(arguments[arguments.length - 1], arguments[0])", milliseconds)

    def test_lazy_portaled_menu_selects_exact_orders_and_preserves_draft_and_focus(self):
        self.script("addComposer('first', {delay: 350}); document.querySelector('input').focus();")
        self.driver.execute_script(self.source)
        self.wait("fixture.composers.first.value.textContent === 'Orders <orders@rushordertees.com>'")
        self.wait("document.activeElement === document.querySelector('input')")
        self.assertEqual(self.script("return fixture.composers.first.choices"), [ORDERS])
        self.assertEqual(self.script("return document.querySelector('input').value"), "Fixture subject")
        self.assertEqual(self.script("return document.querySelector('[contenteditable]').textContent"),
                         "Email body mentioning orders@rushordertees.com")
        self.assertEqual(self.script("return fixture.composers.first.control.getAttribute('aria-expanded')"), "false")

    def test_new_composers_are_discovered_without_reloading_and_processed_separately(self):
        self.driver.execute_script(self.source)
        self.script("addComposer('first'); addComposer('second');")
        self.wait("Object.values(fixture.composers).every(s => s.value.textContent.startsWith('Orders <'))")
        self.script("fixture.composers.first.composer.remove(); addComposer('third');")
        self.wait("fixture.composers.third.choices.length === 1")
        self.assertEqual(self.script("return Object.values(fixture.composers).map(s => s.choices)"),
                         [[ORDERS], [ORDERS], [ORDERS]])

    def test_already_selected_orders_is_untouched(self):
        self.script("addComposer('first', {current: arguments[0]})", ORDERS)
        self.driver.execute_script(self.source)
        self.settle()
        self.assertEqual(self.script("return fixture.composers.first.opens"), 0)

    def test_manual_sender_override_survives_later_mutations(self):
        self.script("addComposer('first')")
        self.driver.execute_script(self.source)
        self.wait("fixture.composers.first.choices.length === 1")
        self.driver.find_element(By.CSS_SELECTOR, '#first [role="combobox"]').click()
        self.wait("!fixture.composers.first.menu.hidden")
        self.driver.find_element(By.CSS_SELECTOR, '[role="option"][title="Agent <agent@example.test>"]').click()
        self.script("document.querySelector('[contenteditable]').textContent = 'Typing more';")
        self.settle()
        self.assertEqual(self.script("return fixture.composers.first.value.textContent"), PERSONAL)
        self.assertEqual(self.script("return fixture.composers.first.choices"), [ORDERS, PERSONAL])

    def test_hidden_and_disabled_composers_wait_until_available(self):
        self.script("addComposer('first', {hidden: true}); addComposer('second', {disabled: true});")
        self.driver.execute_script(self.source)
        self.settle()
        self.assertEqual(self.script("return Object.values(fixture.composers).map(s => s.opens)"), [0, 0])
        self.script("fixture.composers.first.composer.hidden = false; fixture.composers.second.control.setAttribute('aria-disabled', 'false');")
        self.wait("Object.values(fixture.composers).every(s => s.choices.length === 1)")
        self.script("fixture.composers.first.composer.hidden = true")
        self.settle()
        self.script("fixture.composers.first.value.textContent = arguments[0]; fixture.composers.first.composer.hidden = false;", PERSONAL)
        self.wait("fixture.composers.first.choices.length === 2")

    def test_missing_ambiguous_or_disabled_orders_never_selects_a_lookalike_or_reopens(self):
        for options in ({"orders": False}, {"duplicate": True}, {"ordersDisabled": True}):
            with self.subTest(options=options):
                self.driver.get(self.url)
                self.script("addComposer('first', arguments[0])", options)
                self.driver.execute_script(self.source)
                self.wait("fixture.composers.first.closes === 1")
                self.script("document.querySelector('[contenteditable]').textContent += ' More text';")
                self.settle()
                self.assertEqual(self.script("return fixture.composers.first.value.textContent"), PERSONAL)
                self.assertEqual(self.script("return fixture.composers.first.choices"), [])
                self.assertEqual(self.script("return fixture.composers.first.opens"), 1)

    def test_user_typing_during_loading_cancels_pending_selection(self):
        self.script("addComposer('first', {delay: 600})")
        self.driver.execute_script(self.source)
        self.wait("fixture.composers.first.opens === 1")
        self.driver.find_element(By.CSS_SELECTOR, "#first input").send_keys(" manual edit")
        self.settle(800)
        self.assertEqual(self.script("return fixture.composers.first.choices"), [])
        self.assertEqual(self.script("return fixture.composers.first.value.textContent"), PERSONAL)

    def test_closed_composer_during_loading_is_abandoned(self):
        self.script("addComposer('first', {delay: 600})")
        self.driver.execute_script(self.source)
        self.wait("fixture.composers.first.opens === 1")
        self.script("fixture.composers.first.composer.remove(); addComposer('second');")
        self.wait("fixture.composers.second.choices.length === 1")
        self.assertEqual(self.script("return fixture.composers.first.choices"), [])

    def test_rejected_selection_is_not_retried(self):
        self.script("addComposer('first', {rejectSelection: true})")
        self.driver.execute_script(self.source)
        self.wait("fixture.composers.first.closes === 1")
        self.script("document.querySelector('[contenteditable]').textContent += ' More text';")
        self.settle()
        self.assertEqual(self.script("return fixture.composers.first.value.textContent"), PERSONAL)
        self.assertEqual(self.script("return fixture.composers.first.choices"), [ORDERS])


if __name__ == "__main__":
    unittest.main()
