"""Both Salesforce texting layouts using synthetic markup and a loopback server."""

import http.server
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

from selenium import webdriver
from selenium.webdriver.chrome.service import Service

from workers import salesforce_text_messaging as texts


ROOT = Path(__file__).resolve().parents[1]
EMAIL = "customer@example.test"
ORDER = "4705293"
MESSAGES = ["Hello, this is RushOrderTees. Payment of $6.00 per sleeve prints is required.",
            "https://example.test/order-invoice/test?a=1&b=2"]


class SalesforceTextBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                def version(path):
                    return tuple(int(number) for number in next(part for part in path.parts
                        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", part)).split("."))
                driver_path = str(max(candidates, key=version))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver for offline Salesforce text tests.")
        fixture = (ROOT / "tests/fixtures/salesforce_twilio.html").read_bytes()

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
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        cls.artifacts = ROOT / "runtime/debug/salesforce_text_browser"
        cls.artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=cls.artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for argument in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox"):
            options.add_argument(argument)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=self.artifacts)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "texts.json"
        receipt = texts.prepare_receipt(self.path, ORDER, EMAIL, MESSAGES)
        texts.confirm_email(self.path, receipt)

    def navigate(self, kind="Account"):
        record_id = "003000000000001AAA" if kind == "Contact" else "001000000000001AAA"
        self.driver.get(f"{self.base_url}/lightning/r/{kind}/{record_id}/view")

    def send(self, timeout=1):
        return texts.send_text_messages(self.driver, self.path, ORDER, EMAIL, timeout=timeout)

    def script(self, code, *args):
        return self.driver.execute_script(code, *args)

    def test_account_bottom_right_panel_sends_separate_messages_and_ignores_cached_record(self):
        self.navigate()
        result = self.send()
        self.assertEqual(result["confirmed_count"], 2)
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)
        self.assertEqual(self.script("return fixture.textClicks"), 0)
        self.assertEqual(self.script("return fixture.unrelatedClicks"), 0)
        self.assertTrue(self.send()["skipped"])
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)

    def test_contact_opens_text_tab_and_waits_for_lazy_shadow_dom_composer(self):
        self.navigate("Contact")
        self.assertTrue(self.send()["sent"])
        self.assertEqual(self.script("return fixture.textClicks"), 1)
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)
        self.assertEqual(self.script("return fixture.unrelatedClicks"), 0)

    def test_old_outgoing_and_incoming_matches_do_not_confirm_an_uncertain_new_send(self):
        self.navigate()
        self.script("addRow(fixture.panel, arguments[0], 'delivered'); addRow(fixture.panel, arguments[0], 'received', false); fixture.noConfirmation = true;", MESSAGES[0])
        with self.assertRaisesRegex(texts.TextMessageError, "text 1 was not confirmed"):
            self.send(timeout=0)
        saved = texts.load_receipt(self.path, ORDER, EMAIL)
        self.assertEqual(saved["messages"][0]["baseline_count"], 1)
        with self.assertRaisesRegex(texts.TextMessageError, "previous.*unconfirmed"):
            self.send()
        self.assertEqual(self.script("return fixture.sends"), MESSAGES[:1])

    def test_recorded_message_can_continue_while_delivery_status_is_pending(self):
        self.navigate()
        self.script("fixture.delay = 120;")
        self.assertTrue(self.send()["sent"])
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)

    def test_native_entry_binds_lightning_model_and_recovers_an_exact_unbound_draft(self):
        self.navigate()
        self.script("fixture.input.value = arguments[0]; fixture.input.dispatchEvent(new Event('input', {bubbles:true}));", MESSAGES[0])
        self.assertTrue(self.script("return fixture.button.disabled"))
        self.assertTrue(self.send()["sent"])
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)

    def test_native_entry_scrolls_above_a_fixed_salesforce_utility_bar(self):
        self.navigate()
        self.script("""
          fixture.root.style.marginTop = '1800px';
          const utility = document.createElement('div');
          utility.style.cssText = 'position:fixed;bottom:0;height:110px;width:100%;background:#ccc;z-index:10000';
          utility.textContent = 'Salesforce utility bar'; document.body.append(utility);
        """)
        self.assertTrue(self.send()["sent"])
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)

    def test_undelivered_outgoing_records_allow_both_texts_and_do_not_repeat_on_retry(self):
        self.navigate()
        self.script("fixture.status = 'undelivered';")
        result = self.send()
        self.assertEqual([entry["status"] for entry in result["messages"]], ["undelivered", "undelivered"])
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)
        self.assertTrue(self.send()["skipped"])
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)

    def test_pending_first_send_recovers_from_history_before_sending_only_invoice(self):
        self.navigate()
        self.script("fixture.noConfirmation = true;")
        with self.assertRaisesRegex(texts.TextMessageError, "text 1 was not confirmed"):
            self.send(timeout=0)
        self.script("addRow(fixture.panel, arguments[0], 'undelivered'); fixture.noConfirmation = false;", MESSAGES[0])
        result = self.send()
        self.assertEqual((result["recovered_count"], result["sent_count"]), (1, 1))
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)

    def test_first_confirmed_then_disabled_input_can_resume_invoice_without_repeating_first(self):
        self.navigate()
        self.script("fixture.disableSecond = true;")
        with self.assertRaisesRegex(texts.TextMessageError, "input is disabled"):
            self.send()
        self.assertEqual([entry["state"] for entry in texts.load_receipt(self.path, ORDER, EMAIL)["messages"]],
                         ["confirmed", "ready"])
        self.script("fixture.input.disabled = false;")
        self.assertEqual(self.send()["sent_count"], 1)
        self.assertEqual(self.script("return fixture.sends"), MESSAGES)

    def test_wrong_active_customer_cannot_use_matching_email_on_hidden_record_or_page(self):
        self.navigate()
        self.script("const email = fixture.root.querySelector('.customer-email'); email.href = 'mailto:other@example.test'; email.textContent = 'other@example.test';")
        with self.assertRaisesRegex(texts.TextMessageError, "does not show the CRM customer email"):
            self.send()
        self.assertEqual(self.script("return fixture.sends"), [])

    def test_multiple_visible_panels_and_other_drafts_stop_before_send(self):
        self.navigate()
        self.script("makePanel(fixture.root, true);")
        with self.assertRaisesRegex(texts.TextMessageError, "More than one"):
            self.send()
        self.navigate()
        self.script("fixture.input.value = 'Keep this unsent draft';")
        with self.assertRaisesRegex(texts.TextMessageError, "another draft"):
            self.send()
        self.assertEqual(self.script("return fixture.input.value"), "Keep this unsent draft")
        self.assertEqual(self.script("return fixture.sends"), [])

    def test_opt_in_block_is_respected_and_hidden_error_placeholders_are_ignored(self):
        self.navigate()
        self.script("fixture.panel.querySelector('.add-message.hide').classList.remove('hide');")
        with self.assertRaisesRegex(texts.TextMessageError, "Opt-In Required"):
            self.send()
        self.assertEqual(self.script("return fixture.sends"), [])

    def test_missing_widget_and_contact_tab_time_out_without_submission(self):
        self.navigate()
        self.script("fixture.panel.remove();")
        with self.assertRaisesRegex(texts.TextMessageError, "did not become ready"):
            self.send(timeout=0)
        self.navigate("Contact")
        self.script("fixture.textTab.remove();")
        with self.assertRaisesRegex(texts.TextMessageError, "Contact Text tab is missing"):
            self.send(timeout=0)
        self.assertEqual(self.script("return fixture.sends"), [])


if __name__ == "__main__":
    unittest.main()
