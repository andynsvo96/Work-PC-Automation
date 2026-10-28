"""Real editor interaction against an isolated local Flask fixture."""

import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest

from flask import Flask, Response
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.select import Select
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server

from routes.salesforce_template_routes import register_salesforce_template_routes
from salesforce_templates import SalesforceTemplateStore


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "runtime/debug/salesforce_template_editor_tests"


def fixture_app(store):
    app = Flask(__name__, static_folder=str(ROOT / "static"))
    register_salesforce_template_routes(app, store=store)

    @app.get("/")
    def index():
        html = (ROOT / "ui_panel.html").read_text(encoding="utf-8")
        style = re.search(r"<style>([\s\S]*?)</style>", html).group(1)
        section = re.search(
            r"<section class='settings-section' data-settings-section='salesforce-templates'>[\s\S]*?</section>",
            html,
        ).group(0).replace("class='settings-section'", "class='settings-section active'", 1)
        return Response(
            "<!doctype html><html data-theme='light'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Salesforce template editor — local fixture</title><style>" + style + "</style>"
            "<link rel='stylesheet' href='/static/salesforce-template-editor.css'>"
            "<script>window.api=async(path,opts={})=>{const r=await fetch(path,opts);"
            "const d=await r.json();if(!r.ok)throw new Error(d.message);return d;};</script>"
            "<script src='/static/salesforce-template-editor.js' defer></script>"
            "</head><body><div class='wrap'><h2>Settings</h2><div class='settings-panel-card'>"
            + section + "</div></div></body></html>", mimetype="text/html"
        )

    app.config["preview_action_count"] = 0

    @app.get("/unsafe-preview-action")
    def unsafe_preview_action():
        app.config["preview_action_count"] += 1
        return "Fixture only"

    return app


class SalesforceTemplateEditorBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("SALESFORCE_EDITOR_CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                def version(path):
                    return tuple(int(part) for part in next(
                        part for part in path.parts if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", part)
                    ).split("."))
                driver_path = str(max(candidates, key=version))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver for the isolated template editor fixture.")
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix="editor-", dir=ARTIFACTS)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.store = SalesforceTemplateStore(Path(cls.temp.name) / "templates.json")
        cls.app = fixture_app(cls.store)
        cls.http = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.addClassCleanup(cls.http.server_close)
        cls.addClassCleanup(cls.http.shutdown)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}"
        options = webdriver.ChromeOptions()
        for argument in ("--headless=new", "--disable-gpu", "--no-sandbox",
                         "--disable-background-networking", "--window-size=1250,1100"):
            options.add_argument(argument)
        options.add_argument("--user-data-dir=" + str(Path(cls.temp.name) / "profile"))
        options.add_argument("--host-resolver-rules=MAP *.images.example.test 127.0.0.1, MAP images.example.test 127.0.0.1")
        if os.environ.get("SALESFORCE_EDITOR_CHROME"):
            options.binary_location = os.environ["SALESFORCE_EDITOR_CHROME"]
        try:
            cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 4551:
                raise unittest.SkipTest("Windows Application Control blocked the local fixture's ChromeDriver.") from exc
            raise
        cls.addClassCleanup(cls.driver.quit)
        cls.wait = WebDriverWait(cls.driver, 10)

    def setUp(self):
        self.body = (
            '<html><head><title>Email</title></head><body style="height:auto;">\n'
            '<p>Hello,</p><p>Thank you [REQUEST] &amp; more.</p>'
            f'<p><a href="{self.url}/invoice">View invoice</a></p>'
            '<img src="https://images.example.test/logo.png" alt="Logo"></body></html>\n'
        )
        self.store.path.write_text(json.dumps({"schema_version": 1, "templates": [
            {"id": "first", "name": "[AUTO] Example", "subject": "Order #[ORDER-NUMBER]", "body_html": self.body},
            {"id": "second", "name": "[AUTO] Second", "subject": "Other subject", "body_html": "<p>Second body</p>"},
        ]}), encoding="utf-8")
        self.driver.get(self.url)
        self.wait.until(lambda driver: len(Select(driver.find_element(By.ID, "sfTemplateSelect")).options) == 2)
        self.wait.until(lambda driver: driver.find_element(By.ID, "sfTemplateName").get_attribute("value") == "[AUTO] Example")
        self.wait.until(lambda driver: driver.execute_script(
            "return document.getElementById('sfTemplateBodyFrame').contentDocument.body.contentEditable==='true';"
        ))

    def input(self, element_id, value):
        element = self.driver.find_element(By.ID, element_id)
        element.clear()
        element.send_keys(value)

    def mode(self, name):
        self.driver.find_element(By.CSS_SELECTOR, f"[data-sf-mode='{name}']").click()
        self.wait.until(lambda driver: driver.find_element(
            By.CSS_SELECTOR, f"[data-sf-mode='{name}']").get_attribute("aria-pressed") == "true")
        if name == "formatted":
            self.wait.until(lambda driver: driver.find_element(By.ID, "sfTemplateLink").is_enabled())

    def select_body_text(self, text=None):
        self.driver.execute_script("""
            const frame=document.getElementById('sfTemplateBodyFrame'), doc=frame.contentDocument;
            const range=doc.createRange(), selected=arguments[0];
            if(selected) {
              const walker=doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
              let node; while(node=walker.nextNode()) {
                const at=node.textContent.indexOf(selected);
                if(at>=0){range.setStart(node,at);range.setEnd(node,at+selected.length);break;}
              }
            } else {range.selectNodeContents(doc.body);range.collapse(false);}
            const selection=doc.getSelection();selection.removeAllRanges();selection.addRange(range);
            doc.body.focus();doc.dispatchEvent(new Event('selectionchange'));
        """, text)

    def save(self):
        self.driver.find_element(By.ID, "sfTemplateSave").click()
        self.wait.until(lambda driver: driver.find_element(By.ID, "sfTemplateStatus").get_attribute("data-state") == "saved")

    def test_view_toggles_and_metadata_save_preserve_exact_source(self):
        self.mode("html")
        self.assertEqual(self.driver.find_element(By.ID, "sfTemplateBodyHtml").get_attribute("value"), self.body)
        self.mode("formatted")
        self.input("sfTemplateName", "[AUTO] Renamed Example")
        self.input("sfTemplateSubject", "New title #[ORDER-NUMBER]")
        self.save()
        first = next(row for row in self.store.list_templates() if row["id"] == "first")
        self.assertEqual(first["body_html"], self.body)
        self.assertEqual(first["subject"], "New title #[ORDER-NUMBER]")
        self.assertEqual(Select(self.driver.find_element(By.ID, "sfTemplateSelect")).first_selected_option.text, "[AUTO] Renamed Example")
        self.driver.refresh()
        self.wait.until(lambda driver: driver.find_element(By.ID, "sfTemplateSubject").get_attribute("value") == first["subject"])
        self.driver.save_screenshot(str(ARTIFACTS / "editor_light.png"))

    def test_formatting_links_images_and_placeholders_survive_save(self):
        self.select_body_text("Thank you")
        self.driver.find_element(By.CSS_SELECTOR, "[data-sf-command='bold']").click()
        self.driver.find_element(By.CSS_SELECTOR, "[data-sf-command='underline']").click()
        Select(self.driver.find_element(By.ID, "sfTemplateSize")).select_by_value("5")
        self.select_body_text("Hello")
        self.driver.find_element(By.ID, "sfTemplateLink").click()
        self.input("sfTemplateAssetUrl", self.url + "/new-invoice?x=1&y=2")
        self.driver.find_element(By.CSS_SELECTOR, "#sfTemplateAssetForm button[type=submit]").click()
        self.select_body_text()
        self.driver.find_element(By.ID, "sfTemplateImage").click()
        self.input("sfTemplateAssetUrl", "https://another.images.example.test/logo.png?added=1")
        self.input("sfTemplateAssetText", "Added logo")
        self.driver.find_element(By.CSS_SELECTOR, "#sfTemplateAssetForm button[type=submit]").click()
        self.wait.until(lambda driver: driver.find_element(By.ID, "sfTemplateLink").is_enabled())
        self.assertIn("https://another.images.example.test", self.driver.execute_script(
            "return document.getElementById('sfTemplateBodyFrame').contentDocument.querySelector('meta[http-equiv]').content;"
        ))
        self.save()
        saved = next(row for row in self.store.list_templates() if row["id"] == "first")["body_html"]
        self.assertIn("[REQUEST]", saved)
        self.assertIn("View invoice", saved)
        self.assertIn("Added logo", saved)
        self.assertIn("/new-invoice?x=1&amp;y=2", saved)
        self.assertTrue("font-weight: bold" in saved or "<b>" in saved or "<strong>" in saved)
        self.assertTrue("underline" in saved or "<u>" in saved)
        self.mode("html")
        self.assertEqual(self.driver.find_element(By.ID, "sfTemplateBodyHtml").get_attribute("value"), saved)
        self.driver.execute_script("document.documentElement.dataset.theme='dark';")
        self.mode("formatted")
        self.assertEqual(self.driver.execute_script("""
            const doc=document.getElementById('sfTemplateBodyFrame').contentDocument;
            const span=Array.from(doc.querySelectorAll('span')).find(node=>node.textContent==='Thank you');
            return doc.defaultView.getComputedStyle(span).fontSize;
        """), "24px")
        self.driver.save_screenshot(str(ARTIFACTS / "editor_dark.png"))
        self.driver.set_window_size(420, 1000)
        self.assertTrue(self.driver.execute_script("return document.documentElement.scrollWidth<=window.innerWidth;"))
        self.driver.set_window_size(1250, 1100)

    def test_html_preview_cannot_execute_scripts_or_navigate_parent(self):
        self.app.config["preview_action_count"] = 0
        malicious = self.body.replace("</body>", '<script>parent.editorInjected=true;</script>'
                                      '<img src="bad:" onerror="parent.editorInjected=true">'
                                      f'<img src="{self.url}/unsafe-preview-action">'
                                      f'<div style="background-image:url({self.url}/unsafe-preview-action)">Test</div>'
                                      '<a href="javascript:parent.editorInjected=true">Unsafe</a></body>')
        self.mode("html")
        self.input("sfTemplateBodyHtml", malicious)
        self.mode("formatted")
        self.assertFalse(self.driver.execute_script("return window.editorInjected===true;"))
        self.assertEqual(self.driver.execute_script(
            "return document.getElementById('sfTemplateBodyFrame').contentDocument.querySelectorAll('script,[onerror],a[href^=\"javascript:\"]').length;"
        ), 0)
        self.assertEqual(self.app.config["preview_action_count"], 0)
        self.save()
        self.assertEqual(next(row for row in self.store.list_templates() if row["id"] == "first")["body_html"], malicious)

    def test_font_color_lists_indent_and_alignment(self):
        self.select_body_text("Hello")
        Select(self.driver.find_element(By.ID, "sfTemplateFont")).select_by_value("Arial")
        self.driver.execute_script("""
            const input=document.getElementById('sfTemplateColor');
            input.value='#008000';input.dispatchEvent(new Event('input',{bubbles:true}));
        """)
        self.driver.find_element(By.CSS_SELECTOR, "[data-sf-command='justifyCenter']").click()
        self.select_body_text("Thank you")
        for command in ("insertOrderedList", "indent", "outdent", "insertUnorderedList"):
            self.driver.find_element(By.CSS_SELECTOR, f"[data-sf-command='{command}']").click()
        self.save()
        self.mode("html")
        self.mode("formatted")
        styles = self.driver.execute_script("""
            const doc=document.getElementById('sfTemplateBodyFrame').contentDocument;
            const span=Array.from(doc.querySelectorAll('span')).find(node=>node.textContent==='Hello');
            const style=doc.defaultView.getComputedStyle(span);
            return {font:style.fontFamily,color:style.color,align:doc.defaultView.getComputedStyle(span.closest('p')).textAlign,lists:doc.querySelectorAll('ul').length};
        """)
        self.assertIn("Arial", styles["font"])
        self.assertEqual(styles["color"], "rgb(0, 128, 0)")
        self.assertEqual(styles["align"], "center")
        self.assertGreaterEqual(styles["lists"], 1)

    def test_conflict_retains_draft_and_canceling_switch_keeps_selection(self):
        original = next(row for row in self.store.list_templates() if row["id"] == "first")
        self.input("sfTemplateSubject", "My unsaved title")
        Select(self.driver.find_element(By.ID, "sfTemplateSelect")).select_by_value("second")
        self.driver.switch_to.alert.dismiss()
        self.assertEqual(Select(self.driver.find_element(By.ID, "sfTemplateSelect")).first_selected_option.get_attribute("value"), "first")
        self.store.save_template("first", {**original, "subject": "Changed in another tab"})
        self.driver.find_element(By.ID, "sfTemplateSave").click()
        self.wait.until(lambda driver: driver.find_element(By.ID, "sfTemplateStatus").get_attribute("data-state") == "error")
        self.assertEqual(self.driver.find_element(By.ID, "sfTemplateSubject").get_attribute("value"), "My unsaved title")
        self.assertIn("changed since", self.driver.find_element(By.ID, "sfTemplateStatus").text)
        self.driver.find_element(By.ID, "sfTemplateReload").click()
        self.driver.switch_to.alert.accept()
        self.wait.until(lambda driver: driver.find_element(By.ID, "sfTemplateSubject").get_attribute("value") == "Changed in another tab")


if __name__ == "__main__":
    unittest.main()
