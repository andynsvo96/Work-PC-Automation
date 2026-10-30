"""Offline CRM list highlighting checks; no live orders or Automation server."""

import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "crm-order-dark-mode-extension"


class RushListBrowserTests(unittest.TestCase):
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

        fixture = (ROOT / "tests/fixtures/crm_rush_list.html").read_bytes()

        class FixtureHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(fixture)

            def log_message(self, *_args):
                pass

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.addClassCleanup(cls.httpd.server_close)
        cls.addClassCleanup(cls.httpd.shutdown)
        cls.http_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.http_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"
        artifact_dir = ROOT / "runtime/debug/rush_list_tests"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifact_dir)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for arg in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1100,1000"):
            options.add_argument(arg)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)

    def show_list(self, day=30, route="/report/967"):
        self.driver.get(self.base_url + route)
        self.driver.execute_script("""
            window.RealDate = Date;
            window.fixtureNow = new RealDate(2026, 9, arguments[0], 12).getTime();
            window.Date = class extends RealDate {
                constructor(...args) { super(...(args.length ? args : [window.fixtureNow])); }
                static now() { return window.fixtureNow; }
            };
            window.fixtureMidnightTimers = [];
            const realSetTimeout = window.setTimeout.bind(window);
            window.setTimeout = (callback, delay, ...args) => {
                if (delay > 60000) {
                    fixtureMidnightTimers.push({callback, delay});
                    return 0;
                }
                return realSetTimeout(callback, delay, ...args);
            };
            const style = document.createElement('style');
            style.textContent = arguments[1];
            document.head.append(style);
        """, day, (EXTENSION / "rush-list-highlight.css").read_text(encoding="utf-8"))
        self.driver.execute_script((EXTENSION / "rush-list-highlight.js").read_text(encoding="utf-8"))
        self.driver.execute_async_script("requestAnimationFrame(() => requestAnimationFrame(arguments[0]));")

    def highlighted(self):
        return self.driver.execute_script("return [...document.querySelectorAll('.crm-friday-rush')].map(row => row.id).sort();")

    def wait_for_rows(self, ids):
        WebDriverWait(self.driver, 5).until(lambda _: self.highlighted() == sorted(ids))

    def test_friday_highlights_the_fulfillment_field_and_overrides_striped_cells(self):
        self.show_list()
        self.assertEqual(self.highlighted(), ["attribute", "bound", "card", "due-column", "monday"])
        color = self.driver.execute_script("return getComputedStyle(document.querySelector('#monday td')).backgroundColor;")
        self.assertEqual(color, "rgb(34, 236, 72)")
        self.assertTrue(self.driver.execute_script("return document.querySelector('#native').classList.contains('native-green');"))
        self.assertFalse(self.driver.execute_script("return document.documentElement.classList.contains('crm-order-dark-mode');"))

    def test_other_weekdays_and_order_pages_keep_their_existing_colors(self):
        for day, route in [(29, "/report/967"), (31, "/report/967"), (30, "/order/1234567"), (30, "/app#/order/1234567")]:
            with self.subTest(day=day, route=route):
                self.show_list(day, route)
                self.assertEqual(self.highlighted(), [])

    def test_live_text_changes_and_reused_rows_remove_stale_highlights(self):
        self.show_list()
        self.driver.execute_script("""
            document.querySelector('#monday .due').firstChild.data = '11/03/2026';
            document.querySelector('#card .due').textContent = '2026-11-03';
            document.querySelector('#attribute').setAttribute('data-fulfillment-date', '2026-11-03');
        """)
        self.wait_for_rows(["bound", "due-column"])
        self.driver.execute_script("document.querySelector('#tuesday td:last-child').textContent = '11/02/2026';")
        self.wait_for_rows(["bound", "due-column", "tuesday"])

    def test_new_rows_and_embedded_route_navigation_refresh_without_reloading(self):
        self.show_list(route="/app#/reports/orders")
        self.driver.execute_script("""
            const row = document.createElement('div');
            row.id = 'added';
            row.innerHTML = '<a href="/order/1234599">1234599</a> Due Date: 11/02/2026';
            document.body.append(row);
        """)
        self.wait_for_rows(["added", "attribute", "bound", "card", "due-column", "monday"])
        self.driver.execute_script("location.hash = '#/order/1234567';")
        self.wait_for_rows([])
        self.driver.execute_script("location.hash = '#/reports/orders';")
        self.wait_for_rows(["added", "attribute", "bound", "card", "due-column", "monday"])

    def test_date_rollover_removes_only_extension_highlights(self):
        self.show_list()
        self.driver.execute_script("fixtureNow = new RealDate(2026, 9, 31, 0, 1).getTime(); window.dispatchEvent(new Event('focus'));")
        self.wait_for_rows([])
        self.assertEqual(self.driver.execute_script("return getComputedStyle(document.querySelector('#native td')).backgroundColor;"), "rgb(34, 236, 72)")

    def test_midnight_timer_enables_friday_and_clears_saturday_without_user_input(self):
        self.show_list(day=29)
        self.assertEqual(self.highlighted(), [])
        self.assertEqual(self.driver.execute_script("return fixtureMidnightTimers[0].delay;"), 12 * 60 * 60 * 1000 + 50)
        self.driver.execute_script("""
            fixtureNow = new RealDate(2026, 9, 30, 0, 0, 1).getTime();
            fixtureMidnightTimers.shift().callback();
        """)
        self.wait_for_rows(["attribute", "bound", "card", "due-column", "monday"])
        self.driver.execute_script("""
            fixtureNow = new RealDate(2026, 9, 31, 0, 0, 1).getTime();
            fixtureMidnightTimers.shift().callback();
        """)
        self.wait_for_rows([])

    def test_printing_uses_the_original_crm_row_colors(self):
        self.show_list()
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        try:
            self.assertEqual(self.driver.execute_script("return getComputedStyle(document.querySelector('#monday td')).backgroundColor;"), "rgb(238, 238, 238)")
        finally:
            self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})


if __name__ == "__main__":
    unittest.main()
