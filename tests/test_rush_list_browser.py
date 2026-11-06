"""Offline CRM list highlighting and native selection; no live orders/server."""

import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
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
        selection_fixture = (ROOT / "tests/fixtures/crm_select_applicable.html").read_bytes()

        class FixtureHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlsplit(self.path)
                is_selection = url.path == "/report/select-applicable" or parse_qs(url.query).get("fixture") == ["selection"]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(selection_fixture if is_selection else fixture)

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
        return self.driver.execute_script("return [...document.querySelectorAll('.crm-list-rush')].map(row => row.id).sort();")

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
        for day, route in [(29, "/report/967"), (33, "/report/967"), (30, "/order/1234567"), (30, "/app#/order/1234567")]:
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
        self.wait_for_rows(["bound", "due-column"])
        self.driver.execute_script("document.querySelector('#tuesday td:nth-child(2)').textContent = '10/30/2026';")
        self.wait_for_rows(["bound", "due-column", "tuesday"])
        self.driver.execute_script("document.querySelector('#bound span[ng-bind*=scheduledPrintDate]').textContent = '11/02/2026';")
        self.wait_for_rows(["due-column", "tuesday"])

    def test_new_rows_and_embedded_route_navigation_refresh_without_reloading(self):
        self.show_list(route="/app#/reports/orders")
        self.driver.execute_script("""
            const row = document.createElement('div');
            row.id = 'added';
            row.innerHTML = '<a href="/order/1234599">1234599</a> Production Date: 10/30/2026 Due Date: 11/02/2026';
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
        self.wait_for_rows(["tuesday"])
        self.assertEqual(self.driver.execute_script("return getComputedStyle(document.querySelector('#native td')).backgroundColor;"), "rgb(34, 236, 72)")

    def test_midnight_timer_switches_from_friday_to_weekend_and_clears_monday(self):
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
        self.wait_for_rows(["tuesday"])
        self.driver.execute_script("""
            fixtureNow = new RealDate(2026, 10, 1, 0, 0, 1).getTime();
            fixtureMidnightTimers.shift().callback();
        """)
        self.wait_for_rows(["tuesday"])
        self.driver.execute_script("""
            fixtureNow = new RealDate(2026, 10, 2, 0, 0, 1).getTime();
            fixtureMidnightTimers.shift().callback();
        """)
        self.wait_for_rows([])

    def test_both_weekend_days_require_monday_production_and_tuesday_fulfillment(self):
        for day in [31, 32]:
            with self.subTest(day=day):
                self.show_list(day)
                self.assertEqual(self.highlighted(), ["tuesday"])
                self.driver.execute_script("document.querySelector('#tuesday td:nth-child(2)').textContent = '10/30/2026';")
                self.wait_for_rows([])

    def test_group_headers_supply_production_dates_without_crossing_groups(self):
        self.show_list(31, "/report/select-applicable")
        self.assertEqual(self.highlighted(), ["weekend-rush"])
        self.driver.execute_script("document.querySelector('#weekend-rush').setAttribute('data-production-date', '2026-11-03');")
        self.wait_for_rows([])
        self.driver.execute_script("document.querySelector('#weekend-rush').removeAttribute('data-production-date');")
        self.wait_for_rows(["weekend-rush"])
        self.driver.execute_script("document.querySelector('#monday-group td').textContent = 'Production Date: 2026-11-03 | Total - 8';")
        self.wait_for_rows([])
        self.driver.execute_script("document.querySelector('#monday-group td').textContent = 'Production Date: 2026-11-02 | Total - 8';")
        self.wait_for_rows(["weekend-rush"])

    def order_paint(self, row_id):
        return self.driver.execute_script("""
            const row = document.getElementById(arguments[0]);
            const preview = row.nextElementSibling;
            return [row, ...row.cells, preview, ...preview.cells].map(element => {
                const style = getComputedStyle(element);
                return [style.backgroundColor, style.color, style.borderLeftColor, style.borderLeftWidth];
            });
        """, row_id)

    def test_rush_highlight_fills_the_order_and_its_preview_over_tan_and_purple(self):
        for status in ["#f3c49c", "#86209f"]:
            with self.subTest(status=status):
                self.show_list(31, "/report/select-applicable")
                self.driver.execute_script("""
                    const row = document.querySelector('#weekend-rush');
                    row.style.backgroundColor = row.nextElementSibling.style.backgroundColor = arguments[0];
                """, status)
                self.assertTrue(all(paint[0] == "rgb(34, 236, 72)" for paint in self.order_paint("weekend-rush")))
                self.assertEqual(self.order_paint("tan-one")[0][0], "rgb(243, 196, 156)")
                self.assertEqual(self.order_paint("tan-one")[-2][0], "rgb(243, 196, 156)")

    def test_manual_selection_uses_native_colors_and_marker_then_restores_green(self):
        self.show_list(31, "/report/select-applicable")
        self.driver.find_element(By.CSS_SELECTOR, "#tan-one td:last-child").click()
        native_selection = self.order_paint("tan-one")
        WebDriverWait(self.driver, 5).until(lambda _: not self.driver.find_elements(By.CSS_SELECTOR, "#preview-pane.loading"))
        self.driver.find_element(By.CSS_SELECTOR, "#weekend-rush td:last-child").click()
        self.assertEqual(self.order_paint("weekend-rush"), native_selection)
        self.assertEqual(self.highlighted(), ["weekend-rush"])
        self.assertEqual(self.selected_order_ids(), ["1234606"])
        WebDriverWait(self.driver, 5).until(lambda _: not self.driver.find_elements(By.CSS_SELECTOR, "#preview-pane.loading"))
        self.driver.execute_script("document.querySelector('#weekend-rush').dispatchEvent(new MouseEvent('click', {bubbles:true, ctrlKey:true, metaKey:true}));")
        self.assertEqual(self.selected_order_ids(), [])
        self.assertTrue(all(paint[0] == "rgb(34, 236, 72)" for paint in self.order_paint("weekend-rush")))

    def selected_order_ids(self):
        return self.driver.execute_script("return [...fixtureSelection].sort();")

    def run_list_action(self, button_id):
        button = self.driver.find_element(By.ID, button_id)
        button.click()
        WebDriverWait(self.driver, 8).until(lambda _: not button.get_property("disabled"))
        return self.driver.find_element(By.CSS_SELECTOR, "#crm-select-applicable-controls [role='status']").text

    def open_applicable(self):
        return self.run_list_action("crm-open-applicable-button")

    def select_applicable(self):
        return self.run_list_action("crm-select-applicable-button")

    def test_buttons_are_adjacent_below_options_opposite_title_and_only_on_list_routes(self):
        self.show_list(31, "/app?fixture=selection#/report/967")
        positions = self.driver.execute_script("""
            const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return {left:r.left, right:r.right, top:r.top, bottom:r.bottom}; };
            return {open:rect('#crm-open-applicable-button'), select:rect('#crm-select-applicable-button'), title:rect('.dashhead-title'), options:rect('#advanceUserOptionsListForm button')};
        """)
        self.assertGreaterEqual(positions["open"]["top"], positions["options"]["bottom"])
        self.assertGreater(positions["open"]["left"], positions["title"]["right"])
        self.assertLess(positions["open"]["right"], positions["select"]["left"])
        self.assertEqual(positions["open"]["top"], positions["select"]["top"])
        self.assertEqual(positions["select"]["right"], positions["options"]["right"])
        self.assertEqual(self.driver.find_element(By.ID, "crm-open-applicable-button").text, "Open Applicable")
        self.assertEqual(self.driver.find_element(By.ID, "crm-select-applicable-button").text, "Select Applicable")
        self.driver.execute_script("location.hash = '#/order/1234601';")
        WebDriverWait(self.driver, 5).until(lambda _: not self.driver.find_elements(By.ID, "crm-select-applicable-controls"))
        self.driver.execute_script("location.hash = '#/report/967';")
        WebDriverWait(self.driver, 5).until(lambda _: len(self.driver.find_elements(By.ID, "crm-select-applicable-controls")) == 1)

    def test_opens_unique_tan_neon_and_purple_orders_with_exact_native_selection(self):
        self.show_list(31, "/report/select-applicable")
        original_url = self.driver.current_url
        original_tab = self.driver.current_window_handle
        self.assertEqual(self.open_applicable(), "5 selected · 5 tabs opened")
        expected = ["1234601", "1234603", "1234605", "1234606", "1234611"]
        self.assertEqual(self.selected_order_ids(), expected)
        self.assertEqual(self.driver.execute_script("return [...document.querySelectorAll('#report-table .previewPaneSelected')].map(row => row.dataset.id).sort();"), expected)
        self.assertEqual(self.order_paint("weekend-rush"), self.order_paint("tan-one"))
        messages = self.driver.execute_script("return fixtureMessages;")
        self.assertEqual(messages, [{"type": "crm-list:open-applicable", "orderIds": expected}])
        self.assertEqual(self.driver.current_url, original_url)
        self.assertEqual(self.driver.current_window_handle, original_tab)
        clicks = self.driver.execute_script("return fixtureClicks;")
        self.assertNotIn("1234603", [click["id"] for click in clicks])
        self.assertTrue(all(click["ctrlKey"] and click["metaKey"] and not click["shiftKey"] for click in clicks))
        # A second click must still recognize inline status colors under gray
        # native selection and must not toggle already selected orders off.
        self.assertEqual(self.open_applicable(), "5 selected · 5 tabs opened")
        self.assertEqual(self.selected_order_ids(), expected)
        self.assertEqual(self.driver.execute_script("return fixtureClicks.length;"), len(clicks))

    def test_empty_applicable_list_clears_other_selection_and_opens_nothing(self):
        for action in [self.select_applicable, self.open_applicable]:
            with self.subTest(action=action.__name__):
                self.show_list(29, "/report/select-applicable")
                self.driver.execute_script("document.querySelectorAll('#report-table tr[data-id]').forEach(row => row.style.backgroundColor = '#afdaac');")
                self.assertEqual(action(), "No tan, neon-green, or purple orders on this page.")
                self.assertEqual(self.selected_order_ids(), [])
                self.assertEqual(self.driver.execute_script("return fixtureMessages;"), [])

    def test_selection_failure_never_opens_tabs(self):
        self.show_list(31, "/report/select-applicable")
        self.driver.execute_script("fixtureIgnoreClicks = true;")
        self.assertIn("could not be verified", self.open_applicable())
        self.assertEqual(self.driver.execute_script("return fixtureMessages;"), [])

    def test_opening_failure_preserves_selected_orders_and_reports_partial_result(self):
        self.show_list(31, "/report/select-applicable")
        self.driver.execute_script("fixtureReply = {success: false, openedOrderIds: ['1234601'], message: 'Opened 1 of 5 order tabs. Selection remains in the CRM list.'};")
        self.assertIn("Opened 1 of 5", self.open_applicable())
        self.assertEqual(self.selected_order_ids(), ["1234601", "1234603", "1234605", "1234606", "1234611"])
        self.assertEqual(self.driver.execute_script("return fixtureMessages.length;"), 1)

    def test_replaced_rows_stop_opening_until_selection_can_be_verified(self):
        self.show_list(31, "/report/select-applicable")
        self.driver.execute_script("""
            const first = document.querySelector('#tan-one');
            first.addEventListener('click', () => { first.replaceWith(first.cloneNode(true)); }, {once:true});
        """)
        self.assertIn("list changed", self.open_applicable())
        self.assertEqual(self.driver.execute_script("return fixtureMessages;"), [])

    def test_applicability_changes_during_selection_stop_tab_opening(self):
        for action in [self.select_applicable, self.open_applicable]:
            with self.subTest(action=action.__name__):
                self.show_list(31, "/report/select-applicable")
                self.driver.execute_script("""
                    document.querySelector('#tan-one').addEventListener('click', event => {
                        event.currentTarget.style.backgroundColor = '#c47d54';
                    }, {once:true});
                """)
                self.assertIn("list changed", action())
                self.assertEqual(self.driver.execute_script("return fixtureMessages;"), [])

    def test_repeated_clicks_while_selection_is_busy_open_only_one_batch(self):
        self.show_list(31, "/report/select-applicable")
        self.driver.execute_script("""
            const button = document.querySelector('#crm-open-applicable-button');
            button.click();
            button.dispatchEvent(new MouseEvent('click', {bubbles:true}));
            document.querySelector('#crm-select-applicable-button').dispatchEvent(new MouseEvent('click', {bubbles:true}));
            window.fixtureBothDisabled = [...document.querySelectorAll('#crm-select-applicable-controls button')].every(item => item.disabled);
        """)
        WebDriverWait(self.driver, 8).until(lambda _: self.driver.execute_script("return fixtureMessages.length === 1 && [...document.querySelectorAll('#crm-select-applicable-controls button')].every(button => !button.disabled);"))
        self.assertTrue(self.driver.execute_script("return fixtureBothDisabled;"))
        self.assertEqual(self.driver.execute_script("return fixtureMessages.length;"), 1)

    def test_select_only_selects_tan_neon_and_purple_without_opening_tabs_or_using_bridge(self):
        self.show_list(31, "/report/select-applicable")
        original_url = self.driver.current_url
        original_tab = self.driver.current_window_handle
        self.driver.execute_script("chrome.runtime.sendMessage = () => { throw new Error('Selection must not use the tab bridge'); };")
        expected = ["1234601", "1234603", "1234605", "1234606", "1234611"]
        self.assertEqual(self.select_applicable(), "5 orders selected")
        self.assertEqual(self.selected_order_ids(), expected)
        self.assertEqual(self.driver.execute_script("return [...document.querySelectorAll('#report-table .previewPaneSelected')].map(row => row.dataset.id).sort();"), expected)
        self.assertEqual(self.order_paint("weekend-rush"), self.order_paint("tan-one"))
        self.assertEqual(self.driver.execute_script("return fixtureMessages;"), [])
        self.assertEqual(self.driver.current_url, original_url)
        self.assertEqual(self.driver.current_window_handle, original_tab)
        click_count = self.driver.execute_script("return fixtureClicks.length;")
        self.assertEqual(self.select_applicable(), "5 orders selected")
        self.assertEqual(self.driver.execute_script("return fixtureClicks.length;"), click_count)

    def test_open_after_select_only_keeps_the_same_applicable_orders_selected(self):
        self.show_list(31, "/report/select-applicable")
        self.assertEqual(self.select_applicable(), "5 orders selected")
        click_count = self.driver.execute_script("return fixtureClicks.length;")
        self.assertEqual(self.driver.execute_script("return fixtureMessages;"), [])
        self.assertEqual(self.open_applicable(), "5 selected · 5 tabs opened")
        self.assertEqual(self.selected_order_ids(), ["1234601", "1234603", "1234605", "1234606", "1234611"])
        self.assertEqual(self.driver.execute_script("return fixtureClicks.length;"), click_count)
        self.assertEqual(self.driver.execute_script("return fixtureMessages.length;"), 1)

    def test_select_only_on_empty_page_opens_nothing(self):
        self.show_list(31, "/report/select-applicable")
        self.driver.execute_script("document.querySelector('#report-table tbody').replaceChildren(); fixtureSelection.clear();")
        self.assertEqual(self.select_applicable(), "No tan, neon-green, or purple orders on this page.")
        self.assertEqual(self.driver.execute_script("return fixtureMessages;"), [])

    def test_supported_purple_and_neon_shades_open_while_other_colors_remain_excluded(self):
        for purple, neon in [("#86209f", "#20df4b"), ("#9c27b0", "#22ec48")]:
            with self.subTest(purple=purple, neon=neon):
                self.show_list(31, "/report/select-applicable")
                self.driver.execute_script("document.querySelector('#purple').style.backgroundColor = arguments[0]; document.querySelector('#native-neon').style.backgroundColor = arguments[1];", purple, neon)
                self.assertEqual(self.open_applicable(), "5 selected · 5 tabs opened")
                self.assertEqual(self.selected_order_ids(), ["1234601", "1234603", "1234605", "1234606", "1234611"])
        self.driver.execute_script("document.querySelector('#purple').style.backgroundColor = '#e6007a';")
        self.assertEqual(self.open_applicable(), "4 selected · 4 tabs opened")
        self.assertNotIn("1234611", self.selected_order_ids())

    def test_printing_uses_the_original_crm_row_colors(self):
        self.show_list()
        self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "print"})
        try:
            self.assertEqual(self.driver.execute_script("return getComputedStyle(document.querySelector('#monday td')).backgroundColor;"), "rgb(238, 238, 238)")
        finally:
            self.driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {"media": "screen"})


if __name__ == "__main__":
    unittest.main()
