import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"

for path in (ROOT, WORKERS_DIR):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

import server  # noqa: E402
import crm_validate_address  # noqa: E402
import crm_order_goods  # noqa: E402
import crm_auto_splitter  # noqa: E402
import crm_unlock_orders  # noqa: E402
import crm_product_separator  # noqa: E402
import crm_shipping_bypasser  # noqa: E402
import crm_push_back  # noqa: E402
import crm_copyright_cancel  # noqa: E402
import automation_runtime  # noqa: E402


def _report_row(order_id, *, success=True, manual_review_required=False, resolution="validated"):
    return {
        "order_id": str(order_id),
        "success": bool(success),
        "manual_review_required": bool(manual_review_required),
        "resolution": resolution,
        "warnings": [],
    }


class ChromeProfileRuntimeTests(unittest.TestCase):
    def test_chrome_profile_match_is_exact_not_prefix(self):
        root = Path("C:/Automation")
        sanmar_profile = root / "chrome_profile"
        crm_profile = root / "chrome_profile_crm"
        cmdline = f'chrome.exe --user-data-dir="{crm_profile}" --remote-debugging-port=0'

        self.assertFalse(automation_runtime._chrome_cmdline_uses_profile(cmdline, sanmar_profile))
        self.assertTrue(automation_runtime._chrome_cmdline_uses_profile(cmdline, crm_profile))

    def test_chrome_profile_match_supports_unquoted_user_data_dir(self):
        profile = Path("C:/Automation/chrome_profile")
        cmdline = f"chrome.exe --user-data-dir={profile} --headless=new"

        self.assertTrue(automation_runtime._chrome_cmdline_uses_profile(cmdline, profile))


class CrmRecoverableErrorTests(unittest.TestCase):
    class Frame:
        def __init__(self, text):
            self.text = text

    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def default_content(self):
            self.driver.contexts = [self.driver.top_text]

        def frame(self, frame):
            self.driver.contexts.append(frame.text)

        def parent_frame(self):
            if len(self.driver.contexts) > 1:
                self.driver.contexts.pop()

    class Driver:
        def __init__(self, top_text="", frame_text="", current_url="https://crm2.legacy.printfly.com/order/4882286"):
            self.current_url = current_url
            self.top_text = top_text
            self.frame_element = CrmRecoverableErrorTests.Frame(frame_text) if frame_text else None
            self.contexts = [top_text]
            self.switch_to = CrmRecoverableErrorTests.SwitchTo(self)
            self.refresh_count = 0

        def execute_script(self, script):
            return self.contexts[-1] if "document.body" in script else None

        def find_elements(self, by, selector):
            if len(self.contexts) == 1 and self.frame_element is not None:
                return [self.frame_element]
            return []

        def refresh(self):
            self.refresh_count += 1

        def get(self, url):
            self.current_url = url

    def test_refreshes_not_authenticated_modal_inside_crm_iframe(self):
        driver = self.Driver(frame_text="Error Not authenticated Close")

        with mock.patch.object(automation_runtime.time, "sleep"):
            refreshed = automation_runtime.refresh_if_crm_recoverable_error(driver, "CRM order")

        self.assertTrue(refreshed)
        self.assertEqual(driver.refresh_count, 1)
        self.assertEqual(driver.contexts, [driver.top_text])

    def test_accepts_not_authorized_wording(self):
        driver = self.Driver(frame_text="Error: Not authorized")

        self.assertTrue(automation_runtime.crm_authentication_error(driver))

    def test_does_not_refresh_same_text_outside_crm(self):
        driver = self.Driver(top_text="Not authenticated", current_url="https://vendor.example.com/login")

        self.assertFalse(automation_runtime.refresh_if_crm_recoverable_error(driver))
        self.assertEqual(driver.refresh_count, 0)

    def test_safe_get_applies_shared_crm_recovery(self):
        driver = self.Driver(frame_text="Error Not authenticated Close")

        with mock.patch.object(automation_runtime.time, "sleep"):
            loaded = automation_runtime.safe_get_with_partial_load(
                driver,
                "https://crm2.legacy.printfly.com/order/4882286",
                "CRM order",
            )

        self.assertTrue(loaded)
        self.assertEqual(driver.refresh_count, 1)


class CrmCopyrightCancelTests(unittest.TestCase):
    def test_salesforce_contact_is_accepted_as_account_link_alias(self):
        contact_driver = mock.Mock()
        contact_driver.execute_script.return_value = {
            "customer_name": "Steve Eiken",
            "email": "eikensj@comcast.net",
            "salesforce_visible": True,
            "salesforce_label": "Salesforce Contact",
            "panel_text": "Steve Eiken Salesforce Contact eikensj@comcast.net",
        }
        click_driver = mock.Mock()
        click_driver.execute_script.return_value = True
        href_driver = mock.Mock()
        href_driver.execute_script.return_value = "https://printfly.lightning.force.com/one/one.app#/sObject/0034600000zEutDAAS/view"

        with mock.patch.object(crm_copyright_cancel, "_activate_crm_context"), \
             mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope"):
            contact = crm_copyright_cancel._get_crm_contact_info(contact_driver)
            crm_copyright_cancel._click_salesforce_account(click_driver, order_id="4845038", timeout=1)
            href = crm_copyright_cancel._salesforce_account_href(href_driver, order_id="4845038")

        self.assertEqual(contact["salesforce_label"], "Salesforce Contact")
        self.assertIn("0034600000zEutDAAS", href)
        contact_script = contact_driver.execute_script.call_args.args[0].lower()
        self.assertIn("getboundingclientrect", contact_script)
        self.assertIn("visibility !== 'hidden'", contact_script)
        for driver in (contact_driver, click_driver, href_driver):
            script = driver.execute_script.call_args.args[0].lower()
            self.assertIn("salesforce account", script)
            self.assertIn("salesforce contact", script)

    def test_salesforce_draft_preparation_refreshes_once_before_send(self):
        driver = mock.Mock(current_window_handle="salesforce")
        fill = {
            "state": {
                "subject": "RushOrderTees Order #4845038- A Copyrighted Element Removed",
                "body": "Prepared body",
            }
        }
        with ExitStack() as stack:
            open_account = stack.enter_context(
                mock.patch.object(crm_copyright_cancel, "_open_salesforce_account", side_effect=["sf-1", "sf-2"])
            )
            verify = stack.enter_context(
                mock.patch.object(
                    crm_copyright_cancel,
                    "_verify_salesforce_email",
                    side_effect=[crm_copyright_cancel.CopyrightCancelError("Salesforce page stalled"), True],
                )
            )
            stack.enter_context(mock.patch.object(crm_copyright_cancel, "_click_salesforce_email"))
            stack.enter_context(mock.patch.object(crm_copyright_cancel, "_wait_for_email_composer"))
            stack.enter_context(mock.patch.object(crm_copyright_cancel, "_set_salesforce_from_orders", return_value="Orders"))
            stack.enter_context(
                mock.patch.object(crm_copyright_cancel, "_fill_salesforce_email_from_salesforce_template", return_value=fill)
            )
            send = stack.enter_context(
                mock.patch.object(crm_copyright_cancel, "_send_salesforce_email", return_value={"sent": True})
            )
            stack.enter_context(mock.patch.object(crm_copyright_cancel, "_activate_crm_context"))
            scope = stack.enter_context(mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope"))
            stack.enter_context(mock.patch.object(crm_copyright_cancel.time, "sleep"))

            result = crm_copyright_cancel._prepare_and_maybe_send_salesforce_email(
                driver,
                "crm",
                "4845038",
                "customer@example.com",
                dry_run=False,
                process=crm_copyright_cancel.COPYRIGHT_REMOVAL_PROCESS,
                reason="Nike",
            )

        self.assertEqual(open_account.call_count, 2)
        self.assertEqual(verify.call_count, 2)
        driver.refresh.assert_called_once_with()
        scope.assert_called_once_with(driver, order_id="4845038", timeout=30)
        send.assert_called_once()
        self.assertTrue(result["preparation_retried"])
        self.assertIn("stalled", result["first_preparation_error"])

    def test_contact_panel_timeout_refreshes_once_then_retries(self):
        driver = mock.Mock()
        contact = {"email": "customer@example.com", "salesforce_visible": True}
        with mock.patch.object(crm_copyright_cancel.time, "monotonic", side_effect=[0, 0, 1, 1, 1]), \
             mock.patch.object(crm_copyright_cancel.time, "sleep"), \
             mock.patch.object(crm_copyright_cancel, "_activate_crm_context"), \
             mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope") as scope, \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_get_crm_contact_info",
                 side_effect=[crm_copyright_cancel.CopyrightCancelError("contact panel incomplete"), contact],
             ) as get_contact:
            result = crm_copyright_cancel._wait_for_crm_contact_info(driver, order_id="4845038", timeout=0.1)

        self.assertEqual(result, contact)
        driver.refresh.assert_called_once_with()
        self.assertEqual(get_contact.call_count, 2)
        self.assertIn(mock.call(driver, order_id="4845038", timeout=30), scope.call_args_list)

    def test_salesforce_account_click_timeout_refreshes_crm_once_then_retries(self):
        class SwitchTo:
            def __init__(self, driver):
                self.driver = driver

            def window(self, handle):
                self.driver.current_window_handle = handle

        class Driver:
            def __init__(self):
                self.current_window_handle = "crm"
                self.window_handles = ["crm"]
                self.switch_to = SwitchTo(self)
                self.refresh_count = 0

            def refresh(self):
                self.refresh_count += 1

        driver = Driver()
        timeout_error = crm_copyright_cancel.CopyrightCancelError(
            "Salesforce Account/Contact link did not become clickable before timeout. Last error: None"
        )
        with ExitStack() as stack:
            activate = stack.enter_context(mock.patch.object(crm_copyright_cancel, "_activate_crm_context"))
            scope = stack.enter_context(mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope"))
            stack.enter_context(mock.patch.object(crm_copyright_cancel, "_salesforce_account_href", return_value=""))
            click = stack.enter_context(
                mock.patch.object(crm_copyright_cancel, "_click_salesforce_account", side_effect=[timeout_error, None])
            )
            stack.enter_context(
                mock.patch.object(crm_copyright_cancel, "_switch_to_new_or_changed_tab", return_value="salesforce")
            )
            stack.enter_context(mock.patch.object(crm_copyright_cancel, "_attempt_salesforce_login", return_value=False))
            account_page = stack.enter_context(
                mock.patch.object(crm_copyright_cancel, "_wait_for_salesforce_account_page", return_value=True)
            )

            handle = crm_copyright_cancel._open_salesforce_account(
                driver,
                "crm",
                "customer@example.com",
                order_id="4845038",
            )

        self.assertEqual(handle, "salesforce")
        self.assertEqual(driver.refresh_count, 1)
        self.assertEqual(click.call_count, 2)
        self.assertGreaterEqual(activate.call_count, 3)
        scope.assert_called_once_with(driver, order_id="4845038", timeout=30)
        account_page.assert_called_once_with(driver, "customer@example.com", timeout=30)

    def test_copyright_cancel_sales_note_requires_reason(self):
        self.assertEqual(
            crm_copyright_cancel._copyright_cancel_sales_note("Trademark conflict"),
            "Trademark conflict copyright\nemailed copyright cancellation",
        )
        with self.assertRaisesRegex(crm_copyright_cancel.CopyrightCancelError, "Missing Reason"):
            crm_copyright_cancel._copyright_cancel_sales_note("   ")

    def test_content_violation_cancel_sales_note(self):
        self.assertEqual(
            crm_copyright_cancel._cancel_sales_note(
                "Policy conflict",
                crm_copyright_cancel.CONTENT_VIOLATION_CANCEL_PROCESS,
            ),
            "Policy conflict content violation\nemailed content violation cancellation",
        )

    def test_fixed_cancellation_sales_notes_do_not_require_reason(self):
        self.assertEqual(
            crm_copyright_cancel._cancel_sales_note(
                "",
                crm_copyright_cancel.EXISTING_DESIGNS_CANCEL_PROCESS,
            ),
            "Cannot print an screenshot/photograph of a design on a t-shirt\nCancelled",
        )
        self.assertEqual(
            crm_copyright_cancel._cancel_sales_note(
                "",
                crm_copyright_cancel.OUTSIDE_LIMIT_CANCEL_PROCESS,
            ),
            "Cannot print beyond the designated area limit\nCancelled",
        )

    def test_scan_queue_rows_accepts_fixed_cancellations_without_reason(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["1234567", crm_copyright_cancel.EXISTING_DESIGNS_CANCEL_ISSUE_TYPE, "", ""],
            ["7654321", crm_copyright_cancel.OUTSIDE_LIMIT_CANCEL_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(skipped, [])
        self.assertEqual([row.process_key for row in eligible], ["existing_designs_cancel", "outside_limit_cancel"])
        self.assertTrue(all(row.process.cancel_and_refund for row in eligible))

    def test_copyright_reachout_sales_note(self):
        self.assertEqual(
            crm_copyright_cancel._cancel_sales_note(
                "LA Dodgers",
                crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS,
            ),
            "LA Dodgers Copyright\nEmailed txted",
        )

    def test_salesforce_advanced_search_order_selection_uses_shadow_dom_scan(self):
        outer = self

        class Driver:
            def __init__(self):
                self.scripts = []

            def execute_script(self, script, *args):
                self.scripts.append(script)
                outer.assertEqual(args, ("4812079",) if args else args)
                if "click_point" in script and "function orderMatches(text)" in script:
                    outer.assertIn("function all(selector, root)", script)
                    outer.assertIn("child.shadowRoot", script)
                    outer.assertIn("function textOf(el)", script)
                    outer.assertIn("el.getAttribute && el.getAttribute('title')", script)
                    outer.assertNotIn("all('[data-cell-value]", script)
                    outer.assertNotIn("if (orderMatches(textOf(row)))", script)
                    outer.assertNotIn("candidates.push(rowText)", script)
                    return {
                        "row_text": "Salesforce Order 05376876 Printfly Order Id 4812079",
                        "click_point": {"x": 111, "y": 281},
                        "printfly_order_id": "4812079",
                        "match_source": "printfly-order-id-column",
                    }
                if "clean(`${el.innerText || ''} ${el.value || ''}" in script:
                    outer.assertIn("function all(selector, root)", script)
                    outer.assertIn("child.shadowRoot", script)
                    return True
                if "some((el) => visible(el) && /(Advanced Search|Search Orders)/i.test(textOf(el)))" in script:
                    return False
                if "screenX: window.screenX" in script:
                    return {
                        "screenX": 0,
                        "screenY": 0,
                        "outerWidth": 1920,
                        "outerHeight": 1080,
                        "innerWidth": 1904,
                        "innerHeight": 985,
                    }
                outer.fail("Unexpected Salesforce script")

            def execute_cdp_cmd(self, command, params):
                outer.assertEqual(command, "Input.dispatchMouseEvent")
                outer.assertIn(params["type"], {"mouseMoved", "mousePressed", "mouseReleased"})
                return {}

        driver = Driver()

        selected = crm_copyright_cancel._select_salesforce_advanced_search_order(driver, "4812079")

        self.assertEqual(
            selected,
            {
                "row_text": "Salesforce Order 05376876 Printfly Order Id 4812079",
                "click_point": {"x": 111, "y": 281},
                "printfly_order_id": "4812079",
                "match_source": "printfly-order-id-column",
            },
        )
        self.assertEqual(len(driver.scripts), 4)

    def test_salesforce_case_order_lookup_always_opens_advanced_search(self):
        class Field:
            def __init__(self):
                self.keys = []

            def click(self):
                return None

            def clear(self):
                return None

            def send_keys(self, *keys):
                self.keys.append(keys)

        class Driver:
            def __init__(self):
                self.scripts = []

            def execute_script(self, script, *args):
                self.scripts.append(script)
                if "Quick lookup suggestions match Salesforce Order Number" in script:
                    self.assert_quick_lookup_is_disabled(script)
                    return None
                if "text.includes('show more results')" in script:
                    return object()
                if "some((el) => visible(el) && /(Advanced Search|Search Orders)/i.test(textOf(el)))" in script:
                    return True
                raise AssertionError("Unexpected Salesforce lookup script")

            @staticmethod
            def assert_quick_lookup_is_disabled(script):
                if "exactOptions" in script or 'return "selected"' in script:
                    raise AssertionError("Quick Salesforce Order Number selection must remain disabled")

        field = Field()
        driver = Driver()
        with mock.patch.object(crm_copyright_cancel, "_salesforce_field_control", return_value=field), mock.patch.object(
            crm_copyright_cancel.time, "sleep"
        ):
            mode = crm_copyright_cancel._fill_salesforce_case_order_lookup(driver, "4812079")

        self.assertEqual(mode, "advanced")
        self.assertIn(("4812079",), field.keys)
        self.assertTrue(any(crm_copyright_cancel.Keys.ENTER in keys for keys in field.keys))

    def test_hdd_emb_sales_notes_do_not_require_reason(self):
        self.assertEqual(
            crm_copyright_cancel._cancel_sales_note(
                "",
                crm_copyright_cancel.COMPLICATED_EMB_TO_HDD_PROCESS,
            ),
            "Complicated embroidery. Switched to HDD to keep the details. Emailed",
        )
        self.assertEqual(
            crm_copyright_cancel._cancel_sales_note(
                "",
                crm_copyright_cancel.OVERSIZE_EMB_TO_HDD_PROCESS,
            ),
            "Oversize embroidery. Switch to HDD to keep the design size. Emailed",
        )

    def test_scan_queue_rows_skips_copyright_cancel_when_reason_missing(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/1234567", crm_copyright_cancel.COPYRIGHT_CANCEL_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock()
        spreadsheet.title = "Queue"
        worksheet.title = "Sheet1"

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(eligible, [])
        self.assertEqual(skipped[0]["order_id"], "1234567")
        self.assertEqual(skipped[0]["reason"], crm_copyright_cancel.MISSING_REASON_ERROR)

    def test_scan_queue_rows_accepts_content_violation_cancel(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            [
                "https://crm2.legacy.printfly.com/order/1234567",
                crm_copyright_cancel.CONTENT_VIOLATION_CANCEL_ISSUE_TYPE,
                "Policy conflict",
                "",
            ],
        ]
        spreadsheet = mock.Mock()
        spreadsheet.title = "Queue"
        worksheet.title = "Sheet1"

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(skipped, [])
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].order_id, "1234567")
        self.assertEqual(eligible[0].process_key, "content_violation_cancel")

    def test_sheet_scan_is_read_only_when_a_reason_is_missing(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/1234567", crm_copyright_cancel.COPYRIGHT_CANCEL_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")
        worksheet.title = "Sheet1"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            result_path = handle.name

        try:
            with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
                exit_code = crm_copyright_cancel.run_scan_sheet(result_file=result_path)
            payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        finally:
            Path(result_path).unlink(missing_ok=True)

        self.assertEqual(exit_code, 0)
        worksheet.update_cell.assert_not_called()
        self.assertEqual(payload["missing_reason_error_count"], 0)

    def test_sheet_scanner_dry_run_is_read_only_when_a_reason_is_missing(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/1234567", crm_copyright_cancel.COPYRIGHT_CANCEL_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")
        worksheet.title = "Sheet1"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            result_path = handle.name
        args = mock.Mock(
            retry_errors=False,
            limit=0,
            dry_run=True,
            visible=False,
            attach_browser=False,
            debugger_address="127.0.0.1:9222",
            login_wait_seconds=0,
            skip_refund_click=False,
            keep_browser_open=False,
            keep_browser_open_on_error=False,
            result_file=result_path,
        )

        try:
            with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
                exit_code = crm_copyright_cancel.run_process_queue(args)
            payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        finally:
            Path(result_path).unlink(missing_ok=True)

        self.assertEqual(exit_code, 0)
        worksheet.update_cell.assert_not_called()
        self.assertEqual(payload["missing_reason_error_count"], 0)

    def test_scan_queue_rows_accepts_copyright_reachout_with_reason(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            [
                "https://crm2.legacy.printfly.com/order/4785121",
                crm_copyright_cancel.COPYRIGHT_REACHOUT_ISSUE_TYPE,
                "LA Dodgers",
                "",
            ],
        ]
        spreadsheet = mock.Mock()
        spreadsheet.title = "Queue"
        worksheet.title = "Sheet1"

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(skipped, [])
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].order_id, "4785121")
        self.assertEqual(eligible[0].process_key, "copyright_reachout")
        self.assertFalse(eligible[0].process.cancel_and_refund)

    def test_scan_queue_rows_accepts_auto_splitter_without_reason(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/4785121", crm_copyright_cancel.AUTO_SPLITTER_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(skipped, [])
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].order_id, "4785121")
        self.assertEqual(eligible[0].process_key, "auto_splitter")
        self.assertFalse(eligible[0].process.requires_reason)

    def test_scan_queue_rows_accepts_manual_stock_order_without_reason(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/4785121", crm_copyright_cancel.MANUAL_STOCK_ORDER_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(skipped, [])
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].order_id, "4785121")
        self.assertEqual(eligible[0].process_key, "manual_stock_order")
        self.assertFalse(eligible[0].process.requires_reason)

    def test_post_cancel_stock_summary_tracks_cancelled_channel_vendor_rows(self):
        scan = {
            "tabs": [
                {
                    "tab_number": 1,
                    "tab_name": "H-TabOne",
                    "stock": {
                        "state": "ordered",
                        "stock_status_ordered": True,
                        "manual_order_rows": [
                            {"vendor": "S&S Activewear", "po": "H-TabOne-SS01"},
                        ],
                    },
                },
                {
                    "tab_number": 2,
                    "tab_name": "H-TabTwo",
                    "stock": {
                        "state": "not_ordered_or_unknown",
                        "stock_status_ordered": False,
                        "manual_order_rows": [],
                    },
                },
            ],
        }

        summary = crm_copyright_cancel._summarize_post_cancel_stock_scan(scan)

        self.assertTrue(summary["stock_ordered"])
        self.assertEqual(len(summary["cancelled_channel_rows"]), 1)
        self.assertEqual(summary["cancelled_channel_rows"][0]["vendor"], "S&S Activewear")
        self.assertFalse(summary["local_inventory_only"])
        self.assertEqual(summary["unknown_ordered_tabs"], [])

    def test_post_cancel_stock_summary_accepts_sanmar_bulk_stock_state_fallback(self):
        scan = {
            "tabs": [
                {
                    "tab_number": 1,
                    "tab_name": "H-TabOne",
                    "stock": {
                        "state": "ordered_po_only",
                        "stock_status_ordered": True,
                        "has_po_row": True,
                        "manual_order_vendor": "Sanmar (Bulk)",
                        "manual_order_po": "H-TabOne-SM01",
                        "manual_order_rows": [],
                    },
                },
            ],
        }

        summary = crm_copyright_cancel._summarize_post_cancel_stock_scan(scan)

        self.assertTrue(summary["stock_ordered"])
        self.assertEqual(summary["outside_stock_rows"][0]["vendor"], "Sanmar (Bulk)")
        self.assertEqual(summary["outside_stock_rows"][0]["po"], "H-TabOne-SM01")
        self.assertEqual(summary["cancelled_channel_rows"][0]["vendor"], "Sanmar (Bulk)")
        self.assertEqual(summary["unknown_ordered_tabs"], [])

    def test_post_cancel_stock_slack_posts_when_s_and_s_row_exists_with_unknown_ordered_tab(self):
        state = {
            "stock_ordered": True,
            "local_inventory_only": False,
            "is_subcontractor": False,
            "is_mach6_subcontractor": False,
            "outside_stock_rows": [{"vendor": "S&S Activewear", "po": "H-TabOne-SS01"}],
            "cancelled_channel_rows": [{"vendor": "S&S Activewear", "po": "H-TabOne-SS01"}],
            "unknown_ordered_tabs": [{"tab_number": 2, "tab_name": "H-TabTwo", "state": "ordered_header_only"}],
        }

        with mock.patch.object(crm_copyright_cancel, "_read_post_cancel_stock_state", return_value=state), \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_send_post_cancel_slack_message",
                 return_value={"sent": True},
             ) as mock_send:
            result = crm_copyright_cancel._handle_post_cancel_stock_return(
                mock.Mock(),
                "crm-window",
                "4797905",
                "https://crm2.legacy.printfly.com/order/4797905",
                dry_run=False,
            )

        self.assertEqual(result["action"], "slack_inhouse_cancelled_orders")
        mock_send.assert_called_once()

    def test_post_cancel_stock_slack_keeps_local_inventory_only_quiet(self):
        state = {
            "stock_ordered": True,
            "local_inventory_only": True,
            "is_subcontractor": False,
            "is_mach6_subcontractor": False,
            "outside_stock_rows": [],
            "cancelled_channel_rows": [],
            "unknown_ordered_tabs": [],
            "local_inventory_rows": [{"vendor": "Local Inventory", "po": "H-TabOne-LI01"}],
        }

        with mock.patch.object(crm_copyright_cancel, "_read_post_cancel_stock_state", return_value=state), \
             mock.patch.object(crm_copyright_cancel, "_send_post_cancel_slack_message") as mock_send:
            result = crm_copyright_cancel._handle_post_cancel_stock_return(
                mock.Mock(),
                "crm-window",
                "4797905",
                "https://crm2.legacy.printfly.com/order/4797905",
                dry_run=False,
            )

        self.assertEqual(result["action"], "complete_local_inventory")
        mock_send.assert_not_called()

    def test_scan_queue_rows_skips_copyright_reachout_when_reason_missing(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/4785121", crm_copyright_cancel.COPYRIGHT_REACHOUT_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(eligible, [])
        self.assertEqual(skipped[0]["order_id"], "4785121")
        self.assertIn("Missing Reason", skipped[0]["reason"])

    def test_scan_queue_rows_accepts_copyright_removal_with_reason(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/4785121", crm_copyright_cancel.COPYRIGHT_REMOVAL_ISSUE_TYPE, "LA Dodgers", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(skipped, [])
        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0].process_key, "copyright_removal")
        self.assertEqual(eligible[0].reason, "LA Dodgers")
        self.assertFalse(eligible[0].process.cancel_and_refund)

    def test_process_queue_routes_auto_splitter_without_email_flow(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/4785121", crm_copyright_cancel.AUTO_SPLITTER_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock()
        spreadsheet.title = "Queue"
        worksheet.title = "Sheet1"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            result_path = handle.name
        args = mock.Mock(
            retry_errors=False,
            limit=0,
            dry_run=False,
            visible=False,
            attach_browser=False,
            debugger_address="127.0.0.1:9222",
            login_wait_seconds=0,
            skip_refund_click=False,
            keep_browser_open=False,
            keep_browser_open_on_error=False,
            result_file=result_path,
        )

        try:
            with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)), \
                 mock.patch.object(crm_copyright_cancel, "process_auto_splitter_order", return_value={
                     "order_id": "4785121",
                     "dry_run": False,
                     "process": "auto_splitter",
                     "issue_type": crm_copyright_cancel.AUTO_SPLITTER_ISSUE_TYPE,
                     "auto_splitter": {"success": True, "new_order_ids": ["4785999"]},
                 }) as mock_auto_splitter, \
                 mock.patch.object(crm_copyright_cancel, "process_single_order") as mock_process:
                exit_code = crm_copyright_cancel.run_process_queue(args)
            payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        finally:
            Path(result_path).unlink(missing_ok=True)

        self.assertEqual(exit_code, 0)
        mock_auto_splitter.assert_called_once_with(
            "4785121",
            dry_run=False,
            visible=False,
            attach_browser=False,
            debugger_address="127.0.0.1:9222",
            login_wait_seconds=0,
        )
        mock_process.assert_not_called()
        worksheet.batch_clear.assert_called_once_with(["A2:D2"])
        self.assertEqual(payload["processed"][0]["process"], "auto_splitter")

    def test_sheet_error_only_writes_first_error_line(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()

        crm_copyright_cancel._write_sheet_error(
            worksheet,
            headers,
            2,
            "Auto-split failed for order 4859108: Message: script timeout\n"
            "  (Session info: chrome=138.0)\n"
            "Stacktrace:\n"
            "long selenium stack trace",
        )

        worksheet.update_cell.assert_called_once_with(
            2,
            4,
            "Auto-split failed for order 4859108: Message: script timeout",
        )

    def test_process_queue_routes_manual_stock_order_without_email_flow(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/4785121", crm_copyright_cancel.MANUAL_STOCK_ORDER_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock()
        spreadsheet.title = "Queue"
        worksheet.title = "Sheet1"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            result_path = handle.name
        args = mock.Mock(
            retry_errors=False,
            limit=0,
            dry_run=False,
            visible=False,
            attach_browser=False,
            debugger_address="127.0.0.1:9222",
            login_wait_seconds=0,
            skip_refund_click=False,
            keep_browser_open=False,
            keep_browser_open_on_error=False,
            result_file=result_path,
        )

        try:
            with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)), \
                 mock.patch.object(crm_copyright_cancel, "process_manual_stock_order", return_value={
                     "order_id": "4785121",
                     "dry_run": False,
                     "process": "manual_stock_order",
                     "issue_type": crm_copyright_cancel.MANUAL_STOCK_ORDER_ISSUE_TYPE,
                     "shipping_bypasser": {"success": True, "report": []},
                 }) as mock_manual_stock, \
                 mock.patch.object(crm_copyright_cancel, "process_single_order") as mock_process:
                exit_code = crm_copyright_cancel.run_process_queue(args)
            payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
        finally:
            Path(result_path).unlink(missing_ok=True)

        self.assertEqual(exit_code, 0)
        mock_manual_stock.assert_called_once_with(
            "4785121",
            dry_run=False,
            visible=False,
        )
        mock_process.assert_not_called()
        worksheet.batch_clear.assert_called_once_with(["A2:D2"])
        self.assertEqual(payload["processed"][0]["process"], "manual_stock_order")

    def test_process_single_order_routes_auto_splitter_process(self):
        with mock.patch.object(crm_copyright_cancel, "process_auto_splitter_order", return_value={"order_id": "4785121"}) as mock_auto:
            result = crm_copyright_cancel.process_single_order(
                "4785121",
                "",
                dry_run=True,
                process=crm_copyright_cancel.AUTO_SPLITTER_PROCESS.key,
                visible=False,
                attach_browser=False,
                debugger_address="127.0.0.1:9222",
                login_wait_seconds=0,
            )

        self.assertEqual(result["order_id"], "4785121")
        mock_auto.assert_called_once()

    def test_process_single_order_routes_manual_stock_order_process(self):
        with mock.patch.object(crm_copyright_cancel, "process_manual_stock_order", return_value={"order_id": "4785121"}) as mock_manual:
            result = crm_copyright_cancel.process_single_order(
                "4785121",
                "",
                dry_run=True,
                process=crm_copyright_cancel.MANUAL_STOCK_ORDER_PROCESS.key,
                visible=False,
                attach_browser=False,
                debugger_address="127.0.0.1:9222",
                login_wait_seconds=0,
            )

        self.assertEqual(result["order_id"], "4785121")
        mock_manual.assert_called_once()

    def test_manual_stock_order_calls_shipping_bypasser_runner(self):
        payload = {
            "success": True,
            "message": "Shipping Bypasser complete.",
            "target_order_id": "4785121",
            "order_ids": ["4785121"],
            "report": [],
        }

        def fake_shipping_bypasser(**kwargs):
            Path(kwargs["result_file"]).write_text(json.dumps(payload), encoding="utf-8")
            return 0

        with mock.patch.object(crm_copyright_cancel, "_run_shipping_bypasser", side_effect=fake_shipping_bypasser) as mock_run:
            result = crm_copyright_cancel.process_manual_stock_order("4785121", dry_run=True, visible=False)

        self.assertEqual(result["process"], "manual_stock_order")
        self.assertEqual(result["shipping_bypasser"]["target_order_id"], "4785121")
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.kwargs["action"], "shipping_bypass_single")
        self.assertEqual(mock_run.call_args.kwargs["order_id"], "4785121")
        self.assertTrue(mock_run.call_args.kwargs["dry_run"])

    def test_copyright_reachout_body_html_bolds_reason(self):
        body = "Hello,\n\nWhile reviewing your order we noticed that XXXXXX is protected by copyright."

        rendered = crm_copyright_cancel._html_with_bold_placeholder_reason(body, "LA Dodgers")

        self.assertIn("<strong>LA Dodgers</strong>", rendered)
        self.assertNotIn("XXXXXX", rendered)

    def test_named_reason_placeholder_body_html_bolds_reason(self):
        body = "Hello,\n\nWhile reviewing your order we noticed that [REASON] is protected by copyright."

        rendered = crm_copyright_cancel._html_with_bold_placeholder_reason(body, "LA Dodgers")

        self.assertIn("<strong>LA Dodgers</strong>", rendered)
        self.assertNotIn("[REASON]", rendered)

    def test_named_order_placeholder_replacement(self):
        subject = "RushOrderTees Order #[ORDER-NUMBER] - A Copyrighted Element Removed"

        rendered = crm_copyright_cancel._replace_order_placeholders(subject, "4705293")

        self.assertEqual(rendered, "RushOrderTees Order #4705293 - A Copyrighted Element Removed")

    def test_copyright_reachout_body_formatter_keeps_single_message(self):
        body = (
            "Font Size Hello, Thank you once again for placing your order with RushOrderTees! "
            "While reviewing your order we noticed that XXXXXX is protected by copyright, trademark, or intellectual property laws. "
            "Thank you for trusting the RushOrderTees.com team. We appreciate your business. "
            "Hello, Thank you once again for placing your order with RushOrderTees! "
            "While reviewing your order we noticed that XXXXXX is protected by copyright, trademark, or intellectual property laws."
        )

        formatted = crm_copyright_cancel._format_copyright_reachout_body_text(body)

        self.assertEqual(formatted.count("Hello,"), 1)
        self.assertEqual(formatted.count("XXXXXX"), 1)
        self.assertTrue(formatted.endswith("We appreciate your business."))

    def test_copyright_removal_body_formatter_restores_template_paragraphs(self):
        body = (
            "Hello, Thank you once again for placing your order with RushOrderTees! "
            "We wanted to let you know that in reviewing your t-shirt design, our team identified a copyrighted element, XXXXXX, "
            "that could not be used. To ensure your order can move forward without delay, we have removed that element from the design. "
            "Your updated design is now cleared and will proceed through production as planned. "
            "If you would like to provide a replacement graphic or make any further changes, please let us know-we'll be happy to assist. "
            "We are here to help if you have any additional questions. You can reach us at 800-620-1233. "
            "Thank you for trusting the RushOrderTees.com team. We appreciate your business."
        )

        formatted = crm_copyright_cancel._format_placeholder_body_text(
            body,
            process=crm_copyright_cancel.COPYRIGHT_REMOVAL_PROCESS,
        )

        self.assertEqual(formatted.count("XXXXXX"), 1)
        self.assertIn("Hello,\n\nThank you once again", formatted)
        self.assertIn("RushOrderTees!\n\nWe wanted", formatted)
        self.assertIn("from the design.\n\nYour updated design", formatted)
        self.assertIn("800-620-1233.\n\nThank you", formatted)

    def test_send_ready_guard_rejects_hidden_body_placeholder(self):
        driver = mock.Mock()
        with mock.patch.object(
            crm_copyright_cancel,
            "_read_salesforce_email_state",
            return_value={
                "from": crm_copyright_cancel.SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL,
                "subject": "RushOrderTees Order #4705293 - License Required",
                "body": "While reviewing your order we noticed that testorder is protected by copyright.",
            },
        ), mock.patch.object(
            crm_copyright_cancel,
            "_salesforce_email_body_placeholder_state",
            return_value={"placeholder": "[REASON]", "count": 1, "matches": [{"kind": "body_html"}]},
        ):
            with self.assertRaisesRegex(crm_copyright_cancel.CopyrightCancelError, "unresolved placeholder"):
                crm_copyright_cancel._verify_salesforce_email_ready_to_send(
                    driver,
                    "4705293",
                    "",
                    "",
                    process=crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS,
                )

    def test_send_ready_guard_rejects_subject_order_placeholder(self):
        driver = mock.Mock()
        with mock.patch.object(
            crm_copyright_cancel,
            "_read_salesforce_email_state",
            return_value={
                "from": crm_copyright_cancel.SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL,
                "subject": "RushOrderTees Order #[ORDER-NUMBER] - License Required",
                "body": "While reviewing your order we noticed that TEST is protected by copyright.",
            },
        ), mock.patch.object(
            crm_copyright_cancel,
            "_salesforce_email_body_placeholder_state",
            return_value={"placeholder": "[REASON]", "count": 0, "matches": []},
        ):
            with self.assertRaisesRegex(crm_copyright_cancel.CopyrightCancelError, "subject: \\[ORDER-NUMBER\\]"):
                crm_copyright_cancel._verify_salesforce_email_ready_to_send(
                    driver,
                    "4705293",
                    "",
                    "",
                    process=crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS,
                )

    def test_send_ready_guard_rejects_body_order_placeholder_and_legacy_placeholder(self):
        driver = mock.Mock()
        with mock.patch.object(
            crm_copyright_cancel,
            "_read_salesforce_email_state",
            return_value={
                "from": crm_copyright_cancel.SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL,
                "subject": "RushOrderTees Order #4705293 - License Required",
                "body": "While reviewing your order we noticed that Order [ORDER-NUMBER] still has XXXXXX protected by copyright.",
            },
        ), mock.patch.object(
            crm_copyright_cancel,
            "_salesforce_email_body_placeholder_state",
            return_value={"placeholder": "[REASON]", "count": 0, "matches": []},
        ):
            with self.assertRaisesRegex(crm_copyright_cancel.CopyrightCancelError, "body: \\[ORDER-NUMBER\\], XXXXXX"):
                crm_copyright_cancel._verify_salesforce_email_ready_to_send(
                    driver,
                    "4705293",
                    "",
                    "",
                    process=crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS,
                )

    def test_reason_replacement_refuses_keyboard_body_fallback(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"count": 0}

        with mock.patch.object(
            crm_copyright_cancel,
            "_read_salesforce_email_state",
            return_value={"body": "While reviewing your order we noticed that [REASON] is protected by copyright."},
        ), mock.patch.object(crm_copyright_cancel, "_type_salesforce_body_with_keyboard") as mock_type:
            with self.assertRaisesRegex(crm_copyright_cancel.CopyrightCancelError, "refusing to type or paste"):
                crm_copyright_cancel._replace_salesforce_body_placeholder_with_reason(
                    driver,
                    "LA Dodgers",
                    process=crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS,
                )

        mock_type.assert_not_called()

    def test_clear_sheet_queue_row_only_clears_columns_a_to_d(self):
        worksheet = mock.Mock()

        crm_copyright_cancel._clear_sheet_queue_row(worksheet, 7)

        worksheet.batch_clear.assert_called_once_with(["A7:D7"])
        worksheet.delete_rows.assert_not_called()

    def test_scan_queue_rows_accepts_hdd_emb_without_reason(self):
        headers = [
            crm_copyright_cancel.GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_REASON_COLUMN,
            crm_copyright_cancel.GOOGLE_SHEET_ERROR_COLUMN,
        ]
        worksheet = mock.Mock()
        worksheet.get_all_values.return_value = [
            headers,
            ["https://crm2.legacy.printfly.com/order/1234567", crm_copyright_cancel.COMPLICATED_EMB_ISSUE_TYPE, "", ""],
            ["https://crm2.legacy.printfly.com/order/7654321", crm_copyright_cancel.OVERSIZE_EMB_TO_HDD_ISSUE_TYPE, "", ""],
        ]
        spreadsheet = mock.Mock(title="Queue")

        with mock.patch.object(crm_copyright_cancel, "_open_sheet", return_value=(spreadsheet, worksheet)):
            _spreadsheet, _worksheet, _headers, eligible, skipped = crm_copyright_cancel._scan_queue_rows()

        self.assertEqual(skipped, [])
        self.assertEqual([row.process_key for row in eligible], ["complicated_emb_to_hdd", "oversize_emb_to_hdd"])
        self.assertFalse(eligible[0].process.cancel_and_refund)
        self.assertFalse(eligible[1].process.cancel_and_refund)

    def test_complicated_emb_template_search_uses_picker_match(self):
        queries = crm_copyright_cancel._template_search_queries(crm_copyright_cancel.COMPLICATED_EMB_TO_HDD_PROCESS)

        self.assertEqual(queries[0], "[AUTO]")
        self.assertIn("[AUTO] Complicated EMB to HDD", queries)
        self.assertIn("updated to ink printing", crm_copyright_cancel.COMPLICATED_EMB_TO_HDD_PROCESS.body_markers)

    def test_copyright_template_search_uses_exact_template_before_broad_keyword(self):
        cancel_queries = crm_copyright_cancel._template_search_queries(crm_copyright_cancel.COPYRIGHT_CANCEL_PROCESS)
        reachout_queries = crm_copyright_cancel._template_search_queries(crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS)
        removal_queries = crm_copyright_cancel._template_search_queries(crm_copyright_cancel.COPYRIGHT_REMOVAL_PROCESS)

        self.assertEqual(cancel_queries[0], "[AUTO]")
        self.assertEqual(reachout_queries[0], "[AUTO]")
        self.assertEqual(removal_queries[0], "[AUTO]")
        self.assertIn("[AUTO] Copyright Cancel", cancel_queries)
        self.assertIn("[AUTO] Copyright Reachout", reachout_queries)
        self.assertIn("[AUTO] Copyright Removal", removal_queries)
        self.assertNotIn("NO REPLY - Removed Copyright", removal_queries)
        self.assertNotIn("A Copyrighted Element Removed", removal_queries)
        self.assertNotIn("Copyrighted Element Removed", removal_queries)
        self.assertIn("copyright", [query.lower() for query in cancel_queries[1:]])
        self.assertIn("copyright", [query.lower() for query in reachout_queries[1:]])
        self.assertIn("copyright", [query.lower() for query in removal_queries[1:]])
        self.assertNotEqual(cancel_queries[1], reachout_queries[1])
        self.assertTrue(crm_copyright_cancel.COPYRIGHT_CANCEL_PROCESS.replace_body_placeholder_with_reason)
        self.assertTrue(crm_copyright_cancel.COPYRIGHT_REMOVAL_PROCESS.replace_body_placeholder_with_reason)

    def test_template_appears_inserted_requires_expected_body_markers(self):
        driver = mock.Mock()

        with mock.patch.object(
            crm_copyright_cancel,
            "_read_salesforce_email_state",
            return_value={
                "subject": "RushOrderTees Order #XXXXXX - Additional Embroidery Request, additional balance",
                "body": "This is the additional embroidery request template.",
            },
        ), mock.patch.object(
            crm_copyright_cancel,
            "_read_salesforce_email_composer_text",
            return_value="This is the additional embroidery request template.",
        ):
            inserted = crm_copyright_cancel._salesforce_template_appears_inserted(
                driver,
                crm_copyright_cancel.COPYRIGHT_CANCEL_PROCESS,
            )

        self.assertFalse(inserted)

    def test_template_appears_inserted_accepts_expected_body_markers(self):
        driver = mock.Mock()

        with mock.patch.object(
            crm_copyright_cancel,
            "_read_salesforce_email_state",
            return_value={
                "subject": "RushOrderTees Order #XXXXXX - Refund has been issued",
                "body": "While reviewing your order, we processed a refund back to your account.",
            },
        ), mock.patch.object(crm_copyright_cancel, "_read_salesforce_email_composer_text", return_value=""):
            inserted = crm_copyright_cancel._salesforce_template_appears_inserted(
                driver,
                crm_copyright_cancel.COPYRIGHT_CANCEL_PROCESS,
            )

        self.assertTrue(inserted)

    def test_cancel_and_refund_skips_duplicate_work_when_stripe_refund_exists(self):
        driver = mock.Mock()
        state = {
            "transactions": [
                {"amount": "205.60", "tag": "Stripe.com", "type": "Stripe.com"},
                {"amount": "-205.60", "tag": "Refund", "type": "Refund"},
            ],
        }

        with mock.patch.object(crm_copyright_cancel, "_activate_crm_context"), \
             mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope"), \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_append_copyright_cancel_sales_note",
                 return_value={"already_present": True},
             ), \
             mock.patch.object(crm_copyright_cancel, "_get_order_live_state", return_value=state), \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_read_order_refund_fee_amount",
                 side_effect=AssertionError("refund fee should not be recalculated"),
             ):
            result = crm_copyright_cancel._cancel_and_refund_crm_order(
                driver,
                "crm-window",
                "4805418",
                dry_run=False,
                payment={"payment_type": "Stripe.com", "amount": "205.60"},
                reason="University logos",
            )

        self.assertTrue(result["refund"]["already_refunded"])
        self.assertEqual(result["cancel"]["reason"], "already_refunded")
        self.assertEqual(result["refund_fee"]["reason"], "already_refunded")

    def test_cancel_and_refund_skips_refund_work_for_zero_charge_order(self):
        driver = mock.Mock()
        state = {
            "subtotal": 0,
            "shipping_charges": "0.00",
            "transactions": [],
        }

        with mock.patch.object(crm_copyright_cancel, "_activate_crm_context"), \
             mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope"), \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_append_copyright_cancel_sales_note",
                 return_value={"updated": True},
             ), \
             mock.patch.object(crm_copyright_cancel, "_get_order_live_state", return_value=state), \
             mock.patch.object(crm_copyright_cancel, "_crm_order_already_cancelled", return_value=False), \
             mock.patch.object(crm_copyright_cancel, "_cancel_original_order", return_value={"cancelled": True}) as mock_cancel, \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_add_refund_fee_to_original",
                 side_effect=AssertionError("refund fee should be skipped"),
             ), \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_refund_via_stripe_payment_modal",
                 side_effect=AssertionError("refund modal should be skipped"),
             ):
            result = crm_copyright_cancel._cancel_and_refund_crm_order(
                driver,
                "crm-window",
                "4705293",
                dry_run=False,
                payment={"payment_type": "", "amount": "0.00"},
                reason="copyright test",
            )

        mock_cancel.assert_called_once_with(driver)
        self.assertTrue(result["cancel"]["cancelled"])
        self.assertEqual(result["refund_fee"]["reason"], "no_refundable_customer_charge")
        self.assertEqual(result["refund"]["reason"], "no_refundable_customer_charge")
        self.assertNotIn("order_state", result["refund"])

    def test_cancel_and_refund_zero_charge_skips_cancel_when_order_already_cancelled(self):
        driver = mock.Mock()
        state = {
            "subtotal": 0,
            "shipping_charges": "0.00",
            "transactions": [],
        }

        with mock.patch.object(crm_copyright_cancel, "_activate_crm_context"), \
             mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope"), \
             mock.patch.object(
                 crm_copyright_cancel,
                 "_append_copyright_cancel_sales_note",
                 return_value={"already_present": True},
             ), \
             mock.patch.object(crm_copyright_cancel, "_get_order_live_state", return_value=state), \
             mock.patch.object(crm_copyright_cancel, "_crm_order_already_cancelled", return_value=True), \
             mock.patch.object(crm_copyright_cancel, "_cancel_original_order") as mock_cancel:
            result = crm_copyright_cancel._cancel_and_refund_crm_order(
                driver,
                "crm-window",
                "4705293",
                dry_run=False,
                payment={"payment_type": "", "amount": "0.00"},
                reason="copyright test",
            )

        mock_cancel.assert_not_called()
        self.assertEqual(result["cancel"]["reason"], "already_cancelled")
        self.assertEqual(result["refund_fee"]["reason"], "no_refundable_customer_charge")

    def test_append_sales_note_does_not_copy_rendered_history(self):
        driver = mock.Mock()
        historical_note = "QGTBot: Sales Review failed. Existing rendered CRM note."
        update_result = {"updated": True, "already_present": False, "note": "new note"}

        with mock.patch.object(
            crm_copyright_cancel,
            "_order_scope",
            side_effect=[historical_note, update_result],
        ) as mock_order_scope, mock.patch.object(
            crm_copyright_cancel,
            "_save_order_and_wait",
            return_value={"saving": False},
        ):
            crm_copyright_cancel._append_copyright_cancel_sales_note(
                driver,
                "",
                dry_run=False,
                process=crm_copyright_cancel.OVERSIZE_EMB_TO_HDD_PROCESS,
            )

        update_script = mock_order_scope.call_args_list[1].args[1]
        self.assertIn("const existingDraft = String(r.addSalesNotes || '').trim();", update_script)
        self.assertNotIn("r.salesNotes", update_script)
        self.assertNotIn("r.filteredSalesNotes", update_script)

    def test_copyright_removal_sales_note_uses_removed_reason_format(self):
        note = crm_copyright_cancel._cancel_sales_note(
            "LA Dodgers",
            process=crm_copyright_cancel.COPYRIGHT_REMOVAL_PROCESS,
        )

        self.assertEqual(note, "Removed LA Dodgers copyright\nemailed")

    def test_prepare_no_cancel_applies_issue_copyright_status_for_reachout(self):
        driver = mock.Mock()
        status_result = {"status_applied": True, "status": "issue - copyright"}

        with mock.patch.object(
            crm_copyright_cancel,
            "_append_copyright_cancel_sales_note",
            return_value={"updated": True},
        ) as mock_note, mock.patch.object(
            crm_copyright_cancel,
            "_apply_order_status",
            return_value=status_result,
        ) as mock_apply:
            result = crm_copyright_cancel._prepare_no_cancel_crm_action(
                driver,
                "LA Dodgers",
                dry_run=False,
                process=crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS,
            )

        mock_note.assert_called_once_with(
            driver,
            "LA Dodgers",
            dry_run=False,
            process=crm_copyright_cancel.COPYRIGHT_REACHOUT_PROCESS,
        )
        mock_apply.assert_called_once_with(driver, crm_copyright_cancel.COPYRIGHT_REACHOUT_CRM_STATUS, dry_run=False)
        self.assertEqual(result["order_status"], status_result)
        self.assertTrue(result["cancel"]["skipped"])
        self.assertTrue(result["refund"]["skipped"])

    def test_prepare_no_cancel_leaves_hdd_order_status_alone(self):
        driver = mock.Mock()

        with mock.patch.object(
            crm_copyright_cancel,
            "_append_copyright_cancel_sales_note",
            return_value={"updated": True},
        ), mock.patch.object(crm_copyright_cancel, "_apply_order_status") as mock_apply:
            result = crm_copyright_cancel._prepare_no_cancel_crm_action(
                driver,
                "",
                dry_run=False,
                process=crm_copyright_cancel.COMPLICATED_EMB_TO_HDD_PROCESS,
            )

        mock_apply.assert_not_called()
        self.assertIsNone(result["order_status"])

    def test_apply_order_status_dry_run_does_not_type_or_click(self):
        driver = mock.Mock()

        with mock.patch.object(crm_copyright_cancel, "_order_status_already_applied", return_value=False):
            result = crm_copyright_cancel._apply_order_status(driver, "issue - copyright", dry_run=True)

        driver.execute_script.assert_not_called()
        self.assertFalse(result["status_applied"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["status"], "issue - copyright")

    @mock.patch.object(crm_copyright_cancel, "safe_driver_quit")
    @mock.patch.object(crm_copyright_cancel, "_prepare_and_maybe_send_salesforce_email")
    @mock.patch.object(crm_copyright_cancel, "_prepare_no_cancel_crm_action")
    @mock.patch.object(crm_copyright_cancel, "_cancel_and_refund_crm_order")
    @mock.patch.object(crm_copyright_cancel, "_read_payment_summary")
    @mock.patch.object(crm_copyright_cancel, "_wait_for_crm_contact_info")
    @mock.patch.object(crm_copyright_cancel, "_wait_for_order_scope")
    @mock.patch.object(crm_copyright_cancel, "_switch_to_crm_app_frame")
    @mock.patch.object(crm_copyright_cancel, "_login_to_crm_if_needed")
    @mock.patch.object(crm_copyright_cancel, "safe_get_with_partial_load")
    @mock.patch.object(crm_copyright_cancel, "_open_driver")
    def test_hdd_process_does_not_call_payment_cancel_or_refund_helpers(
        self,
        mock_open_driver,
        _mock_get,
        _mock_login,
        _mock_frame,
        _mock_wait_order,
        mock_contact,
        mock_payment,
        mock_cancel_refund,
        mock_no_cancel_action,
        mock_salesforce_email,
        _mock_quit,
    ):
        driver = mock.Mock(current_window_handle="crm-tab")
        mock_open_driver.return_value = driver
        mock_contact.return_value = {"email": "buyer@example.com"}
        mock_no_cancel_action.return_value = {"sales_note": {"updated": True}}
        mock_salesforce_email.return_value = {"sent": False, "dry_run": True}

        details = crm_copyright_cancel.process_single_order(
            "1234567",
            "",
            dry_run=True,
            process=crm_copyright_cancel.COMPLICATED_EMB_TO_HDD_PROCESS,
        )

        self.assertEqual(details["process"], "complicated_emb_to_hdd")
        mock_no_cancel_action.assert_called_once()
        mock_salesforce_email.assert_called_once()
        mock_payment.assert_not_called()
        mock_cancel_refund.assert_not_called()


class CrmUnlockOrdersTests(unittest.TestCase):
    def test_blank_locked_report_reloads_once_before_returning_no_orders(self):
        driver = mock.Mock()
        with mock.patch.object(crm_unlock_orders, "safe_get_with_partial_load") as safe_get, \
             mock.patch.object(crm_unlock_orders, "login_if_needed", return_value=False), \
             mock.patch.object(crm_unlock_orders, "wait_for_order_rows", side_effect=[[], []]) as rows, \
             mock.patch.object(crm_unlock_orders, "_looks_like_no_orders_state", return_value=False):
            result = crm_unlock_orders._open_locked_report_rows(driver)

        self.assertEqual(result, [])
        self.assertEqual(rows.call_count, 2)
        self.assertEqual(safe_get.call_count, 2)

    def test_verify_update_complete_extends_wait_while_apply_is_in_progress(self):
        clock = {"now": 100.0}
        sleep_calls = []

        def fake_time():
            return clock["now"]

        def fake_sleep(seconds):
            sleep_calls.append(seconds)
            clock["now"] += 5.0

        def fake_has_success(_driver):
            return clock["now"] >= 120.0

        def fake_has_progress(_driver):
            return True

        with mock.patch.object(crm_unlock_orders, "CRM_ACTION_TIMEOUT", 15), \
             mock.patch.object(crm_unlock_orders.time, "time", side_effect=fake_time), \
             mock.patch.object(crm_unlock_orders.time, "sleep", side_effect=fake_sleep), \
             mock.patch.object(crm_unlock_orders, "_has_update_success_message", side_effect=fake_has_success), \
             mock.patch.object(crm_unlock_orders, "_has_unlock_apply_progress", side_effect=fake_has_progress), \
             mock.patch.object(crm_unlock_orders, "_wait_for_order_count_to_settle", return_value=1):
            result = crm_unlock_orders.verify_update_complete(object(), previous_order_count=8)

        self.assertTrue(result["success_message_seen"])
        self.assertGreaterEqual(clock["now"], 120.0)
        self.assertGreaterEqual(len(sleep_calls), 4)

    @mock.patch.object(crm_unlock_orders, "safe_driver_quit")
    @mock.patch.object(crm_unlock_orders, "build_chrome_driver")
    @mock.patch.object(crm_unlock_orders, "kill_stale_chrome")
    @mock.patch.object(crm_unlock_orders, "_looks_like_no_orders_state", return_value=False)
    @mock.patch.object(crm_unlock_orders, "verify_update_complete", return_value={"success_message_seen": True, "no_orders_remaining": False, "remaining_order_count": 0})
    @mock.patch.object(crm_unlock_orders, "click_ok_on_modal")
    @mock.patch.object(crm_unlock_orders, "maybe_wait_for_confirmation_modal", return_value=False)
    @mock.patch.object(crm_unlock_orders, "click_apply")
    @mock.patch.object(crm_unlock_orders, "get_apply_button")
    @mock.patch.object(crm_unlock_orders, "choose_unlock_status")
    @mock.patch.object(crm_unlock_orders, "wait_for_order_preview_panel", return_value=object())
    @mock.patch.object(crm_unlock_orders, "select_all_orders", return_value=1)
    @mock.patch.object(crm_unlock_orders, "_collect_order_ids")
    @mock.patch.object(crm_unlock_orders, "_open_locked_report_rows")
    def test_unlocker_live_reopens_list_until_no_orders_remain(
        self,
        mock_open_rows,
        mock_collect_ids,
        _mock_select_all,
        _mock_preview,
        _mock_choose,
        _mock_get_apply,
        _mock_click_apply,
        _mock_confirm,
        _mock_ok,
        _mock_verify,
        _mock_no_orders,
        _mock_kill,
        mock_build_driver,
        _mock_quit,
    ):
        mock_build_driver.return_value = mock.Mock()
        mock_open_rows.side_effect = [[object()], [object()], []]
        mock_collect_ids.side_effect = [["1000001"], ["1000002"]]

        result = crm_unlock_orders._run_once("unlock_all", dry_run=False, headless_mode=True)

        self.assertEqual(result["order_ids"], ["1000001", "1000002"])
        self.assertEqual(result["order_count"], 2)
        self.assertEqual(result["refresh_passes"], 3)
        self.assertEqual(mock_open_rows.call_count, 3)


class CrmPushBackTests(unittest.TestCase):
    def test_push_back_precheck_skips_lime_green_max_rush_before_dates(self):
        result = crm_push_back._precheck_row(
            {
                "orderId": "4883001",
                "rowText": "Order 4883001",
                "colorLabel": "lime_green",
            }
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "max_rush_lime_green_skipped")
        self.assertEqual(result["row_color"], "lime_green")
        self.assertFalse(result["manual_review_required"])
        self.assertIn("Max Rush", result["message"])

    def test_push_back_report_collection_explicitly_skips_lime_green_rows(self):
        driver = mock.Mock()
        driver.execute_script.return_value = [
            {
                "orderId": "4883001",
                "colors": [{"backgroundColor": "rgb(34, 236, 72)"}],
            },
            {
                "orderId": "4883002",
                "colors": [{"backgroundColor": "rgb(243, 196, 156)"}],
            },
        ]

        with mock.patch.object(crm_push_back, "safe_get_with_partial_load"), mock.patch.object(
            crm_push_back, "login_if_needed", return_value=False
        ):
            rows = crm_push_back._collect_push_back_rows_with_driver(
                driver,
                "rush",
                5,
                "https://crm.example/push-back",
            )

        self.assertEqual([row["orderId"] for row in rows], ["4883002"])
        self.assertEqual(rows[0]["colorLabel"], "tan")

    def test_push_back_stock_ordered_precheck_requires_status_and_stock_ordered(self):
        self.assertTrue(
            crm_push_back._text_indicates_push_back_stock_already_ordered(
                "Stock Status: Ordered Stock : Ordered"
            )
        )
        self.assertFalse(
            crm_push_back._text_indicates_push_back_stock_already_ordered(
                "Stock Status: Need To Order Stock : Ordered"
            )
        )
        self.assertFalse(
            crm_push_back._text_indicates_push_back_stock_already_ordered(
                "Stock Status: Ordered Stock : Need To Order"
            )
        )

    @mock.patch.object(crm_push_back, "_run_order_goods_with_push_back_status")
    @mock.patch.object(crm_push_back, "_change_crm_production_date_with_retry")
    @mock.patch.object(crm_push_back, "_page_indicates_push_back_stock_already_ordered", return_value=True)
    @mock.patch.object(crm_push_back, "_open_and_read_order")
    def test_push_back_skips_open_order_when_stock_status_and_stock_are_ordered(
        self,
        mock_open,
        _mock_stock_ordered,
        mock_change,
        mock_order_goods,
    ):
        row = {
            "orderId": "4771443",
            "rowText": "Order 4771443 Production Date: 2026-06-30 Fulfillment Date: 2026-07-03",
            "productionText": "Production Date: 2026-06-30",
            "colorLabel": "tan",
        }
        mock_open.return_value = {
            "production_date": crm_shipping_bypasser.datetime(2026, 6, 30).date(),
            "due_date": crm_shipping_bypasser.datetime(2026, 7, 3).date(),
        }

        result = crm_push_back._run_order_with_driver(mock.Mock(), row, "rush", "https://crm.example/report")

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "stock_already_ordered_skipped")
        self.assertFalse(result["manual_review_required"])
        mock_change.assert_not_called()
        mock_order_goods.assert_not_called()

    def test_save_retry_returns_target_date_when_refresh_shows_first_save_persisted(self):
        driver = mock.Mock()
        target_date = crm_shipping_bypasser.datetime(2026, 6, 26).date()

        with mock.patch.object(crm_push_back, "_change_crm_production_date", side_effect=RuntimeError("CRM froze during save")) as mock_change, \
             mock.patch.object(crm_push_back, "_refresh_and_read_order_for_save_retry", return_value={"production_date": target_date}) as mock_refresh:
            saved_date, retry_count, retry_error = crm_push_back._change_crm_production_date_with_retry(
                driver,
                "4756567",
                target_date,
                "rush",
                "https://crm.example/report",
            )

        self.assertEqual(saved_date, target_date)
        self.assertEqual(retry_count, 1)
        self.assertIn("CRM froze during save", retry_error)
        self.assertEqual(mock_change.call_count, 1)
        mock_refresh.assert_called_once_with(driver, "4756567", "rush", "https://crm.example/report")

    def test_save_retry_refreshes_and_tries_save_once_more_when_target_not_persisted(self):
        driver = mock.Mock()
        stale_date = crm_shipping_bypasser.datetime(2026, 6, 25).date()
        target_date = crm_shipping_bypasser.datetime(2026, 6, 26).date()

        with mock.patch.object(
            crm_push_back,
            "_change_crm_production_date",
            side_effect=[RuntimeError("CRM froze during save"), target_date],
        ) as mock_change, \
             mock.patch.object(crm_push_back, "_refresh_and_read_order_for_save_retry", return_value={"production_date": stale_date}):
            saved_date, retry_count, retry_error = crm_push_back._change_crm_production_date_with_retry(
                driver,
                "4756567",
                target_date,
                "rush",
                "https://crm.example/report",
            )

        self.assertEqual(saved_date, target_date)
        self.assertEqual(retry_count, 1)
        self.assertIn("CRM froze during save", retry_error)
        self.assertEqual(mock_change.call_count, 2)

    def test_push_back_retries_next_business_day_after_no_purchase_plan(self):
        row = {
            "orderId": "4882684",
            "rowText": "Order 4882684 Production Date: 2026-07-20 Due Date: 2026-07-24",
            "productionText": "Production Date: 2026-07-20",
            "colorLabel": "tan",
        }
        no_plan = [{
            "success": False,
            "outcome": "auto_order_no_purchase_plan",
            "message": "Failed to auto order stock: No purchase plan available for products",
        }]
        ordered = [{
            "success": True,
            "outcome": "auto_order_succeeded",
            "message": "(Auto Order) Goods have been ordered successfully.",
        }]
        with mock.patch.object(
            crm_push_back,
            "_open_and_read_order",
            return_value={
                "production_date": crm_shipping_bypasser.datetime(2026, 7, 20).date(),
                "due_date": crm_shipping_bypasser.datetime(2026, 7, 24).date(),
            },
        ), mock.patch.object(
            crm_push_back,
            "_page_indicates_push_back_stock_already_ordered",
            return_value=False,
        ), mock.patch.object(
            crm_push_back,
            "_change_crm_production_date_with_retry",
            side_effect=lambda _driver, _order_id, target, _filter, _url: (target, 0, None),
        ) as change_date, mock.patch.object(
            crm_push_back,
            "_run_order_goods_with_push_back_status",
            side_effect=[no_plan, ordered],
        ), mock.patch.object(
            crm_push_back,
            "_wait_for_push_back_stock_confirmation",
            return_value=True,
        ):
            result = crm_push_back._run_order_with_driver(mock.Mock(), row, "rush", "https://crm.example/report")

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "push_back_saved_stock_ordered")
        self.assertEqual(len(result["stock_order_attempts"]), 2)
        self.assertEqual(change_date.call_args_list[0].args[2].isoformat(), "2026-07-21")
        self.assertEqual(change_date.call_args_list[1].args[2].isoformat(), "2026-07-22")

    def test_push_back_no_purchase_plan_fails_at_due_date_guard(self):
        row = {
            "orderId": "4877971",
            "rowText": "Order 4877971 Production Date: 2026-07-20 Due Date: 2026-07-22",
            "productionText": "Production Date: 2026-07-20",
            "colorLabel": "tan",
        }
        no_plan = [{
            "success": False,
            "outcome": "auto_order_no_purchase_plan",
            "message": "Failed to auto order stock: No purchase plan available for products",
        }]
        with mock.patch.object(
            crm_push_back,
            "_open_and_read_order",
            return_value={
                "production_date": crm_shipping_bypasser.datetime(2026, 7, 20).date(),
                "due_date": crm_shipping_bypasser.datetime(2026, 7, 22).date(),
            },
        ), mock.patch.object(
            crm_push_back,
            "_page_indicates_push_back_stock_already_ordered",
            return_value=False,
        ), mock.patch.object(
            crm_push_back,
            "_change_crm_production_date_with_retry",
            side_effect=lambda _driver, _order_id, target, _filter, _url: (target, 0, None),
        ), mock.patch.object(
            crm_push_back,
            "_run_order_goods_with_push_back_status",
            return_value=no_plan,
        ):
            result = crm_push_back._run_order_with_driver(mock.Mock(), row, "rush", "https://crm.example/report")

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "push_back_no_purchase_plan_due_date_reached")
        self.assertEqual(result["saved_production_date"], "2026-07-21")

    def test_push_back_routes_shipment_cost_failure_to_shipping_bypasser(self):
        row = {
            "orderId": "4882019",
            "rowText": "Order 4882019 Production Date: 2026-07-20 Due Date: 2026-07-24",
            "productionText": "Production Date: 2026-07-20",
            "colorLabel": "tan",
        }
        shipment_cost = [{
            "success": False,
            "outcome": "auto_order_shipment_cost_exceeded",
            "message": "Failed to auto order stock: Purchase plan exceeded maximum shipment cost as percentage of product cost",
        }]
        with mock.patch.object(
            crm_push_back,
            "_open_and_read_order",
            return_value={
                "production_date": crm_shipping_bypasser.datetime(2026, 7, 20).date(),
                "due_date": crm_shipping_bypasser.datetime(2026, 7, 24).date(),
            },
        ), mock.patch.object(
            crm_push_back,
            "_page_indicates_push_back_stock_already_ordered",
            return_value=False,
        ), mock.patch.object(
            crm_push_back,
            "_change_crm_production_date_with_retry",
            side_effect=lambda _driver, _order_id, target, _filter, _url: (target, 0, None),
        ), mock.patch.object(
            crm_push_back,
            "_run_order_goods_with_push_back_status",
            return_value=shipment_cost,
        ), mock.patch.object(
            crm_push_back,
            "_run_shipping_bypasser_with_current_crm_driver",
            return_value={"success": True, "message": "Stock manually ordered.", "report": [], "manual_review_required": False},
        ) as shipping_bypasser:
            driver = mock.Mock()
            result = crm_push_back._run_order_with_driver(driver, row, "rush", "https://crm.example/report")

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "push_back_shipping_bypass_ordered")
        shipping_bypasser.assert_called_once_with(driver, "4882019", dry_run=False)

    def test_auto_order_feedback_classifier_matches_saved_crm_messages(self):
        self.assertEqual(
            crm_order_goods._classify_auto_order_feedback_text(
                "Failed to auto order stock: No purchase plan available for products"
            ),
            "no_purchase_plan",
        )
        self.assertEqual(
            crm_order_goods._classify_auto_order_feedback_text(
                "Failed to auto order stock: Purchase plan exceeded maximum shipment cost as percentage of product cost"
            ),
            "shipment_cost_exceeded",
        )
        self.assertEqual(
            crm_order_goods._classify_auto_order_feedback_text(
                "(Auto Order) Goods have been ordered successfully."
            ),
            "ordered",
        )

    def test_auto_order_feedback_prefers_failure_over_visible_stale_success(self):
        driver = mock.Mock()
        driver.execute_script.return_value = [
            "(Auto Order) Goods have been ordered successfully.",
            "Failed to auto order stock: No purchase plan available for products",
        ]

        feedback = crm_order_goods._read_visible_auto_order_feedback(driver)

        self.assertEqual(feedback["kind"], "no_purchase_plan")

    def test_push_back_stock_summary_preserves_exact_crm_feedback(self):
        result = [{
            "success": False,
            "outcome": "auto_order_no_purchase_plan",
            "message": "Failed to auto order stock: No purchase plan available for products",
        }]

        summary = crm_push_back._stock_order_summary(result)

        self.assertIn("0/1", summary)
        self.assertIn("No purchase plan available for products", summary)

    @mock.patch.object(crm_push_back, "_run_order_worker_payload")
    @mock.patch.object(crm_push_back, "_clone_profile_for_worker")
    @mock.patch.object(crm_push_back, "_collect_push_back_rows")
    def test_parallel_push_back_batch_processes_rows_with_worker_limit(
        self,
        mock_collect_rows,
        mock_clone_profile,
        mock_worker_payload,
    ):
        rows = [
            {"orderId": "4771443", "rowText": "", "productionText": ""},
            {"orderId": "4771444", "rowText": "", "productionText": ""},
        ]
        mock_collect_rows.return_value = rows
        mock_clone_profile.side_effect = lambda _base, _label, worker_slot=None, **_kwargs: (
            None,
            f"profile_{worker_slot}",
        )
        mock_worker_payload.side_effect = lambda row, *_args, **_kwargs: crm_push_back._result(
            row["orderId"],
            True,
            "push_back_saved_stock_ordered",
            "ok",
            manual_review_required=False,
            stock_order_attempted=True,
            stock_order_success=True,
        )

        payload = crm_push_back._run_parallel_batch_with_mode(
            True,
            processing_filter="rush",
            dry_run=False,
            batch_size=2,
            list_url="https://crm.example/push-back",
            parallel_workers=4,
        )

        self.assertTrue(payload["success"])
        self.assertEqual(payload["order_ids"], ["4771443", "4771444"])
        self.assertEqual(payload["parallel_workers"], 2)
        self.assertEqual(mock_worker_payload.call_count, 2)


class ShippingBypasserTests(unittest.TestCase):
    def test_crm_readiness_refreshes_once_before_reopening_order(self):
        driver = mock.Mock()
        with mock.patch.object(
            crm_shipping_bypasser,
            "_wait_for_order_goods_page_ready",
            side_effect=[False, True],
        ) as ready, mock.patch.object(crm_shipping_bypasser.time, "sleep"):
            result = crm_shipping_bypasser._require_crm_order_ready_once_with_refresh(driver, "4845038")

        self.assertTrue(result)
        driver.refresh.assert_called_once_with()
        self.assertEqual(ready.call_count, 2)

    def test_clickable_text_finder_includes_angular_edit_order_controls(self):
        driver = mock.Mock()
        driver.execute_script.return_value = None

        result = crm_shipping_bypasser._find_clickable_by_text(driver, r"edit\s+order")

        self.assertIsNone(result)
        script = driver.execute_script.call_args.args[0]
        self.assertIn("[ng-click]", script)
        self.assertIn("editModeOn", script)

    def test_clickable_text_finder_retries_in_default_content(self):
        element = mock.Mock()
        driver = mock.Mock()
        driver.execute_script.side_effect = [None, element]

        result = crm_shipping_bypasser._find_clickable_by_text(driver, r"edit\s+order")

        self.assertIs(result, element)
        driver.switch_to.default_content.assert_called_once()
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_clickable_text_finder_scans_frames_and_preserves_found_context(self):
        frame = mock.Mock(name="crm_app_frame")
        element = mock.Mock()
        driver = mock.Mock()
        driver.execute_script.side_effect = [None, None, element]
        driver.find_elements.return_value = [frame]

        result = crm_shipping_bypasser._find_clickable_by_text(driver, r"edit\s+order")

        self.assertIs(result, element)
        driver.switch_to.frame.assert_called_once_with(frame)
        self.assertEqual(driver.switch_to.default_content.call_count, 2)

    def test_youth_product_quantities_strip_y_prefix_for_sanmar(self):
        product = {
            "product_id": "YST350",
            "quantities": {"YM": 1, "YL": 2},
        }

        self.assertEqual(
            crm_shipping_bypasser._sanmar_quantities_for_product(product),
            {"M": 1, "L": 2},
        )

    def test_gildan_youth_b_product_quantities_strip_y_prefix_for_sanmar(self):
        product = {
            "product_id": "G500B",
            "product_name": "Gildan Heavy Cotton Kids T-Shirt",
            "quantities": {"YXS": 2, "YS": 2, "YM": 2, "YL": 1},
        }

        self.assertEqual(
            crm_shipping_bypasser._sanmar_quantities_for_product(product),
            {"XS": 2, "S": 2, "M": 2, "L": 1},
        )

    def test_non_youth_product_quantities_keep_sizes(self):
        product = {
            "product_id": "ST350",
            "quantities": {"YM": 1, "M": 2},
        }

        self.assertEqual(
            crm_shipping_bypasser._sanmar_quantities_for_product(product),
            {"YM": 1, "M": 2},
        )

    def test_infant_month_range_quantities_map_to_sanmar_month_sizes(self):
        product = {
            "product_id": "4400",
            "product_name": "Rabbit Skins Baby Onesie",
            "quantities": {"3-6MOS": 2, "6-12MOS": 1, "0003": 1},
        }

        self.assertEqual(
            crm_shipping_bypasser._sanmar_quantities_for_product(product),
            {"6M": 2, "12M": 1, "3M": 1},
        )

    def test_one_size_quantities_map_to_sanmar_osfa(self):
        product = {
            "product_id": "CP80",
            "product_name": "Port & Company Six-Panel Twill Cap",
            "quantities": {"ONESIZE": 4},
        }

        self.assertEqual(
            crm_shipping_bypasser._sanmar_quantities_for_product(product),
            {"OSFA": 4},
        )

    def test_cornerstone_safety_vest_combo_sizes_are_kept_for_sanmar(self):
        product = {
            "product_id": "CSV102",
            "product_name": "CornerStone ANSI 107 Class 2 Mesh Back Safety Vest",
            "quantities": {"S/M": 2, "L / XL": 10, "2XL/3XL": 1, "4X/5X": 3},
        }

        self.assertEqual(
            crm_shipping_bypasser._sanmar_quantities_for_product(product),
            {"S/M": 2, "L/XL": 10, "2/3X": 1, "4/5X": 3},
        )

    def test_style_sub_uses_detail_style_as_sanmar_product_id(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {
            "bodyText": "Due Date: 06/22/26 Production Date: 06/19/26",
            "items": [
                {
                    "stockLine": "Style Sub - Alpha Stock",
                    "color": "Style_Sub",
                    "styleSubStyle": "LST420LS",
                    "styleSubColor": "White",
                    "styleSubDescription": "Sport-Tek Women's Posi-UV Pro Long Sleeve",
                    "quantities": {"M": 3},
                    "sizes": ["M"],
                }
            ],
            "activeTabText": "H-MarkStrutn991 1 - QTY: 3",
            "activePanelText": "",
        }

        with mock.patch.object(crm_shipping_bypasser, "refresh_if_crm_challenge_attempts_exceeded", return_value=False):
            order = crm_shipping_bypasser._extract_order_data(driver, "4717943")

        self.assertEqual(order["product_id"], "LST420LS")
        self.assertEqual(order["color"], "White")
        self.assertEqual(order["product_name"], "Sport-Tek Women's Posi-UV Pro Long Sleeve")
        self.assertEqual(order["quantities"], {"M": 3})
        self.assertEqual(order["po"], "H-MarkStrutn991")

    def test_3001c_search_uses_base_3001_inventory_handler(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {"product_id": "3001C", "is_a4": False}
        )

        self.assertEqual(options["search_id"], "3001")
        self.assertTrue(options["click_inventory_button"])
        self.assertEqual(options["handler"], "3001C")

    def test_bella_canvas_crm_c_suffix_maps_to_sanmar_bc_style(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {
                "product_id": "3413C",
                "product_name": "BELLA+CANVAS Triblend T-Shirt",
            }
        )

        self.assertEqual(options["search_id"], "BC3413")
        self.assertTrue(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Bella+Canvas")
        self.assertIn("BC3413", options["expected_style_keys"])
        self.assertNotIn("BC3413C", options["expected_style_keys"])

    def test_bella_canvas_known_id_maps_without_product_name(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {"product_id": "3413C"}
        )

        self.assertEqual(options["search_id"], "BC3413")
        self.assertIn("BC3413", options["expected_style_keys"])

    def test_bella_canvas_b_prefix_maps_to_sanmar_bc_style(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {
                "product_id": "B6500",
                "product_name": "Bella + Canvas Women's Jersey Long Sleeve Shirt",
            }
        )

        self.assertEqual(options["search_id"], "BC6500")
        self.assertTrue(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Bella+Canvas")
        self.assertIn("BC6500", options["expected_style_keys"])

    def test_bella_canvas_100b_maps_to_sanmar_bc100b(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {
                "product_id": "100B",
                "product_name": "Bella + Canvas Jersey Baby Short-Sleeve Onesie",
            }
        )

        self.assertEqual(options["search_id"], "BC100B")
        self.assertTrue(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Bella+Canvas")
        self.assertIn("BC100B", options["expected_style_keys"])

    def test_sanmar_inventory_view_clicks_inventory_pricing_gate(self):
        driver = mock.Mock()
        driver.execute_script.return_value = "BC6500 Check inventory and pricing"

        with mock.patch.object(
            crm_shipping_bypasser,
            "_sanmar_inventory_controls_visible",
            side_effect=[False, True],
        ), mock.patch.object(
            crm_shipping_bypasser,
            "_click_sanmar_inventory_pricing_button",
            return_value=True,
        ) as click_gate, mock.patch.object(crm_shipping_bypasser.time, "sleep"):
            self.assertTrue(crm_shipping_bypasser._ensure_sanmar_inventory_view(driver))

        click_gate.assert_called_once_with(driver, timeout=1)

    def test_sanmar_auth_state_confirms_cart_without_login_form(self):
        self.assertTrue(
            crm_shipping_bypasser._sanmar_state_confirms_login(
                {
                    "text": "My Shopping Box Continue Checkout",
                    "hasPasswordInput": False,
                }
            )
        )
        self.assertFalse(
            crm_shipping_bypasser._sanmar_state_confirms_login(
                {
                    "text": "My Shopping Box Log In",
                    "hasPasswordInput": True,
                }
            )
        )

    def test_sanmar_autofilled_login_click_waits_for_filled_password(self):
        driver = mock.Mock()
        driver.execute_script.side_effect = [
            {"clicked": False, "reason": "password_not_filled"},
            {"clicked": True},
        ]

        with mock.patch.object(crm_shipping_bypasser.time, "sleep"), mock.patch.object(
            crm_shipping_bypasser.time,
            "time",
            side_effect=[0, 0, 0],
        ):
            clicked = crm_shipping_bypasser._click_sanmar_autofilled_login(driver, timeout=1)

        self.assertTrue(clicked)
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_rabbit_skins_search_adds_rs_prefix_and_inventory_handler(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {"product_id": "4400", "product_name": "Infant Short Sleeve Baby Rib Bodysuit"}
        )

        self.assertEqual(options["search_id"], "RS4400")
        self.assertTrue(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Rabbit Skins")
        self.assertIn("RS4400", options["expected_style_keys"])
        self.assertIn("4400", options["expected_style_keys"])

    def test_a4_search_keeps_prefixed_handler(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {"product_id": "NW3201", "is_a4": True}
        )

        self.assertEqual(options["search_id"], "a4NW3201")
        self.assertFalse(options["click_inventory_button"])
        self.assertEqual(options["handler"], "A4")

    def test_jerzees_search_adds_sanmar_m_suffix(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {"product_id": "562", "product_name": "Jerzees NuBlend Crewneck Sweatshirt"}
        )

        self.assertEqual(options["search_id"], "562M")
        self.assertFalse(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Jerzees")
        self.assertIn("562", options["expected_style_keys"])
        self.assertIn("562M", options["expected_style_keys"])

    def test_jerzees_search_keeps_existing_sanmar_suffix(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {"product_id": "29LS", "product_name": "Jerzees Dri-Power Long Sleeve T-Shirt"}
        )

        self.assertEqual(options["search_id"], "29LS")
        self.assertEqual(options["handler"], "Jerzees")

    def test_next_level_n_style_does_not_use_a4_prefix(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {
                "product_id": "N6210",
                "product_name": "Next Level Cotton Blend T-Shirt",
                "is_a4": False,
            }
        )

        self.assertEqual(options["search_id"], "NL6210")
        self.assertFalse(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Next Level")
        self.assertIn("N6210", options["expected_style_keys"])
        self.assertIn("NL6210", options["expected_style_keys"])

    def test_next_level_trailing_nl_maps_to_sanmar_prefix(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {
                "product_id": "3933NL",
                "product_name": "Women's Cotton Tank",
                "is_a4": False,
            }
        )

        self.assertEqual(options["search_id"], "NL3933")
        self.assertFalse(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Next Level")
        self.assertIn("3933NL", options["expected_style_keys"])
        self.assertIn("NL3933", options["expected_style_keys"])

    def test_next_level_bare_numeric_style_adds_sanmar_prefix(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {
                "product_id": "3600",
                "product_name": "Next Level Cotton T-Shirt",
                "is_a4": False,
            }
        )

        self.assertEqual(options["search_id"], "NL3600")
        self.assertFalse(options["click_inventory_button"])
        self.assertEqual(options["handler"], "Next Level")
        self.assertIn("3600", options["expected_style_keys"])
        self.assertIn("NL3600", options["expected_style_keys"])

    def test_bare_numeric_style_does_not_add_next_level_prefix_without_brand(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {
                "product_id": "3600",
                "product_name": "Generic Cotton T-Shirt",
                "is_a4": False,
            }
        )

        self.assertEqual(options["search_id"], "3600")
        self.assertEqual(options["handler"], "")
        self.assertNotIn("NL3600", options["expected_style_keys"])

    def test_gildan_search_maps_short_crm_ids_to_sanmar_styles(self):
        cases = {
            "G500": "5000",
            "G500B": "5000B",
            "G500L": "5000L",
            "G640": "64000",
            "G640L": "64000L",
            "G640CVC": "64000CVC",
        }

        for crm_style, sanmar_style in cases.items():
            with self.subTest(crm_style=crm_style):
                options = crm_shipping_bypasser._sanmar_search_options_for_product(
                    {"product_id": crm_style, "product_name": "Gildan Heavy Cotton T-Shirt"}
                )

                self.assertEqual(options["search_id"], sanmar_style)
                self.assertFalse(options["click_inventory_button"])
                self.assertEqual(options["handler"], "Gildan")
                self.assertIn(crm_style, options["expected_style_keys"])
                self.assertIn(sanmar_style, options["expected_style_keys"])

    def test_gildan_search_keeps_exact_sanmar_g_styles(self):
        options = crm_shipping_bypasser._sanmar_search_options_for_product(
            {"product_id": "G2400", "product_name": "Gildan Ultra Cotton Long Sleeve T-Shirt"}
        )

        self.assertEqual(options["search_id"], "G2400")
        self.assertEqual(options["handler"], "Gildan")

    def test_cart_validation_matches_same_style_lines_by_color(self):
        product_lines = [
            {
                "product": {"index": 1, "product_id": "ST350LS", "color": "Carolina Blue"},
                "search_id": "ST350LS",
                "expected_style_keys": ["ST350LS"],
                "quantities": {"L": 1, "M": 1},
            },
            {
                "product": {"index": 2, "product_id": "ST350LS", "color": "Black"},
                "search_id": "ST350LS",
                "expected_style_keys": ["ST350LS"],
                "quantities": {"L": 1, "M": 1},
            },
            {
                "product": {"index": 3, "product_id": "ST350LS", "color": "Iron Grey"},
                "search_id": "ST350LS",
                "expected_style_keys": ["ST350LS"],
                "quantities": {"L": 1, "M": 1, "XL": 4},
            },
        ]
        cart_lines = [
            {"style": "ST350LS", "color": "Carolina Blue", "size": "M", "quantity": 1, "warehouse": "Phoenix, AZ"},
            {"style": "ST350LS", "color": "Carolina Blue", "size": "L", "quantity": 1, "warehouse": "Phoenix, AZ"},
            {"style": "ST350LS", "color": "Black", "size": "M", "quantity": 1, "warehouse": "Phoenix, AZ"},
            {"style": "ST350LS", "color": "Black", "size": "L", "quantity": 1, "warehouse": "Phoenix, AZ"},
            {"style": "ST350LS", "color": "Iron Grey", "size": "M", "quantity": 1, "warehouse": "Phoenix, AZ"},
            {"style": "ST350LS", "color": "Iron Grey", "size": "L", "quantity": 1, "warehouse": "Phoenix, AZ"},
            {"style": "ST350LS", "color": "Iron Grey", "size": "XL", "quantity": 4, "warehouse": "Phoenix, AZ"},
        ]

        with mock.patch.object(crm_shipping_bypasser, "_read_sanmar_cart_lines", return_value=cart_lines):
            result = crm_shipping_bypasser._validate_sanmar_cart_contents(
                mock.Mock(),
                product_lines,
                warehouse="Phoenix, AZ",
            )

        self.assertTrue(result["success"], result.get("issues"))

    def test_sanmar_color_alias_matches_abbreviated_crm_heathered_color(self):
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("Hth Watr/He Ch")

        self.assertIn("Heathered Watermelon/ Heathered Charcoal", aliases)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "Heathered Watermelon/ Heathered Charcoal",
                "Hth Watr/He Ch",
            )
        )

    def test_sanmar_color_alias_matches_deep_red_white_crm_abbreviation(self):
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("DpRd/Whit")

        self.assertIn("Deep Red/ White", aliases)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "Deep Red/ White",
                "DpRd/Whit",
            )
        )

    def test_sanmar_color_abbreviation_rejects_different_deep_combo(self):
        self.assertFalse(
            crm_shipping_bypasser._cart_color_matches(
                "Deep Royal/ White",
                "DpRd/Whit",
            )
        )

    def test_sanmar_color_alias_rejects_different_heathered_combo(self):
        self.assertFalse(
            crm_shipping_bypasser._cart_color_matches(
                "Heathered Watermelon/ Heathered Cherry",
                "Hth Watr/He Ch",
            )
        )

    def test_sanmar_4528_true_navy_matches_j_navy(self):
        product = {"product_id": "4528"}
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("TRUE NAVY", product=product)

        self.assertIn("J. Navy", aliases)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "J. Navy",
                "TRUE NAVY",
                product=product,
            )
        )

    def test_sanmar_j325_btl_grey_matches_battleship_grey(self):
        product = {
            "product_id": "J325",
            "product_name": "Port Authority Core Soft Shell Vest",
        }
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("Btl Grey", product=product)

        self.assertIn("Battleship Grey", aliases)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "Battleship Grey",
                "Btl Grey",
                product=product,
            )
        )

    def test_sanmar_j325_dress_blue_navy_variant_matches_sanmar_label(self):
        product = {
            "product_id": "J325",
            "product_name": "Port Authority Core Soft Shell Vest",
        }

        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "Dress Blue Navy",
                "DsBlNavy",
                product=product,
            )
        )

    def test_sanmar_k700_river_blue_navy_variant_matches_sanmar_label(self):
        product = {
            "product_id": "K700",
            "product_name": "Port Authority Shirt Collar Polo",
        }
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("RiverBlNv", product=product)

        self.assertIn("River Blue Navy", aliases)
        self.assertIn(
            "River Blue Navy",
            crm_shipping_bypasser._sanmar_color_label_options("RiverBlNv", product=product),
        )
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "River Blue Navy",
                "RiverBlNv",
                product=product,
            )
        )

    def test_sanmar_true_navy_j_navy_alias_is_limited_to_4528(self):
        self.assertNotIn("J. Navy", crm_shipping_bypasser._sanmar_color_alias_labels("TRUE NAVY"))
        self.assertFalse(
            crm_shipping_bypasser._cart_color_matches(
                "J. Navy",
                "TRUE NAVY",
                product={"product_id": "4529"},
            )
        )

    def test_sanmar_safety_color_aliases_match_abbreviated_sanmar_names(self):
        self.assertIn("S. Orange", crm_shipping_bypasser._sanmar_color_alias_labels("Safety Orange"))
        self.assertIn("S. Green", crm_shipping_bypasser._sanmar_color_alias_labels("Safety Green"))
        self.assertTrue(crm_shipping_bypasser._cart_color_matches("S. Orange", "Safety Orange"))
        self.assertTrue(crm_shipping_bypasser._cart_color_matches("S. Green", "Safety Green"))

    def test_sanmar_lst402_color_aliases_match_product_colors(self):
        product = {
            "product_id": "LST402",
            "product_name": "Sport-Tek Women's PosiCharge Tri-Blend Wicking Tank",
        }
        cases = [
            ("Black Triad Solid", "Black Triad Sld"),
            ("Dark Grey Heather", "Dk Grey Hthr"),
            ("Light Grey Heather", "Lt Grey Hthr"),
            ("Pink Raspberry Heather", "Pnk Raspberry Hthr"),
            ("Pond Blue Heather", "Pond Blue Hthr"),
        ]

        for sanmar_color, crm_color in cases:
            with self.subTest(crm_color=crm_color):
                aliases = crm_shipping_bypasser._sanmar_color_alias_labels(crm_color, product=product)

                self.assertIn(sanmar_color, aliases)
                self.assertTrue(
                    crm_shipping_bypasser._cart_color_matches(
                        sanmar_color,
                        crm_color,
                        product=product,
                    )
                )

    def test_sanmar_selected_color_label_strips_add_to_shopping_box_suffix(self):
        self.assertEqual(
            crm_shipping_bypasser._clean_sanmar_selected_color_label("Pond Blue Heather Add to shopping box"),
            "Pond Blue Heather",
        )

    def test_sanmar_a4_slash_color_alias_confirms_sanmar_spacing(self):
        product = {"product_id": "NF1270", "product_name": "A4 Reversible Mesh Tank", "is_a4": True}

        labels = crm_shipping_bypasser._sanmar_color_label_options("ROYAL/WHITE", product=product)

        self.assertIn("ROYAL/ WHITE", labels)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "Royal/ White",
                "ROYAL/WHITE",
                product=product,
            )
        )

    def test_rabbit_skins_white_solid_black_matches_sanmar_white_black(self):
        product = {
            "product_id": "RS3330",
            "product_name": "Rabbit Skins Toddler Baseball Fine Jersey Tee",
        }
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("White Solid/ Black", product=product)

        self.assertIn("White/ Black", aliases)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "White/ Black",
                "White Solid/ Black",
                product=product,
            )
        )

    def test_bella_canvas_grass_green_abbreviation_matches_sanmar_not_aqua(self):
        product = {
            "product_id": "3413C",
            "product_name": "Bella + Canvas Tri-Blend T-Shirt",
        }
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("GRASS GRN TRBLND", product=product)

        self.assertIn("Grass Green Triblend", aliases)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "Grass Green Triblend",
                "GRASS GRN TRBLND",
                product=product,
            )
        )
        self.assertFalse(
            crm_shipping_bypasser._cart_color_matches(
                "Aqua Triblend",
                "GRASS GRN TRBLND",
                product=product,
            )
        )

    def test_bella_canvas_3413_crm_colors_match_available_sanmar_colors(self):
        product = {
            "product_id": "3413C",
            "product_name": "Bella + Canvas Tri-Blend T-Shirt",
        }
        cases = {
            "AQUA TRIBLEND": "Aqua Triblend",
            "ATH GREY TRBLND": "Athletic Grey Triblend",
            "BERRY TRIBLEND": "Berry Triblend",
            "BLK HTHR TRIBLND": "Black Heather Triblend",
            "BLUE TRBLND": "Blue Triblend",
            "BROWN TRIBLEND": "Brown Triblend",
            "Blue Storm Triblend": "Blue Storm Triblend",
            "Brick Triblend": "Brick Triblend",
            "CARDINAL TRBLND": "Cardinal Triblend",
            "CHAR-BLACK TRIB": "Charcoal-Black Triblend",
            "CLAY TRIBLEND": "Clay Triblend",
            "Cement Triblend": "Cement Triblend",
            "Charity Pink Triblend": "Charity Pink Triblend",
            "DENIM TRIBLEND": "Denim Triblend",
            "Dark Lavender Triblend": "Dark Lavender Triblend",
            "Dusty Blue Triblend": "Dusty Blue Triblend",
            "EMERALD TRIBLEND": "Emerald Triblend",
            "GRASS GRN TRBLND": "Grass Green Triblend",
            "GREEN TRIBLEND": "Green Triblend",
            "GREY TRIBLEND": "Grey Triblend",
            "ICE BLUE TRIBLND": "Ice Blue Triblend",
            "Kelly Triblend": "Kelly Triblend",
            "Lilac Triblend": "Lilac Triblend",
            "MAROON TRIBLEND": "Maroon Triblend",
            "MAUVE TRIBLEND": "Mauve Triblend",
            "Military Green Triblend": "Military Green Triblend",
            "MINT TRIBLEND": "Mint Triblend",
            "Mustard Triblend": "Mustard Triblend",
            "NAVY TRIBLEND": "Navy Triblend",
            "OATMEAL TRIBLEND": "Oatmeal Triblend",
            "OLIVE TRIBLEND": "Olive Triblend",
            "ORANGE TRIBLEND": "Orange Triblend",
            "Orchid Triblend": "Orchid Triblend",
            "PEACH TRIBLEND": "Peach Triblend",
            "PURPLE TRIBLEND": "Purple Triblend",
            "Pale Yellow Triblend": "Pale Yellow Triblend",
            "Pink Triblend": "Pink Triblend",
            "RED TRIBLEND": "Red Triblend",
            "SD DARK GRY TRBL": "Solid Dark Grey Triblend",
            "SEA GREEN TRBLND": "Sea Green Triblend",
            "SLD BLK TRIBLEND": "Solid Black Triblend",
            "SOLID NVY TRBLND": "Solid Navy Triblend",
            "SOLID RED TRIBLN": "Solid Red Triblend",
            "SOLID WHT TRBLND": "Solid White Triblend",
            "STEEL BLU TRBLND": "Steel Blue Triblend",
            "Solid Asphalt Triblend": "Solid Asphalt Triblend",
            "Solid Blue Triblend": "Solid Blue Triblend",
            "Solid Carolina Blue Triblend": "Solid Carolina Blue Triblend",
            "Solid Forest Triblend": "Solid Forest Triblend",
            "Solid Kelly Triblend": "Solid Kelly Triblend",
            "Solid Maroon Triblend": "Solid Maroon Triblend",
            "Solid Natural Triblend": "Solid Natural Triblend",
            "Solid Orange Triblend": "Solid Orange Triblend",
            "Solid Silver Triblend": "Solid Silver Triblend",
            "Solid Slate Triblend": "Solid Slate Triblend",
            "Solid Team Purple Triblend": "Solid Team Purple Triblend",
            "Solid True Royal Triblend": "Solid True Royal Triblend",
            "Storm Triblend": "Storm Triblend",
            "Sunset Triblend": "Sunset Triblend",
            "TEAL TRIBLEND": "Teal Triblend",
            "TRUE ROYAL TRBLN": "True Royal Triblend",
            "Tan Triblend": "Tan Triblend",
            "WHITE FLECK TRIBLD": "White Fleck Triblend",
            "YLLW GLD TRBLND": "Yellow Gold Triblend",
        }

        for crm_color, sanmar_color in cases.items():
            with self.subTest(crm_color=crm_color):
                self.assertIn(
                    sanmar_color,
                    crm_shipping_bypasser._sanmar_color_label_options(crm_color, product=product),
                )
                self.assertTrue(
                    crm_shipping_bypasser._cart_color_matches(
                        sanmar_color,
                        crm_color,
                        product=product,
                    )
                )

        unsupported = (
            "Espresso Triblend",
            "Sand Dune Triblend",
            "Solid Gold Triblend",
            "Spring Green Triblend",
        )
        for crm_color in unsupported:
            with self.subTest(crm_color=crm_color):
                self.assertEqual(
                    crm_shipping_bypasser._sanmar_color_alias_labels(crm_color, product=product),
                    [],
                )

        false_matches = (
            ("Green Triblend", "SEA GREEN TRBLND"),
            ("Blue Storm Triblend", "Storm Triblend"),
            ("Oatmeal Triblend", "TEAL TRIBLEND"),
        )
        for sanmar_color, crm_color in false_matches:
            with self.subTest(sanmar_color=sanmar_color, crm_color=crm_color):
                self.assertFalse(
                    crm_shipping_bypasser._cart_color_matches(
                        sanmar_color,
                        crm_color,
                        product=product,
                    )
                )

    def test_bella_canvas_heather_columbia_blue_abbreviation_matches_sanmar(self):
        product = {
            "product_id": "100B",
            "product_name": "Bella + Canvas Jersey Baby Short-Sleeve Onesie",
        }
        aliases = crm_shipping_bypasser._sanmar_color_alias_labels("HTHR COLUM BLUE", product=product)

        self.assertIn("Heather Columbia Blue", aliases)
        self.assertTrue(
            crm_shipping_bypasser._cart_color_matches(
                "Heather Columbia Blue",
                "HTHR COLUM BLUE",
                product=product,
            )
        )

    def test_cart_validation_matches_pc54_safety_color_aliases(self):
        product_lines = [
            {
                "product": {"index": 1, "product_id": "PC54", "color": "Safety Orange"},
                "search_id": "PC54",
                "expected_style_keys": ["PC54"],
                "quantities": {"M": 2},
                "warehouse": "Phoenix, AZ",
            },
            {
                "product": {"index": 2, "product_id": "PC54", "color": "Safety Green"},
                "search_id": "PC54",
                "expected_style_keys": ["PC54"],
                "quantities": {"L": 3},
                "warehouse": "Phoenix, AZ",
            },
        ]
        cart_lines = [
            {"style": "PC54", "color": "S. Orange", "size": "M", "quantity": 2, "warehouse": "Phoenix, AZ"},
            {"style": "PC54", "color": "S. Green", "size": "L", "quantity": 3, "warehouse": "Phoenix, AZ"},
        ]

        with mock.patch.object(crm_shipping_bypasser, "_read_sanmar_cart_lines", return_value=cart_lines):
            result = crm_shipping_bypasser._validate_sanmar_cart_contents(
                mock.Mock(),
                product_lines,
                warehouse="Phoenix, AZ",
            )

        self.assertTrue(result["success"], result.get("issues"))

    def test_cart_validation_matches_4528_true_navy_to_j_navy(self):
        product_lines = [
            {
                "product": {"index": 1, "product_id": "4528", "color": "TRUE NAVY"},
                "search_id": "4528",
                "expected_style_keys": ["4528"],
                "quantities": {"L": 2},
            },
        ]
        cart_lines = [
            {"style": "4528", "color": "J. Navy", "size": "L", "quantity": 2, "warehouse": "Robbinsville, NJ"},
        ]

        with mock.patch.object(crm_shipping_bypasser, "_read_sanmar_cart_lines", return_value=cart_lines):
            result = crm_shipping_bypasser._validate_sanmar_cart_contents(
                mock.Mock(),
                product_lines,
                warehouse="Robbinsville, NJ",
            )

        self.assertTrue(result["success"], result.get("issues"))

    def test_single_warehouse_failure_message_names_unavailable_sizes(self):
        message = crm_shipping_bypasser._single_warehouse_failure_message(
            [
                {
                    "search_id": "5000",
                    "quantities": {"S": 1},
                    "inventory": [
                        {"warehouse": "Phoenix, AZ", "stock": {"S": 0}},
                        {"warehouse": "Reno, NV", "stock": {"S": 0}},
                    ],
                },
                {
                    "search_id": "5000B",
                    "quantities": {"M": 2},
                    "inventory": [
                        {"warehouse": "Phoenix, AZ", "stock": {"M": 0}},
                        {"warehouse": "Reno, NV", "stock": {"M": 0}},
                    ],
                },
            ],
            "mach6",
        )

        self.assertIn("5000 S needs 1, max available 0", message)
        self.assertIn("5000B M needs 2, max available 0", message)

    def test_shipping_bypasser_common_warehouse_requires_stock_buffer(self):
        product_lines = [
            {
                "quantities": {"XL": 5},
                "inventory": [
                    {"warehouse": "Robbinsville, NJ", "stock": {"XL": 14}},
                    {"warehouse": "Richmond, VA", "stock": {"XL": 15}},
                ],
            }
        ]

        warehouse = crm_shipping_bypasser._choose_common_warehouse(product_lines, "inhouse")

        self.assertEqual(warehouse, "Richmond, VA")

    def test_shipping_bypasser_multi_warehouse_allocates_only_above_buffer(self):
        product_lines = [
            {
                "product": {"index": 1, "product_id": "5000"},
                "quantities": {"XL": 5},
                "inventory": [
                    {"warehouse": "Robbinsville, NJ", "stock": {"XL": 14}},
                    {"warehouse": "Richmond, VA", "stock": {"XL": 11}},
                ],
            }
        ]

        plan = crm_shipping_bypasser._choose_multi_warehouse_plan(product_lines, "inhouse")

        self.assertEqual(plan["mode"], "multi_warehouse")
        self.assertEqual(plan["warehouses"], ["Robbinsville, NJ", "Richmond, VA"])
        self.assertEqual(plan["pieces_by_warehouse"], {"Robbinsville, NJ": 4, "Richmond, VA": 1})
        self.assertEqual([line["quantities"] for line in plan["expanded_lines"]], [{"XL": 4}, {"XL": 1}])

    def test_shipping_bypasser_warehouse_plan_prefers_single_complete_warehouse(self):
        product_lines = [
            {
                "product": {"index": 1, "product_id": "5000"},
                "quantities": {"S": 2, "M": 2},
                "inventory": [
                    {"warehouse": "Robbinsville, NJ", "stock": {"S": 20, "M": 0}},
                    {"warehouse": "Richmond, VA", "stock": {"S": 20, "M": 20}},
                ],
            }
        ]

        warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(product_lines, "inhouse")

        self.assertEqual(warehouse, "Richmond, VA")
        self.assertEqual(plan["mode"], "single_warehouse")
        self.assertEqual(plan["warehouses"], ["Richmond, VA"])
        self.assertEqual([line["warehouse"] for line in plan["expanded_lines"]], ["Richmond, VA"])

    def test_shipping_bypasser_single_warehouse_plan_supplies_shipping_warehouse(self):
        warehouse = crm_shipping_bypasser._single_warehouse_from_plan(
            None,
            {
                "mode": "single_warehouse",
                "warehouses": ["Robbinsville, NJ"],
                "expanded_lines": [],
            },
        )

        self.assertEqual(warehouse, "Robbinsville, NJ")

    def test_failed_order_cleanup_attaches_cleared_cart_message(self):
        report = [
            crm_shipping_bypasser._result(
                "4600001",
                False,
                "eta_on_or_after_due_date",
                "SanMar ETA is too late.",
            )
        ]
        cleanup = {
            "attempted": True,
            "success": True,
            "message": "SanMar cart was cleared after failed order 4600001.",
        }

        with mock.patch.object(crm_shipping_bypasser, "_clear_sanmar_cart", return_value=cleanup) as clear_cart:
            ok = crm_shipping_bypasser._cleanup_after_failed_order(mock.Mock(), "4600001", report)

        self.assertTrue(ok)
        clear_cart.assert_called_once()
        self.assertEqual(report[0]["sanmar_cart_cleanup"], cleanup)
        self.assertIn("SanMar cart was cleared", report[0]["message"])

    def test_preexisting_sanmar_cart_stop_does_not_clear_cart(self):
        report = [
            crm_shipping_bypasser._result(
                "4600001",
                False,
                "sanmar_cart_not_empty",
                "SanMar shopping box already has items.",
                stop_run=True,
            )
        ]

        with mock.patch.object(crm_shipping_bypasser, "_clear_sanmar_cart") as clear_cart:
            ok = crm_shipping_bypasser._cleanup_after_failed_order(mock.Mock(), "4600001", report)

        self.assertTrue(ok)
        clear_cart.assert_not_called()
        self.assertNotIn("sanmar_cart_cleanup", report[0])

    def test_failed_cart_cleanup_stops_batch_before_next_order(self):
        report = [
            crm_shipping_bypasser._result(
                "4600001",
                False,
                "checkout_warehouse_mismatch",
                "Warehouse mismatch.",
            )
        ]
        cleanup = {
            "attempted": True,
            "success": False,
            "message": "SanMar cart could not be cleared after failed order 4600001.",
        }

        with mock.patch.object(crm_shipping_bypasser, "_clear_sanmar_cart", return_value=cleanup):
            ok = crm_shipping_bypasser._cleanup_after_failed_order(mock.Mock(), "4600001", report)

        self.assertFalse(ok)
        self.assertEqual(report[-1]["outcome"], "sanmar_cart_cleanup_failed")
        self.assertTrue(report[-1]["stop_run"])

    def test_shipping_bypasser_failed_order_ids_dedupes_cleanup_rows(self):
        payload = {
            "success": False,
            "order_ids": ["4600001", "4600002"],
            "report": [
                {"order_id": "4600001", "success": False, "outcome": "checkout_warehouse_mismatch"},
                {"order_id": "4600001", "success": False, "outcome": "sanmar_cart_cleanup_failed"},
                {"order_id": "4600002", "success": True, "outcome": "shipping_bypass_ordered"},
            ],
        }

        self.assertEqual(server._shipping_bypasser_failed_order_ids(payload), ["4600001"])

    def test_shipping_bypasser_summary_names_failed_orders(self):
        report = [
            {"order_id": "4600001", "success": False, "outcome": "checkout_warehouse_mismatch"},
            {"order_id": "4600002", "success": True, "outcome": "shipping_bypass_ordered"},
        ]

        message = crm_shipping_bypasser._summary_message(report, refresh_passes=1, order_count=2)

        self.assertIn("1 order(s) need attention: 4600001.", message)

    def test_shipping_bypasser_partial_summary_stays_compact(self):
        report = [
            {
                "order_id": "4646449",
                "success": False,
                "outcome": "sanmar_product_not_found",
                "message": "Tab 1 skipped.",
                "stock_tab_index": 1,
                "stock_tab_count": 2,
            },
            {
                "order_id": "4646449",
                "success": True,
                "outcome": "shipping_bypass_ordered",
                "message": "Tab 2 ordered.",
                "stock_tab_index": 2,
                "stock_tab_count": 2,
                "stock_tab_label": "H-Example124 2 - QTY: 1",
                "order": {"po": "H-Example124"},
                "sanmar_confirmation": {
                    "po": "H-Example124",
                    "url": "https://www.sanmar.com/checkout/submission?orderCode=123",
                },
            },
        ]

        message = crm_shipping_bypasser._summary_message(report, refresh_passes=2, order_count=1)

        self.assertEqual(
            message,
            "Shipping Bypasser processed 1 order(s) and 2 stock tab(s) across 2 CRM list refresh pass(es). "
            "0 order(s) succeeded. 1 order(s) partially succeeded.",
        )
        self.assertNotIn("customer PO H-Example124", message)
        self.assertNotIn("https://www.sanmar.com/checkout/submission?orderCode=123", message)

    def test_shipping_bypasser_history_preserves_multiple_tab_confirmations(self):
        payload = {
            "success": True,
            "order_ids": ["4646449"],
            "report": [
                {
                    "order_id": "4646449",
                    "success": True,
                    "outcome": "shipping_bypass_ordered",
                    "message": "Tab 1 ordered.",
                    "stock_tab_index": 1,
                    "stock_tab_count": 2,
                    "stock_tab_label": "FirstTab 1 - QTY: 4",
                    "sanmar_confirmation": {"po": "FirstTab", "url": "https://sanmar.example/1"},
                },
                {
                    "order_id": "4646449",
                    "success": True,
                    "outcome": "shipping_bypass_ordered",
                    "message": "Tab 2 ordered.",
                    "stock_tab_index": 2,
                    "stock_tab_count": 2,
                    "stock_tab_label": "SecondTab 2 - QTY: 8",
                    "sanmar_confirmation": {"po": "SecondTab", "url": "https://sanmar.example/2"},
                },
            ],
        }

        rows = server._build_crm_shipping_bypasser_order_results(payload)

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["stock_tab_index"] for row in rows], [1, 2])
        self.assertEqual([row["sanmar_confirmation"]["po"] for row in rows], ["FirstTab", "SecondTab"])

    def test_stock_history_normalization_preserves_shipping_bypasser_tab_rows(self):
        rows = server._normalize_crm_stock_order_results(
            [
                {
                    "order_id": "4646449",
                    "success": True,
                    "status": "Already stock ordered",
                    "outcome": "already_stock_ordered",
                    "message": "Tab 1 skipped.",
                    "stock_tab_index": 1,
                    "stock_tab_count": 2,
                    "stock_tab_label": "FirstTab 1 - QTY: 4",
                },
                {
                    "order_id": "4646449",
                    "success": True,
                    "status": "Bypassed",
                    "outcome": "shipping_bypass_ordered",
                    "message": "Tab 2 ordered.",
                    "stock_tab_index": 2,
                    "stock_tab_count": 2,
                    "stock_tab_label": "SecondTab 2 - QTY: 8",
                    "sanmar_confirmation": {"po": "SecondTab", "url": "https://sanmar.example/2"},
                    "eta_by_warehouse": {"Richmond, VA": "2026-06-16"},
                },
            ],
            ["4646449"],
            True,
            "ok",
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["stock_tab_index"] for row in rows], [1, 2])
        self.assertEqual(rows[1]["sanmar_confirmation"]["po"], "SecondTab")
        self.assertEqual(rows[1]["eta_by_warehouse"], {"Richmond, VA": "2026-06-16"})

    def test_shipping_bypasser_history_tab_summary_names_skipped_and_ordered_tabs(self):
        rows = server._build_crm_shipping_bypasser_order_results(
            {
                "success": True,
                "order_ids": ["4677955"],
                "report": [
                    {
                        "order_id": "4677955",
                        "success": True,
                        "outcome": "already_stock_ordered",
                        "message": "Skipped because stock is already ordered for this tab.",
                        "stock_tab_index": 1,
                        "stock_tab_count": 2,
                        "stock_tab_label": "H-AlexHorn364 1 - QTY: 3",
                    },
                    {
                        "order_id": "4677955",
                        "success": True,
                        "outcome": "shipping_bypass_ordered",
                        "message": "Tab 2 ordered.",
                        "stock_tab_index": 2,
                        "stock_tab_count": 2,
                        "stock_tab_label": "H-AlexHorn366 2 - QTY: 4",
                        "sanmar_confirmation": {
                            "po": "H-AlexHorn366",
                            "url": "https://www.sanmar.com/checkout/submission?orderCode=90223234",
                        },
                    },
                ],
            }
        )

        summary = server._crm_shipping_bypasser_history_tab_summary(rows)

        self.assertIn("4677955", summary)
        self.assertIn("tab 1 of 2", summary)
        self.assertIn("skipped because stock is already ordered", summary)
        self.assertIn("tab 2 of 2", summary)
        self.assertIn("customer PO H-AlexHorn366", summary)
        self.assertIn("https://www.sanmar.com/checkout/submission?orderCode=90223234", summary)

    def test_shipping_bypasser_history_tab_summary_includes_recovered_already_ordered_confirmation(self):
        rows = server._build_crm_shipping_bypasser_order_results(
            {
                "success": True,
                "order_ids": ["4809107"],
                "report": [
                    {
                        "order_id": "4809107",
                        "success": True,
                        "outcome": "already_stock_ordered",
                        "message": "Skipped because stock is already ordered for this tab.",
                        "stock_tab_index": 1,
                        "stock_tab_count": 2,
                        "stock_tab_label": "H-DouglasROM156 1 - QTY: 1",
                        "sanmar_confirmation": {
                            "po": "H-DouglasROM156",
                            "url": "https://www.sanmar.com/checkout/submission?orderCode=91124708",
                        },
                    },
                ],
            }
        )

        summary = server._crm_shipping_bypasser_history_tab_summary(rows)

        self.assertIn("customer PO H-DouglasROM156", summary)
        self.assertIn("https://www.sanmar.com/checkout/submission?orderCode=91124708", summary)

    def test_shipping_bypasser_historical_confirmation_recovers_failed_submission_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "crm_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "run_history": [
                            {
                                "automation_key": "shipping_bypasser",
                                "dry_run": False,
                                "order_results": [
                                    {
                                        "order_id": "4809107",
                                        "success": False,
                                        "outcome": "pending_sanmar_submitted_crm_record_failed",
                                        "sanmar_confirmation": {
                                            "po": "H-DouglasROM156",
                                            "url": "https://www.sanmar.com/checkout/submission?orderCode=91124708",
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            confirmation = crm_shipping_bypasser._historical_shipping_bypass_confirmation(
                "H-DouglasROM156",
                state_path=str(state_path),
            )

        self.assertEqual(
            confirmation["url"],
            "https://www.sanmar.com/checkout/submission?orderCode=91124708",
        )

    def test_shipping_bypasser_history_message_stays_compact_for_ui(self):
        verbose = (
            "Shipping Bypasser processed 4 order(s) and 8 stock tab(s) across 2 CRM list refresh pass(es). "
            "3 order(s) succeeded. 1 order(s) partially succeeded: 4690028 "
            "(successful stock tab(s): tab 1 of 3 (H-PHYLLISKIN1687, SanMar confirmation "
            "https://www.sanmar.com/checkout/submission?orderCode=90284964); tab 2 of 3 "
            "(H-PHYLLISKIN1688, SanMar confirmation https://www.sanmar.com/checkout/submission?orderCode=90284945)). "
            "Stock tabs: 4695571: tab 1 of 3 skipped because stock is already ordered; "
            "tab 2 of 3 ordered stock, customer PO H-marcoinver602."
        )

        self.assertEqual(
            server._compact_crm_shipping_bypasser_history_message(verbose),
            "Shipping Bypasser processed 4 order(s) and 8 stock tab(s) across 2 CRM list refresh pass(es). "
            "3 order(s) succeeded. 1 order(s) partially succeeded.",
        )

    def test_shipping_bypasser_partial_order_counts_as_successful_history(self):
        payload = {
            "success": False,
            "order_ids": ["4646449"],
            "report": [
                {
                    "order_id": "4646449",
                    "success": True,
                    "outcome": "shipping_bypass_ordered",
                    "stock_tab_index": 1,
                    "stock_tab_count": 2,
                    "stock_tab_label": "FirstTab 1 - QTY: 4",
                    "order": {"po": "FirstTab"},
                    "sanmar_confirmation": {"po": "FirstTab", "url": "https://sanmar.example/1"},
                },
                {
                    "order_id": "4646449",
                    "success": False,
                    "outcome": "no_single_warehouse",
                    "message": "Tab 2 skipped.",
                    "stock_tab_index": 2,
                    "stock_tab_count": 2,
                    "manual_review_required": True,
                },
            ],
        }

        rows = server._build_crm_shipping_bypasser_order_results(payload)

        self.assertTrue(crm_shipping_bypasser._report_orders_succeeded_or_partially_succeeded(payload["report"]))
        self.assertTrue(server._crm_shipping_bypasser_order_results_success(rows))
        self.assertTrue(all(row["success"] for row in rows))
        self.assertEqual(rows[1]["status"], "Partially successful")
        self.assertEqual(rows[1]["sanmar_confirmation"]["po"], "FirstTab")
        self.assertIn("tab 1 of 2", rows[1]["message"])
        self.assertIn("https://sanmar.example/1", rows[1]["message"])

    def test_shipping_bypasser_partial_requires_confirmed_success_details(self):
        report = [
            {
                "order_id": "4646449",
                "success": True,
                "outcome": "shipping_bypass_ordered",
                "stock_tab_index": 1,
                "stock_tab_count": 2,
                "order": {"po": "MissingConfirmation"},
            },
            {
                "order_id": "4646449",
                "success": False,
                "outcome": "no_single_warehouse",
                "message": "Tab 2 skipped.",
                "stock_tab_index": 2,
                "stock_tab_count": 2,
            },
        ]

        message = crm_shipping_bypasser._summary_message(report, refresh_passes=1, order_count=1)

        self.assertFalse(crm_shipping_bypasser._report_orders_succeeded_or_partially_succeeded(report))
        self.assertNotIn("partially succeeded", message)
        self.assertIn("1 order(s) need attention: 4646449.", message)

    def test_shipping_bypasser_runtime_config_allows_multi_tab_list(self):
        url = "https://crm.example/report?salesNotes=Shipping+is+too+expensive&tabs%5Blow%5D=&tabs%5Bhigh%5D="

        self.assertEqual(crm_shipping_bypasser._validate_runtime_config(url), url)

    def test_shipping_bypasser_po_falls_back_to_tab_name_prefix(self):
        self.assertEqual(
            crm_shipping_bypasser._po_from_tab_label("ChrisWilliam679 1 - QTY: 10 Design Previews"),
            "ChrisWilliam679",
        )
        self.assertEqual(
            crm_shipping_bypasser._po_from_tab_label("H-Example123 2 - QTY: 4 Design Previews"),
            "H-Example123",
        )
        self.assertEqual(
            crm_shipping_bypasser._po_from_tab_label("H-Example124 3 - QTY: 1 View Proofs"),
            "H-Example124",
        )

    def test_shipping_bypasser_stock_tab_summary_accepts_view_proofs_label(self):
        self.assertEqual(
            crm_shipping_bypasser._stock_tab_summary_label("H-Example124 3 - QTY: 1 View Proofs"),
            "H-Example124 3 - QTY: 1",
        )

    def test_shipping_bypasser_history_only_skips_successful_orders(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            state_path = handle.name
            json.dump(
                {
                    "run_history": [
                        {
                            "automation_key": "shipping_bypasser",
                            "success": False,
                            "order_ids": ["4622786", "4622599"],
                            "order_results": [
                                {"order_id": "4622786", "success": False, "outcome": "sanmar_cart_mismatch"},
                                {"order_id": "4622599", "success": False, "outcome": "sanmar_cart_not_empty"},
                                {"order_id": "4623000", "success": True, "outcome": "shipping_bypass_ordered"},
                            ],
                            "dry_run": False,
                        },
                        {
                            "automation_key": "shipping_bypasser",
                            "success": True,
                            "order_ids": ["4624000"],
                            "order_results": [],
                            "dry_run": False,
                        },
                        {
                            "automation_key": "shipping_bypasser",
                            "success": True,
                            "order_ids": ["4625000"],
                            "order_results": [
                                {"order_id": "4625000", "success": True, "outcome": "shipping_bypass_ready"},
                            ],
                            "dry_run": True,
                        },
                    ]
                },
                handle,
            )
        try:
            skipped = crm_shipping_bypasser._load_historical_shipping_bypass_order_ids(state_path)
        finally:
            Path(state_path).unlink(missing_ok=True)

        self.assertEqual(skipped, {"4623000", "4624000"})


class CrmAutoSplitterTests(unittest.TestCase):
    @mock.patch.object(crm_auto_splitter, "build_chrome_driver")
    def test_splitter_driver_uses_five_minute_script_timeout(self, mock_build_driver):
        crm_auto_splitter._build_splitter_driver("splitter-profile", visible=False)

        self.assertEqual(
            mock_build_driver.call_args.kwargs["script_timeout"],
            5 * 60,
        )

    def test_original_scan_refreshes_once_when_tabs_are_initially_missing(self):
        driver = mock.Mock()
        driver.execute_script.return_value = ""
        design = {"tab_number": 1, "design_name": "Recovered", "quantity": 1, "stock": {}}
        with mock.patch.object(crm_auto_splitter.time, "monotonic", side_effect=[0, 100, 100, 200]), \
             mock.patch.object(crm_auto_splitter, "_visible_design_tab_numbers", return_value=[]), \
             mock.patch.object(crm_auto_splitter, "_design_total_tab_numbers_from_page_text", side_effect=[[], [1]]), \
             mock.patch.object(crm_auto_splitter, "_wait_for_crm_context"), \
             mock.patch.object(crm_auto_splitter, "_click_design_tab", return_value=True), \
             mock.patch.object(crm_auto_splitter, "_scan_current_design_detail", return_value=design), \
             mock.patch.object(crm_auto_splitter, "_extract_order_totals_from_text", return_value={}), \
             mock.patch.object(crm_auto_splitter, "_summarize_original_stock", return_value={}), \
             mock.patch.object(crm_auto_splitter, "_subcontractor_from_page_text", return_value=""), \
             mock.patch.object(crm_auto_splitter._product_separator, "_order_stock_status_from_text", return_value={}):
            result = crm_auto_splitter._scan_original_order(driver, expected_tab_count=1)

        driver.refresh.assert_called_once_with()
        self.assertEqual(result["detected_tab_count"], 1)
        self.assertEqual(result["designs"][0]["design_name"], "Recovered")

    def test_payment_detection_uses_explicit_paid_amount(self):
        self.assertTrue(crm_auto_splitter._payment_is_detected({"amount_paid": "125.00", "transactions": []}))
        self.assertFalse(
            crm_auto_splitter._payment_is_detected(
                {
                    "amount_paid": "0.00",
                    "transactions": [{"amount": "125.00", "tag": "Stripe.com"}],
                }
            )
        )

    def test_payment_detection_falls_back_to_positive_non_refund_transaction(self):
        self.assertTrue(
            crm_auto_splitter._payment_is_detected(
                {"amount_paid": None, "transactions": [{"amount": "125.00", "tag": "PayPal"}]}
            )
        )
        self.assertFalse(
            crm_auto_splitter._payment_is_detected(
                {"amount_paid": None, "transactions": [{"amount": "-125.00", "tag": "Refund"}]}
            )
        )

    def test_split_quote_finalization_routes_by_transaction_presence(self):
        driver = mock.Mock()
        with mock.patch.object(crm_auto_splitter, "_record_split_payment_and_wait_for_order", return_value="paid") as paid, \
             mock.patch.object(crm_auto_splitter, "_convert_unpaid_split_quote_and_wait_for_order", return_value="unpaid") as unpaid:
            self.assertEqual(
                crm_auto_splitter._finalize_split_quote_and_wait_for_order(driver, "Stripe.com", "pi_test"),
                "paid",
            )
            self.assertEqual(
                crm_auto_splitter._finalize_split_quote_and_wait_for_order(driver, "", ""),
                "unpaid",
            )

        paid.assert_called_once_with(driver, "Stripe Manual CC Entry", "pi_test")
        unpaid.assert_called_once_with(driver)

    def test_unpaid_quote_conversion_never_records_a_transaction(self):
        driver = mock.Mock()
        conversion = {"started": True, "action": "produceWithoutPayment", "source": "quote_scope"}
        with mock.patch.object(crm_auto_splitter, "_quote_scope", return_value=conversion) as quote_scope, \
             mock.patch.object(
                 crm_auto_splitter,
                 "_find_modal_text",
                 return_value="Warning Do you want to create an order without a Payment?",
             ), \
             mock.patch.object(crm_auto_splitter, "_click_modal_choice", return_value=True) as confirm, \
             mock.patch.object(crm_auto_splitter, "_wait_for_new_split_order", return_value="4882000") as wait_order, \
             mock.patch.object(crm_auto_splitter, "_open_record_transaction") as open_transaction, \
             mock.patch.object(crm_auto_splitter.time, "sleep"):
            result = crm_auto_splitter._convert_unpaid_split_quote_and_wait_for_order(driver)

        self.assertEqual(result, "4882000")
        self.assertIn("producewithoutpayment", quote_scope.call_args.args[1].lower())
        self.assertNotIn("recordtransaction", quote_scope.call_args.args[1].lower())
        confirm.assert_called_once_with(driver, "yes")
        wait_order.assert_called_once_with(driver, "Unpaid split quote conversion was started")
        open_transaction.assert_not_called()

    def test_unpaid_original_finalization_skips_refund_actions(self):
        driver = mock.Mock()
        with mock.patch.object(crm_auto_splitter, "_add_refund_fee_to_original") as add_refund_fee, \
             mock.patch.object(crm_auto_splitter, "_cancel_original_order") as cancel_order, \
             mock.patch.object(crm_auto_splitter, "_open_record_transaction") as open_transaction, \
             mock.patch.object(crm_auto_splitter, "_save_transaction_modal_with_amount") as save_transaction, \
             mock.patch.object(crm_auto_splitter, "_add_original_transfer_note") as add_note, \
             mock.patch.object(
                 crm_auto_splitter,
                 "_read_order_totals",
                 return_value={"grand_total": "125.00", "paid": "0.00", "balance_due": "125.00"},
             ):
            result = crm_auto_splitter._finalize_original_order_after_split(
                driver,
                False,
                crm_auto_splitter.Decimal("125.00"),
                crm_auto_splitter.Decimal("125.00"),
                "transferred to 4882000, 4882001",
            )

        cancel_order.assert_called_once_with(driver)
        add_note.assert_called_once_with(driver, "transferred to 4882000, 4882001")
        add_refund_fee.assert_not_called()
        open_transaction.assert_not_called()
        save_transaction.assert_not_called()
        self.assertTrue(result["payment_actions_skipped"])
        self.assertEqual(result["refund_fee_amount"], "0.00")

    def test_paid_original_finalization_keeps_existing_refund_actions(self):
        driver = mock.Mock()
        totals = [
            {"grand_total": "0.00", "paid": "125.00", "balance_due": "-125.00"},
            {"grand_total": "0.00", "paid": "0.00", "balance_due": "0.00"},
        ]
        with mock.patch.object(crm_auto_splitter, "_add_refund_fee_to_original") as add_refund_fee, \
             mock.patch.object(crm_auto_splitter, "_cancel_original_order") as cancel_order, \
             mock.patch.object(crm_auto_splitter, "_open_record_transaction") as open_transaction, \
             mock.patch.object(crm_auto_splitter, "_save_transaction_modal_with_amount") as save_transaction, \
             mock.patch.object(crm_auto_splitter, "_add_original_transfer_note") as add_note, \
             mock.patch.object(crm_auto_splitter, "_read_order_totals", side_effect=totals), \
             mock.patch.object(crm_auto_splitter.time, "sleep"):
            result = crm_auto_splitter._finalize_original_order_after_split(
                driver,
                True,
                crm_auto_splitter.Decimal("125.00"),
                crm_auto_splitter.Decimal("125.00"),
                "transferred to 4882000, 4882001",
            )

        add_refund_fee.assert_called_once_with(driver, crm_auto_splitter.Decimal("125.00"))
        cancel_order.assert_called_once_with(driver)
        open_transaction.assert_called_once_with(driver, quote=False)
        save_transaction.assert_called_once_with(
            driver,
            "Refund",
            "transferred to 4882000, 4882001",
            amount=crm_auto_splitter.Decimal("-125.00"),
        )
        add_note.assert_called_once_with(driver, "transferred to 4882000, 4882001")
        self.assertFalse(result["payment_actions_skipped"])
        self.assertEqual(result["refund_fee_amount"], "125.00")

    def test_cancel_confirmation_accepts_crm_cancel_order_status(self):
        self.assertTrue(crm_auto_splitter._is_cancel_order_status("Cancel Order"))
        self.assertTrue(crm_auto_splitter._is_cancel_order_status("Cancelled"))
        self.assertFalse(crm_auto_splitter._is_cancel_order_status("Stock Auto Ordering Queued"))

    def test_cancel_confirmation_accepts_status_history_cancel_order(self):
        body = "Status History and Art Changes Status History 05/17 Api Scripts Cancel Order 05/17 Auto Ordering Ordered Stock"

        self.assertTrue(crm_auto_splitter._status_history_confirms_cancel_order(body))

    def test_refund_fee_amount_match_uses_absolute_amount(self):
        self.assertTrue(crm_auto_splitter._money_amount_matches("-$445.11", "445.11"))
        self.assertFalse(crm_auto_splitter._money_amount_matches("-$445.10", "445.11"))

    def test_resume_can_use_split_total_after_original_refund_fee_zeroes_total(self):
        split_total = crm_auto_splitter.Decimal("445.11")
        original_grand_total = crm_auto_splitter.Decimal("0.00")
        resume_existing_order_ids = ["4536164", "4536167"]

        if original_grand_total == crm_auto_splitter.Decimal("0.00") and resume_existing_order_ids and split_total > crm_auto_splitter.Decimal("0.00"):
            original_grand_total = split_total.quantize(crm_auto_splitter.Decimal("0.01"))

        self.assertEqual(original_grand_total, crm_auto_splitter.Decimal("445.11"))

    def test_auto_divisions_use_fewest_orders_with_ten_tab_limit(self):
        cases = {
            12: (2, [6, 6]),
            19: (2, [10, 9]),
            20: (2, [10, 10]),
            21: (3, [7, 7, 7]),
            31: (4, [8, 8, 8, 7]),
        }

        for tab_count, expected in cases.items():
            divisions, sizes = expected
            with self.subTest(tab_count=tab_count):
                self.assertEqual(crm_auto_splitter._auto_divisions_for_tab_count(tab_count), divisions)
                ranges = crm_auto_splitter._split_ranges(tab_count, divisions)
                self.assertEqual([item["tab_count"] for item in ranges], sizes)
                self.assertTrue(crm_auto_splitter._validate_split_ranges_within_limit(ranges))

    def test_auto_divisions_reject_orders_at_or_under_limit(self):
        with self.assertRaises(crm_auto_splitter.SplitterError):
            crm_auto_splitter._auto_divisions_for_tab_count(10)

    def test_header_only_stock_ordered_does_not_require_stock_routing_review(self):
        stock_summary = crm_auto_splitter._summarize_original_stock(
            [
                {
                    "tab_number": 1,
                    "design_id": "13000001",
                    "design_name": "H-Test001",
                    "stock": {
                        "state": "ordered_header_only",
                        "stock_status_ordered": True,
                        "has_po_row": False,
                        "manual_order_vendor": "",
                        "manual_order_po": "",
                        "manual_order_rows": [],
                    },
                }
            ]
        )

        routing = crm_auto_splitter._planned_stock_routing(stock_summary, subcontractor="")

        self.assertEqual(stock_summary["unknown_ordered_tabs"], [])
        self.assertEqual([item["tab_number"] for item in stock_summary["header_only_ordered_tabs"]], [1])
        self.assertEqual(routing["action"], "header_only_no_transfer")
        self.assertEqual(routing["reason"], "stock_ordered_header_only")

    def test_manual_order_rows_parse_sanmar_bulk_vendor_label(self):
        rows = crm_product_separator._manual_order_rows_from_text(
            "PO Vendor Order # Order Date\nSanmar (Bulk) H-AmyeRushin761-WJ01 62552 07/06/2026"
        )

        self.assertEqual(rows[0]["vendor"], "Sanmar")
        self.assertEqual(rows[0]["po"], "H-AmyeRushin761-WJ01")
        self.assertEqual(rows[0]["vendor_order_number"], "62552")

    def test_incomplete_manual_order_stock_still_requires_stock_routing_review(self):
        stock_summary = crm_auto_splitter._summarize_original_stock(
            [
                {
                    "tab_number": 1,
                    "design_id": "13000001",
                    "design_name": "H-Test001",
                    "stock": {
                        "state": "ordered_po_only",
                        "stock_status_ordered": False,
                        "has_po_row": True,
                        "manual_order_vendor": "S&S Activewear",
                        "manual_order_po": "",
                        "manual_order_rows": [],
                    },
                }
            ]
        )

        routing = crm_auto_splitter._planned_stock_routing(stock_summary, subcontractor="")

        self.assertEqual(stock_summary["header_only_ordered_tabs"], [])
        self.assertEqual([item["tab_number"] for item in stock_summary["unknown_ordered_tabs"]], [1])
        self.assertEqual(routing["action"], "manual_review")
        self.assertEqual(routing["reason"], "stock_ordered_vendor_po_unknown")

    def test_scan_original_order_can_infer_tabs_from_total_markers(self):
        driver = mock.Mock()
        driver.execute_script.return_value = ""
        designs = [
            {"tab_number": index, "design_id": str(1000 + index), "design_name": f"Design {index}"}
            for index in range(1, 13)
        ]

        with mock.patch.object(crm_auto_splitter, "_visible_design_tab_numbers", return_value=[]), \
             mock.patch.object(crm_auto_splitter, "_design_total_tab_numbers_from_page_text", return_value=list(range(1, 13))), \
             mock.patch.object(crm_auto_splitter, "_click_design_tab", return_value=True), \
             mock.patch.object(crm_auto_splitter, "_scan_current_design_detail", side_effect=designs):
            scan = crm_auto_splitter._scan_original_order(driver)

        self.assertEqual(scan["detected_tab_count"], 12)
        self.assertEqual([design["tab_number"] for design in scan["designs"]], list(range(1, 13)))

    def test_extract_order_totals_reads_promo_amount_and_code(self):
        totals = crm_auto_splitter._extract_order_totals_from_text(
            "Shipping: $468.76 Promo(s): $5.00 [BrightShirt34] "
            "Subtotal before Tax: $1,665.71 Grand Total: $1,786.40"
        )

        self.assertEqual(totals["promo"], "5.00")
        self.assertEqual(totals["promo_code"], "BrightShirt34")

    def test_build_split_plan_allocates_promo_credit_like_shipping(self):
        designs = [
            {"tab_number": index, "design_id": str(1000 + index), "design_name": f"Design {index}"}
            for index in range(1, 4)
        ]

        plan = crm_auto_splitter._build_split_plan(
            designs,
            3,
            "4650000",
            shipping_amount=crm_auto_splitter.Decimal("4.00"),
            promo_amount=crm_auto_splitter.Decimal("5.00"),
            promo_code="BrightShirt34",
        )

        self.assertEqual([item["shipping_charge"] for item in plan], ["1.34", "1.33", "1.33"])
        self.assertEqual([item["promo_credit"] for item in plan], ["1.67", "1.67", "1.66"])
        self.assertEqual([item["promo_code"] for item in plan], ["BrightShirt34"] * 3)

    def test_split_total_mismatch_message_names_old_new_and_difference(self):
        message = crm_auto_splitter._split_total_mismatch_message(
            crm_auto_splitter.Decimal("100.00"),
            crm_auto_splitter.Decimal("95.00"),
            crm_auto_splitter.Decimal("-5.00"),
        )

        self.assertIn("old/original $100.00", message)
        self.assertIn("new/split $95.00", message)
        self.assertIn("difference -$5.00", message)

    def test_discount_fee_detection_matches_existing_negative_discount(self):
        with mock.patch.object(
            crm_auto_splitter,
            "_order_fee_rows",
            return_value=[{"name": "Discount", "code": "discount", "amount": "-1.67"}],
        ):
            self.assertTrue(
                crm_auto_splitter._order_fee_already_present(
                    mock.Mock(),
                    "Discount",
                    crm_auto_splitter.Decimal("-1.67"),
                )
            )

    def test_split_promo_discount_fee_uses_negative_discount_amount(self):
        with mock.patch.object(crm_auto_splitter, "_add_order_fee", return_value={"skipped": False}) as mock_add:
            result = crm_auto_splitter._add_discount_fee_to_split_order(mock.Mock(), "1.67")

        self.assertFalse(result["skipped"])
        mock_add.assert_called_once_with(
            mock.ANY,
            "Discount",
            crm_auto_splitter.Decimal("-1.67"),
            fallback_code="discount",
        )

    def test_new_split_applies_quote_discount_before_recording_payment(self):
        events = []
        driver = mock.Mock()
        split = {
            "split_index": 1,
            "keep_design_names": ["Design 1"],
            "keep_design_ids": [101],
            "delete_design_ids": [202],
            "shipping_charge": "156.26",
            "promo_credit": "1.67",
            "promo_code": "BrightShirt34",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_auto_splitter, "kill_stale_chrome"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "_handle_login_if_needed"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "_is_login_page", return_value=False))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "_wait_for_crm_context_with_reload"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "_wait_for_order_scope"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "_copy_order_to_quote"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "_open_order_scope_with_reload"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "safe_driver_quit"))
            stack.enter_context(mock.patch.object(crm_auto_splitter, "_build_splitter_driver", return_value=driver))
            stack.enter_context(
                mock.patch.object(
                    crm_auto_splitter,
                    "_configure_quote_split",
                    side_effect=lambda *args, **kwargs: events.append("configure_quote") or {},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    crm_auto_splitter,
                    "_add_discount_fee_to_split_quote",
                    side_effect=lambda *args, **kwargs: events.append("quote_discount") or {"skipped": False},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    crm_auto_splitter,
                    "_save_quote",
                    side_effect=lambda *args, **kwargs: events.append("save_quote") or {"quote_id": 123},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    crm_auto_splitter,
                    "_record_split_payment_and_wait_for_order",
                    side_effect=lambda *args, **kwargs: events.append("record_payment") or "4678000",
                )
            )
            stack.enter_context(
                mock.patch.object(
                    crm_auto_splitter,
                    "_read_order_totals",
                    return_value={"grand_total": "100.00", "paid": "100.00", "balance_due": "0.00"},
                )
            )
            post_order_discount = stack.enter_context(
                mock.patch.object(crm_auto_splitter, "_add_discount_fee_to_split_order")
            )

            result = crm_auto_splitter._create_split_order_in_worker(
                split,
                "4676620",
                "https://crm.example.test/order/4676620",
                30,
                {},
                "Stripe.com",
                "pi_test",
                Path("chrome_profile_test"),
            )

        self.assertEqual(result["order_id"], "4678000")
        self.assertLess(events.index("quote_discount"), events.index("save_quote"))
        self.assertLess(events.index("save_quote"), events.index("record_payment"))
        post_order_discount.assert_not_called()

    @mock.patch.object(crm_auto_splitter, "safe_driver_quit")
    @mock.patch.object(crm_auto_splitter, "_extract_process_batch_order_ids")
    @mock.patch.object(crm_auto_splitter, "_open_browser_if_requested")
    def test_process_batch_dry_run_reopens_list_until_no_new_orders(self, mock_open_browser, mock_extract_ids, _mock_quit):
        mock_open_browser.return_value = (mock.Mock(), "https://crm.example.test/report")
        mock_extract_ids.side_effect = [["4536106"], ["4536164"], []]

        with tempfile.TemporaryDirectory() as tmp:
            result_file = str(Path(tmp) / "result.json")
            exit_code = crm_auto_splitter.run_process_batch(
                list_url="https://crm.example.test/report",
                dry_run=True,
                result_file=result_file,
            )
            with open(result_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["order_ids"], ["4536106", "4536164"])
        self.assertEqual(payload["refresh_passes"], 2)
        self.assertEqual(mock_extract_ids.call_count, 3)
        self.assertEqual(mock_extract_ids.call_args_list[1].kwargs["exclude_order_ids"], {"4536106"})
        self.assertEqual(mock_extract_ids.call_args_list[2].kwargs["exclude_order_ids"], {"4536106", "4536164"})


class CrmProductSeparatorTests(unittest.TestCase):
    def test_crm_context_timeout_refreshes_only_once_before_error(self):
        driver = mock.Mock()
        with self.assertRaises(crm_product_separator.ProductSeparatorError):
            crm_product_separator._wait_for_crm_context(driver, timeout=0)

        driver.refresh.assert_called_once_with()

    def _product_separator_mixed_scan(self):
        return {
            "order_id": "4600001",
            "tab_count": 1,
            "tabs": [
                {
                    "tab_number": 1,
                    "tab_name": "H-Test001",
                    "quantity": 2,
                    "products": [
                        {
                            "product_name": "Adult Tee",
                            "group": "adult_general",
                            "group_label": "Adult/general",
                            "color": "BLACK",
                            "sizes": ["S"],
                        },
                        {
                            "product_name": "Snapback Cap",
                            "group": "hat_cap",
                            "group_label": "Hat/cap",
                            "color": "BLACK",
                            "sizes": ["ONESIZE"],
                        },
                    ],
                    "groups": [
                        {"group": "adult_general", "group_label": "Adult/general"},
                        {"group": "hat_cap", "group_label": "Hat/cap"},
                    ],
                    "needs_split": True,
                }
            ],
        }

    def _product_separator_clean_scan(self):
        return {
            "order_id": "4600001",
            "tab_count": 2,
            "tabs": [
                {
                    "tab_number": 1,
                    "tab_name": "H-Test001",
                    "quantity": 1,
                    "products": [
                        {
                            "product_name": "Adult Tee",
                            "group": "adult_general",
                            "group_label": "Adult/general",
                            "color": "BLACK",
                            "sizes": ["S"],
                        },
                    ],
                    "groups": [{"group": "adult_general", "group_label": "Adult/general"}],
                    "needs_split": False,
                },
                {
                    "tab_number": 2,
                    "tab_name": "H-Test002",
                    "quantity": 1,
                    "products": [
                        {
                            "product_name": "Snapback Cap",
                            "group": "hat_cap",
                            "group_label": "Hat/cap",
                            "color": "BLACK",
                            "sizes": ["ONESIZE"],
                        },
                    ],
                    "groups": [{"group": "hat_cap", "group_label": "Hat/cap"}],
                    "needs_split": False,
                },
            ],
        }

    def test_report_order_color_filter_includes_lime_green(self):
        self.assertIn("[34, 236, 72]", crm_product_separator.REPORT_ORDER_IDS_JS)
        self.assertIn("limeGreen", crm_product_separator.REPORT_ORDER_IDS_JS)

    def test_scan_visible_products_ignores_explicit_zero_quantity_products(self):
        driver = mock.Mock()
        driver.execute_script.return_value = [
            {"product_name": "Adult Tee", "total_quantity": 12},
            {"product_name": "Youth Tee Preserved By CRM", "total_quantity": 0},
            {"product_name": "Legacy Product Without Parsed Total"},
        ]

        products = crm_product_separator._scan_visible_products(driver)

        self.assertEqual(
            [product["product_name"] for product in products],
            ["Adult Tee", "Legacy Product Without Parsed Total"],
        )

    def test_custom_names_and_numbers_detector_targets_visible_edit_button(self):
        driver = mock.Mock()
        driver.execute_script.return_value = True

        self.assertTrue(crm_product_separator._active_tab_has_custom_names_and_numbers(driver))
        script = driver.execute_script.call_args.args[0]
        self.assertIn("editnamesandnumbers", script.lower())
        self.assertIn(".filter(visible)", script)

    def test_separator_plan_skips_only_mixed_tabs_with_custom_names_and_numbers(self):
        scan = self._product_separator_mixed_scan()
        scan["tabs"][0]["custom_names_and_numbers_present"] = True
        normal_tab = json.loads(json.dumps(scan["tabs"][0]))
        normal_tab.update(
            {
                "tab_number": 2,
                "tab_name": "H-Test002",
                "custom_names_and_numbers_present": False,
            }
        )
        scan["tabs"].append(normal_tab)
        scan["tab_count"] = 2

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertTrue(plan["needs_split"])
        self.assertTrue(plan["custom_names_and_numbers_present"])
        self.assertEqual([tab["tab_number"] for tab in plan["custom_names_and_numbers_tabs"]], [1])
        self.assertEqual([tab["source_tab_number"] for tab in plan["split_tabs"]], [2])

    def test_split_verification_ignores_custom_names_and_numbers_mixed_tab(self):
        scan = self._product_separator_mixed_scan()
        scan["tabs"][0]["custom_names_and_numbers_present"] = True

        self.assertEqual(crm_product_separator._tabs_still_needing_split(scan), [])

    def test_ladies_micro_ribbed_baby_tee_stays_with_adult_products(self):
        product = crm_product_separator._classify_product(
            {
                "product_name": "1010BE Ladies' Micro Ribbed Baby Tee",
                "text": "1010BE Ladies' Micro Ribbed Baby Tee Alpha Stock Size: XS S M",
            }
        )

        self.assertEqual(product["group"], "adult_general")
        self.assertEqual(product["group_label"], "Adult/general")

        scan = crm_product_separator._fallback_scan_from_order_summary(
            "4600001 Summary: Adult Tee (5) / 1010BE Ladies' Micro Ribbed Baby Tee (9) Quote",
            expected_order_id="4600001",
        )

        self.assertIsNotNone(scan)
        self.assertFalse(scan["tabs"][0]["needs_split"])
        self.assertEqual([group["group"] for group in scan["tabs"][0]["groups"]], ["adult_general"])

    def test_separator_plan_reuses_existing_matching_split_tab_on_rerun(self):
        scan = {
            "tabs": [
                {
                    "tab_number": 6,
                    "tab_name": "H-NicholeKus882",
                    "quantity": 14,
                    "products": [
                        {
                            "product_name": "G500 Gildan Heavy Cotton T-Shirt",
                            "group": "adult_general",
                            "group_label": "Adult/general",
                            "color": "MAROON",
                            "total_quantity": 11,
                        },
                        {
                            "product_name": "G500B Gildan Heavy Cotton Kids T-Shirt",
                            "group": "youth",
                            "group_label": "Youth",
                            "color": "MAROON",
                            "total_quantity": 3,
                        },
                    ],
                    "groups": [
                        {"group": "adult_general", "group_label": "Adult/general"},
                        {"group": "youth", "group_label": "Youth"},
                    ],
                    "needs_split": True,
                    "stock": {"state": "ordered_header_only"},
                },
                {
                    "tab_number": 12,
                    "tab_name": "H-NicholeKus888",
                    "quantity": 3,
                    "products": [
                        {
                            "product_name": "G500B Gildan Heavy Cotton Kids T-Shirt",
                            "group": "youth",
                            "group_label": "Youth",
                            "color": "MAROON",
                            "total_quantity": 3,
                        },
                    ],
                    "groups": [{"group": "youth", "group_label": "Youth"}],
                    "needs_split": False,
                    "stock": {"state": "ordered_header_only"},
                },
            ],
        }

        plan = crm_product_separator._build_separator_plan(scan)

        assignments = plan["split_tabs"][0]["assignments"]
        self.assertEqual([assignment["source"] for assignment in assignments], ["original", "existing"])
        self.assertEqual(assignments[1]["tab_number"], 12)
        self.assertEqual(plan["production_notes"], [])

    @mock.patch.object(crm_product_separator, "_prepare_worker_profiles")
    @mock.patch.object(crm_product_separator, "safe_driver_quit")
    @mock.patch.object(crm_product_separator, "_extract_report_order_ids")
    @mock.patch.object(crm_product_separator, "_build_driver")
    def test_list_run_with_no_detected_orders_passes_short_message(
        self,
        mock_build_driver,
        mock_extract_order_ids,
        _mock_quit,
        mock_prepare_profiles,
    ):
        driver = mock.Mock()
        mock_build_driver.return_value = (driver, None)
        mock_extract_order_ids.return_value = []

        with tempfile.TemporaryDirectory() as tmp:
            result_file = str(Path(tmp) / "result.json")
            exit_code = crm_product_separator.run_product_separator_list(
                list_url="https://crm.example.test/report",
                list_mode="rush",
                dry_run=True,
                workers=4,
                result_file=result_file,
            )
            with open(result_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["message"], "No orders detected")
        self.assertEqual(payload["order_count"], 0)
        self.assertEqual(payload["order_ids"], [])
        mock_prepare_profiles.assert_not_called()

    @mock.patch.object(crm_product_separator, "_run_product_separator_order_id_batch")
    @mock.patch.object(crm_product_separator, "_prepare_worker_profiles", return_value=("worker-root", ["profile-1", "profile-2"]))
    @mock.patch.object(crm_product_separator, "safe_driver_quit")
    @mock.patch.object(crm_product_separator, "_extract_report_order_ids")
    @mock.patch.object(crm_product_separator, "_build_driver")
    def test_list_run_reopens_report_after_each_processed_pass(
        self,
        mock_build_driver,
        mock_extract_order_ids,
        _mock_quit,
        _mock_prepare_profiles,
        mock_run_batch,
    ):
        mock_build_driver.return_value = (mock.Mock(), None)
        mock_extract_order_ids.side_effect = [["4600001"], ["4600002"], []]
        mock_run_batch.side_effect = [
            {
                "worker_count": 1,
                "order_results": [{"order_id": "4600001", "success": True, "needs_split": False, "manual_review_required": False}],
                "split_order_ids": [],
                "skipped_order_ids": ["4600001"],
                "manual_review_order_ids": [],
                "failed_order_ids": [],
            },
            {
                "worker_count": 1,
                "order_results": [{"order_id": "4600002", "success": True, "needs_split": False, "manual_review_required": False}],
                "split_order_ids": [],
                "skipped_order_ids": ["4600002"],
                "manual_review_order_ids": [],
                "failed_order_ids": [],
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            result_file = str(Path(tmp) / "result.json")
            exit_code = crm_product_separator.run_product_separator_list(
                list_url="https://crm.example.test/report",
                list_mode="rush",
                dry_run=True,
                workers=2,
                result_file=result_file,
            )
            with open(result_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["order_ids"], ["4600001", "4600002"])
        self.assertEqual(payload["refresh_passes"], 2)
        self.assertEqual(mock_extract_order_ids.call_count, 3)
        self.assertEqual(mock_extract_order_ids.call_args_list[1].kwargs["exclude_order_ids"], {"4600001"})
        self.assertEqual(mock_extract_order_ids.call_args_list[2].kwargs["exclude_order_ids"], {"4600001", "4600002"})

    @mock.patch.object(crm_product_separator, "_run_product_separator_order_id_batch")
    @mock.patch.object(crm_product_separator, "_prepare_worker_profiles", return_value=("worker-root", ["profile-1"]))
    @mock.patch.object(crm_product_separator, "safe_driver_quit")
    @mock.patch.object(crm_product_separator, "_extract_report_order_ids")
    @mock.patch.object(crm_product_separator, "_build_driver")
    def test_list_run_summary_includes_manual_review_count(
        self,
        mock_build_driver,
        mock_extract_order_ids,
        _mock_quit,
        _mock_prepare_profiles,
        mock_run_batch,
    ):
        mock_build_driver.return_value = (mock.Mock(), None)
        mock_extract_order_ids.side_effect = [["4600001"], []]
        mock_run_batch.return_value = {
            "worker_count": 1,
            "order_results": [
                {
                    "order_id": "4600001",
                    "success": False,
                    "needs_split": False,
                    "manual_review_required": True,
                    "resolution": "manual_review",
                }
            ],
            "split_order_ids": [],
            "skipped_order_ids": [],
            "manual_review_order_ids": ["4600001"],
            "failed_order_ids": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            result_file = str(Path(tmp) / "result.json")
            exit_code = crm_product_separator.run_product_separator_list(
                list_url="https://crm.example.test/report",
                list_mode="rush",
                dry_run=True,
                workers=1,
                result_file=result_file,
            )
            with open(result_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertEqual(exit_code, 4)
        self.assertFalse(payload["success"])
        self.assertEqual(
            payload["message"],
            "Product Separator list dry run complete. 0 order(s) need splitting, 0 already okay, 1 require manual review.",
        )
        self.assertEqual(payload["manual_review_order_ids"], ["4600001"])

    @mock.patch.object(server, "_execute_crm_product_separator_script")
    def test_live_batch_with_no_detected_orders_passes_short_message(self, mock_run_script):
        preflight_payload = {
            "success": True,
            "message": "No orders detected",
            "action": "product_separator_list",
            "dry_run": True,
            "list_mode": "rush",
            "parallel_workers": 4,
            "order_count": 0,
            "order_ids": [],
            "split_order_ids": [],
            "skipped_order_ids": [],
            "manual_review_order_ids": [],
            "failed_order_ids": [],
            "report": [],
        }
        mock_run_script.return_value = (True, "No orders detected", preflight_payload)

        ok, message, payload = server._execute_crm_product_separator_worker(
            dry_run=False,
            list_mode="rush",
            parallel_workers=4,
        )

        self.assertTrue(ok)
        self.assertEqual(message, "No orders detected")
        self.assertEqual(payload["message"], "No orders detected")
        self.assertEqual(payload["order_ids"], [])
        self.assertEqual(payload["order_results"], [])
        self.assertEqual(mock_run_script.call_count, 1)

    @mock.patch.object(server, "_execute_crm_product_separator_script")
    def test_live_batch_reports_custom_names_and_numbers_skipped_order(self, mock_run_script):
        preflight_payload = {
            "success": True,
            "message": (
                "Product Separator list dry run complete. 0 order(s) need splitting, "
                "0 already okay, 1 skipped for custom names and numbers."
            ),
            "action": "product_separator_list",
            "dry_run": True,
            "list_mode": "rush",
            "order_ids": ["4883479"],
            "split_order_ids": [],
            "skipped_order_ids": ["4883479"],
            "custom_names_and_numbers_order_ids": ["4883479"],
            "manual_review_order_ids": [],
            "failed_order_ids": [],
            "report": [
                {
                    "order_id": "4883479",
                    "success": True,
                    "resolution": "skipped_custom_names_and_numbers",
                    "needs_split": False,
                    "custom_names_and_numbers_present": True,
                    "custom_names_and_numbers_tabs": [{"tab_number": 1}],
                    "message": (
                        "Product Separator skipped order 4883479: "
                        "Custom names and numbers present on mixed-product tab 1."
                    ),
                }
            ],
        }
        mock_run_script.return_value = (True, preflight_payload["message"], preflight_payload)

        ok, message, payload = server._execute_crm_product_separator_worker(
            dry_run=False,
            list_mode="rush",
            parallel_workers=4,
        )

        self.assertTrue(ok)
        self.assertIn("4883479", message)
        self.assertIn("custom names and numbers", message.lower())
        self.assertEqual(payload["custom_names_and_numbers_order_ids"], ["4883479"])
        self.assertEqual(payload["order_results"][0]["status"], "Skipped")
        self.assertTrue(payload["order_results"][0]["custom_names_and_numbers_present"])
        self.assertEqual(mock_run_script.call_count, 1)

    def test_not_authenticated_page_is_detected_before_design_tab_failure(self):
        driver = mock.Mock()
        driver.execute_script.return_value = "\u00d7 Error Not authenticated close"

        self.assertTrue(crm_product_separator._is_not_authenticated_page(driver))

    @mock.patch.object(crm_product_separator, "_run_order_dry_worker")
    def test_order_chunk_worker_retries_transient_crm_readiness_error(self, mock_dry_worker):
        mock_dry_worker.side_effect = [
            {
                "success": False,
                "message": "Product Separator failed for order 4607189: CRM app did not become ready.",
                "target_order_id": "4607189",
            },
            {
                "success": True,
                "message": "Product Separator skipped order 4607189: no mixed product tabs detected.",
                "target_order_id": "4607189",
            },
        ]

        results = crm_product_separator._run_order_chunk_worker(["4607189"], "profile", "results")

        self.assertEqual(mock_dry_worker.call_count, 2)
        self.assertTrue(results[0]["success"])
        self.assertTrue(results[0]["retried_after_transient_error"])
        self.assertIn("CRM app did not become ready", results[0]["first_attempt_message"])

    def test_no_split_stock_ordered_missing_manual_order_row_is_skipped(self):
        driver = mock.Mock()
        scan = {
            "order_id": "4600001",
            "tab_count": 1,
            "order_stock_status": crm_product_separator._order_stock_status_from_text(
                "Stock Status: Ordered Stock : Ordered"
            ),
            "tabs": [
                {
                    "tab_number": 1,
                    "tab_name": "H-Test001",
                    "quantity": 1,
                    "products": [
                        {
                            "product_name": "Adult Tee",
                            "group": "adult_general",
                            "group_label": "Adult/general",
                            "color": "BLACK",
                            "sizes": ["S"],
                        }
                    ],
                    "groups": [{"group": "adult_general", "group_label": "Adult/general"}],
                    "needs_split": False,
                    "stock": {
                        "state": "ordered_header_only",
                        "stock_status_ordered": True,
                        "has_vendor_section": True,
                        "has_po_row": False,
                        "manual_order_vendor": "",
                        "manual_order_po": "",
                        "manual_order_rows": [],
                    },
                }
            ],
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_driver", return_value=(driver, None)))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_handle_login_if_needed"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_wait_for_crm_context"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_recover_not_authenticated_page"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_scan_order", return_value=scan))
            mock_write = stack.enter_context(mock.patch.object(crm_product_separator, "_write_result"))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_driver_quit"))

            exit_code = crm_product_separator.run_product_separator_order(order_id="4600001", dry_run=True)

        self.assertEqual(exit_code, 0)
        self.assertTrue(mock_write.call_args.args[0])
        self.assertEqual(mock_write.call_args.kwargs["resolution"], "skipped_no_split_needed")
        self.assertFalse(mock_write.call_args.kwargs["manual_review_required"])
        self.assertNotIn("manual_order_reconciliation", mock_write.call_args.kwargs["report"])

    def test_order_with_only_custom_names_and_numbers_mixed_tab_is_skipped_and_reported(self):
        driver = mock.Mock()
        scan = self._product_separator_mixed_scan()
        scan["order_id"] = "4883479"
        scan["tabs"][0]["custom_names_and_numbers_present"] = True

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_driver", return_value=(driver, None)))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_handle_login_if_needed"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_wait_for_crm_context"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_recover_not_authenticated_page"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_scan_order", return_value=scan))
            mock_apply = stack.enter_context(mock.patch.object(crm_product_separator, "_apply_live_split"))
            mock_write = stack.enter_context(mock.patch.object(crm_product_separator, "_write_result"))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_driver_quit"))

            exit_code = crm_product_separator.run_product_separator_order(order_id="4883479", dry_run=False)

        self.assertEqual(exit_code, 0)
        mock_apply.assert_not_called()
        self.assertTrue(mock_write.call_args.args[0])
        self.assertIn("4883479", mock_write.call_args.args[1])
        self.assertIn("Custom names and numbers present", mock_write.call_args.args[1])
        self.assertEqual(mock_write.call_args.kwargs["resolution"], "skipped_custom_names_and_numbers")
        self.assertEqual(
            [tab["tab_number"] for tab in mock_write.call_args.kwargs["custom_names_and_numbers_tabs"]],
            [1],
        )

    def test_stock_ordered_status_rejects_stock_history_confirmation(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {
            "already_applied": True,
            "clicked_apply": False,
            "confirmation": "stock_history",
        }

        with self.assertRaises(crm_product_separator.ProductSeparatorError):
            crm_product_separator._apply_stock_ordered_status(driver)

        driver.execute_script.assert_called_once()

    def test_stock_ordered_status_accepts_visible_header_confirmation(self):
        driver = mock.Mock()
        driver.execute_script.side_effect = [
            {"success": True},
            None,
            True,
            "Stock Status: Ordered",
        ]

        with mock.patch.object(crm_product_separator.time, "sleep"), mock.patch.object(
            crm_product_separator.time,
            "monotonic",
            side_effect=[0, 1],
        ):
            result = crm_product_separator._apply_stock_ordered_status(driver)

        self.assertTrue(result["status_applied"])
        self.assertEqual(result["confirmation"], "header")
        driver.refresh.assert_not_called()

    @mock.patch.object(crm_product_separator, "_wait_for_crm_context")
    def test_stock_ordered_status_refreshes_after_stale_inline_confirmation(self, mock_wait_context):
        driver = mock.Mock()
        driver.execute_script.side_effect = [
            {"success": True},
            None,
            True,
            "Stock Status: Need To Order",
            "Stock Status: Ordered",
        ]

        with mock.patch.object(crm_product_separator.time, "sleep"), mock.patch.object(
            crm_product_separator.time,
            "monotonic",
            side_effect=[0, 1, 31, 31, 32],
        ):
            result = crm_product_separator._apply_stock_ordered_status(driver)

        self.assertTrue(result["status_applied"])
        self.assertEqual(result["confirmation"], "header_after_refresh")
        driver.refresh.assert_called_once()
        mock_wait_context.assert_called_once_with(driver)

    def test_stock_ordered_status_verification_requires_current_header(self):
        verification = {
            "scan_after": {
                "order_stock_status": {
                    "state": "need_to_order",
                    "status_text": "Need To Order",
                    "stock_status_ordered": False,
                    "stock_status_needs_order": True,
                },
                "tabs": [
                    {"stock": {"stock_status_ordered": True, "has_po_row": True}},
                    {"stock": {"stock_status_ordered": True, "has_po_row": True}},
                ]
            }
        }

        result = crm_product_separator._verify_stock_ordered_status_persisted(verification)

        self.assertFalse(result["stock_status_verified"])

    def test_stock_ordered_status_verification_accepts_refreshed_header(self):
        verification = {
            "scan_after": {
                "tabs": [
                    {"stock": {"stock_status_ordered": False}},
                ]
            },
            "scan_after_refresh": {
                "tabs": [
                    {"stock": {"stock_status_ordered": True}},
                    {"stock": {"stock_status_ordered": True}},
                ]
            },
        }

        result = crm_product_separator._verify_stock_ordered_status_persisted(verification)

        self.assertTrue(result["stock_status_verified"])
        self.assertEqual(result["verification_scan"], "scan_after_refresh")

    def test_stock_state_accepts_current_stock_ordered_label(self):
        result = crm_product_separator._stock_state_from_text(
            "Stock Status: Ordered Stock : Ordered"
        )

        self.assertTrue(result["stock_status_ordered"])
        self.assertEqual(result["state"], "ordered_header_only")

    def test_stock_state_does_not_accept_false_stock_ordered_label(self):
        result = crm_product_separator._stock_state_from_text(
            "Stock Ordered: false Stock : Need To Order"
        )

        self.assertFalse(result["stock_status_ordered"])

    def test_order_stock_status_detects_need_to_order_before_ordered_stock_line(self):
        result = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Need To Order Stock : Ordered"
        )

        self.assertEqual(result["state"], "need_to_order")
        self.assertFalse(result["stock_status_ordered"])
        self.assertTrue(result["stock_status_needs_order"])

    def test_separator_plan_skips_stock_apply_when_order_status_needs_order(self):
        scan = self._product_separator_mixed_scan()
        scan["order_stock_status"] = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Need To Order Stock : Ordered"
        )
        scan["tabs"][0]["stock"] = {
            "state": "ordered",
            "stock_status_ordered": True,
            "manual_order_vendor": "S&S Activewear",
            "manual_order_po": "H-Test001-SS01",
            "manual_order_rows": [{"vendor": "S&S Activewear", "po": "H-Test001-SS01"}],
        }

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertTrue(plan["stock_ordered_for_all_affected_tabs"])
        self.assertFalse(plan["apply_stock_ordered_after_split"])
        self.assertIn("Need To Order", plan["stock_ordered_apply_skip_reason"])
        self.assertEqual(plan["production_notes"], ["tab 1 and 2 in 1 box"])

    def test_separator_plan_skips_production_note_when_source_tab_not_ordered(self):
        scan = self._product_separator_mixed_scan()
        scan["order_stock_status"] = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Need To Order Stock : Need To Order"
        )
        scan["tabs"][0]["stock"] = {"state": "not_ordered_or_unknown", "stock_status_ordered": False}

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertFalse(plan["stock_ordered_for_all_affected_tabs"])
        self.assertFalse(plan["apply_stock_ordered_after_split"])
        self.assertEqual(plan["production_notes"], [])

    def test_separator_plan_allows_mixed_split_tabs_without_global_stock_apply(self):
        scan = self._product_separator_mixed_scan()
        scan["order_stock_status"] = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Need To Order Stock : Ordered"
        )
        scan["tabs"][0]["stock"] = {
            "state": "ordered_po_only",
            "stock_status_ordered": False,
            "manual_order_vendor": "S&S Activewear",
            "manual_order_po": "H-Test001-SS01",
            "manual_order_rows": [{"vendor": "S&S Activewear", "po": "H-Test001-SS01"}],
        }
        second_tab = {
            **scan["tabs"][0],
            "tab_number": 2,
            "tab_name": "H-Test002",
            "quantity": 2,
            "stock": {"state": "not_ordered_or_unknown", "stock_status_ordered": False},
        }
        scan["tabs"].append(second_tab)
        scan["tab_count"] = 2

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertTrue(plan["mixed_stock_state"])
        self.assertFalse(plan["manual_review_required"])
        self.assertFalse(plan["apply_stock_ordered_after_split"])
        self.assertIn("mixed stock-ordered state", plan["stock_ordered_apply_skip_reason"])
        self.assertEqual(plan["production_notes"], ["tab 1 and 3 in 1 box"])

    def test_separator_plan_skips_stock_apply_for_vendor_manual_order_copy(self):
        scan = self._product_separator_mixed_scan()
        scan["order_stock_status"] = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Ordered Stock : Ordered"
        )
        scan["tabs"][0]["stock"] = {
            "state": "ordered",
            "stock_status_ordered": True,
            "manual_order_vendor": "S&S Activewear",
            "manual_order_po": "H-Test001-SS01",
            "manual_order_rows": [{"vendor": "S&S Activewear", "po": "H-Test001-SS01"}],
        }

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertTrue(plan["stock_ordered_for_all_affected_tabs"])
        self.assertFalse(plan["apply_stock_ordered_after_split"])
        self.assertIn("copied Manual Order rows", plan["stock_ordered_apply_skip_reason"])
        self.assertEqual(plan["production_notes"], ["tab 1 and 2 in 1 box"])

    def test_separator_plan_auto_orders_explicit_local_inventory_without_box_note(self):
        scan = self._product_separator_mixed_scan()
        scan["order_stock_status"] = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Ordered Stock : Ordered"
        )
        scan["tabs"][0]["stock"] = {
            "state": "ordered",
            "stock_status_ordered": True,
            "manual_order_vendor": "Local Inventory",
            "manual_order_po": "H-Test001-LI01",
            "manual_order_rows": [{"vendor": "Local Inventory", "po": "H-Test001-LI01"}],
        }

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertFalse(plan["manual_review_required"])
        self.assertEqual(plan["manual_order_records"], [])
        self.assertEqual(plan["production_notes"], [])
        self.assertEqual(
            [target["target_tab_number"] for target in plan["local_inventory_auto_order_targets"]],
            [2],
        )
        self.assertIn("Local Inventory", plan["stock_ordered_apply_skip_reason"])

    def test_separator_plan_keeps_header_only_stock_without_manual_order_copy(self):
        scan = self._product_separator_mixed_scan()
        scan["order_stock_status"] = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Ordered Stock : Ordered"
        )
        scan["tabs"][0]["stock"] = {
            "state": "ordered_header_only",
            "stock_status_ordered": True,
            "manual_order_vendor": "",
            "manual_order_po": "",
            "manual_order_rows": [],
        }

        plan = crm_product_separator._build_separator_plan(scan)

        self.assertFalse(plan["manual_review_required"])
        self.assertEqual(plan["manual_order_records"], [])
        self.assertEqual(plan["production_notes"], ["tab 1 and 2 in 1 box"])
        self.assertEqual(plan["local_inventory_auto_order_targets"], [])
        self.assertIn("already stock ordered", plan["stock_ordered_apply_skip_reason"])

    def test_manual_order_verification_accepts_dom_fallback(self):
        driver = mock.Mock()
        records = [
            {
                "target_tab_number": 2,
                "target_tab_name": "H-Test002",
                "po": "H-Test001-SS01",
                "vendor": "S&S Activewear",
            }
        ]

        with mock.patch.object(crm_product_separator._order_goods, "_activate_stock_tab") as activate, mock.patch.object(
            crm_product_separator._shipping_bypasser,
            "_crm_manual_order_row_exists",
            return_value=True,
        ) as row_exists, mock.patch.object(crm_product_separator.time, "sleep"):
            verification = crm_product_separator._verify_manual_order_records_visible_in_dom(driver, records)

        self.assertTrue(verification["verified"])
        activate.assert_called_once_with(driver, 1)
        row_exists.assert_called_once_with(driver, "H-Test001-SS01", vendor_name="S&S Activewear")

    def test_tabs_still_needing_split_filters_mixed_tabs(self):
        scan = {
            "tabs": [
                {"tab_number": 1, "needs_split": False},
                {"tab_number": 2, "needs_split": True},
            ]
        }

        remaining = crm_product_separator._tabs_still_needing_split(scan)

        self.assertEqual([tab["tab_number"] for tab in remaining], [2])

    def test_scan_looks_unchanged_after_split_compares_product_state(self):
        mixed_scan = self._product_separator_mixed_scan()
        clean_scan = self._product_separator_clean_scan()

        self.assertTrue(crm_product_separator._scan_looks_unchanged_after_split(mixed_scan, mixed_scan))
        self.assertFalse(crm_product_separator._scan_looks_unchanged_after_split(mixed_scan, clean_scan))

    @mock.patch.object(crm_product_separator, "_recover_not_authenticated_page")
    @mock.patch.object(crm_product_separator, "_wait_for_crm_context")
    @mock.patch.object(crm_product_separator, "_handle_login_if_needed")
    @mock.patch.object(crm_product_separator, "safe_get_with_partial_load")
    @mock.patch.object(crm_product_separator, "_scan_order")
    def test_split_verification_refreshes_once_before_failing(
        self,
        mock_scan_order,
        mock_safe_get,
        mock_handle_login,
        mock_wait_context,
        mock_recover_auth,
    ):
        driver = mock.Mock()
        mixed_scan = {
            "tabs": [
                {
                    "tab_number": 1,
                    "tab_name": "H-Test001",
                    "needs_split": True,
                    "groups": [{"group_label": "Adult/general"}, {"group_label": "Hat/cap"}],
                }
            ]
        }
        clean_scan = {
            "tabs": [
                {"tab_number": 1, "tab_name": "H-Test001", "needs_split": False},
                {"tab_number": 2, "tab_name": "H-Test002", "needs_split": False},
            ]
        }
        mock_scan_order.side_effect = [mixed_scan, clean_scan]

        verification, remaining = crm_product_separator._verify_split_persisted_after_save(
            driver,
            "https://crm2.legacy.printfly.com/order/4600001",
            "4600001",
            login_wait_seconds=0,
        )

        self.assertEqual(remaining, [])
        self.assertTrue(verification["verification_refresh_attempted"])
        self.assertEqual(verification["scan_after"], mixed_scan)
        self.assertEqual(verification["scan_after_refresh"], clean_scan)
        self.assertIn("H-Test001", verification["remaining_before_refresh"])
        self.assertEqual(verification["remaining_after_refresh"], "")
        driver.refresh.assert_called_once()
        mock_safe_get.assert_called_once()
        self.assertEqual(mock_scan_order.call_count, 2)
        self.assertEqual(mock_handle_login.call_count, 2)
        self.assertEqual(mock_wait_context.call_count, 2)
        self.assertEqual(mock_recover_auth.call_count, 2)

    @mock.patch.object(crm_product_separator, "_recover_not_authenticated_page")
    @mock.patch.object(crm_product_separator, "_wait_for_crm_context")
    @mock.patch.object(crm_product_separator, "_handle_login_if_needed")
    @mock.patch.object(crm_product_separator, "safe_get_with_partial_load")
    @mock.patch.object(crm_product_separator, "_scan_order")
    def test_split_verification_does_not_refresh_when_first_scan_is_clean(
        self,
        mock_scan_order,
        mock_safe_get,
        _mock_handle_login,
        _mock_wait_context,
        _mock_recover_auth,
    ):
        driver = mock.Mock()
        clean_scan = {"tabs": [{"tab_number": 1, "needs_split": False}]}
        mock_scan_order.return_value = clean_scan

        verification, remaining = crm_product_separator._verify_split_persisted_after_save(
            driver,
            "https://crm2.legacy.printfly.com/order/4600001",
            "4600001",
        )

        self.assertEqual(remaining, [])
        self.assertFalse(verification["verification_refresh_attempted"])
        self.assertEqual(verification["scan_after"], clean_scan)
        self.assertNotIn("scan_after_refresh", verification)
        driver.refresh.assert_not_called()
        mock_safe_get.assert_called_once()
        mock_scan_order.assert_called_once()

    def test_live_order_retries_split_once_when_first_save_makes_no_change(self):
        driver = mock.Mock()
        mixed_scan = self._product_separator_mixed_scan()
        clean_scan = self._product_separator_clean_scan()
        retryable_plan = {"needs_split": True, "manual_review_required": False, "split_tabs": []}
        first_verification = {
            "scan_after": mixed_scan,
            "verification_refresh_attempted": True,
            "scan_after_refresh": mixed_scan,
        }
        second_verification = {
            "scan_after": clean_scan,
            "verification_refresh_attempted": False,
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_driver", return_value=(driver, None)))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_handle_login_if_needed"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_wait_for_crm_context"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_recover_not_authenticated_page"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_scan_order", return_value=mixed_scan))
            mock_build_plan = stack.enter_context(
                mock.patch.object(crm_product_separator, "_build_separator_plan", return_value=retryable_plan)
            )
            mock_apply = stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_apply_live_split",
                    side_effect=[{"attempt": 1}, {"attempt": 2}],
                )
            )
            mock_verify = stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_verify_split_persisted_after_save",
                    side_effect=[
                        (first_verification, mixed_scan["tabs"]),
                        (second_verification, []),
                    ],
                )
            )
            mock_write = stack.enter_context(mock.patch.object(crm_product_separator, "_write_result"))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_driver_quit"))

            exit_code = crm_product_separator.run_product_separator_order(order_id="4600001", dry_run=False)

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_build_plan.call_count, 2)
        self.assertEqual(mock_apply.call_count, 2)
        self.assertEqual(mock_verify.call_count, 2)
        self.assertTrue(mock_write.call_args.args[0])
        report = mock_write.call_args.kwargs["report"]
        self.assertEqual(report["live"], {"attempt": 1})
        self.assertEqual(report["live_retry"]["live"], {"attempt": 2})
        self.assertTrue(report["live_retry"]["attempted"])
        self.assertEqual(report["live_retry"]["scan_after"], clean_scan)

    def test_live_order_zeroes_source_quantities_only_after_total_mismatch(self):
        driver = mock.Mock()
        initial_scan = self._product_separator_mixed_scan()
        mismatched_scan = {
            "tabs": [
                {
                    "tab_number": 1,
                    "tab_name": "H-Test001",
                    "quantity": 2,
                    "products": initial_scan["tabs"][0]["products"],
                    "groups": initial_scan["tabs"][0]["groups"],
                    "needs_split": True,
                },
                {
                    "tab_number": 2,
                    "tab_name": "H-Test002",
                    "quantity": 1,
                    "products": [
                        {
                            "product_name": "Snapback Cap",
                            "group": "hat_cap",
                            "group_label": "Hat/cap",
                            "color": "BLACK",
                            "sizes": ["ONESIZE"],
                            "total_quantity": 1,
                        },
                    ],
                    "groups": [{"group": "hat_cap", "group_label": "Hat/cap"}],
                    "needs_split": False,
                },
            ]
        }
        clean_scan = self._product_separator_clean_scan()
        plan = crm_product_separator._build_separator_plan(initial_scan)
        first_verification = {
            "scan_after": mismatched_scan,
            "verification_refresh_attempted": False,
        }
        second_verification = {
            "scan_after": clean_scan,
            "verification_refresh_attempted": False,
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_driver", return_value=(driver, None)))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_handle_login_if_needed"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_wait_for_crm_context"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_recover_not_authenticated_page"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_scan_order", return_value=initial_scan))
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_separator_plan", return_value=plan))
            mock_apply = stack.enter_context(mock.patch.object(crm_product_separator, "_apply_live_split", return_value={"attempt": 1}))
            mock_cleanup = stack.enter_context(
                mock.patch.object(crm_product_separator, "_apply_source_quantity_cleanup", return_value={"attempted": True})
            )
            stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_verify_split_persisted_after_save",
                    side_effect=[
                        (first_verification, mismatched_scan["tabs"][:1]),
                        (second_verification, []),
                    ],
                )
            )
            mock_write = stack.enter_context(mock.patch.object(crm_product_separator, "_write_result"))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_driver_quit"))

            exit_code = crm_product_separator.run_product_separator_order(order_id="4600001", dry_run=False)

        self.assertEqual(exit_code, 0)
        mock_apply.assert_called_once()
        mock_cleanup.assert_called_once()
        cleanup_targets = mock_cleanup.call_args.args[1]
        self.assertEqual([target["tab_number"] for target in cleanup_targets], [1])
        self.assertEqual(cleanup_targets[0]["keep_group"], "adult_general")
        report = mock_write.call_args.kwargs["report"]
        self.assertEqual(report["quantity_total_check"]["actual_total"], 2)
        self.assertTrue(report["final_quantity_total_check"]["matches"])

    def test_live_order_skips_stock_apply_when_order_status_needed_before_split(self):
        driver = mock.Mock()
        initial_scan = self._product_separator_mixed_scan()
        initial_scan["order_stock_status"] = crm_product_separator._order_stock_status_from_text(
            "Stock Status: Need To Order Stock : Ordered"
        )
        initial_scan["tabs"][0]["stock"] = {
            "state": "ordered",
            "stock_status_ordered": True,
            "manual_order_vendor": "S&S Activewear",
            "manual_order_po": "H-Test001-SS01",
            "manual_order_rows": [{"vendor": "S&S Activewear", "po": "H-Test001-SS01"}],
        }
        clean_scan = self._product_separator_clean_scan()
        clean_scan["order_stock_status"] = initial_scan["order_stock_status"]
        for tab in clean_scan["tabs"]:
            tab["stock"] = {"stock_status_ordered": True}
        plan = crm_product_separator._build_separator_plan(initial_scan)
        verification = {
            "scan_after": clean_scan,
            "verification_refresh_attempted": False,
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_driver", return_value=(driver, None)))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_handle_login_if_needed"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_wait_for_crm_context"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_recover_not_authenticated_page"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_scan_order", return_value=initial_scan))
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_separator_plan", return_value=plan))
            stack.enter_context(mock.patch.object(crm_product_separator, "_apply_live_split", return_value={"attempt": 1}))
            stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_verify_split_persisted_after_save",
                    return_value=(verification, []),
                )
            )
            mock_stock_apply = stack.enter_context(mock.patch.object(crm_product_separator, "_apply_stock_ordered_status"))
            stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_record_separator_manual_orders",
                    return_value={"attempted": True, "records": []},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_verify_manual_order_records_persisted",
                    return_value={"verified": True, "missing_records": []},
                )
            )
            mock_write = stack.enter_context(mock.patch.object(crm_product_separator, "_write_result"))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_driver_quit"))

            exit_code = crm_product_separator.run_product_separator_order(order_id="4600001", dry_run=False)

        self.assertEqual(exit_code, 0)
        mock_stock_apply.assert_not_called()
        report = mock_write.call_args.kwargs["report"]
        self.assertTrue(report["stock_status_apply"]["skipped"])
        self.assertIn("Need To Order", report["stock_status_apply"]["reason"])

    def test_live_order_fails_after_retry_when_second_save_still_makes_no_change(self):
        driver = mock.Mock()
        mixed_scan = self._product_separator_mixed_scan()
        retryable_plan = {"needs_split": True, "manual_review_required": False, "split_tabs": []}
        unchanged_verification = {
            "scan_after": mixed_scan,
            "verification_refresh_attempted": True,
            "scan_after_refresh": mixed_scan,
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_driver", return_value=(driver, None)))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_handle_login_if_needed"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_wait_for_crm_context"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_recover_not_authenticated_page"))
            stack.enter_context(mock.patch.object(crm_product_separator, "_scan_order", return_value=mixed_scan))
            stack.enter_context(mock.patch.object(crm_product_separator, "_build_separator_plan", return_value=retryable_plan))
            mock_apply = stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_apply_live_split",
                    side_effect=[{"attempt": 1}, {"attempt": 2}],
                )
            )
            mock_verify = stack.enter_context(
                mock.patch.object(
                    crm_product_separator,
                    "_verify_split_persisted_after_save",
                    side_effect=[
                        (unchanged_verification, mixed_scan["tabs"]),
                        (unchanged_verification, mixed_scan["tabs"]),
                    ],
                )
            )
            mock_write = stack.enter_context(mock.patch.object(crm_product_separator, "_write_result"))
            stack.enter_context(mock.patch.object(crm_product_separator, "safe_driver_quit"))

            exit_code = crm_product_separator.run_product_separator_order(order_id="4600001", dry_run=False)

        self.assertEqual(exit_code, 4)
        self.assertEqual(mock_apply.call_count, 2)
        self.assertEqual(mock_verify.call_count, 2)
        self.assertFalse(mock_write.call_args.args[0])
        self.assertIn("after retrying save/split once", mock_write.call_args.args[1])
        self.assertTrue(mock_write.call_args.kwargs["manual_review_required"])
        self.assertEqual(mock_write.call_args.kwargs["resolution"], "split_verification_failed")

    @mock.patch.object(server, "_execute_crm_product_separator_script")
    def test_live_batch_continues_when_preflight_has_split_orders_and_one_failure(self, mock_run_script):
        preflight_payload = {
            "success": False,
            "message": "Product Separator list dry run complete. 1 order(s) need splitting, 1 already okay.",
            "action": "product_separator_list",
            "dry_run": True,
            "list_mode": "rush",
            "parallel_workers": 4,
            "order_ids": ["4609102", "4607350", "4609999"],
            "split_order_ids": ["4609102"],
            "skipped_order_ids": ["4609999"],
            "failed_order_ids": ["4607350"],
            "report": [
                {
                    "order_id": "4609102",
                    "success": True,
                    "resolution": "dry_run_ready",
                    "needs_split": True,
                    "message": "ready",
                },
                {
                    "order_id": "4607350",
                    "success": False,
                    "resolution": None,
                    "needs_split": False,
                    "message": "CRM authentication failed: Not authenticated.",
                },
                {
                    "order_id": "4609999",
                    "success": True,
                    "resolution": "skipped_no_split_needed",
                    "needs_split": False,
                    "message": "already okay",
                },
            ],
        }
        live_payload = {
            "success": True,
            "message": "Product Separator completed order 4609102.",
            "action": "product_separator_order",
            "dry_run": False,
            "target_order_id": "4609102",
            "order_ids": ["4609102"],
            "resolution": "split_complete",
            "duration_seconds": 12.0,
        }
        mock_run_script.side_effect = [
            (False, preflight_payload["message"], preflight_payload),
            (True, live_payload["message"], live_payload),
        ]

        ok, message, payload = server._execute_crm_product_separator_worker(
            dry_run=False,
            list_mode="rush",
            parallel_workers=4,
        )

        self.assertFalse(ok)
        self.assertEqual(mock_run_script.call_count, 2)
        self.assertEqual(payload["dry_run"], False)
        self.assertEqual(payload["live_order_ids"], ["4609102"])
        self.assertEqual(payload["skipped_order_ids"], ["4609999"])
        self.assertEqual(payload["order_ids"], ["4609102", "4607350", "4609999"])
        self.assertEqual(payload["order_count"], 3)
        self.assertIn("1/1 live order", message)
        rows_by_order = {row["order_id"]: row for row in payload["order_results"]}
        self.assertEqual(rows_by_order["4609102"]["status"], "Separated")
        self.assertEqual(rows_by_order["4607350"]["status"], "Needs attention")
        self.assertEqual(rows_by_order["4609999"]["status"], "Skipped")
        self.assertEqual([row["order_id"] for row in payload["order_results"]], ["4609102", "4607350", "4609999"])

    @mock.patch.object(server, "_execute_crm_product_separator_script")
    def test_live_batch_attention_summary_labels_preflight_manual_review(self, mock_run_script):
        preflight_payload = {
            "success": False,
            "message": (
                "Product Separator list dry run complete. "
                "1 order(s) need splitting, 0 already okay, 1 require manual review."
            ),
            "action": "product_separator_list",
            "dry_run": True,
            "list_mode": "rush",
            "parallel_workers": 4,
            "order_ids": ["4609102", "4607350"],
            "split_order_ids": ["4609102"],
            "skipped_order_ids": [],
            "manual_review_order_ids": ["4607350"],
            "failed_order_ids": [],
            "report": [
                {
                    "order_id": "4609102",
                    "success": True,
                    "resolution": "dry_run_ready",
                    "needs_split": True,
                    "message": "ready",
                },
                {
                    "order_id": "4607350",
                    "success": False,
                    "resolution": "manual_review",
                    "needs_split": False,
                    "manual_review_required": True,
                    "message": "missing Manual Order row could not be reconciled",
                },
            ],
        }
        live_payload = {
            "success": False,
            "message": "Product Separator verification failed for order 4609102.",
            "action": "product_separator_order",
            "dry_run": False,
            "target_order_id": "4609102",
            "order_ids": ["4609102"],
            "resolution": "split_verification_failed",
            "duration_seconds": 12.0,
        }
        mock_run_script.side_effect = [
            (False, preflight_payload["message"], preflight_payload),
            (False, live_payload["message"], live_payload),
        ]

        ok, message, payload = server._execute_crm_product_separator_worker(
            dry_run=False,
            list_mode="rush",
            parallel_workers=4,
        )

        self.assertFalse(ok)
        self.assertIn("1 order(s) require manual review and 1 order(s) need attention", message)
        self.assertIn("manual review 4607350", message)
        self.assertIn("live 4609102", message)
        rows_by_order = {row["order_id"]: row for row in payload["order_results"]}
        self.assertEqual(rows_by_order["4607350"]["status"], "Manual review")
        self.assertTrue(rows_by_order["4607350"]["manual_review_required"])

    def test_product_separator_history_preserves_stock_ordered_skipped_status(self):
        rows = server._build_crm_product_separator_order_results(
            {
                "success": True,
                "message": "Product Separator completed order 4609102.",
                "target_order_id": "4609102",
                "resolution": "split_complete",
                "report": {
                    "stock_status_apply": {
                        "status_applied": False,
                        "skipped": True,
                        "reason": "Order Stock Status is Need To Order; do not apply Stock Ordered automatically.",
                        "order_stock_status_before_split": "Need To Order",
                    },
                },
            }
        )

        self.assertEqual(rows[0]["stock_ordered_status"]["label"], "Stock Ordered not applied")
        self.assertIn("Need To Order", rows[0]["stock_ordered_status"]["reason"])

    def test_product_separator_history_preserves_stock_ordered_applied_status(self):
        rows = server._build_crm_product_separator_order_results(
            {
                "success": True,
                "message": "Product Separator completed order 4609102.",
                "target_order_id": "4609102",
                "resolution": "split_complete",
                "report": {
                    "stock_status_apply": {
                        "status_applied": True,
                        "confirmation": "header",
                        "order_stock_status_before_split": "Stock Ordered",
                    },
                },
            }
        )

        self.assertEqual(rows[0]["stock_ordered_status"]["label"], "Stock Ordered applied")
        self.assertTrue(rows[0]["stock_ordered_status"]["applied"])

    def test_product_separator_history_treats_already_stock_ordered_as_applied(self):
        rows = server._build_crm_product_separator_order_results(
            {
                "success": True,
                "message": "Product Separator completed order 4609102.",
                "target_order_id": "4609102",
                "resolution": "split_complete",
                "report": {
                    "stock_status_apply": {
                        "status_applied": False,
                        "already_applied": True,
                        "confirmation": "header",
                    },
                },
            }
        )

        self.assertEqual(rows[0]["stock_ordered_status"]["label"], "Stock Ordered already applied")
        self.assertTrue(rows[0]["stock_ordered_status"]["applied"])

    @mock.patch.object(server, "_execute_crm_product_separator_script")
    def test_live_batch_retries_transient_live_order_failure(self, mock_run_script):
        preflight_payload = {
            "success": True,
            "message": "Product Separator list dry run complete. 1 order(s) need splitting, 0 already okay.",
            "action": "product_separator_list",
            "dry_run": True,
            "list_mode": "rush",
            "parallel_workers": 4,
            "order_ids": ["4607567"],
            "split_order_ids": ["4607567"],
            "report": [
                {
                    "order_id": "4607567",
                    "success": True,
                    "resolution": "dry_run_ready",
                    "needs_split": True,
                    "message": "ready",
                },
            ],
        }
        retryable_payload = {
            "success": False,
            "message": "Product Separator failed for order 4607567: CRM app did not become ready.",
            "target_order_id": "4607567",
        }
        live_payload = {
            "success": True,
            "message": "Product Separator completed order 4607567.",
            "target_order_id": "4607567",
            "order_ids": ["4607567"],
            "resolution": "split_complete",
        }
        mock_run_script.side_effect = [
            (True, preflight_payload["message"], preflight_payload),
            (False, retryable_payload["message"], retryable_payload),
            (True, live_payload["message"], live_payload),
        ]

        ok, _message, payload = server._execute_crm_product_separator_worker(
            dry_run=False,
            list_mode="rush",
            parallel_workers=4,
        )

        self.assertTrue(ok)
        self.assertEqual(mock_run_script.call_count, 3)
        self.assertEqual(payload["live_order_ids"], ["4607567"])
        rows_by_order = {row["order_id"]: row for row in payload["order_results"]}
        self.assertEqual(rows_by_order["4607567"]["status"], "Separated")

    def test_server_recovers_partial_auto_splitter_order_ids(self):
        payload = {
            "success": False,
            "dry_run": False,
            "target_order_id": "4536106",
            "expected_tab_count": 12,
            "divisions": 2,
            "new_order_ids": ["4536164"],
            "report": {
                "partial": True,
                "split_orders": [{"order_id": "4536164"}, {"order_id": "4536167"}],
            },
        }

        self.assertTrue(server._crm_auto_splitter_recovery_payload_is_usable(payload, "4536106", 12, 2))
        self.assertEqual(server._crm_auto_splitter_recovery_order_ids_from_payload(payload), ["4536164", "4536167"])

    @mock.patch.object(crm_auto_splitter.time, "sleep", return_value=None)
    @mock.patch.object(crm_auto_splitter.time, "monotonic", side_effect=[0, 1, 2, 3, 4])
    @mock.patch.object(crm_auto_splitter, "_click_ng_button", return_value=True)
    def test_auto_splitter_order_save_waits_for_edit_order_button(self, _mock_click, _mock_monotonic, _mock_sleep):
        visible_states = [
            {"editOrderVisible": False, "saveOrderVisible": True, "saveOrderEnabled": False},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
        ]
        with mock.patch.object(crm_auto_splitter, "_order_scope", side_effect=Exception("scope busy")):
            with mock.patch.object(crm_auto_splitter, "_visible_order_save_state", side_effect=visible_states):
                result = crm_auto_splitter._save_order_and_wait(mock.Mock())

        self.assertTrue(result["editOrderVisible"])

    @mock.patch.object(crm_auto_splitter.time, "sleep", return_value=None)
    @mock.patch.object(crm_auto_splitter.time, "monotonic", side_effect=[0, 1, 2, 3, 4])
    @mock.patch.object(crm_auto_splitter, "_click_ng_button", return_value=True)
    def test_auto_splitter_order_save_does_not_finish_while_save_button_remains_enabled(self, _mock_click, _mock_monotonic, _mock_sleep):
        scope_state = {"saving": False, "editMode": False, "id": "4544103"}
        visible_states = [
            {"editOrderVisible": False, "saveOrderVisible": True, "saveOrderEnabled": True},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
        ]
        with mock.patch.object(crm_auto_splitter, "_order_scope", return_value=scope_state):
            with mock.patch.object(crm_auto_splitter, "_visible_order_save_state", side_effect=visible_states) as mock_visible:
                result = crm_auto_splitter._save_order_and_wait(mock.Mock())

        self.assertEqual(result["id"], "4544103")
        self.assertEqual(mock_visible.call_count, 3)

    @mock.patch.object(crm_auto_splitter, "_copy_quote_timeout_seconds", return_value=10)
    @mock.patch.object(crm_auto_splitter.time, "sleep", return_value=None)
    @mock.patch.object(crm_auto_splitter.time, "monotonic", side_effect=[0, 1])
    def test_auto_splitter_copy_order_clears_copied_quote_art_notes(self, _mock_monotonic, _mock_sleep, _mock_timeout):
        driver = mock.Mock()
        quote_state = {"quote_id": 123, "order_id": "4776969", "design_count": 4, "design_ids": [1, 2, 3, 4]}
        clear_result = {"artNotes": "", "addArtNotes": "", "artNoteOptions": "similar"}

        with mock.patch.object(crm_auto_splitter, "_wait_for_order_scope") as mock_wait_order:
            with mock.patch.object(crm_auto_splitter, "_order_scope") as mock_order_scope:
                with mock.patch.object(crm_auto_splitter, "_activate_crm_context"):
                    with mock.patch.object(crm_auto_splitter, "_wait_for_quote_scope", return_value=quote_state):
                        with mock.patch.object(crm_auto_splitter, "_clear_copied_quote_art_notes", return_value=clear_result) as mock_clear:
                            result = crm_auto_splitter._copy_order_to_quote(driver, "4776969", 4)

        mock_wait_order.assert_called_once_with(driver, order_id="4776969")
        mock_order_scope.assert_called_once()
        mock_clear.assert_called_once_with(driver)
        self.assertEqual(result["art_notes_clear"], clear_result)

    @mock.patch.object(server, "save_crm_state")
    @mock.patch.object(server, "load_crm_state")
    def test_auto_splitter_persists_into_own_history(self, mock_load_state, _mock_save_state):
        mock_load_state.return_value = {
            "last_run_timestamp": None,
            "last_run_success": None,
            "last_run_message": None,
            "last_order_count": 0,
            "total_runs": 0,
            "total_orders_processed": 0,
            "last_order_ids": [],
            "run_history": [],
            "auto_splitter_run_history": [],
        }

        state = server._persist_crm_auto_splitter_run_result(
            True,
            "Auto-split complete.",
            {
                "success": True,
                "dry_run": False,
                "target_order_id": "4536106",
                "new_order_ids": ["4536164", "4536167"],
                "expected_tab_count": 12,
                "divisions": 2,
                "duration_seconds": 63.9,
                "report": {"parallel_workers": 2},
            },
            dry_run=False,
        )

        self.assertEqual(state["run_history"], [])
        self.assertEqual(state["auto_splitter_run_history"][0]["automation_key"], "auto_splitter")
        self.assertEqual(state["auto_splitter_run_history"][0]["order_ids"], ["4536106", "4536164", "4536167"])
        self.assertEqual(state["auto_splitter_run_history"][0]["expected_tab_count"], 12)

    def test_stock_history_migrates_auto_splitter_entries_out(self):
        saved_state = {
            "run_history": [
                {
                    "timestamp": "2026-05-17T10:00:00",
                    "automation_key": "auto_splitter",
                    "automation_label": "Auto Splitter",
                    "success": True,
                    "order_ids": ["4536106", "4536164"],
                    "message": "split",
                },
                {
                    "timestamp": "2026-05-17T10:01:00",
                    "automation_key": "stock_unlocker",
                    "automation_label": "Stock Unlocker",
                    "success": True,
                    "order_ids": ["4537000"],
                    "message": "unlock",
                },
            ]
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(saved_state, handle)
            state_path = handle.name
        try:
            with mock.patch.object(server, "CRM_STATE_FILE", state_path):
                state = server.load_crm_state()
        finally:
            Path(state_path).unlink(missing_ok=True)

        self.assertEqual([row["automation_key"] for row in state["run_history"]], ["stock_unlocker"])
        self.assertEqual([row["automation_key"] for row in state["auto_splitter_run_history"]], ["auto_splitter"])

    @mock.patch.object(server, "_finish_crm_auto_splitter_runtime")
    @mock.patch.object(server, "_persist_crm_auto_splitter_run_result")
    @mock.patch.object(server, "_execute_crm_auto_splitter_worker")
    def test_auto_splitter_live_runs_dry_preflight_first(self, mock_execute, mock_persist, _mock_finish):
        mock_execute.side_effect = [
            (True, "dry ok", {"success": True, "dry_run": True, "expected_tab_count": 12, "divisions": 2}),
            (True, "live ok", {"success": True, "dry_run": False, "expected_tab_count": 12, "divisions": 2}),
        ]

        server._crm_auto_splitter_run_thread("4536106", 12, 2, dry_run=False, parallel_workers=2)

        self.assertEqual(mock_execute.call_count, 2)
        self.assertTrue(mock_execute.call_args_list[0].kwargs["dry_run"])
        self.assertFalse(mock_execute.call_args_list[0].kwargs["show_terminal"])
        self.assertFalse(mock_execute.call_args_list[1].kwargs["dry_run"])
        self.assertTrue(mock_persist.call_args.args[0])

    @mock.patch.object(server, "_finish_crm_auto_splitter_runtime")
    @mock.patch.object(server, "_persist_crm_auto_splitter_run_result")
    @mock.patch.object(server, "_execute_crm_auto_splitter_worker")
    def test_auto_splitter_live_uses_preflight_computed_counts(self, mock_execute, mock_persist, _mock_finish):
        mock_execute.side_effect = [
            (True, "dry ok", {"success": True, "dry_run": True, "expected_tab_count": 21, "divisions": 3}),
            (True, "live ok", {"success": True, "dry_run": False, "expected_tab_count": 21, "divisions": 3}),
        ]

        server._crm_auto_splitter_run_thread("4536106", None, None, dry_run=False, parallel_workers=3)

        self.assertEqual(mock_execute.call_count, 2)
        self.assertEqual(mock_execute.call_args_list[0].args[1:3], (None, None))
        self.assertEqual(mock_execute.call_args_list[1].args[1:3], (21, 3))
        self.assertTrue(mock_persist.call_args.args[0])

    @mock.patch.object(server, "_finish_crm_auto_splitter_runtime")
    @mock.patch.object(server, "_persist_crm_auto_splitter_run_result")
    @mock.patch.object(server, "_execute_crm_auto_splitter_worker")
    def test_auto_splitter_live_stops_when_dry_preflight_fails(self, mock_execute, mock_persist, _mock_finish):
        mock_execute.return_value = (False, "tab count mismatch", {"success": False, "dry_run": True, "message": "tab count mismatch"})

        server._crm_auto_splitter_run_thread("4536106", 12, 2, dry_run=False, parallel_workers=2)

        self.assertEqual(mock_execute.call_count, 1)
        self.assertFalse(mock_persist.call_args.args[0])
        self.assertIn("dry run failed", mock_persist.call_args.args[1].lower())


class CrmAddressBatchWorkerTests(unittest.TestCase):
    def test_rush_po_box_shipping_issue_saves_exact_note_then_applies_issue(self):
        driver = mock.Mock()
        shipping_modal = object()
        calls = []

        with mock.patch.object(
            crm_validate_address,
            "_reload_order_for_shipping_issue",
            side_effect=lambda *args, **kwargs: calls.append("reload"),
        ), mock.patch.object(
            crm_validate_address,
            "_add_shipping_issue_sales_note",
            side_effect=lambda *args, **kwargs: calls.append(("note", args[1], kwargs["dry_run"])) or {"updated": True},
        ), mock.patch.object(
            crm_validate_address,
            "_apply_order_status",
            side_effect=lambda *args, **kwargs: calls.append(("status", args[1], kwargs["dry_run"])) or {"status_applied": True},
        ):
            result = crm_validate_address._handle_shipping_issue(
                driver,
                shipping_modal,
                "4885010",
                crm_validate_address.RUSH_PO_BOX_SALES_NOTE,
                "po_box_rush",
                dry_run=False,
            )

        self.assertEqual(
            calls,
            [
                "reload",
                (
                    "note",
                    "Cannot use PO Box for rush orders. USPS cannot guarantee delivery time\nNeed physical address",
                    False,
                ),
                ("status", "Issue - Shipping", False),
            ],
        )
        self.assertTrue(result["success"])
        self.assertFalse(result["manual_review_required"])
        self.assertEqual(result["outcome"], "po_box_rush_shipping_issue_applied")

    def test_missing_street_number_shipping_issue_dry_run_previews_note_and_issue(self):
        driver = mock.Mock()
        shipping_modal = object()
        calls = []

        with mock.patch.object(
            crm_validate_address,
            "_reload_order_for_shipping_issue",
            side_effect=lambda *args, **kwargs: calls.append("reload"),
        ), mock.patch.object(
            crm_validate_address,
            "_add_shipping_issue_sales_note",
            side_effect=lambda *args, **kwargs: calls.append(("note", args[1], kwargs["dry_run"])) or {"dry_run": True},
        ), mock.patch.object(
            crm_validate_address,
            "_apply_order_status",
            side_effect=lambda *args, **kwargs: calls.append(("status", args[1], kwargs["dry_run"])) or {"dry_run": True},
        ):
            result = crm_validate_address._handle_shipping_issue(
                driver,
                shipping_modal,
                "4885365",
                crm_validate_address.MISSING_STREET_NUMBER_SALES_NOTE,
                "missing_street_number",
                dry_run=True,
            )

        self.assertEqual(
            calls,
            [
                "reload",
                ("note", "Incomplete shipping address", True),
                ("status", "Issue - Shipping", True),
            ],
        )
        self.assertTrue(result["success"])
        self.assertFalse(result["manual_review_required"])
        self.assertEqual(result["outcome"], "missing_street_number_shipping_issue_ready")

    def test_address_timeout_reloads_order_once_then_retries(self):
        driver = mock.Mock()
        recovered = {
            "order_id": "4845038",
            "success": True,
            "message": "Recovered.",
            "manual_review_required": False,
            "resolution": "validated",
        }
        with mock.patch.object(
            crm_validate_address,
            "_evaluate_and_resolve_order",
            side_effect=[crm_validate_address.TimeoutException("validator timed out"), recovered],
        ) as evaluate, mock.patch.object(crm_validate_address, "safe_get_with_partial_load") as safe_get, \
             mock.patch.object(crm_validate_address, "login_if_needed", return_value=False), \
             mock.patch.object(crm_validate_address, "_elapsed_seconds", return_value=1.0):
            payload = crm_validate_address._run_once_with_driver(driver, order_id="4845038")

        self.assertTrue(payload["success"])
        self.assertTrue(payload["report"][0]["retried_after_timeout"])
        self.assertEqual(evaluate.call_count, 2)
        safe_get.assert_called_once()

    @mock.patch.object(crm_validate_address.time, "sleep")
    @mock.patch.object(crm_validate_address, "_wait_for_target_order_open")
    @mock.patch.object(crm_validate_address, "login_if_needed", return_value=False)
    @mock.patch.object(crm_validate_address, "safe_get_with_partial_load")
    def test_open_target_order_refreshes_once_after_timeout(
        self,
        mock_safe_get,
        _mock_login,
        mock_wait_open,
        _mock_sleep,
    ):
        driver = mock.Mock()
        driver.window_handles = ["main"]
        driver.current_window_handle = "main"
        mock_wait_open.side_effect = [False, True]

        result = crm_validate_address._open_target_order(driver, "4795590", shipping_filter="rush")

        self.assertEqual(result, "4795590")
        mock_safe_get.assert_called_once()
        driver.refresh.assert_called_once()
        self.assertEqual(mock_wait_open.call_count, 2)

    @mock.patch.object(crm_validate_address.time, "sleep")
    @mock.patch.object(crm_validate_address, "_wait_for_target_order_open", return_value=False)
    @mock.patch.object(crm_validate_address, "login_if_needed", return_value=False)
    @mock.patch.object(crm_validate_address, "safe_get_with_partial_load")
    def test_open_target_order_fails_after_refresh_retry_times_out(
        self,
        _mock_safe_get,
        _mock_login,
        mock_wait_open,
        _mock_sleep,
    ):
        driver = mock.Mock()
        driver.window_handles = ["main"]
        driver.current_window_handle = "main"

        with self.assertRaisesRegex(crm_validate_address.TimeoutException, "Order 4795590 did not open"):
            crm_validate_address._open_target_order(driver, "4795590", shipping_filter="rush")

        driver.refresh.assert_called_once()
        self.assertEqual(mock_wait_open.call_count, 2)

    def test_crm_attempt_modes_skip_visible_fallback_when_disabled(self):
        with mock.patch.object(crm_validate_address, "CRM_HEADLESS", True):
            with mock.patch.object(crm_validate_address, "CRM_ALLOW_VISIBLE_FALLBACK", False):
                self.assertEqual(crm_validate_address._crm_attempt_modes(), [True])
            with mock.patch.object(crm_validate_address, "CRM_ALLOW_VISIBLE_FALLBACK", True):
                self.assertEqual(crm_validate_address._crm_attempt_modes(), [True, False])

    def test_profile_clone_ignore_skips_component_payload_dirs(self):
        ignored = crm_validate_address._profile_clone_ignore(
            "unused",
            ["ActorSafetyLists", "Cache", "Cookies", "SingletonLock", "notes.txt"],
        )

        self.assertEqual(ignored, {"ActorSafetyLists", "Cache", "SingletonLock"})

    def test_shipping_list_row_classifier_includes_lime_green(self):
        self.assertEqual(
            crm_validate_address._classify_shipping_list_row_color((34, 236, 72)),
            "lime_green",
        )
        self.assertIn("lime_green", crm_validate_address.ALLOWED_SHIPPING_LIST_ROW_LABELS)

    def test_shipping_list_row_classifier_includes_purple_variants(self):
        for rgb in ((156, 31, 188), (128, 43, 181), (112, 36, 174)):
            with self.subTest(rgb=rgb):
                self.assertEqual(
                    crm_validate_address._classify_shipping_list_row_color(rgb),
                    "purple",
                )
        self.assertIn("purple", crm_validate_address.ALLOWED_SHIPPING_LIST_ROW_LABELS)
        self.assertIn("purple", crm_validate_address.ALLOWED_813_ORDER_GOODS_ROW_LABELS)

    def test_shipping_list_row_classifier_uses_purple_marker(self):
        details = {
            "top": 0,
            "left": 0,
            "width": 100,
            "height": 20,
            "colors": [
                {
                    "backgroundColor": "rgba(0, 0, 0, 0)",
                    "tag": "TR",
                    "id": "",
                    "className": "scheduled-purple-order",
                }
            ],
        }

        row = crm_validate_address._describe_shipping_list_order_row_from_details(details)

        self.assertEqual(row["label"], "purple")

    def test_shipping_list_order_selection_accepts_purple_for_default_and_813_rows(self):
        driver = mock.Mock()
        purple_link = mock.Mock()
        tan_link = mock.Mock()
        driver.find_elements.return_value = [purple_link, tan_link]
        row_details = [
            {
                "displayed": True,
                "text": "4681259 | Lite Mach 6 Manufacturing",
                "label": "purple",
                "rgb": (156, 31, 188),
                "rect": (0, 0, 100, 20),
                "element": {},
            },
            {
                "displayed": True,
                "text": "4680186 | Lite",
                "label": "tan",
                "rgb": (245, 202, 153),
                "rect": (20, 0, 100, 20),
                "element": {},
            },
        ]

        with mock.patch.object(crm_validate_address, "_describe_shipping_list_order_rows", return_value=row_details):
            default_rows = crm_validate_address._find_shipping_list_orders(
                driver,
                limit=5,
                timeout=0.01,
                allowed_row_labels=crm_validate_address.ALLOWED_SHIPPING_LIST_ROW_LABELS,
            )
            rows_813 = crm_validate_address._find_shipping_list_orders(
                driver,
                limit=5,
                timeout=0.01,
                allowed_row_labels=crm_validate_address.ALLOWED_813_ORDER_GOODS_ROW_LABELS,
            )

        self.assertEqual([row["order_id"] for row in default_rows], ["4681259", "4680186"])
        self.assertEqual([row["order_id"] for row in rows_813], ["4681259"])

    def test_state_route_with_house_number_allows_no_candidates_override(self):
        address = {
            "address": "294 NJ-36",
            "city": "West Long Branch",
            "state": "New Jersey",
            "zip": "07764",
        }

        self.assertTrue(crm_validate_address._is_highway_address(address["address"]))
        self.assertTrue(crm_validate_address._allow_override_after_no_candidates(address))

    def test_us_highway_without_house_number_is_not_missing_street_number(self):
        address = {
            "address": "US Highway 19",
            "city": "Chiefland",
            "state": "Florida",
            "zip": "32626",
        }

        self.assertTrue(crm_validate_address._is_highway_address(address["address"]))
        self.assertFalse(crm_validate_address._is_missing_street_number(address["address"]))
        self.assertTrue(crm_validate_address._allow_override_after_no_candidates(address))

    def test_abbreviated_us_route_without_house_number_is_not_missing_street_number(self):
        address = {
            "address": "U.S. Hwy 19",
            "city": "Chiefland",
            "state": "Florida",
            "zip": "32626",
        }

        self.assertTrue(crm_validate_address._is_highway_address(address["address"]))
        self.assertFalse(crm_validate_address._is_missing_street_number(address["address"]))
        self.assertTrue(crm_validate_address._allow_override_after_no_candidates(address))

    def test_letter_prefixed_rural_highway_address_has_house_number(self):
        address = {
            "address": "N8008 US-12",
            "city": "Elkhorn",
            "state": "Wisconsin",
            "zip": "53121",
        }

        self.assertEqual(crm_validate_address._house_token(address["address"]), "N8008")
        self.assertEqual(crm_validate_address._street_core(address["address"]), "US 12")
        self.assertTrue(crm_validate_address._is_highway_address(address["address"]))
        self.assertFalse(crm_validate_address._is_missing_street_number(address["address"]))
        self.assertTrue(crm_validate_address._looks_like_street_portion(address["address"]))

    def test_email_like_shipping_address_is_not_treated_as_street(self):
        address = {
            "address": "customer123@example.com",
            "city": "Philadelphia",
            "state": "Pennsylvania",
            "zip": "19107",
        }

        self.assertTrue(crm_validate_address._address_fields_contain_email(address))
        self.assertTrue(crm_validate_address._is_missing_street_number(address["address"]))
        self.assertFalse(crm_validate_address._looks_like_street_portion(address["address"]))
        self.assertFalse(crm_validate_address._looks_clearly_valid_for_override(address))
        self.assertFalse(crm_validate_address._allow_override_after_no_candidates(address))

    def test_email_in_shipping_address_is_manual_review_before_validation(self):
        driver = object()
        shipping_modal = object()
        address = {
            "recipient": "Example Recipient",
            "address": "customer@example.com",
            "address_cont": "",
            "city": "Philadelphia",
            "state": "Pennsylvania",
            "zip": "19107",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_target_order", return_value="4772305"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_shipping_panel_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_shipping_editor", return_value=shipping_modal))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))))
            mock_collect = stack.enter_context(mock.patch.object(crm_validate_address, "_collect_existing_address_options"))

            result = crm_validate_address._evaluate_and_resolve_order(
                driver,
                order_id="4772305",
                dry_run=False,
                shipping_filter="rush",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "email_in_shipping_address")
        self.assertTrue(result["manual_review_required"])
        mock_collect.assert_not_called()

    def test_already_valid_email_shipping_address_is_manual_review(self):
        driver = object()
        address = {
            "recipient": "Example Recipient",
            "address": "customer@example.com",
            "address_cont": "",
            "city": "Philadelphia",
            "state": "Pennsylvania",
            "zip": "19107",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_target_order", return_value="4772305"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_shipping_panel_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=True))
            mock_open_editor = stack.enter_context(mock.patch.object(crm_validate_address, "_open_shipping_editor"))

            result = crm_validate_address._evaluate_and_resolve_order(
                driver,
                order_id="4772305",
                dry_run=False,
                shipping_filter="rush",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "email_in_shipping_address")
        self.assertTrue(result["manual_review_required"])
        mock_open_editor.assert_not_called()

    def test_shipping_address_caps_normalization_ignores_address_cont(self):
        mixed_case = {
            "address": "2000 Oak Creek Rd",
            "address_cont": "Apt 228",
            "city": "River Ridge",
            "state": "Louisiana",
            "zip": "70123",
        }
        normalized = {
            "address": "2000 OAK CREEK RD",
            "address_cont": "Apt 228",
            "city": "NEW ORLEANS",
            "state": "Louisiana",
            "zip": "70123-5683",
        }

        self.assertTrue(crm_validate_address._shipping_address_needs_caps_normalization(mixed_case))
        self.assertFalse(crm_validate_address._shipping_address_needs_caps_normalization(normalized))

    def test_assess_address_text_accepts_directional_state_code_and_city_difference(self):
        address = {
            "address": "717 S Main GQ St",
            "city": "Salisbury",
            "state": "North Carolina",
            "zip": "28146",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "717 S MAIN GQ ST GRANITE QUARRY NC 28146-9133",
        )

        self.assertTrue(assessment["required_match"])
        self.assertTrue(assessment["city_only_mismatch"])
        self.assertFalse(assessment["postal_full_match"])
        self.assertFalse(assessment["city_match"])

    def test_assess_address_text_accepts_canadian_postal_prefix_match(self):
        address = {
            "address": "3588 Overlander Dr",
            "city": "Kamloops",
            "state": "British Columbia",
            "zip": "V2B 6T6",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "3588 OVERLANDER DR KAMLOOPS BC V2B 6Y1",
        )

        self.assertTrue(assessment["street_match"])
        self.assertTrue(assessment["city_match"])
        self.assertTrue(assessment["state_match"])
        self.assertTrue(assessment["safe_postal_prefix_match"])
        self.assertTrue(assessment["canadian_postal_prefix_match"])
        self.assertTrue(crm_validate_address._assessment_can_be_used(assessment))

    def test_dedupe_address_identifier_moves_box_into_address_cont_when_hash_present(self):
        address, address_cont, extracted = crm_validate_address._dedupe_address_identifier(
            "12345 W Example Route 66 Box 965",
            "#965",
        )

        self.assertEqual(address, "12345 W EXAMPLE ROUTE 66")
        self.assertEqual(address_cont, "BOX 965 #965")
        self.assertEqual(extracted, "BOX 965")

    def test_dedupe_address_identifier_moves_embedded_po_box_into_address_cont(self):
        address, address_cont, extracted = crm_validate_address._dedupe_address_identifier(
            "213 North High St., PO Box 176",
            "",
        )

        self.assertEqual(address, "213 North High St")
        self.assertEqual(address_cont, "PO Box 176")
        self.assertEqual(extracted, "PO Box 176")

    def test_classify_po_box_address_treats_embedded_po_box_as_mixed_street_address(self):
        profile = crm_validate_address._classify_po_box_address(
            {
                "address": "213 North High St., PO Box 176",
                "address_cont": "",
            }
        )

        self.assertTrue(profile["mixed_po_box_and_street"])
        self.assertTrue(profile["needs_embedded_po_box_split"])
        self.assertFalse(profile["po_box_only"])
        self.assertEqual(profile["street_line"], "213 North High St")
        self.assertEqual(profile["po_box_line"], "PO Box 176")

    def test_classify_po_box_address_treats_bare_embedded_box_as_mixed_street_address(self):
        profile = crm_validate_address._classify_po_box_address(
            {
                "address": "105 main street box 233",
                "address_cont": "",
            }
        )

        self.assertTrue(profile["mixed_po_box_and_street"])
        self.assertTrue(profile["needs_embedded_po_box_split"])
        self.assertFalse(profile["po_box_only"])
        self.assertEqual(profile["street_line"], "105 MAIN STREET")
        self.assertEqual(profile["po_box_line"], "BOX 233")

    def test_order_totals_shipping_class_distinguishes_free_from_priced_shipping(self):
        self.assertEqual(
            crm_validate_address._order_totals_shipping_class_from_text(
                "Order Totals Design 1 Total: $69.76 Shipping: info Free Grand Total: $147.20"
            ),
            "free",
        )
        self.assertEqual(
            crm_validate_address._order_totals_shipping_class_from_text(
                "Order Totals Design 1 Total: $71.80 Shipping: $37.99 Grand Total: $109.79"
            ),
            "rush",
        )
        self.assertEqual(
            crm_validate_address._order_totals_shipping_class_from_text(
                "Order Totals Design 1 Total: $71.80 Shipping: $25.00 Grand Total: $96.80"
            ),
            "international_standard",
        )
        self.assertEqual(
            crm_validate_address._order_totals_shipping_class_from_text(
                "Order Totals Design 1 Total: $71.80 Shipping: $25.01 Grand Total: $96.81"
            ),
            "rush",
        )

    def test_po_box_policy_allows_exact_international_standard_rate_even_from_rush_list(self):
        warnings = []

        policy = crm_validate_address._po_box_shipping_policy_filter(
            mock.Mock(),
            "rush",
            warnings,
            detected_shipping_class="international_standard",
        )

        self.assertEqual(policy, "free")
        self.assertTrue(any("$25 standard international/military" in warning for warning in warnings))

    def test_po_box_policy_treats_shipping_above_standard_international_rate_as_rush(self):
        warnings = []

        policy = crm_validate_address._po_box_shipping_policy_filter(
            mock.Mock(),
            "free",
            warnings,
            detected_shipping_class="rush",
        )

        self.assertEqual(policy, "rush")
        self.assertTrue(any("rush" in warning.lower() for warning in warnings))

    def test_effective_address_cont_ignores_locality_overflow(self):
        address_fields = {
            "address": "123 West Example Street, Apt. 2D",
            "address_cont": "Example City, NY 10034",
            "city": "New York (Manhattan)",
            "state": "New York",
            "zip": "10034",
        }

        self.assertEqual(crm_validate_address._effective_address_cont(address_fields), "")

        address, address_cont, extracted = crm_validate_address._dedupe_address_identifier(
            address_fields["address"],
            crm_validate_address._effective_address_cont(address_fields),
        )

        self.assertEqual(address, "123 WEST EXAMPLE STREET")
        self.assertEqual(address_cont, "APT. 2D")
        self.assertEqual(extracted, "APT. 2D")

    def test_normalize_display_address_line_converts_leading_number_word(self):
        self.assertEqual(
            crm_validate_address._normalize_display_address_line("One Example Way"),
            "1 Example Way",
        )

    def test_format_plain_us_zip_plus4_adds_separator(self):
        self.assertEqual(
            crm_validate_address._format_plain_us_zip_plus4("440701741"),
            "44070-1741",
        )

    def test_format_plain_us_zip_plus4_leaves_other_postal_codes_unchanged(self):
        self.assertEqual(crm_validate_address._format_plain_us_zip_plus4("44070-1741"), "44070-1741")
        self.assertEqual(crm_validate_address._format_plain_us_zip_plus4("V4V 2P7"), "V4V 2P7")

    def test_normalize_plain_us_zip_plus4_rewrites_the_zip_field_before_validation(self):
        zip_field = mock.Mock()
        zip_field.get_attribute.return_value = "440701741"
        warnings = []

        with mock.patch.object(crm_validate_address, "_find_address_form_input", return_value=zip_field):
            with mock.patch.object(crm_validate_address, "_set_input_value") as mock_set:
                with mock.patch.object(crm_validate_address.time, "sleep"):
                    changed = crm_validate_address._normalize_plain_us_zip_plus4(object(), warnings)

        self.assertTrue(changed)
        mock_set.assert_called_once_with(zip_field, "44070-1741")
        self.assertTrue(any("44070-1741" in warning for warning in warnings))

    def test_clean_city_field_value_strips_embedded_state_code(self):
        self.assertEqual(
            crm_validate_address._clean_city_field_value("Los Angeles, CA", "California", "90013"),
            "Los Angeles",
        )

    def test_clean_address_line_locality_suffix_removes_duplicate_city_state_zip(self):
        cleaned, removed = crm_validate_address._clean_address_line_locality_suffix(
            "580 72ND ST., Miami Beach, FL 33141",
            "Miami Beach",
            "Florida",
            "33141",
        )

        self.assertEqual(cleaned, "580 72ND ST.")
        self.assertEqual(removed, "Miami Beach, FL 33141")

    def test_clean_address_line_locality_suffix_removes_duplicate_city_state_without_zip(self):
        cleaned, removed = crm_validate_address._clean_address_line_locality_suffix(
            "1522 K St NW, Washington, DC",
            "Washington DC",
            "District Of Columbia",
            "20005",
        )

        self.assertEqual(cleaned, "1522 K St NW")
        self.assertEqual(removed, "Washington, DC")

    def test_address_cont_looks_like_street_fragment_when_main_address_is_only_number(self):
        address_fields = {
            "address": "2265",
            "address_cont": "Lake Crest Ct.",
            "city": "Martinez",
            "state": "California",
            "zip": "94553",
        }

        self.assertTrue(crm_validate_address._address_cont_looks_like_street_fragment(address_fields))

    def test_rewrite_split_street_fragment_clears_preserved_address_cont(self):
        initial = {
            "recipient": "Example Recipient",
            "address": "2265",
            "address_cont": "Lake Crest Ct.",
            "city": "Martinez",
            "state": "California",
            "zip": "94553",
        }
        fixed = {
            **initial,
            "address": "2265 Lake Crest Ct.",
            "address_cont": "",
        }
        address_field = object()
        address_cont_field = object()
        city_field = object()

        warnings = []
        with mock.patch.object(crm_validate_address, "_extract_current_address", side_effect=[dict(initial), dict(fixed)]):
            with mock.patch.object(
                crm_validate_address,
                "_find_address_form_input",
                side_effect=[address_field, address_cont_field, city_field],
            ):
                with mock.patch.object(crm_validate_address, "_set_input_value") as mock_set:
                    with mock.patch.object(crm_validate_address.time, "sleep"):
                        current, preserved = crm_validate_address._rewrite_address_fields_if_needed(
                            object(),
                            warnings,
                            preserved_address_cont="Lake Crest Ct.",
                        )

        self.assertEqual(current["address"], "2265 Lake Crest Ct.")
        self.assertEqual(current["address_cont"], "")
        self.assertEqual(preserved, "")
        mock_set.assert_any_call(address_field, "2265 Lake Crest Ct.")
        mock_set.assert_any_call(address_cont_field, "")
        self.assertTrue(any("main address only contained the house number" in item for item in warnings))

    def test_recover_misaligned_street_address_moves_street_from_address_cont(self):
        recovered = crm_validate_address._recover_misaligned_street_address(
            {
                "address": "Hyatt Place Washington DC/White Hou",
                "address_cont": "1522 K St NW, Washington, DC",
                "city": "Washington DC",
                "state": "District Of Columbia",
                "zip": "20005",
            }
        )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["source_field"], "address_cont")
        self.assertEqual(recovered["address"], "1522 K St NW")
        self.assertEqual(recovered["address_cont"], "Hyatt Place Washington DC/White Hou")
        self.assertEqual(recovered["removed_locality_suffix"], "Washington, DC")

    def test_recover_misaligned_street_address_extracts_embedded_street_from_address(self):
        recovered = crm_validate_address._recover_misaligned_street_address(
            {
                "address": "Main Street Events - Hyatt Place Washington DC/White Hou 1522 K St NW, Washington, DC",
                "address_cont": "",
                "city": "Washington DC",
                "state": "District Of Columbia",
                "zip": "20005",
            }
        )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["source_field"], "address")
        self.assertEqual(recovered["address"], "1522 K St NW")
        self.assertEqual(recovered["address_cont"], "Main Street Events - Hyatt Place Washington DC/White Hou")
        self.assertEqual(recovered["removed_locality_suffix"], "Washington, DC")

    def test_pick_existing_address_prefers_matching_address_cont(self):
        address = {
            "address": "123 West Example Street",
            "address_cont": "Apt. 2D",
            "city": "New York",
            "state": "New York",
            "zip": "10034",
        }
        options = [
            {"text": "Example Resident - 123 W Example St Apt 3D New York NY 10034", "preferred_all_caps": True},
            {"text": "Example Resident - 123 W Example St Apt 2D New York NY 10034", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Example Resident - 123 W Example St Apt 2D New York NY 10034")
        self.assertTrue(best["assessment"]["secondary_match"])

    def test_pick_existing_address_preserves_bare_canadian_address_cont(self):
        address = {
            "address": "42 Grenfell Dr",
            "address_cont": "218",
            "city": "Wabush",
            "state": "Newfoundland And Labrador",
            "zip": "A0R 1B0",
        }
        options = [
            {"text": "Megan Smith - 42 Grenfell Dr Wabush NL, A0R 1B0", "preferred_all_caps": False},
            {"text": "Megan Smith - 42 Grenfell Dr 218 Wabush NL, A0R 1B0", "preferred_all_caps": False},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Megan Smith - 42 Grenfell Dr 218 Wabush NL, A0R 1B0")
        self.assertTrue(best["assessment"]["secondary_match"])

    def test_address_cont_preservation_accepts_punctuated_rural_route(self):
        self.assertTrue(
            crm_validate_address._address_cont_value_preserved("RR 2", "R.R. 2")
        )

    def test_pick_existing_address_preserves_punctuated_rural_route(self):
        address = {
            "address": "420 Holmes Street",
            "address_cont": "RR 2",
            "city": "Clinton",
            "state": "Ontario",
            "zip": "N0M 1L0",
        }
        options = [
            {"text": "Nancy Mayhew - 420 HOLMES STREET CENTRAL HURON ON, N0M 1L0", "preferred_all_caps": True},
            {"text": "Nancy Mayhew - 420 HOLMES STREET R.R. 2 CENTRAL HURON ON, N0M 1L0", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Nancy Mayhew - 420 HOLMES STREET R.R. 2 CENTRAL HURON ON, N0M 1L0")
        self.assertTrue(best["assessment"]["secondary_match"])

    def test_pick_existing_address_accepts_matching_apo_without_house_number(self):
        address = {
            "recipient": "Javier Almeidaluera",
            "address": "CLB31, UPR 38463, Box 162",
            "address_cont": "CLB31, UPR 38463, Box 162",
            "city": "FPO",
            "state": "Armed Forces Pacific",
            "zip": "96384-6301",
        }
        options = [
            {
                "text": "Javier Almeidaluera - CLB-31/USMC - CLB31, UPR 38463, Box 162 FPO AP, 96384-6301",
                "preferred_all_caps": False,
            },
            {
                "text": "Javier Almeidaluera - USMC/CLB-31 - CLB31, UPR 38463, Box 162 CLB31, UPR 38463, Box 162 FPO AP, 96384-6301",
                "preferred_all_caps": False,
            },
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], options[1]["text"])
        self.assertTrue(best["assessment"]["military_without_house_match"])
        self.assertTrue(best["assessment"]["required_match"])
        self.assertNotIn("address_number", best["assessment"]["mismatch_fields"])

    def test_pick_existing_address_can_rescue_missing_street_number(self):
        address = {
            "address": "Example Drive West",
            "address_cont": "",
            "city": "Orange",
            "state": "Connecticut",
            "zip": "06477",
        }
        options = [
            {"text": "Example Resident - 5111 Example Dr W Orange CT 06477", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertTrue(best["assessment"]["missing_number_rescue"])

    def test_address_cont_street_number_candidate_uses_bare_cont_number(self):
        address = {
            "address": "Frist Lane",
            "address_cont": "3061",
            "city": "Princeton",
            "state": "New Jersey",
            "zip": "08544",
        }

        self.assertEqual(
            crm_validate_address._address_cont_street_number_candidate(address),
            "3061",
        )
        address["address_cont"] = "#3061"
        self.assertEqual(
            crm_validate_address._address_cont_street_number_candidate(address),
            "3061",
        )

    def test_address_cont_street_number_candidate_rejects_unit_number(self):
        address = {
            "address": "Main Street",
            "address_cont": "Apt 4",
            "city": "Princeton",
            "state": "New Jersey",
            "zip": "08544",
        }

        self.assertEqual(crm_validate_address._address_cont_street_number_candidate(address), "")

    def test_address_cont_street_number_candidate_requires_street_name(self):
        address = {
            "address": "",
            "address_cont": "3061",
            "city": "Princeton",
            "state": "New Jersey",
            "zip": "08544",
        }

        self.assertEqual(crm_validate_address._address_cont_street_number_candidate(address), "")

    def test_pick_existing_address_handles_hyphenated_house_number_and_numbered_street(self):
        address = {
            "address": "119-15 192 Street St. Albans NY",
            "address_cont": "",
            "city": "St. Albans",
            "state": "New York",
            "zip": "11412",
        }
        options = [
            {"text": "Example Customer - 119-15 192 Street St. Albans NY 11412", "preferred_all_caps": False},
            {"text": "Example Customer - 11915 192ND ST SAINT ALBANS NY 11412-3624", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Example Customer - 11915 192ND ST SAINT ALBANS NY 11412-3624")
        self.assertTrue(best["assessment"]["required_match"])

    def test_assess_address_text_accepts_hawaii_hyphenated_house_number_and_city_alias(self):
        address = {
            "address": "61-4032 Example Dr",
            "address_cont": "",
            "city": "Waimea",
            "state": "Hawaii",
            "zip": "96743",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "61-4032 EXAMPLE DR KAMUELA HI 96743-9711",
        )

        self.assertTrue(assessment["required_match"])
        self.assertTrue(assessment["house_match"])
        self.assertTrue(assessment["street_match"])
        self.assertTrue(assessment["city_match"])

    def test_pick_existing_address_prefers_canadian_validated_postal_prefix_match(self):
        address = {
            "address": "3588 Example Dr",
            "address_cont": "",
            "city": "Kamloops",
            "state": "British Columbia",
            "zip": "V2B 6T6",
        }
        options = [
            {"text": "Example Customer - 3588 Example Dr Kamloops BC V2B 6T6", "preferred_all_caps": False},
            {"text": "Example Customer - 3588 EXAMPLE DR KAMLOOPS BC V2B 6Y1", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Example Customer - 3588 EXAMPLE DR KAMLOOPS BC V2B 6Y1")
        self.assertTrue(best["assessment"]["safe_postal_prefix_match"])
        self.assertTrue(best["assessment"]["canadian_postal_prefix_match"])

    def test_existing_address_looks_like_weak_duplicate_when_not_all_caps(self):
        best_existing = {
            "option": {
                "text": "Example Customer - 457 Example Drive Vallejo CA 94591-7126",
                "preferred_all_caps": False,
            },
            "assessment": {
                "exact_match": True,
                "city_match": True,
                "postal_full_match": True,
            },
        }

        self.assertTrue(crm_validate_address._existing_address_looks_like_weak_duplicate(best_existing))

    def test_existing_address_resolution_can_require_green_valid_state(self):
        address = {
            "recipient": "Example Recipient",
            "address": "174 Example Trail",
            "address_cont": "",
            "city": "Alpine",
            "state": "Wyoming",
            "zip": "83128",
        }
        best_existing = {
            "option": {
                "text": "Example Recipient - 174 Example Trail Alpine WY 83128",
                "preferred_all_caps": False,
            },
            "assessment": {
                "city_only_mismatch": False,
            },
        }

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))):
                with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                    with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=True):
                        with mock.patch.object(crm_validate_address, "_prepare_shipping_form_for_save") as mock_prepare:
                            result = crm_validate_address._try_resolve_with_existing_address(
                                object(),
                                object(),
                                "4389708",
                                True,
                                dict(address),
                                dict(address),
                                "",
                                [],
                                existing_options=[best_existing["option"]],
                                best_existing=best_existing,
                                accept_save_button_ready=False,
                            )

        self.assertIsNone(result)
        mock_prepare.assert_not_called()

    def test_existing_address_resolution_can_reuse_prevalidated_selection(self):
        address = {
            "recipient": "Example Recipient",
            "address": "5774 Example Rd",
            "address_cont": "",
            "city": "Livingston",
            "state": "Texas",
            "zip": "77351",
        }
        best_existing = {
            "option": {
                "text": "Example Recipient - Test Ranch Livingston - 5774 Example Rd Livingston TX, 77351",
                "preferred_all_caps": False,
            },
            "assessment": {
                "city_only_mismatch": False,
            },
        }

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))):
                with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                    with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=False):
                        with mock.patch.object(crm_validate_address, "_prepare_shipping_form_for_save", return_value=(None, dict(address), "")):
                            with mock.patch.object(crm_validate_address, "_save_shipping_transaction") as mock_save:
                                result = crm_validate_address._try_resolve_with_existing_address(
                                    object(),
                                    object(),
                                    "4391701",
                                    True,
                                    dict(address),
                                    dict(address),
                                    "",
                                    [],
                                    existing_options=[best_existing["option"]],
                                    best_existing=best_existing,
                                    accept_save_button_ready=False,
                                    allow_prevalidated_selection=True,
                                )

        self.assertIsNotNone(result)
        self.assertTrue(result["success"])
        self.assertEqual(result["resolution"], "existing_address")
        mock_save.assert_called_once()

    def test_existing_address_resolution_can_use_safe_assessed_current_address_after_no_candidates(self):
        original_address = {
            "recipient": "Example Recipient",
            "address": "2301 Example Rd",
            "address_cont": "203",
            "city": "Mansfield",
            "state": "Texas",
            "zip": "76063",
        }
        selected_address = {
            "recipient": "Example Recipient",
            "address": "2301 Example Rd",
            "address_cont": "203",
            "city": "Mansfield",
            "state": "Texas",
            "zip": "76063",
        }
        best_existing = {
            "option": {
                "text": "Example Recipient - 2301 Example Rd Mansfield TX 76063",
                "preferred_all_caps": True,
            },
            "assessment": {
                "city_only_mismatch": False,
            },
        }
        warnings = []

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(selected_address))):
                with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                    with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=False):
                        with mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(selected_address)):
                            with mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False):
                                with mock.patch.object(
                                    crm_validate_address,
                                    "_persist_validated_address_via_modal_scope",
                                    return_value={"ok": True},
                                ) as mock_persist:
                                    with mock.patch.object(crm_validate_address, "_prepare_shipping_form_for_save", return_value=(None, dict(selected_address), "203")):
                                        with mock.patch.object(crm_validate_address, "_save_shipping_transaction") as mock_save:
                                            result = crm_validate_address._try_resolve_with_existing_address(
                                                object(),
                                                object(),
                                                "4415466",
                                                False,
                                                dict(original_address),
                                                dict(original_address),
                                                "203",
                                                warnings,
                                                existing_options=[best_existing["option"]],
                                                best_existing=best_existing,
                                                accept_save_button_ready=False,
                                                allow_assessed_current_address=True,
                                            )

        self.assertIsNotNone(result)
        self.assertTrue(result["success"])
        self.assertEqual(result["resolution"], "existing_address")
        self.assertEqual(result["outcome"], "existing_address_saved")
        self.assertEqual(mock_persist.call_count, 1)
        mock_save.assert_called_once()
        self.assertTrue(any("still matched safely" in item for item in warnings))

    def test_existing_address_resolution_skips_saved_address_that_drops_address_cont(self):
        address = {
            "recipient": "Example Recipient",
            "address": "4230 Example Ct",
            "address_cont": "106",
            "city": "Greenacres",
            "state": "Florida",
            "zip": "33467",
        }
        best_existing = {
            "option": {
                "text": "Example Recipient - 4230 EXAMPLE CT LAKE WORTH FL 33467-4302",
                "preferred_all_caps": True,
            },
            "assessment": crm_validate_address._assess_existing_address_text(
                address,
                "Example Recipient - 4230 EXAMPLE CT LAKE WORTH FL 33467-4302",
            ),
        }
        warnings = []

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text") as mock_select:
            result = crm_validate_address._try_resolve_with_existing_address(
                object(),
                object(),
                "4523920",
                False,
                dict(address),
                dict(address),
                "106",
                warnings,
                existing_options=[best_existing["option"]],
                best_existing=best_existing,
            )

        self.assertIsNone(result)
        mock_select.assert_not_called()
        self.assertTrue(any("running Save & Verify Address instead" in item for item in warnings))

    def test_find_best_existing_address_option_rejects_saved_address_that_drops_embedded_box(self):
        address = {
            "recipient": "Example Recipient",
            "address": "105 main street box 233",
            "address_cont": "",
            "city": "Domremy",
            "state": "Saskatchewan",
            "zip": "S0k1g0",
        }
        options = [
            {
                "text": "Example Recipient - 105 main street Domremy SK s0k1g0",
                "preferred_all_caps": False,
            }
        ]

        self.assertIsNone(crm_validate_address._find_best_existing_address_option(address, options))

    def test_resolution_from_assessment_marks_zip_near_match(self):
        warnings = []

        resolution = crm_validate_address._resolution_from_assessment(
            "validated_address",
            "Validated address",
            {"postal_near_match": True},
            warnings,
        )

        self.assertEqual(resolution, "validated_address_zip_near_match")
        self.assertTrue(any("ZIP difference" in item for item in warnings))

    def test_resolution_from_assessment_marks_zip_prefix_match(self):
        warnings = []

        resolution = crm_validate_address._resolution_from_assessment(
            "validated_address",
            "Validated address",
            {"postal_prefix_match": True, "postal_base_match": False},
            warnings,
        )

        self.assertEqual(resolution, "validated_address_zip_prefix_match")
        self.assertTrue(any("first 2 digits" in item for item in warnings))

    def test_pick_validation_candidate_prefers_all_caps(self):
        address = {
            "address": "101 Example Farm Dr",
            "city": "West Windsor",
            "state": "Vermont",
            "zip": "05037",
        }
        candidates = [
            {"text": "101 Example Farm Rd Brownsville VT 05037-4440", "preferred_all_caps": False},
            {"text": "101 EXAMPLE FARM RD BROWNSVILLE VT 05037-4440", "preferred_all_caps": True},
        ]

        best, saw_zip_plus4_only = crm_validate_address._pick_validation_candidate(address, candidates)

        self.assertFalse(saw_zip_plus4_only)
        self.assertIsNotNone(best)
        self.assertEqual(best["candidate"]["text"], "101 EXAMPLE FARM RD BROWNSVILLE VT 05037-4440")

    def test_pick_validation_candidate_prefers_matching_address_cont(self):
        address = {
            "address": "123 West Example Street",
            "address_cont": "Apt. 2D",
            "city": "New York",
            "state": "New York",
            "zip": "10034",
        }
        candidates = [
            {"text": "123 W EXAMPLE ST APT 3D NEW YORK NY 10034", "preferred_all_caps": True},
            {"text": "123 W EXAMPLE ST APT 2D NEW YORK NY 10034", "preferred_all_caps": True},
        ]

        best, saw_zip_plus4_only = crm_validate_address._pick_validation_candidate(address, candidates)

        self.assertFalse(saw_zip_plus4_only)
        self.assertIsNotNone(best)
        self.assertEqual(best["candidate"]["text"], "123 W EXAMPLE ST APT 2D NEW YORK NY 10034")
        self.assertTrue(best["assessment"]["secondary_match"])

    def test_address_cont_preservation_accepts_unit_prefix_normalization(self):
        self.assertTrue(
            crm_validate_address._address_cont_value_preserved("Unit C202", "C202")
        )
        self.assertTrue(
            crm_validate_address._address_cont_value_preserved("Unit C202", "#C202")
        )

    def test_address_cont_preservation_accepts_reordered_identifier_with_extra_attention_text(self):
        self.assertTrue(
            crm_validate_address._address_cont_value_preserved("636 BEH", "BEH 636 Attn FSAE")
        )

    def test_address_cont_preservation_accepts_reordered_box_identifier(self):
        self.assertTrue(
            crm_validate_address._address_cont_value_preserved("BOX 787", "787 BOX")
        )

    def test_assess_address_text_accepts_one_digit_zip_difference(self):
        address = {
            "address": "6921 S Example St W",
            "address_cont": "",
            "city": "Muskogee",
            "state": "Oklahoma",
            "zip": "74403",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "6921 S EXAMPLE ST W MUSKOGEE OK 74401-8913",
        )

        self.assertTrue(assessment["safe_postal_near_match"])
        self.assertTrue(assessment["postal_near_match"])
        self.assertFalse(assessment["postal_full_match"])

    def test_assess_address_text_can_preserve_building_identifier_with_transposed_zip(self):
        address = {
            "address": "1 Example Way",
            "address_cont": "Building 3",
            "city": "Round Rock",
            "state": "Texas",
            "zip": "78628",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "1 EXAMPLE WAY ROUND ROCK TX 78682-7000",
        )

        self.assertTrue(assessment["safe_postal_near_match"])
        self.assertTrue(assessment["secondary_preserved"])
        self.assertFalse(assessment["secondary_match"])

    def test_assess_address_text_accepts_compact_runon_street(self):
        address = {
            "address": "2EXAMPLEPINELN",
            "address_cont": "",
            "city": "QUEENSBURY",
            "state": "New York",
            "zip": "12804",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "2 EXAMPLE PINE LN QUEENSBURY NY 12804-9014",
        )

        self.assertTrue(assessment["required_match"])
        self.assertTrue(assessment["compact_runon_match"])
        self.assertTrue(assessment["street_match"])
        self.assertTrue(assessment["house_match"])

    def test_assess_address_text_accepts_matching_zip_prefix_when_rest_matches(self):
        address = {
            "address": "257 Example Ave",
            "address_cont": "",
            "city": "Yonkers",
            "state": "New York",
            "zip": "10456",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "257 EXAMPLE AVE YONKERS NY 10703-1903",
        )

        self.assertTrue(assessment["safe_postal_prefix_match"])
        self.assertTrue(assessment["postal_prefix_match"])
        self.assertFalse(assessment["postal_base_match"])

    def test_assess_address_text_accepts_canadian_postal_near_match_and_preserves_address_cont(self):
        address = {
            "address": "25 Brunetville Rd",
            "address_cont": "30",
            "city": "Kapuskasing",
            "state": "Ontario",
            "zip": "P5N 2E9",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "25 BRUNETVILLE RD KAPUSKASING ON P5N 2E8",
        )

        self.assertTrue(assessment["safe_postal_near_match"])
        self.assertTrue(assessment["postal_near_match"])
        self.assertTrue(assessment["secondary_preserved"])
        self.assertFalse(assessment["secondary_match"])

    def test_compact_runon_zip_plus4_override_line_uses_shared_candidate_address(self):
        address = {
            "address": "2EXAMPLEPINELN",
            "address_cont": "",
            "city": "QUEENSBURY",
            "state": "New York",
            "zip": "12804",
        }
        assessed = crm_validate_address._assessed_validation_candidates(
            address,
            [
                {"text": "2 EXAMPLE PINE LN QUEENSBURY NY 12804-9014", "preferred_all_caps": True},
                {"text": "2 EXAMPLE PINE LN QUEENSBURY NY 12804-9012", "preferred_all_caps": True},
            ],
        )

        self.assertTrue(crm_validate_address._has_zip_plus4_bug(address, assessed))
        self.assertEqual(
            crm_validate_address._compact_runon_zip_plus4_override_line(
                address,
                crm_validate_address._postal_extension_bug_candidates(address, assessed),
            ),
            "2 EXAMPLE PINE LN",
        )

    def test_zip_plus4_bug_detection_requires_multiple_variants(self):
        address = {
            "address": "101 Example Farm Dr",
            "city": "West Windsor",
            "state": "Vermont",
            "zip": "05037",
        }
        safe_single = crm_validate_address._assessed_validation_candidates(
            address,
            [{"text": "101 EXAMPLE FARM RD BROWNSVILLE VT 05037-4440", "preferred_all_caps": True}],
        )
        buggy_multiple = crm_validate_address._assessed_validation_candidates(
            address,
            [
                {"text": "101 EXAMPLE FARM RD BROWNSVILLE VT 05037-4440", "preferred_all_caps": True},
                {"text": "101 EXAMPLE FARM RD BROWNSVILLE VT 05037-1234", "preferred_all_caps": True},
            ],
        )

        self.assertFalse(crm_validate_address._has_zip_plus4_bug(address, safe_single))
        self.assertTrue(crm_validate_address._has_zip_plus4_bug(address, buggy_multiple))

    def test_final_save_ready_accepts_global_save_button(self):
        class _Button:
            def is_enabled(self):
                return True

        driver = object()
        shipping_modal = object()

        def visible_side_effect(root, selectors):
            if root is shipping_modal:
                return []
            if root is driver:
                return [_Button()]
            return []

        with mock.patch.object(crm_validate_address, "_visible_elements", side_effect=visible_side_effect):
            self.assertTrue(crm_validate_address._final_save_ready(driver, shipping_modal, timeout=0.01))

    def test_open_shipping_editor_retries_when_modal_does_not_appear_on_first_click(self):
        driver = object()
        edit_button = object()
        modal = object()

        with mock.patch.object(crm_validate_address, "_switch_to_order_app_frame"):
            with mock.patch.object(crm_validate_address, "_find_shipping_edit_button", return_value=edit_button) as mock_find:
                with mock.patch.object(crm_validate_address, "_click_with_fallback") as mock_click:
                    with mock.patch.object(
                        crm_validate_address,
                        "_wait_for_shipping_modal",
                        side_effect=[
                            crm_validate_address.TimeoutException("first click missed"),
                            modal,
                        ],
                    ) as mock_wait:
                        with mock.patch.object(crm_validate_address.time, "sleep"):
                            result = crm_validate_address._open_shipping_editor(driver)

        self.assertIs(result, modal)
        self.assertEqual(mock_click.call_count, 2)
        self.assertEqual(mock_find.call_count, 2)
        self.assertEqual(mock_wait.call_count, 2)

    def test_ensure_override_ready_can_persist_override_when_ui_state_is_missing(self):
        warnings = []

        with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
            with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=False):
                with mock.patch.object(
                    crm_validate_address,
                    "_persist_validated_address_via_modal_scope",
                    return_value={"ok": True},
                ) as mock_persist:
                    ready, use_scope_send = crm_validate_address._ensure_override_ready(
                        object(),
                        object(),
                        warnings,
                        dry_run=False,
                    )

        self.assertTrue(ready)
        self.assertTrue(use_scope_send)
        self.assertEqual(mock_persist.call_count, 1)
        self.assertTrue(any("Persisted the override through the CRM modal service" in item for item in warnings))

    def test_save_shipping_transaction_can_accept_success_banner(self):
        driver = mock.Mock()
        shipping_modal = object()
        save_button = object()

        with mock.patch.object(crm_validate_address, "_wait_for_final_save_button", return_value=save_button):
            with mock.patch.object(crm_validate_address, "_click_with_fallback") as mock_click:
                with mock.patch.object(crm_validate_address, "_body_text", return_value="Shipping Transaction added successfully"):
                    with mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=False):
                        with mock.patch.object(crm_validate_address, "safe_get_with_partial_load") as mock_reload:
                            crm_validate_address._save_shipping_transaction(
                                driver,
                                shipping_modal,
                                "4832341",
                                dry_run=False,
                                accept_success_banner=True,
                            )

        mock_click.assert_called_once_with(driver, save_button)
        mock_reload.assert_not_called()

    def test_free_po_box_override_uses_scope_send_when_ui_state_is_missing(self):
        driver = object()
        shipping_modal = object()
        address = {
            "recipient": "Example Recipient",
            "address": "P.O. Box 550193",
            "address_cont": "",
            "city": "South Lake Tahoe",
            "state": "California",
            "zip": "96150",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_target_order", return_value="4605090"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_shipping_panel_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_read_order_totals_shipping_class", return_value="free"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_shipping_editor", return_value=shipping_modal))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))))
            stack.enter_context(mock.patch.object(crm_validate_address, "_collect_existing_address_options", return_value=[]))
            stack.enter_context(mock.patch.object(crm_validate_address, "_find_best_existing_address_option", return_value=None))
            stack.enter_context(mock.patch.object(crm_validate_address, "_rewrite_address_fields_if_needed", return_value=(dict(address), "")))
            stack.enter_context(mock.patch.object(crm_validate_address, "_apply_override"))
            mock_ensure = stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_override_ready", return_value=(True, True)))
            stack.enter_context(
                mock.patch.object(
                    crm_validate_address,
                    "_prepare_shipping_form_for_save",
                    return_value=(None, dict(address), ""),
                )
            )
            mock_save = stack.enter_context(mock.patch.object(crm_validate_address, "_save_shipping_transaction"))

            result = crm_validate_address._evaluate_and_resolve_order(
                driver,
                order_id="4605090",
                dry_run=False,
                shipping_filter="all",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "po_box_free_override_saved")
        self.assertEqual(result["resolution"], "override")
        mock_ensure.assert_called_once_with(driver, shipping_modal, mock.ANY, dry_run=False)
        mock_save.assert_called_once_with(driver, shipping_modal, "4605090", False, use_scope_send=True)

    def test_valid_split_street_address_is_rewritten_instead_of_skipped(self):
        driver = object()
        shipping_modal = object()
        address = {
            "recipient": "Example Recipient",
            "address": "2265",
            "address_cont": "LAKE CREST CT.",
            "city": "MARTINEZ",
            "state": "California",
            "zip": "94553",
        }
        fixed_address = {
            **address,
            "address": "2265 LAKE CREST CT.",
            "address_cont": "",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_target_order", return_value="4832907"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_shipping_panel_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=True))
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_shipping_editor", return_value=shipping_modal))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_address_is_valid", return_value=True))
            stack.enter_context(
                mock.patch.object(
                    crm_validate_address,
                    "_ensure_recipient_present",
                    side_effect=[
                        (True, dict(address)),
                        (True, dict(fixed_address)),
                        (True, dict(fixed_address)),
                    ],
                )
            )
            stack.enter_context(mock.patch.object(crm_validate_address, "_collect_existing_address_options", return_value=[]))
            mock_try_existing = stack.enter_context(mock.patch.object(crm_validate_address, "_try_resolve_with_existing_address", return_value=None))
            mock_rewrite = stack.enter_context(
                mock.patch.object(
                    crm_validate_address,
                    "_rewrite_address_fields_if_needed",
                    return_value=(dict(fixed_address), ""),
                )
            )
            stack.enter_context(mock.patch.object(crm_validate_address, "_find_visible_element", return_value=None))
            stack.enter_context(
                mock.patch.object(
                    crm_validate_address,
                    "_prepare_shipping_form_for_save",
                    return_value=(None, dict(fixed_address), ""),
                )
            )
            mock_save = stack.enter_context(mock.patch.object(crm_validate_address, "_save_shipping_transaction"))

            result = crm_validate_address._evaluate_and_resolve_order(
                driver,
                order_id="4832907",
                dry_run=False,
                shipping_filter="free",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "already_valid")
        self.assertEqual(result["final_address"]["address"], "2265 LAKE CREST CT.")
        self.assertEqual(result["final_address"]["address_cont"], "")
        mock_rewrite.assert_called()
        mock_try_existing.assert_not_called()
        mock_save.assert_called_once_with(driver, shipping_modal, "4832907", False)

    def test_apo_psc_address_bypasses_street_number_guard_and_overrides(self):
        driver = object()
        shipping_modal = object()
        address = {
            "recipient": "Example Recipient",
            "address": "PSC 1300, Unit 95716 MED",
            "address_cont": "",
            "city": "APO",
            "state": "Armed Forces Americas",
            "zip": "34042",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_target_order", return_value="4717598"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_shipping_panel_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_shipping_editor", return_value=shipping_modal))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))))
            stack.enter_context(mock.patch.object(crm_validate_address, "_collect_existing_address_options", return_value=[]))
            stack.enter_context(mock.patch.object(crm_validate_address, "_find_best_existing_address_option", return_value=None))
            stack.enter_context(mock.patch.object(crm_validate_address, "_try_resolve_with_existing_address", return_value=None))
            stack.enter_context(mock.patch.object(crm_validate_address, "_rewrite_address_fields_if_needed", return_value=(dict(address), "")))
            stack.enter_context(mock.patch.object(crm_validate_address, "_apply_override"))
            mock_ensure = stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_override_ready", return_value=(True, True)))
            stack.enter_context(
                mock.patch.object(
                    crm_validate_address,
                    "_prepare_shipping_form_for_save",
                    return_value=(None, dict(address), ""),
                )
            )
            mock_save = stack.enter_context(mock.patch.object(crm_validate_address, "_save_shipping_transaction"))

            result = crm_validate_address._evaluate_and_resolve_order(
                driver,
                order_id="4717598",
                dry_run=False,
                shipping_filter="rush",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "apo_override_saved")
        self.assertEqual(result["resolution"], "apo_override")
        mock_ensure.assert_called_once_with(driver, shipping_modal, mock.ANY, dry_run=False)
        mock_save.assert_called_once_with(
            driver,
            shipping_modal,
            "4717598",
            False,
            use_scope_send=True,
            accept_success_banner=True,
        )

    def test_apo_address_uses_matching_existing_address_before_override(self):
        driver = object()
        shipping_modal = object()
        address = {
            "recipient": "Javier Almeidaluera",
            "address": "CLB31, UPR 38463, Box 162",
            "address_cont": "CLB31, UPR 38463, Box 162",
            "city": "FPO",
            "state": "Armed Forces Pacific",
            "zip": "96384-6301",
        }
        option = {
            "text": "Javier Almeidaluera - USMC/CLB-31 - CLB31, UPR 38463, Box 162 CLB31, UPR 38463, Box 162 FPO AP, 96384-6301",
            "preferred_all_caps": False,
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_target_order", return_value="4832341"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_shipping_panel_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_shipping_editor", return_value=shipping_modal))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))))
            stack.enter_context(mock.patch.object(crm_validate_address, "_collect_existing_address_options", return_value=[option]))
            stack.enter_context(mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True))
            stack.enter_context(mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=True))
            stack.enter_context(mock.patch.object(crm_validate_address, "_persist_validated_address_via_modal_scope", return_value={"ok": True}))
            stack.enter_context(
                mock.patch.object(
                    crm_validate_address,
                    "_prepare_shipping_form_for_save",
                    return_value=(None, dict(address), address["address_cont"]),
                )
            )
            mock_save = stack.enter_context(mock.patch.object(crm_validate_address, "_save_shipping_transaction"))
            mock_apply_override = stack.enter_context(mock.patch.object(crm_validate_address, "_apply_override"))

            result = crm_validate_address._evaluate_and_resolve_order(
                driver,
                order_id="4832341",
                dry_run=False,
                shipping_filter="rush",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "existing_address_saved")
        self.assertEqual(result["resolution"], "existing_address")
        mock_apply_override.assert_not_called()
        mock_save.assert_called_once_with(driver, shipping_modal, "4832341", False, use_scope_send=True)

    def test_attempt_validation_candidate_selection_accepts_final_save_ready(self):
        warnings = []
        button = object()

        with mock.patch.object(crm_validate_address, "_select_validation_candidate_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_wait_for_any", return_value=button):
                with mock.patch.object(crm_validate_address, "_click_with_fallback") as mock_click:
                    with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                        with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=True):
                            ready, use_scope_send = crm_validate_address._attempt_validation_candidate_selection(
                                object(),
                                object(),
                                object(),
                                "8905 EXAMPLE HILL WAY ELK GROVE CA 95624-1457",
                                warnings,
                                dry_run=True,
                            )

        self.assertTrue(ready)
        self.assertFalse(use_scope_send)
        self.assertEqual(mock_click.call_count, 1)
        self.assertTrue(any("final Save button became available" in item for item in warnings))

    def test_attempt_validation_candidate_selection_can_persist_validated_address(self):
        warnings = []
        button = object()

        with mock.patch.object(crm_validate_address, "_select_validation_candidate_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_wait_for_any", return_value=button):
                with mock.patch.object(crm_validate_address, "_click_with_fallback"):
                    with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=True):
                        with mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False):
                            with mock.patch.object(
                                crm_validate_address,
                                "_persist_validated_address_via_modal_scope",
                                return_value={"ok": True},
                            ) as mock_persist:
                                ready, use_scope_send = crm_validate_address._attempt_validation_candidate_selection(
                                    object(),
                                    object(),
                                    object(),
                                    "8905 EXAMPLE HILL WAY ELK GROVE CA 95624-1457",
                                    warnings,
                                    dry_run=False,
                                )

        self.assertTrue(ready)
        self.assertTrue(use_scope_send)
        self.assertEqual(mock_persist.call_count, 1)
        self.assertTrue(any("Persisted the validated address through the CRM modal service" in item for item in warnings))

    @mock.patch.object(crm_validate_address, "safe_driver_quit")
    @mock.patch.object(crm_validate_address, "_run_once_with_driver")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids_with_driver")
    @mock.patch.object(crm_validate_address, "_build_crm_session_driver")
    def test_shared_session_batch_stops_after_requested_total(
        self,
        mock_build_driver,
        mock_collect_orders,
        mock_run_once,
        mock_safe_quit,
    ):
        driver = object()
        mock_build_driver.return_value = driver
        collect_limits = []

        def collect_side_effect(driver_arg, shipping_filter, limit, list_url_override=None, exclude_order_ids=None):
            del driver_arg, shipping_filter, list_url_override
            collect_limits.append((limit, tuple(sorted(exclude_order_ids or []))))
            if len(collect_limits) == 1:
                return ["1000001"]
            if len(collect_limits) == 2:
                return ["1000002", "1000003", "1000004"]
            raise AssertionError("Batch collection should stop once the requested total is reached.")

        mock_collect_orders.side_effect = collect_side_effect
        mock_run_once.side_effect = lambda driver_arg, order_id=None, **kwargs: {
            "success": True,
            "message": f"ok {order_id}",
            "report": [_report_row(order_id)],
        }

        finished_payloads, attempted_order_ids, refresh_passes = crm_validate_address._run_batch_reusing_session(
            dry_run=True,
            shipping_filter="rush",
            batch_size=3,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertEqual([item[0] for item in collect_limits], [3, 2])
        self.assertEqual(attempted_order_ids, ["1000001", "1000002", "1000003"])
        self.assertEqual(refresh_passes, 2)
        self.assertEqual(len(finished_payloads), 3)
        mock_safe_quit.assert_called_with(driver, profile_path=str((ROOT / "chrome_profile_crm").resolve()))

    @mock.patch.object(crm_validate_address, "safe_driver_quit")
    @mock.patch.object(crm_validate_address, "_run_once_with_driver")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids_with_driver")
    @mock.patch.object(crm_validate_address, "_build_crm_session_driver")
    def test_shared_session_batch_runs_until_no_orders_when_batch_size_is_blank(
        self,
        mock_build_driver,
        mock_collect_orders,
        mock_run_once,
        mock_safe_quit,
    ):
        driver = object()
        mock_build_driver.return_value = driver
        collect_limits = []

        def collect_side_effect(driver_arg, shipping_filter, limit, list_url_override=None, exclude_order_ids=None):
            del driver_arg, shipping_filter, list_url_override
            collect_limits.append((limit, tuple(sorted(exclude_order_ids or []))))
            if len(collect_limits) == 1:
                return ["1000001"]
            if len(collect_limits) == 2:
                return ["1000002"]
            return []

        mock_collect_orders.side_effect = collect_side_effect
        mock_run_once.side_effect = lambda driver_arg, order_id=None, **kwargs: {
            "success": True,
            "message": f"ok {order_id}",
            "report": [_report_row(order_id)],
        }

        finished_payloads, attempted_order_ids, refresh_passes = crm_validate_address._run_batch_reusing_session(
            dry_run=True,
            shipping_filter="rush",
            batch_size=None,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertEqual(
            [item[0] for item in collect_limits],
            [crm_validate_address.CONTINUOUS_BATCH_FETCH_LIMIT] * 3,
        )
        self.assertEqual(attempted_order_ids, ["1000001", "1000002"])
        self.assertEqual(refresh_passes, 3)
        self.assertEqual(len(finished_payloads), 2)
        mock_safe_quit.assert_called_with(driver, profile_path=str((ROOT / "chrome_profile_crm").resolve()))

    @mock.patch.object(crm_validate_address, "write_result_payload")
    @mock.patch.object(crm_validate_address.shutil, "rmtree")
    @mock.patch.object(crm_validate_address, "_clone_profile_for_worker")
    @mock.patch.object(crm_validate_address, "_run_single_payload")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_stops_after_requested_total(
        self,
        mock_collect_orders,
        mock_run_single_payload,
        mock_clone_profile,
        _mock_rmtree,
        _mock_write_result_payload,
    ):
        collect_limits = []
        mock_clone_profile.side_effect = lambda profile_path, label, *args, **kwargs: (
            str(ROOT / "tests_tmp" / label),
            str(ROOT / "tests_tmp" / label / "profile"),
        )

        def collect_side_effect(shipping_filter, limit, profile_path, list_url_override=None, exclude_order_ids=None, visible=False):
            del shipping_filter, profile_path, list_url_override, visible
            collect_limits.append((limit, tuple(sorted(exclude_order_ids or []))))
            if len(collect_limits) == 1:
                return ["2000001", "2000002"]
            if len(collect_limits) == 2:
                return ["2000003", "2000004"]
            raise AssertionError("Parallel batch collection should stop once the requested total is reached.")

        mock_collect_orders.side_effect = collect_side_effect
        mock_run_single_payload.side_effect = lambda order_id=None, **kwargs: {
            "success": True,
            "message": f"ok {order_id}",
            "target_order_id": str(order_id),
            "report": [_report_row(order_id)],
        }

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="free",
            batch_size=3,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertEqual([item[0] for item in collect_limits], [3, 1])
        self.assertEqual(payload["order_ids"], ["2000001", "2000002", "2000003"])
        self.assertEqual(payload["order_count"], 3)
        self.assertEqual(payload["refresh_passes"], 2)

    @mock.patch.object(crm_validate_address.time, "sleep")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_retries_retryable_list_collection_once(self, mock_collect_orders, _mock_sleep):
        mock_collect_orders.side_effect = [RuntimeError("invalid session id"), []]

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="rush",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertTrue(payload["success"])
        self.assertEqual(payload["resolution"], "no_orders")
        self.assertEqual(mock_collect_orders.call_count, 2)

    @mock.patch.object(crm_validate_address.time, "sleep")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_reports_list_collection_failure_after_retry(self, mock_collect_orders, _mock_sleep):
        mock_collect_orders.side_effect = RuntimeError("invalid session id")

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="rush",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertFalse(payload["success"])
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["resolution"], "list_collection_failed")
        self.assertIn("invalid session id", payload["message"])
        self.assertEqual(mock_collect_orders.call_count, 2)

    @mock.patch.object(crm_validate_address, "write_result_payload")
    @mock.patch.object(crm_validate_address.shutil, "rmtree")
    @mock.patch.object(crm_validate_address, "_clone_profile_for_worker")
    @mock.patch.object(crm_validate_address, "_run_single_payload")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_retries_retryable_worker_exception_once(
        self,
        mock_collect_orders,
        mock_run_single_payload,
        mock_clone_profile,
        _mock_rmtree,
        _mock_write_result_payload,
    ):
        mock_collect_orders.side_effect = [["2000001"], []]
        mock_clone_profile.side_effect = lambda profile_path, label, *args, **kwargs: (
            str(ROOT / "tests_tmp" / label),
            str(ROOT / "tests_tmp" / label / "profile"),
        )
        mock_run_single_payload.side_effect = [
            {
                "success": False,
                "message": "Message: The Shipping Transaction modal did not appear before the timeout expired.",
                "target_order_id": "2000001",
                "retryable": True,
                "report": [
                    {
                        "order_id": "2000001",
                        "success": False,
                        "outcome": "worker_exception",
                        "message": "Message: The Shipping Transaction modal did not appear before the timeout expired.",
                        "manual_review_required": True,
                        "warnings": [],
                        "retry_attempted": False,
                    }
                ],
            },
            {
                "success": True,
                "message": "ok 2000001",
                "target_order_id": "2000001",
                "report": [_report_row("2000001")],
            },
        ]

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="free",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertTrue(payload["success"])
        self.assertEqual(mock_run_single_payload.call_count, 2)
        self.assertEqual(mock_clone_profile.call_count, 2)
        self.assertTrue(payload["report"][0]["retry_attempted"])
        self.assertTrue(payload["report"][0]["transient_retry_attempted"])

    @mock.patch.object(crm_validate_address, "write_result_payload")
    @mock.patch.object(crm_validate_address.shutil, "rmtree")
    @mock.patch.object(crm_validate_address, "_clone_profile_for_worker")
    @mock.patch.object(crm_validate_address, "_run_single_payload")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_does_not_retry_validation_manual_review(
        self,
        mock_collect_orders,
        mock_run_single_payload,
        mock_clone_profile,
        _mock_rmtree,
        _mock_write_result_payload,
    ):
        mock_collect_orders.side_effect = [["2000001"], []]
        mock_clone_profile.side_effect = lambda profile_path, label, *args, **kwargs: (
            str(ROOT / "tests_tmp" / label),
            str(ROOT / "tests_tmp" / label / "profile"),
        )
        mock_run_single_payload.return_value = {
            "success": False,
            "message": "Skipped because the suggested validated address did not match the original shipping address closely enough.",
            "target_order_id": "2000001",
            "report": [
                {
                    "order_id": "2000001",
                    "success": False,
                    "outcome": "validated_candidate_mismatch",
                    "message": "Skipped because the suggested validated address did not match the original shipping address closely enough.",
                    "manual_review_required": True,
                    "warnings": [],
                    "retry_attempted": False,
                }
            ],
        }

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="free",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertFalse(payload["success"])
        self.assertEqual(mock_run_single_payload.call_count, 1)
        self.assertEqual(mock_clone_profile.call_count, 1)
        self.assertFalse(payload["report"][0]["retry_attempted"])

    @mock.patch.object(server, "_run_script")
    def test_execute_worker_supports_continuous_batch_mode(self, mock_run_script):
        mock_run_script.return_value = (
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            {"success": False, "message": "CRMAddressValidator timed out after 180 seconds."},
        )

        ok, message, payload = server._execute_crm_address_worker(
            dry_run=True,
            shipping_filter="rush",
            action="validate_batch",
            batch_size=None,
            parallel_workers=1,
        )

        self.assertFalse(ok)
        self.assertEqual(message, "CRMAddressValidator timed out after 180 seconds.")
        self.assertEqual(payload["action"], "validate_batch")
        self.assertIsNone(payload["batch_size"])
        self.assertEqual(payload["parallel_workers"], 1)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--parallel-workers", args)
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)
        self.assertNotIn("--batch-size", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])
        self.assertGreaterEqual(
            mock_run_script.call_args.kwargs["timeout"],
            server.CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS,
        )


class CrmAddressServerTests(unittest.TestCase):
    @mock.patch.object(server, "_run_script")
    def test_execute_worker_enriches_batch_payload_and_uses_scaled_timeout(self, mock_run_script):
        mock_run_script.return_value = (
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            {"success": False, "message": "CRMAddressValidator timed out after 180 seconds."},
        )

        ok, message, payload = server._execute_crm_address_worker(
            dry_run=True,
            shipping_filter="rush",
            action="validate_batch",
            batch_size=3,
            parallel_workers=1,
        )

        self.assertFalse(ok)
        self.assertEqual(message, "CRMAddressValidator timed out after 180 seconds.")
        self.assertEqual(payload["action"], "validate_batch")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["shipping_filter"], "rush")
        self.assertEqual(payload["batch_size"], 3)
        self.assertEqual(payload["parallel_workers"], 1)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])
        self.assertGreater(mock_run_script.call_args.kwargs["timeout"], 180)

    @mock.patch.object(server, "_run_script")
    def test_execute_push_back_worker_passes_parallel_workers(self, mock_run_script):
        mock_run_script.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok", "action": "push_back_batch"},
        )

        ok, message, payload = server._execute_crm_push_back_worker(
            dry_run=True,
            batch_size=4,
            processing_filter="rush",
            parallel_workers=3,
            list_url="https://crm.example/push-back",
        )

        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertEqual(payload["parallel_workers"], 3)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--parallel-workers", args)
        self.assertIn("3", args)
        self.assertIn("--batch-size", args)
        self.assertIn("4", args)

    @mock.patch.object(server, "_audit_result")
    @mock.patch.object(server, "_record_crm_processing_report_result")
    @mock.patch.object(server, "save_crm_mass_emailer_state")
    @mock.patch.object(server, "load_crm_mass_emailer_state")
    @mock.patch.object(server, "ensure_crm_mass_emailer_state_file")
    def test_mass_emailer_history_preserves_order_success_and_sheet_errors(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        _mock_save_state,
        _mock_processing_report,
        _mock_audit_result,
    ):
        mock_load_state.return_value = server._default_crm_mass_emailer_state()
        payload = {
            "success": False,
            "message": "Processed 1 sheet scanner row(s); 1 failed.",
            "action": "process_queue",
            "dry_run": False,
            "processed": [
                {
                    "row_number": 12,
                    "order_id": "4600001",
                    "issue_type": "Copyright - Cancel",
                    "message": "Completed successfully.",
                }
            ],
            "failures": [
                {
                    "row_number": 13,
                    "order_id": "4600002",
                    "issue_type": "Copyright - Cancel",
                    "error": "Google Sheet error text.",
                    "error_type": "CopyrightCancelError",
                }
            ],
        }

        state = server._persist_crm_mass_emailer_run_result(
            False,
            "Processed 1 sheet scanner row(s); 1 failed.",
            payload,
            dry_run=False,
        )

        details = state["run_history"][0]["order_details"]
        self.assertEqual(state["run_history"][0]["order_ids"], ["4600001", "4600002"])
        self.assertTrue(details[0]["success"])
        self.assertEqual(details[0]["status"], "Success")
        self.assertEqual(details[0]["function_label"], "Copyright - Cancel")
        self.assertFalse(details[1]["success"])
        self.assertEqual(details[1]["status"], "Needs attention")
        self.assertEqual(details[1]["function_label"], "Copyright - Cancel")
        self.assertEqual(details[1]["message"], "Google Sheet error text.")
        _mock_processing_report.assert_called_once()

    def test_mass_emailer_history_keeps_same_order_failures_for_different_sheet_rows(self):
        payload = {
            "success": False,
            "message": "Processed 0 sheet scanner row(s); 2 failed.",
            "action": "process_queue",
            "dry_run": False,
            "failures": [
                {
                    "row_number": 3,
                    "order_id": "4705293",
                    "issue_type": "Copyright - Cancel",
                    "process": "copyright_cancel",
                    "error": "Refund fee skipped.",
                },
                {
                    "row_number": 2,
                    "order_id": "4705293",
                    "issue_type": "Copyright Removal",
                    "process": "copyright_removal",
                    "error": "Removal template missing.",
                },
            ],
        }

        details = server._crm_mass_emailer_order_details_from_payload(payload)

        self.assertEqual(len(details), 2)
        self.assertEqual([item["function_label"] for item in details], ["Copyright - Cancel", "Copyright Removal"])
        self.assertEqual([item["message"] for item in details], ["Refund fee skipped.", "Removal template missing."])

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_defaults_to_continuous_rush_dry_run(self, mock_run_script):
        mock_run_script.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok", "action": "order_goods_batch"},
        )

        ok, message, payload = server._execute_crm_order_goods_worker(dry_run=True, batch_size=None)

        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertEqual(payload["action"], "order_goods_batch")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["shipping_filter"], "rush")
        self.assertIsNone(payload["batch_size"])
        args = mock_run_script.call_args.args[1]
        self.assertEqual(args[:2], ["--action", "order_goods_batch"])
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)
        self.assertNotIn("--batch-size", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])
        self.assertGreaterEqual(
            mock_run_script.call_args.kwargs["timeout"],
            server.CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS,
        )

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_passes_limited_batch_size(self, mock_run_script):
        mock_run_script.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok"},
        )

        ok, _message, payload = server._execute_crm_order_goods_worker(dry_run=False, batch_size=3)

        self.assertTrue(ok)
        self.assertEqual(payload["batch_size"], 3)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--batch-size", args)
        self.assertIn("3", args)
        self.assertNotIn("--visible", args)
        self.assertFalse(mock_run_script.call_args.kwargs["show_terminal"])

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_passes_single_order_id(self, mock_run_script):
        mock_run_script.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok", "action": "order_goods_single"},
        )

        ok, _message, payload = server._execute_crm_order_goods_worker(dry_run=True, order_id="https://crm2.legacy.printfly.com/order/4418860")

        self.assertTrue(ok)
        self.assertEqual(payload["action"], "order_goods_single")
        self.assertEqual(payload["target_order_id"], "4418860")
        self.assertEqual(payload["order_ids"], ["4418860"])
        self.assertEqual(payload["batch_size"], 1)
        self.assertEqual(payload["parallel_workers"], 1)
        args = mock_run_script.call_args.args[1]
        self.assertEqual(args[:2], ["--action", "order_goods_single"])
        self.assertIn("--order-id", args)
        self.assertIn("4418860", args)
        self.assertNotIn("--batch-size", args)
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_rejects_invalid_single_order_id(self, mock_run_script):
        ok, message, payload = server._execute_crm_order_goods_worker(dry_run=True, order_id="bad")

        self.assertFalse(ok)
        self.assertIn("7-digit", message)
        self.assertEqual(payload["resolution"], "invalid_order_id")
        mock_run_script.assert_not_called()

    def test_order_goods_unlock_retry_selects_native_status_without_send_keys(self):
        driver = mock.Mock()
        control = mock.Mock()
        control.send_keys.side_effect = AssertionError("native dropdown should not receive text keys")

        with mock.patch.object(crm_order_goods, "_resolve_stock_unlock_text_control", return_value=control):
            with mock.patch.object(crm_order_goods, "_click_with_fallback"):
                with mock.patch.object(crm_order_goods, "_select_native_stock_unlock_option", return_value=True):
                    with mock.patch.object(crm_order_goods.time, "sleep"):
                        self.assertTrue(crm_order_goods._choose_stock_unlock_status_from_control(driver, control))

        control.send_keys.assert_not_called()

    def test_order_goods_top_panel_unlock_script_handles_dropdown_and_types_full_status(self):
        captured = {}
        driver = mock.Mock()

        def execute_async_script(script):
            captured["script"] = script
            return {"success": True}

        driver.execute_async_script.side_effect = execute_async_script

        result = crm_order_goods._apply_stock_unlock_with_top_panel_script(driver)

        self.assertTrue(result["success"])
        self.assertIn("chooseNativeSelect", captured["script"])
        self.assertIn("'select'", captured["script"])
        self.assertIn("setTypedValue(searchInput, 'Stock Auto Ordering Unlocked')", captured["script"])

    @mock.patch.object(server, "_run_script")
    def test_stock_unlocker_dry_run_uses_visible_terminal(self, mock_run_script):
        mock_run_script.return_value = (True, "ok", {"success": True, "message": "ok"})

        ok, _message, _payload = server._execute_crm_worker(dry_run=True)

        self.assertTrue(ok)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--dry-run", args)
        self.assertIn("--visible", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])

    @mock.patch.object(server, "_run_script")
    def test_stock_unlocker_worker_passes_mode_specific_list_url(self, mock_run_script):
        mock_run_script.return_value = (True, "ok", {"success": True, "message": "ok"})
        list_url = "https://crm.example/report/free-unlocker"

        ok, _message, _payload = server._execute_crm_worker(list_url=list_url)

        self.assertTrue(ok)
        args = mock_run_script.call_args.args[1]
        self.assertEqual(args[args.index("--list-url") + 1], list_url)

    @mock.patch.object(server, "_persist_crm_order_goods_run_result", return_value={})
    @mock.patch.object(server, "_finish_crm_order_goods_runtime")
    @mock.patch.object(server, "_start_crm_order_goods_runtime")
    @mock.patch.object(server, "_execute_crm_order_goods_worker")
    @mock.patch.object(server, "_saved_crm_automation_parallel_workers", return_value=1)
    @mock.patch.object(server, "load_crm_address_state")
    def test_processing_order_goods_step_runs_headless_hidden(
        self,
        mock_load_address_state,
        _mock_saved_workers,
        mock_execute,
        _mock_start_runtime,
        _mock_finish_runtime,
        _mock_persist,
    ):
        mock_load_address_state.return_value = {"saved_parallel_workers": 1}
        mock_execute.return_value = (True, "ok", {"success": True, "message": "ok", "order_ids": []})

        result = server._run_crm_processing_step("order_goods", "rush")

        self.assertTrue(result["success"])
        mock_execute.assert_called_once_with(
            dry_run=False,
            batch_size=None,
            parallel_workers=1,
            list_url=None,
            visible=False,
            show_terminal=False,
        )

    def test_order_goods_runtime_config_accepts_explicit_list_override(self):
        self.assertEqual(
            crm_order_goods._validate_runtime_config("https://crm.example/report?shippingCharges%5Bhigh%5D=1"),
            "https://crm.example/report?shippingCharges%5Bhigh%5D=1",
        )

    @mock.patch.object(crm_order_goods, "_find_sanmar_order_goods_button", return_value=None)
    def test_order_goods_skips_when_stock_already_ordered(self, mock_button):
        driver = mock.Mock()
        driver.execute_script.return_value = "Stock Status: Ordered Stock : Ordered"

        result = crm_order_goods._order_goods_for_open_order(driver, "4418860", dry_run=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "already_stock_ordered")
        mock_button.assert_not_called()

    def test_order_goods_does_not_treat_stock_ordered_false_label_as_ordered(self):
        self.assertFalse(crm_order_goods._text_indicates_stock_already_ordered("Stock Ordered: false"))
        self.assertFalse(crm_order_goods._text_indicates_stock_already_ordered("Stock Ordered"))
        self.assertFalse(crm_order_goods._text_indicates_stock_already_ordered("Stock Auto Ordering Queued"))
        self.assertTrue(crm_order_goods._text_indicates_stock_already_ordered("Stock Status: Ordered"))
        self.assertTrue(crm_order_goods._text_indicates_stock_already_ordered("Stock : Ordered"))

    def test_stock_tab_script_prefers_unique_header_design_tabs(self):
        self.assertIn("#main-header-design-tabs button", crm_order_goods.STOCK_TAB_SCRIPT)
        self.assertIn("if (!tabs.length)", crm_order_goods.STOCK_TAB_SCRIPT)

    @mock.patch.object(crm_order_goods, "_page_indicates_stock_already_ordered", return_value=False)
    @mock.patch.object(crm_order_goods, "_refresh_order_after_stock_unlock", return_value="orderable")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_click_with_fallback")
    @mock.patch.object(crm_order_goods, "_find_sanmar_order_goods_button")
    def test_order_goods_unlocks_and_retries_when_button_disabled(
        self,
        mock_button,
        mock_click,
        mock_unlock,
        mock_refresh,
        _mock_ordered,
    ):
        disabled_button = mock.Mock()
        disabled_button.is_enabled.return_value = False
        enabled_button = mock.Mock()
        enabled_button.is_enabled.return_value = True
        mock_button.side_effect = [disabled_button, enabled_button]
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlocked",
            "message": "Unlocked first.",
            "manual_review_required": False,
        }

        result = crm_order_goods._order_goods_for_open_order(mock.Mock(), "4418860", dry_run=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "order_goods_clicked")
        self.assertTrue(result["stock_unlocked_before_order_goods"])
        mock_unlock.assert_called_once_with(mock.ANY, "4418860", dry_run=False, force=True)
        mock_refresh.assert_called_once_with(mock.ANY, "4418860", stock_tab_index=None)
        mock_click.assert_called_once_with(mock.ANY, enabled_button)

    def test_order_goods_button_fallback_finds_visible_input_value(self):
        driver = mock.Mock()
        control = mock.Mock()
        control.is_displayed.return_value = True
        control.text = ""

        def get_attribute(name):
            values = {
                "value": "order goods",
                "aria-label": "",
                "title": "",
            }
            return values.get(name)

        control.get_attribute.side_effect = get_attribute
        driver.find_elements.return_value = [control]
        driver.execute_script.side_effect = [True, None]

        result = crm_order_goods._find_sanmar_order_goods_button_fallback(driver)

        self.assertIs(result, control)
        driver.find_elements.assert_called_once()

    def test_order_goods_button_finder_prefers_direct_enabled_order_goods_button(self):
        class FakeButton:
            pass

        disabled_status_container = FakeButton()
        enabled_order_goods_button = FakeButton()
        driver = mock.Mock()
        driver.execute_script.return_value = enabled_order_goods_button

        result = crm_order_goods._find_sanmar_order_goods_button(driver, timeout=0.1)

        self.assertIs(result, enabled_order_goods_button)
        script = driver.execute_script.call_args.args[0]
        self.assertIn("directMatches", script)
        self.assertIn("!== 'order goods'", script)

    @mock.patch.object(crm_order_goods, "_order_goods_for_all_stock_tabs")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_wait_for_order_goods_page_ready", return_value=False)
    @mock.patch.object(crm_order_goods, "_open_target_order")
    def test_order_goods_stops_when_order_page_never_renders_expected_order(
        self,
        mock_open,
        mock_ready,
        mock_unlock,
        mock_order_tabs,
    ):
        driver = mock.Mock()

        with self.assertRaises(crm_order_goods.TimeoutException):
            crm_order_goods._run_order_with_driver(driver, "4418860", dry_run=False)

        self.assertEqual(mock_open.call_count, 2)
        self.assertEqual(mock_ready.call_count, 2)
        mock_unlock.assert_not_called()
        mock_order_tabs.assert_not_called()

    @mock.patch.object(crm_order_goods, "_order_goods_for_all_stock_tabs")
    @mock.patch.object(crm_order_goods, "_wait_after_stock_unlock", return_value="orderable")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_wait_for_order_goods_page_ready", return_value=True)
    @mock.patch.object(crm_order_goods, "_open_target_order")
    def test_order_goods_unlocks_locked_order_then_refreshes_before_ordering(self, mock_open, _mock_ready, mock_unlock, _mock_wait_after_unlock, mock_order_tabs):
        driver = mock.Mock()
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlocked",
            "message": "Unlocked first.",
            "manual_review_required": False,
        }
        mock_order_tabs.return_value = [
            {"order_id": "4418860", "success": True, "outcome": "order_goods_clicked", "message": "clicked", "manual_review_required": False}
        ]

        results = crm_order_goods._run_order_with_driver(driver, "4418860", dry_run=False)

        self.assertEqual(mock_open.call_count, 2)
        driver.refresh.assert_called_once()
        mock_order_tabs.assert_called_once_with(driver, "4418860", dry_run=False)
        self.assertTrue(results[0]["stock_unlocked_before_order_goods"])
        self.assertIn("Unlocked first.", results[0]["warnings"])

    @mock.patch.object(crm_order_goods, "_order_goods_for_all_stock_tabs")
    @mock.patch.object(crm_order_goods, "_wait_after_stock_unlock", return_value="ordered")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_wait_for_order_goods_page_ready", return_value=True)
    @mock.patch.object(crm_order_goods, "_open_target_order")
    def test_order_goods_treats_post_unlock_auto_ordered_stock_as_success(self, _mock_open, _mock_ready, mock_unlock, _mock_wait_after_unlock, mock_order_tabs):
        driver = mock.Mock()
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlocked",
            "message": "Unlocked first.",
            "manual_review_required": False,
        }

        results = crm_order_goods._run_order_with_driver(driver, "4418860", dry_run=False)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["outcome"], "already_stock_ordered")
        self.assertTrue(results[0]["stock_unlocked_before_order_goods"])
        mock_order_tabs.assert_not_called()

    @mock.patch.object(crm_order_goods, "_order_goods_for_all_stock_tabs")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_wait_for_order_goods_page_ready", return_value=True)
    @mock.patch.object(crm_order_goods, "_open_target_order")
    def test_order_goods_single_dry_run_reports_locked_unlock_without_ordering(self, _mock_open, _mock_ready, mock_unlock, mock_order_tabs):
        driver = mock.Mock()
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlock_ready",
            "message": "Would unlock.",
            "manual_review_required": False,
        }

        results = crm_order_goods._run_order_with_driver(driver, "4418860", dry_run=True)

        self.assertEqual(results[0]["outcome"], "stock_unlock_ready")
        driver.refresh.assert_not_called()
        mock_order_tabs.assert_not_called()

    @mock.patch.object(crm_order_goods, "_click_with_fallback")
    @mock.patch.object(crm_order_goods, "_order_goods_for_open_order")
    def test_order_goods_processes_each_stock_tab(self, mock_order_goods, _mock_click):
        driver = mock.Mock()
        tab_element_1 = mock.Mock()
        tab_element_2 = mock.Mock()

        def execute_script_side_effect(_script, *args):
            if not args:
                return [
                    {"index": 0, "label": "H-RyanFowler602 1 - QTY: 2 Design Previews"},
                    {"index": 1, "label": "H-RyanFowler603 2 - QTY: 2 Design Previews"},
                ]
            index = int(args[0])
            return {
                "element": tab_element_1 if index == 0 else tab_element_2,
                "label": f"H-RyanFowler60{index + 2} {index + 1} - QTY: 2 Design Previews",
                "count": 2,
            }

        driver.execute_script.side_effect = execute_script_side_effect
        mock_order_goods.side_effect = [
            {"order_id": "4418860", "success": True, "outcome": "order_goods_ready", "message": "ready", "manual_review_required": False},
            {"order_id": "4418860", "success": True, "outcome": "order_goods_ready", "message": "ready", "manual_review_required": False},
        ]

        results = crm_order_goods._order_goods_for_all_stock_tabs(driver, "4418860", dry_run=True)

        self.assertEqual(len(results), 2)
        self.assertEqual(mock_order_goods.call_count, 2)
        self.assertEqual(results[0]["stock_tab_index"], 1)
        self.assertEqual(results[1]["stock_tab_index"], 2)
        self.assertEqual(results[0]["stock_tab_count"], 2)
        self.assertIn("H-RyanFowler602", results[0]["stock_tab_label"])

    @mock.patch.object(crm_order_goods, "_click_with_fallback")
    @mock.patch.object(crm_order_goods, "_order_goods_for_open_order")
    def test_order_goods_continues_when_multi_tab_has_manual_order_tab(self, mock_order_goods, _mock_click):
        driver = mock.Mock()
        tab_element_1 = mock.Mock()
        tab_element_2 = mock.Mock()

        def execute_script_side_effect(script, *args):
            if script == crm_order_goods.STOCK_TAB_SCRIPT and not args:
                return [
                    {"index": 0, "label": "H-NicoleDeGr551 1 - QTY: 2 Design Previews"},
                    {"index": 1, "label": "H-NicoleDeGr552 2 - QTY: 1 Design Previews"},
                ]
            if script == crm_order_goods.STOCK_TAB_SCRIPT:
                index = int(args[0])
                return {
                    "element": tab_element_1 if index == 0 else tab_element_2,
                    "label": f"H-NicoleDeGr55{index + 1} {index + 1} - QTY: 1 Design Previews",
                    "count": 2,
                }
            return "Stock : Ordered Order Goods From Vendor: Manual Order"

        driver.execute_script.side_effect = execute_script_side_effect
        mock_order_goods.side_effect = [
            crm_order_goods.TimeoutException("button not found"),
            {"order_id": "4427569", "success": True, "outcome": "order_goods_clicked", "message": "clicked", "manual_review_required": False},
        ]

        results = crm_order_goods._order_goods_for_all_stock_tabs(driver, "4427569", dry_run=False)

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["outcome"], "stock_tab_not_vendor_orderable")
        self.assertEqual(results[1]["outcome"], "order_goods_clicked")

    def test_order_goods_loads_skip_ids_from_order_goods_history_only(self):
        state = {
            "run_history": [
                {
                    "automation_key": "order_goods",
                    "order_ids": ["4418860", "not-an-id"],
                },
                {
                    "automation_key": "stock_unlocker",
                    "order_ids": ["4419999"],
                },
                {
                    "automation_label": "Rush Order Goods",
                    "order_ids": ["4418871"],
                },
            ]
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            state_path = handle.name
        try:
            skipped = crm_order_goods._load_historical_order_goods_order_ids(state_path)
        finally:
            Path(state_path).unlink(missing_ok=True)

        self.assertEqual(skipped, {"4418860", "4418871"})

    @mock.patch.object(crm_order_goods, "_load_historical_order_goods_order_ids", return_value={"4418860"})
    @mock.patch.object(crm_order_goods, "_build_crm_session_driver")
    @mock.patch.object(crm_order_goods, "_collect_batch_order_ids_with_driver", return_value=[])
    def test_order_goods_batch_excludes_historical_ids_from_collection(self, mock_collect, mock_build_driver, _mock_history):
        driver = mock.Mock()
        mock_build_driver.return_value = driver

        payload = crm_order_goods._run_batch_with_mode(False, dry_run=True, batch_size=None, profile_path=str(ROOT))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["skipped_historical_order_count"], 1)
        self.assertEqual(mock_collect.call_args.kwargs["exclude_order_ids"], {"4418860"})

    @mock.patch.object(crm_order_goods.shutil, "rmtree")
    @mock.patch.object(crm_order_goods, "_clone_profile_for_worker")
    @mock.patch.object(crm_order_goods, "_order_goods_worker_payload")
    @mock.patch.object(crm_order_goods, "_collect_batch_order_ids")
    @mock.patch.object(crm_order_goods, "_load_historical_order_goods_order_ids", return_value=set())
    def test_parallel_order_goods_retries_retryable_worker_once(
        self,
        _mock_history,
        mock_collect,
        mock_worker_payload,
        mock_clone_profile,
        _mock_rmtree,
    ):
        mock_collect.side_effect = [["4418860"], []]
        mock_clone_profile.side_effect = [
            ("temp-a", "profile-a"),
            ("temp-b", "profile-b"),
        ]
        mock_worker_payload.side_effect = [
            {
                "success": False,
                "order_ids": ["4418860"],
                "report": [
                    {
                        "order_id": "4418860",
                        "success": False,
                        "outcome": "worker_exception",
                        "message": "Message: button was not found",
                        "retryable": True,
                    }
                ],
            },
            {
                "success": True,
                "order_ids": ["4418860"],
                "report": [
                    {
                        "order_id": "4418860",
                        "success": True,
                        "outcome": "order_goods_clicked",
                        "message": "Clicked.",
                    }
                ],
            },
        ]

        payload = crm_order_goods._run_parallel_batch_with_mode(
            False,
            dry_run=False,
            batch_size=1,
            profile_path=str(ROOT),
            list_url="https://crm.example/report?shippingCharges%5Blow%5D=1",
            parallel_workers=4,
        )

        self.assertTrue(payload["success"])
        self.assertEqual(mock_worker_payload.call_count, 2)
        self.assertEqual(payload["report"][0]["outcome"], "order_goods_clicked")

    @mock.patch.object(server, "save_crm_state")
    @mock.patch.object(server, "load_crm_state")
    def test_order_goods_persists_into_shared_stock_history(self, mock_load_state, _mock_save_state):
        mock_load_state.return_value = {
            "last_run_timestamp": None,
            "last_run_success": None,
            "last_run_message": None,
            "last_order_count": 0,
            "total_runs": 0,
            "total_orders_processed": 0,
            "last_order_ids": [],
            "run_history": [],
        }

        state = server._persist_crm_order_goods_run_result(
            True,
            "ok",
            {
                "order_count": 2,
                "order_ids": ["4418860", "4418871"],
                "duration_seconds": 12.4,
                "report": [
                    {"order_id": "4418860", "success": True, "outcome": "already_stock_ordered", "message": "Already ordered.", "duration_seconds": 4.2},
                    {"order_id": "4418871", "success": False, "outcome": "order_goods_locked", "message": "Button disabled.", "duration_seconds": 5.5},
                ],
            },
            dry_run=False,
        )

        self.assertEqual(state["last_order_ids"], ["4418860", "4418871"])
        self.assertEqual(state["run_history"][0]["automation_key"], "order_goods")
        self.assertEqual(state["run_history"][0]["automation_label"], "Rush Order Goods")
        self.assertEqual(state["run_history"][0]["duration_seconds"], 12.4)
        self.assertEqual(state["run_history"][0]["order_results"][0]["status"], "Already stock ordered")
        self.assertEqual(state["run_history"][0]["order_results"][0]["duration_seconds"], 4.2)
        self.assertEqual(state["run_history"][0]["order_results"][1]["status"], "Locked")

    def test_order_goods_history_backfills_details_from_last_result(self):
        payload = {
            "action": "order_goods_batch",
            "order_ids": ["4418860", "4418871"],
            "report": [
                {"order_id": "4418860", "success": True, "outcome": "order_goods_clicked", "message": "Clicked."},
                {"order_id": "4418871", "success": False, "outcome": "order_goods_locked", "message": "Button disabled."},
            ],
        }
        saved_state = {
            "run_history": [
                {
                    "automation_key": "order_goods",
                    "automation_label": "Rush Order Goods",
                    "success": False,
                    "order_ids": ["4418860", "4418871"],
                    "message": "1 succeeded. 1 needs attention.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_state.json"
            state_path.write_text(json.dumps(saved_state), encoding="utf-8")
            with mock.patch.object(server, "CRM_STATE_FILE", str(state_path)):
                with mock.patch.object(server, "_load_last_result_payload", return_value=payload):
                    state = server.load_crm_state()

        results = state["run_history"][0]["order_results"]
        self.assertEqual(results[0]["status"], "Ordered")
        self.assertEqual(results[0]["message"], "Clicked.")
        self.assertEqual(results[1]["status"], "Locked")
        self.assertEqual(results[1]["message"], "Button disabled.")

    def test_processing_steps_run_validator_unlocker_then_order_goods_on_rush(self):
        state = {
            "processing_filter": "rush",
            "address_validator_enabled": True,
            "product_separator_enabled": True,
            "auto_splitter_enabled": True,
            "stock_unlocker_enabled": True,
            "order_goods_enabled": True,
        }

        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            ["address_validator_batch", "product_separator", "auto_splitter", "stock_unlocker", "order_goods"],
        )

    def test_processing_high_value_uses_rush_like_steps(self):
        state = {
            "processing_filter": "high_value",
            "address_validator_enabled": True,
            "product_separator_enabled": True,
            "auto_splitter_enabled": True,
            "stock_unlocker_enabled": True,
            "order_goods_enabled": True,
            "shipping_bypasser_enabled": True,
            "push_back_enabled": True,
        }

        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            [
                "address_validator_batch",
                "product_separator",
                "auto_splitter",
                "stock_unlocker",
                "order_goods",
                "shipping_bypasser",
                "push_back",
            ],
        )

    def test_processing_free_mode_enables_unlocker_and_order_goods(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=True,
                    order_goods_enabled=True,
                    processing_filter="free",
                )

        self.assertTrue(ok)
        self.assertTrue(state["address_validator_enabled"])
        self.assertTrue(state["product_separator_enabled"])
        self.assertTrue(state["auto_splitter_enabled"])
        self.assertTrue(state["stock_unlocker_enabled"])
        self.assertTrue(state["order_goods_enabled"])
        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            ["address_validator_batch", "product_separator", "auto_splitter", "stock_unlocker", "order_goods"],
        )

    def test_processing_free_unlocker_uses_free_report_url(self):
        list_url = "https://crm.example/report/free-unlocker"
        with mock.patch.object(server, "CRM_UNLOCKER_FREE_URL", list_url):
            self.assertEqual(
                server._crm_processing_mode_list_url_for_step("free", "stock_unlocker"),
                list_url,
            )

    def test_processing_free_order_goods_uses_free_report_url(self):
        list_url = "https://crm.example/report/free-order-goods"
        with mock.patch.object(server, "CRM_ORDER_GOODS_FREE_URL", list_url):
            self.assertEqual(
                server._crm_processing_mode_list_url_for_step("free", "order_goods"),
                list_url,
            )

    def test_processing_auto_splitter_uses_mode_specific_report_urls(self):
        urls = {
            "rush": "https://crm.example/report/split-rush",
            "free": "https://crm.example/report/split-free",
            "all": "https://crm.example/report/split-all",
            "high_value": "https://crm.example/report/split-high-value",
        }
        with mock.patch.multiple(
            server,
            CRM_AUTO_SPLITTER_LIST_URL_RUSH=urls["rush"],
            CRM_AUTO_SPLITTER_LIST_URL_FREE=urls["free"],
            CRM_AUTO_SPLITTER_LIST_URL_ALL=urls["all"],
            CRM_AUTO_SPLITTER_LIST_URL_HIGH_VALUE=urls["high_value"],
        ):
            for processing_filter, expected_url in urls.items():
                self.assertEqual(
                    server._crm_processing_mode_list_url_for_step(processing_filter, "auto_splitter"),
                    expected_url,
                )

    def test_processing_auto_splitter_reports_missing_mode_link(self):
        with mock.patch.object(server, "CRM_AUTO_SPLITTER_LIST_URL_ALL", ""):
            result = server._run_crm_processing_step("auto_splitter", "all")

        self.assertFalse(result["success"])
        self.assertIn("CRM_AUTO_SPLITTER_LIST_URL_ALL is empty", result["message"])

    @mock.patch.object(server, "_automation_stop_is_blocking", return_value=False)
    @mock.patch.object(server, "_execute_crm_auto_splitter_worker")
    @mock.patch.object(server, "_run_script")
    def test_auto_splitter_batch_preflights_then_splits_each_list_order(
        self,
        mock_run_script,
        mock_execute_splitter,
        _mock_stop,
    ):
        mock_run_script.return_value = (
            True,
            "Found two orders.",
            {"success": True, "order_ids": ["4700001", "4700002"]},
        )
        mock_execute_splitter.side_effect = [
            (True, "Preflight one.", {"expected_tab_count": 12, "divisions": 2}),
            (True, "Split one.", {"new_order_ids": ["4800001", "4800002"]}),
            (True, "Preflight two.", {"expected_tab_count": 21, "divisions": 3}),
            (False, "Split two failed.", {"new_order_ids": []}),
        ]

        ok, message, payload = server._execute_crm_auto_splitter_batch(
            "https://crm.example/report/split-all",
            minimum_tabs=10,
            parallel_workers=3,
        )

        self.assertFalse(ok)
        self.assertIn("1 need attention", message)
        self.assertEqual(payload["order_ids"], ["4700001", "4700002"])
        self.assertEqual(payload["new_order_ids"], ["4800001", "4800002"])
        self.assertEqual([row["success"] for row in payload["order_results"]], [True, False])
        self.assertEqual(mock_execute_splitter.call_count, 4)
        self.assertEqual(mock_execute_splitter.call_args_list[0].args[:3], ("4700001", None, None))
        self.assertEqual(mock_execute_splitter.call_args_list[1].args[:3], ("4700001", 12, 2))
        self.assertEqual(mock_execute_splitter.call_args_list[2].args[:3], ("4700002", None, None))
        self.assertEqual(mock_execute_splitter.call_args_list[3].args[:3], ("4700002", 21, 3))

    def test_processing_free_order_goods_reports_missing_mode_link(self):
        with mock.patch.object(server, "CRM_ORDER_GOODS_FREE_URL", ""):
            result = server._run_crm_processing_step("order_goods", "free")

        self.assertFalse(result["success"])
        self.assertIn("CRM_ORDER_GOODS_FREE_URL is empty", result["message"])

    def test_processing_state_migrates_existing_free_mode_to_unlocker_enabled(self):
        saved_state = server._default_crm_processing_state()
        saved_state.pop("free_unlocker_mode_available", None)
        saved_state["mode_preferences"]["free"]["stock_unlocker_enabled"] = False
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            state_path.write_text(json.dumps(saved_state), encoding="utf-8")
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                state = server.load_crm_processing_state()

        self.assertTrue(state["free_unlocker_mode_available"])
        self.assertTrue(state["mode_preferences"]["free"]["stock_unlocker_enabled"])

    def test_processing_state_migrates_new_free_and_all_steps_to_enabled(self):
        saved_state = server._default_crm_processing_state()
        saved_state.pop("expanded_unlocker_order_goods_modes_available", None)
        saved_state["mode_preferences"]["free"]["order_goods_enabled"] = False
        saved_state["mode_preferences"]["all"]["stock_unlocker_enabled"] = False
        saved_state["mode_preferences"]["all"]["order_goods_enabled"] = False
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            state_path.write_text(json.dumps(saved_state), encoding="utf-8")
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                state = server.load_crm_processing_state()

        self.assertTrue(state["expanded_unlocker_order_goods_modes_available"])
        self.assertTrue(state["mode_preferences"]["free"]["order_goods_enabled"])
        self.assertTrue(state["mode_preferences"]["all"]["stock_unlocker_enabled"])
        self.assertTrue(state["mode_preferences"]["all"]["order_goods_enabled"])

    @mock.patch.object(server, "_finish_crm_runtime")
    @mock.patch.object(server, "_persist_crm_run_result", return_value={})
    @mock.patch.object(server, "_run_crm_unlock_with_retry", return_value=(True, "ok", {"success": True}))
    @mock.patch.object(server, "_start_crm_runtime")
    def test_processing_free_unlocker_step_passes_free_report_to_worker(
        self,
        _mock_start,
        mock_run_unlocker,
        _mock_persist,
        _mock_finish,
    ):
        list_url = "https://crm.example/report/free-unlocker"
        with mock.patch.object(server, "CRM_UNLOCKER_FREE_URL", list_url):
            result = server._run_crm_processing_step("stock_unlocker", "free")

        self.assertTrue(result["success"])
        mock_run_unlocker.assert_called_once_with(dry_run=False, list_url=list_url)

    def test_processing_switch_to_rush_preserves_selected_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=True,
                    order_goods_enabled=True,
                    processing_filter="free",
                )
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=False,
                    address_validator_enabled=False,
                    product_separator_enabled=False,
                    auto_splitter_enabled=False,
                    order_goods_enabled=False,
                    processing_filter="rush",
                )

        self.assertTrue(ok)
        self.assertFalse(state["address_validator_enabled"])
        self.assertFalse(state["product_separator_enabled"])
        self.assertFalse(state["stock_unlocker_enabled"])
        self.assertFalse(state["order_goods_enabled"])
        self.assertEqual(server._crm_processing_selected_steps_from_state(state), [])

    def test_processing_mode_preferences_restore_after_switch_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                server.update_crm_processing_preferences(
                    stock_unlocker_enabled=False,
                    address_validator_enabled=True,
                    product_separator_enabled=False,
                    auto_splitter_enabled=True,
                    order_goods_enabled=True,
                    shipping_bypasser_enabled=True,
                    processing_filter="rush",
                )
                server.update_crm_processing_preferences(
                    address_validator_enabled=False,
                    order_goods_enabled=False,
                    shipping_bypasser_enabled=True,
                    processing_filter="813",
                )
                ok, _message, state = server.update_crm_processing_preferences(processing_filter="rush")
                reloaded = server.load_crm_processing_state()

        self.assertTrue(ok)
        self.assertEqual(state["processing_filter"], "rush")
        self.assertTrue(state["address_validator_enabled"])
        self.assertFalse(state["product_separator_enabled"])
        self.assertFalse(state["stock_unlocker_enabled"])
        self.assertTrue(state["order_goods_enabled"])
        self.assertTrue(state["shipping_bypasser_enabled"])
        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            ["address_validator_batch", "auto_splitter", "order_goods", "shipping_bypasser"],
        )
        self.assertEqual(reloaded["processing_filter"], "rush")
        self.assertEqual(
            server._crm_processing_selected_steps_from_state(reloaded),
            ["address_validator_batch", "auto_splitter", "order_goods", "shipping_bypasser"],
        )
        self.assertFalse(reloaded["mode_preferences"]["813"]["order_goods_enabled"])
        self.assertTrue(reloaded["mode_preferences"]["813"]["shipping_bypasser_enabled"])

    def test_processing_ignores_legacy_mass_emailer_preference(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                server.update_crm_processing_preferences(
                    mass_emailer_enabled=False,
                    address_validator_enabled=True,
                    processing_filter="rush",
                )
                state_813 = server.update_crm_processing_preferences(processing_filter="813")[2]
                server.update_crm_processing_preferences(
                    mass_emailer_enabled=True,
                    processing_filter="813",
                )
                state_rush = server.update_crm_processing_preferences(processing_filter="rush")[2]
                reloaded = server.load_crm_processing_state()

        self.assertNotIn("mass_emailer_enabled", state_813)
        self.assertNotIn("mass_emailer", server._crm_processing_selected_steps_from_state(state_813))
        self.assertNotIn("mass_emailer_enabled", state_rush)
        self.assertNotIn("mass_emailer", server._crm_processing_selected_steps_from_state(state_rush))
        self.assertNotIn("mass_emailer_enabled", reloaded)
        self.assertNotIn("mass_emailer_enabled", reloaded["mode_preferences"]["rush"])
        self.assertNotIn("mass_emailer_enabled", reloaded["mode_preferences"]["813"])

    def test_processing_preferences_route_ignores_queue_only_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                # Route behavior is under test here, not optional remote-access
                # authentication from this machine's local config.py.
                with mock.patch.object(server, "APP_PIN_REQUIRED", False):
                    response = server.app.test_client().post(
                        "/crm/process/preferences",
                        json={
                            "stock_unlocker_enabled": True,
                            "address_validator_enabled": False,
                            "product_separator_enabled": False,
                            "order_goods_enabled": True,
                            "processing_filter": "rush",
                            "advanced_mode": "repeat",
                            "repeat_interval_minutes": 10,
                        },
                    )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertFalse(payload["state"]["address_validator_enabled"])
        self.assertFalse(payload["state"]["product_separator_enabled"])
        self.assertTrue(payload["state"]["stock_unlocker_enabled"])
        self.assertTrue(payload["state"]["order_goods_enabled"])

    def test_processing_state_load_handles_legacy_history_without_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "run_history": [
                            {
                                "timestamp": "2026-05-30T10:00:00",
                                "processing_filter": "rush",
                                "selected_steps": ["stock_unlocker"],
                                "step_results": [],
                                "duration_seconds": 1.2,
                                "message": "Legacy row",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                state = server.load_crm_processing_state()

        self.assertFalse(state["run_history"][0]["success"])
        self.assertEqual(state["run_history"][0]["selected_steps"], ["stock_unlocker"])

    def test_processing_step_metrics_count_orders_and_order_errors(self):
        payload = {
            "success": False,
            "order_count": 3,
            "order_ids": ["4410001", "4410002", "4410003"],
            "report": [
                {"order_id": "4410001", "success": True, "outcome": "order_goods_clicked"},
                {"order_id": "4410002", "success": False, "outcome": "order_goods_locked"},
                {"order_id": "4410003", "success": True, "outcome": "already_stock_ordered"},
            ],
        }

        metrics = server._crm_processing_step_metrics(
            "order_goods",
            payload,
            False,
            "Order Goods processed 3 order(s); 1 needs attention.",
        )

        self.assertEqual(metrics["order_count"], 3)
        self.assertEqual(metrics["successful_order_count"], 2)
        self.assertEqual(metrics["error_count"], 1)

    def test_processing_step_error_details_keep_each_failed_order(self):
        payload = {
            "success": False,
            "order_count": 3,
            "order_ids": ["4410001", "4410002", "4410003"],
            "report": [
                {
                    "order_id": "4410001",
                    "success": True,
                    "outcome": "order_goods_clicked",
                    "message": "Ordered successfully.",
                },
                {
                    "order_id": "4410002",
                    "success": False,
                    "outcome": "order_goods_locked",
                    "message": "The order is locked.",
                },
                {
                    "order_id": "4410003",
                    "success": False,
                    "outcome": "missing_due_date",
                    "message": "The due date is missing.",
                },
            ],
        }

        details = server._crm_processing_step_error_details(
            "order_goods",
            payload,
            False,
            "2 order(s) need attention.",
        )

        self.assertEqual([item["order_id"] for item in details], ["4410002", "4410003"])
        self.assertEqual(
            [item["message"] for item in details],
            ["The order is locked.", "The due date is missing."],
        )

    def test_processing_summary_treats_recorded_order_errors_as_attention(self):
        success, message = server._build_crm_processing_summary(
            [
                {
                    "key": "shipping_bypasser",
                    "success": True,
                    "order_count": 2,
                    "successful_order_count": 1,
                    "error_count": 1,
                    "errors": [
                        {
                            "order_id": "4410002",
                            "status": "Partially successful",
                            "message": "One stock tab needs attention.",
                        }
                    ],
                    "message": "One order partially succeeded.",
                }
            ]
        )

        self.assertFalse(success)
        self.assertIn("Shipping Bypasser", message)

    def test_processing_report_filters_daily_weekly_monthly_and_all_time(self):
        state = server._default_crm_processing_state()
        server._append_crm_processing_report(
            state,
            "2026-07-16T09:00:00",
            [
                {
                    "key": "address_validator_batch",
                    "success": True,
                    "order_count": 4,
                    "error_count": 1,
                    "duration_seconds": 30,
                    "message": "Processed 4 order(s). 1 order(s) need attention.",
                }
            ],
        )
        server._append_crm_processing_report(
            state,
            "2026-07-14T09:00:00",
            [
                {
                    "key": "stock_unlocker",
                    "success": True,
                    "order_count": 2,
                    "error_count": 0,
                    "duration_seconds": 10,
                    "message": "Unlocked 2 orders successfully.",
                }
            ],
        )
        server._append_crm_processing_report(
            state,
            "2026-06-30T09:00:00",
            [
                {
                    "key": "order_goods",
                    "success": False,
                    "order_count": 5,
                    "error_count": 2,
                    "duration_seconds": 50,
                    "message": "Processed 5 order(s). 2 order(s) need attention.",
                }
            ],
        )

        report = server._build_crm_processing_report(state, now=server.datetime(2026, 7, 16, 12, 0, 0))

        self.assertEqual(report["periods"]["daily"]["total_orders_processed"], 4)
        self.assertEqual(report["periods"]["weekly"]["total_orders_processed"], 6)
        self.assertEqual(report["periods"]["monthly"]["total_orders_processed"], 6)
        self.assertEqual(report["periods"]["all"]["total_orders_processed"], 11)
        self.assertEqual(report["periods"]["all"]["total_errors"], 3)
        self.assertEqual(report["periods"]["all"]["total_duration_seconds"], 90.0)

    def test_processing_status_payload_includes_quick_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                payload = server.get_crm_processing_status_payload()

        self.assertIn("report", payload)
        self.assertEqual(set(payload["report"]["periods"]), {"daily", "weekly", "monthly", "all"})
        self.assertEqual(len(payload["report"]["periods"]["all"]["rows"]), 8)

    def test_processing_report_backfills_live_sheet_scanner_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            processing_path = Path(tmp) / "crm_processing_state.json"
            scanner_path = Path(tmp) / "crm_mass_emailer_state.json"
            processing_path.write_text(json.dumps(server._default_crm_processing_state() | {
                "report_sheet_scanner_available": False,
            }), encoding="utf-8")
            scanner_path.write_text(
                json.dumps(
                    {
                        "run_history": [
                            {
                                "timestamp": "2026-07-16T09:00:00",
                                "success": False,
                                "action": "process_queue",
                                "dry_run": False,
                                "order_count": 3,
                                "failure_count": 1,
                                "duration_seconds": 25.5,
                                "message": "Processed 3 rows; 1 failed.",
                            },
                            {
                                "timestamp": "2026-07-16T08:00:00",
                                "success": True,
                                "action": "process_queue",
                                "dry_run": True,
                                "order_count": 9,
                                "failure_count": 0,
                                "duration_seconds": 99,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(processing_path)), \
                 mock.patch.object(server, "CRM_MASS_EMAILER_STATE_FILE", str(scanner_path)):
                state = server.load_crm_processing_state()
                report = server._build_crm_processing_report(
                    state,
                    now=server.datetime(2026, 7, 16, 12, 0, 0),
                )

        scanner_row = next(row for row in report["periods"]["daily"]["rows"] if row["key"] == "mass_emailer")
        self.assertEqual(scanner_row["orders_processed"], 4)
        self.assertEqual(scanner_row["successful_orders"], 3)
        self.assertEqual(scanner_row["error_count"], 1)
        self.assertEqual(scanner_row["duration_seconds"], 25.5)
        self.assertEqual(scanner_row["run_count"], 1)

    def test_processing_rush_can_run_unlocker_and_order_goods_without_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=False,
                    product_separator_enabled=False,
                    auto_splitter_enabled=False,
                    order_goods_enabled=True,
                    processing_filter="rush",
                )

        self.assertTrue(ok)
        self.assertFalse(state["address_validator_enabled"])
        self.assertFalse(state["product_separator_enabled"])
        self.assertTrue(state["stock_unlocker_enabled"])
        self.assertTrue(state["order_goods_enabled"])
        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            ["stock_unlocker", "order_goods"],
        )

    def test_processing_bypass_only_selects_shipping_bypasser_step(self):
        state = {
            "processing_filter": "rush",
            "address_validator_enabled": False,
            "product_separator_enabled": False,
            "stock_unlocker_enabled": False,
            "order_goods_enabled": False,
            "shipping_bypasser_enabled": True,
        }

        self.assertEqual(server._crm_processing_selected_steps_from_state(state), ["shipping_bypasser"])

    def test_shipping_bypasser_history_keeps_sanmar_confirmation_link(self):
        payload = {
            "success": True,
            "order_ids": ["4609966"],
            "report": [
                {
                    "order_id": "4609966",
                    "success": True,
                    "outcome": "shipping_bypass_ordered",
                    "message": "Ordered.",
                    "sanmar_confirmation": {
                        "url": "https://www.sanmar.com/checkout/thank-you",
                        "web_reference": "12345",
                        "po": "H-Example123",
                        "screenshot": "screenshots/sanmar_shipping_bypass_4609966.png",
                    },
                }
            ],
        }

        results = server._build_crm_order_goods_order_results(payload)

        self.assertEqual(results[0]["status"], "Bypassed")
        self.assertEqual(results[0]["sanmar_confirmation"]["url"], "https://www.sanmar.com/checkout/thank-you")
        self.assertEqual(results[0]["sanmar_confirmation"]["po"], "H-Example123")

    def test_shipping_bypasser_runs_each_stock_tab(self):
        crm_driver = mock.Mock()
        sanmar_driver = mock.Mock()
        tabs = [
            {"label": "H-TabOne 1 - QTY : 1 Design Previews"},
            {"label": "H-TabTwo 2 - QTY : 2 Design Previews"},
        ]

        def process_tab(_crm_driver, _sanmar_driver, order_id, **kwargs):
            return {
                "order_id": order_id,
                "success": True,
                "outcome": "shipping_bypass_ready",
                "message": "Ready.",
                "manual_review_required": False,
                "stock_tab_index": kwargs.get("stock_tab_index"),
                "stock_tab_count": kwargs.get("stock_tab_count"),
                "stock_tab_label": kwargs.get("stock_tab_label"),
            }

        with mock.patch.object(crm_shipping_bypasser, "_open_target_order") as mock_open, \
             mock.patch.object(crm_shipping_bypasser, "_wait_for_order_goods_page_ready") as mock_ready, \
             mock.patch.object(crm_shipping_bypasser, "_find_stock_tabs", return_value=tabs), \
             mock.patch.object(crm_shipping_bypasser, "_activate_stock_tab", side_effect=tabs), \
             mock.patch.object(crm_shipping_bypasser, "_process_open_order", side_effect=process_tab) as mock_process:
            results = crm_shipping_bypasser._run_order_with_drivers(crm_driver, sanmar_driver, "4636204", dry_run=True)

        self.assertEqual(mock_open.call_count, 2)
        self.assertEqual(mock_ready.call_count, 2)
        self.assertEqual(mock_process.call_count, 2)
        self.assertEqual([item["stock_tab_index"] for item in results], [1, 2])
        self.assertEqual([item["stock_tab_count"] for item in results], [2, 2])
        self.assertIn("H-TabTwo", results[1]["stock_tab_label"])

    def test_shipping_bypasser_keeps_successful_tabs_when_later_reload_fails(self):
        crm_driver = mock.Mock()
        sanmar_driver = mock.Mock()
        tabs = [
            {"label": "H-TabOne 1 - QTY : 1 Design Previews"},
            {"label": "H-TabTwo 2 - QTY : 2 Design Previews"},
            {"label": "H-TabThree 3 - QTY : 1 Design Previews"},
        ]

        def process_tab(_crm_driver, _sanmar_driver, order_id, **kwargs):
            tab_index = kwargs.get("stock_tab_index")
            return {
                "order_id": order_id,
                "success": True,
                "outcome": "shipping_bypass_ordered",
                "message": "Ordered.",
                "manual_review_required": False,
                "stock_tab_index": tab_index,
                "stock_tab_count": kwargs.get("stock_tab_count"),
                "stock_tab_label": kwargs.get("stock_tab_label"),
                "order": {"po": f"H-Tab{tab_index}"},
                "sanmar_confirmation": {
                    "po": f"H-Tab{tab_index}",
                    "url": f"https://www.sanmar.com/checkout/submission?orderCode={tab_index}",
                },
            }

        with mock.patch.object(
            crm_shipping_bypasser,
            "_open_target_order",
            side_effect=[None, None, RuntimeError("Order 4636204 did not open before the timeout expired.")],
        ), mock.patch.object(crm_shipping_bypasser, "_wait_for_order_goods_page_ready"), \
             mock.patch.object(crm_shipping_bypasser, "_find_stock_tabs", return_value=tabs), \
             mock.patch.object(crm_shipping_bypasser, "_activate_stock_tab", side_effect=tabs[:2]), \
             mock.patch.object(crm_shipping_bypasser, "_process_open_order", side_effect=process_tab), \
             mock.patch.object(crm_shipping_bypasser, "safe_take_screenshot"):
            results = crm_shipping_bypasser._run_order_with_drivers(crm_driver, sanmar_driver, "4636204", dry_run=False)

        self.assertEqual(len(results), 3)
        self.assertEqual([item["success"] for item in results], [True, True, False])
        self.assertEqual(results[2]["stock_tab_index"], 3)
        self.assertEqual(results[2]["outcome"], "worker_exception")

        message = crm_shipping_bypasser._summary_message(results, refresh_passes=1, order_count=1)

        self.assertIn("partially succeeded", message)
        self.assertIn("customer PO H-Tab1", message)
        self.assertIn("https://www.sanmar.com/checkout/submission?orderCode=1", message)
        self.assertIn("customer PO H-Tab2", message)
        self.assertIn("https://www.sanmar.com/checkout/submission?orderCode=2", message)

    def test_shipping_bypasser_stops_when_tab_detection_is_incomplete(self):
        crm_driver = mock.Mock()
        sanmar_driver = mock.Mock()
        tabs = [{"label": "H-TabOne 1 - QTY : 1 Design Previews"}]

        with mock.patch.object(crm_shipping_bypasser, "_open_target_order") as mock_open, \
             mock.patch.object(crm_shipping_bypasser, "_wait_for_order_goods_page_ready") as mock_ready, \
             mock.patch.object(crm_shipping_bypasser, "_find_stock_tabs", return_value=tabs), \
             mock.patch.object(crm_shipping_bypasser, "_visible_design_tab_number_hints", return_value=[1, 2, 3]), \
             mock.patch.object(crm_shipping_bypasser, "_process_open_order") as mock_process:
            results = crm_shipping_bypasser._run_order_with_drivers(crm_driver, sanmar_driver, "4638803", dry_run=False)

        mock_open.assert_called_once()
        self.assertEqual(mock_ready.call_count, 2)
        crm_driver.refresh.assert_called_once_with()
        mock_process.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertEqual(results[0]["outcome"], "stock_tab_detection_incomplete")
        self.assertTrue(results[0]["manual_review_required"])
        self.assertEqual(results[0]["detected_design_tab_numbers"], [1, 2, 3])

    def test_shipping_bypasser_manual_order_guard_includes_s_and_s(self):
        driver = mock.Mock()

        def execute_script(script, po):
            self.assertEqual(po, "H-Example123")
            self.assertIn("sanmar", script)
            self.assertIn("s\\s*&\\s*s", script)
            self.assertIn("ssactivewear", script)
            self.assertNotIn("poNodes", script)
            self.assertIn("-[a-z0-9]", script)
            return True

        driver.execute_script.side_effect = execute_script

        self.assertTrue(crm_shipping_bypasser._crm_manual_order_row_exists(driver, "H-Example123"))

    def test_shipping_bypasser_yellow_manual_order_visual_guard_accepts_s_and_s_suffix_po(self):
        driver = mock.Mock()

        def execute_script(script, po):
            self.assertEqual(po, "H-MarashaMil266")
            self.assertIn("yellowish", script)
            self.assertIn("s\\s*&\\s*s", script)
            self.assertIn("-[a-z0-9]", script)
            return True

        driver.execute_script.side_effect = execute_script

        self.assertTrue(crm_shipping_bypasser._crm_stock_order_yellow_visual_exists(driver, "H-MarashaMil266"))

    def test_shipping_bypasser_history_customer_po_prevents_duplicate_order(self):
        state = {
            "run_history": [
                {
                    "automation_key": "shipping_bypasser",
                    "dry_run": False,
                    "order_results": [
                        {
                            "success": True,
                            "outcome": "shipping_bypass_ordered",
                            "sanmar_confirmation": {"po": "H-MarashaMil266"},
                        }
                    ],
                }
            ]
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(state, handle)
            state_path = handle.name

        try:
            self.assertTrue(crm_shipping_bypasser._historical_shipping_bypass_po_exists("H-MarashaMil266", state_path=state_path))
            self.assertFalse(crm_shipping_bypasser._historical_shipping_bypass_po_exists("H-MarashaMil265", state_path=state_path))
        finally:
            Path(state_path).unlink(missing_ok=True)

    def test_shipping_bypasser_weekend_eta_moves_production_target_to_monday(self):
        saturday_eta = crm_shipping_bypasser.datetime(2026, 6, 13).date()
        sunday_eta = crm_shipping_bypasser.datetime(2026, 6, 14).date()
        monday = crm_shipping_bypasser.datetime(2026, 6, 15).date()

        self.assertEqual(crm_shipping_bypasser._shipping_bypasser_production_target_for_eta(saturday_eta), monday)
        self.assertEqual(crm_shipping_bypasser._shipping_bypasser_production_target_for_eta(sunday_eta), monday)
        self.assertEqual(crm_shipping_bypasser._shipping_bypasser_production_target_for_eta(monday), monday)

    def test_shipping_bypasser_weekend_eta_requires_due_date_after_monday(self):
        saturday_eta = crm_shipping_bypasser.datetime(2026, 6, 13).date()
        monday_due_date = crm_shipping_bypasser.datetime(2026, 6, 15).date()

        production_target = crm_shipping_bypasser._shipping_bypasser_production_target_for_eta(saturday_eta)

        self.assertFalse(production_target < monday_due_date)

    def test_shipping_bypasser_production_date_warning_ok_is_acknowledged(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"success": True, "found": True}

        with mock.patch.object(crm_shipping_bypasser.time, "sleep"):
            clicked = crm_shipping_bypasser._acknowledge_crm_production_date_warning(driver, timeout=0)

        self.assertTrue(clicked)
        script = driver.execute_script.call_args.args[0]
        self.assertIn("ground\\s+shipping", script)
        self.assertIn("^ok$", script)

    def test_shipping_bypasser_production_date_save_retries_refresh_before_failing(self):
        driver = mock.Mock()
        target_date = crm_shipping_bypasser.datetime(2026, 6, 18).date()
        stale_date = crm_shipping_bypasser.datetime(2026, 6, 17).date()

        with mock.patch.object(crm_shipping_bypasser, "_find_clickable_by_text", side_effect=[mock.Mock(), mock.Mock()]), \
             mock.patch.object(crm_shipping_bypasser, "_click_with_fallback"), \
             mock.patch.object(crm_shipping_bypasser, "_set_crm_edit_date_field", return_value=target_date), \
             mock.patch.object(crm_shipping_bypasser, "_wait_for_crm_shipping_method_selection"), \
             mock.patch.object(crm_shipping_bypasser, "_acknowledge_crm_production_date_warning", side_effect=[True, False]) as mock_warning, \
             mock.patch.object(crm_shipping_bypasser, "_wait_for_text"), \
             mock.patch.object(crm_shipping_bypasser, "_wait_for_order_goods_page_ready"), \
             mock.patch.object(crm_shipping_bypasser, "_extract_order_data", return_value={"production_date": stale_date}), \
             mock.patch.object(crm_shipping_bypasser.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "after save and refresh"):
                crm_shipping_bypasser._change_crm_production_date(driver, "4681506", target_date)

        self.assertEqual(
            [call.kwargs for call in mock_warning.call_args_list],
            [{"timeout": 12}, {"timeout": 5}],
        )
        self.assertTrue(all(call.args == (driver,) for call in mock_warning.call_args_list))
        self.assertEqual(driver.refresh.call_count, 2)

    def test_shipping_bypasser_single_warehouse_eta_is_scoped_to_selected_warehouse(self):
        driver = mock.Mock()
        selected_eta = crm_shipping_bypasser.datetime(2026, 6, 16).date()

        with mock.patch.object(
            crm_shipping_bypasser,
            "_select_ups_and_latest_eta_by_warehouse",
            return_value={
                "latest_eta": selected_eta,
                "eta_by_warehouse": {"Richmond, VA": selected_eta},
            },
        ) as mock_scoped, mock.patch.object(
            crm_shipping_bypasser,
            "_select_ups_and_eta",
            return_value=crm_shipping_bypasser.datetime(2026, 6, 22).date(),
        ) as mock_unscoped:
            eta_state = crm_shipping_bypasser._select_ups_eta_for_shipping_plan(
                driver,
                "inhouse",
                warehouse="Richmond, VA",
                selected_warehouses=["Richmond, VA"],
                multi_warehouse=False,
            )

        self.assertEqual(eta_state["eta"], selected_eta)
        self.assertEqual(eta_state["eta_by_warehouse"], {"Richmond, VA": "2026-06-16"})
        mock_scoped.assert_called_once_with(driver, ["Richmond, VA"])
        mock_unscoped.assert_not_called()

    def test_sanmar_checkout_shipping_wait_allows_loader_to_clear(self):
        driver = mock.Mock()
        driver.execute_script.side_effect = [
            {
                "readyState": "complete",
                "loading": True,
                "hasShippingMethodText": False,
                "hasUpsText": False,
                "upsRadioCount": 0,
                "loadingNodes": [{"className": "checkout-logo-loading"}],
            },
            {
                "readyState": "complete",
                "loading": False,
                "hasShippingMethodText": True,
                "hasUpsText": True,
                "upsRadioCount": 1,
            },
        ]

        with mock.patch.object(crm_shipping_bypasser.time, "sleep") as mock_sleep:
            state = crm_shipping_bypasser._wait_for_sanmar_checkout_shipping_methods(
                driver,
                timeout=3,
                settle_seconds=0,
            )

        self.assertEqual(state["upsRadioCount"], 1)
        mock_sleep.assert_called_once_with(0.5)

    def test_shipping_bypasser_waits_for_checkout_shipping_before_ups_lookup(self):
        driver = mock.Mock()
        selected_eta = crm_shipping_bypasser.datetime(2026, 6, 16).date()
        calls = []

        def wait_for_shipping(_driver):
            calls.append("wait")

        def scoped_lookup(_driver, _warehouses):
            calls.append("lookup")
            return {
                "latest_eta": selected_eta,
                "eta_by_warehouse": {"Richmond, VA": selected_eta},
            }

        with mock.patch.object(
            crm_shipping_bypasser,
            "_wait_for_sanmar_checkout_shipping_methods",
            side_effect=wait_for_shipping,
        ), mock.patch.object(
            crm_shipping_bypasser,
            "_select_ups_and_latest_eta_by_warehouse",
            side_effect=scoped_lookup,
        ):
            crm_shipping_bypasser._select_ups_eta_for_shipping_plan(
                driver,
                "inhouse",
                warehouse="Richmond, VA",
                selected_warehouses=["Richmond, VA"],
                multi_warehouse=False,
            )

        self.assertEqual(calls, ["wait", "lookup"])

    def test_shipping_bypasser_ups_failure_names_selected_warehouse(self):
        driver = mock.Mock()

        with mock.patch.object(
            crm_shipping_bypasser,
            "_select_ups_and_latest_eta_by_warehouse",
            side_effect=RuntimeError("UPS estimated delivery date could not be read for warehouse(s): Richmond, VA"),
        ), mock.patch.object(
            crm_shipping_bypasser,
            "_select_ups_and_eta",
            side_effect=RuntimeError("UPS shipping option was not available."),
        ):
            with self.assertRaisesRegex(RuntimeError, "Selected warehouse: Richmond, VA"):
                crm_shipping_bypasser._select_ups_eta_for_shipping_plan(
                    driver,
                    "inhouse",
                    warehouse="Richmond, VA",
                    selected_warehouses=["Richmond, VA"],
                    multi_warehouse=False,
                )

    def test_shipping_bypasser_ups_failure_captures_checkout_diagnostic_when_order_id_supplied(self):
        driver = mock.Mock()
        diagnostic = {"screenshot": "runtime/screenshots/sanmar_checkout_4802826.png"}

        with mock.patch.object(
            crm_shipping_bypasser,
            "_select_ups_and_latest_eta_by_warehouse",
            side_effect=RuntimeError("UPS estimated delivery date could not be read for warehouse(s): Richmond, VA"),
        ), mock.patch.object(
            crm_shipping_bypasser,
            "_select_ups_and_eta",
            side_effect=RuntimeError("UPS shipping option was not available."),
        ), mock.patch.object(
            crm_shipping_bypasser,
            "_capture_sanmar_checkout_diagnostic",
            return_value=diagnostic,
        ) as mock_capture:
            with self.assertRaisesRegex(RuntimeError, "Selected warehouse: Richmond, VA") as ctx:
                crm_shipping_bypasser._select_ups_eta_for_shipping_plan(
                    driver,
                    "inhouse",
                    warehouse="Richmond, VA",
                    selected_warehouses=["Richmond, VA"],
                    multi_warehouse=False,
                    order_id="4802826",
                )

        mock_capture.assert_called_once_with(driver, order_id="4802826", reason="ups_unavailable")
        self.assertEqual(getattr(ctx.exception, "sanmar_checkout_diagnostic"), diagnostic)

    def test_shipping_bypasser_partial_history_and_notification_names_skipped_tab(self):
        payload = {
            "success": False,
            "order_ids": ["4636204"],
            "report": [
                {
                    "order_id": "4636204",
                    "success": True,
                    "outcome": "shipping_bypass_ordered",
                    "message": "Tab one ordered.",
                    "stock_tab_index": 1,
                    "stock_tab_count": 2,
                    "stock_tab_label": "H-TabOne",
                },
                {
                    "order_id": "4636204",
                    "success": False,
                    "outcome": "sanmar_product_not_found",
                    "message": "SanMar product could not be found for ABC123.",
                    "stock_tab_index": 2,
                    "stock_tab_count": 2,
                    "stock_tab_label": "H-TabTwo",
                },
            ],
        }

        results = server._build_crm_order_goods_order_results(payload)

        self.assertFalse(results[0]["success"])
        self.assertEqual(results[0]["status"], "Partially successful")
        self.assertEqual(results[0]["outcome"], "partial_success")
        self.assertIn("tab 2 of 2", results[0]["message"])
        self.assertIn("H-TabTwo", results[0]["message"])

        with mock.patch.object(server, "notify_user") as mock_notify:
            server._notify_shipping_bypasser_problem_orders(payload)

        mock_notify.assert_called_once()
        self.assertIn("4636204 tab 2 of 2", mock_notify.call_args.args[1])
        self.assertIn("H-TabTwo", mock_notify.call_args.args[1])

    def test_processing_all_mode_can_disable_validator_and_run_other_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=False,
                    product_separator_enabled=False,
                    auto_splitter_enabled=False,
                    order_goods_enabled=True,
                    processing_filter="all",
                )

        self.assertTrue(ok)
        self.assertFalse(state["address_validator_enabled"])
        self.assertFalse(state["product_separator_enabled"])
        self.assertTrue(state["stock_unlocker_enabled"])
        self.assertTrue(state["order_goods_enabled"])
        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            ["stock_unlocker", "order_goods"],
        )

    def test_processing_all_unlocker_and_order_goods_use_all_report_urls(self):
        unlocker_url = "https://crm.example/report/all-unlocker"
        order_goods_url = "https://crm.example/report/all-order-goods"
        with mock.patch.object(server, "CRM_UNLOCKER_ALL_URL", unlocker_url), mock.patch.object(
            server, "CRM_ORDER_GOODS_ALL_URL", order_goods_url
        ):
            self.assertEqual(
                server._crm_processing_mode_list_url_for_step("all", "stock_unlocker"),
                unlocker_url,
            )
            self.assertEqual(
                server._crm_processing_mode_list_url_for_step("all", "order_goods"),
                order_goods_url,
            )

    def test_processing_all_unlocker_reports_missing_mode_link(self):
        with mock.patch.object(server, "CRM_UNLOCKER_ALL_URL", ""):
            result = server._run_crm_processing_step("stock_unlocker", "all")

        self.assertFalse(result["success"])
        self.assertIn("CRM_UNLOCKER_ALL_URL is empty", result["message"])

    @mock.patch.object(server, "_finish_crm_address_runtime")
    @mock.patch.object(server, "_persist_crm_address_run_result", return_value={})
    @mock.patch.object(server, "_run_crm_address_with_retry")
    @mock.patch.object(server, "_start_crm_address_runtime")
    @mock.patch.object(server, "load_crm_address_state")
    def test_processing_all_address_step_uses_configured_all_list_url(
        self,
        mock_load_state,
        mock_start_runtime,
        mock_run_retry,
        mock_persist,
        _mock_finish_runtime,
    ):
        all_url = "https://crm.example/report/all"
        mock_load_state.return_value = {"saved_batch_size": 3, "saved_parallel_workers": 2}
        mock_run_retry.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok", "action": "validate_batch", "list_url": all_url},
        )

        with mock.patch.object(server, "CRM_SHIPPING_ALL_URL", all_url):
            result = server._run_crm_processing_step("address_validator_batch", "all")

        self.assertTrue(result["success"])
        self.assertEqual(mock_start_runtime.call_args.kwargs["active_filter"], "all")
        self.assertEqual(mock_start_runtime.call_args.kwargs["list_url"], all_url)
        self.assertEqual(mock_run_retry.call_args.kwargs["list_url"], all_url)
        self.assertEqual(mock_persist.call_args.kwargs["list_url"], all_url)

    def test_worker_all_shipping_filter_uses_all_url(self):
        all_url = "https://crm.example/report/all"
        with mock.patch.object(crm_validate_address, "CRM_SHIPPING_ALL_URL", all_url):
            self.assertEqual(crm_validate_address._normalize_shipping_filter("all"), "all")
            self.assertEqual(crm_validate_address._shipping_list_url_for_filter("all"), all_url)

    def test_worker_high_value_shipping_filter_uses_high_value_url(self):
        high_value_url = "https://crm.example/report/high-value"
        with mock.patch.object(crm_validate_address, "CRM_SHIPPING_HIGH_VALUE_URL", high_value_url):
            self.assertEqual(crm_validate_address._normalize_shipping_filter("high-value"), "high_value")
            self.assertEqual(crm_validate_address._shipping_list_url_for_filter("high_value"), high_value_url)

    @mock.patch.object(server, "log_automation_event")
    @mock.patch.object(server.time, "sleep")
    @mock.patch.object(server, "_execute_crm_address_worker")
    def test_batch_failures_do_not_retry_from_server_layer(
        self,
        mock_execute_worker,
        mock_sleep,
        _mock_log_event,
    ):
        mock_execute_worker.return_value = (
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            {"success": False, "message": "CRMAddressValidator timed out after 180 seconds.", "action": "validate_batch"},
        )

        ok, message, payload = server._run_crm_address_with_retry(
            None,
            "rush",
            dry_run=True,
            action="validate_batch",
            batch_size=3,
            parallel_workers=1,
        )

        self.assertFalse(ok)
        self.assertIn("timed out", message.lower())
        self.assertEqual(payload.get("action"), "validate_batch")
        self.assertEqual(mock_execute_worker.call_count, 1)
        mock_sleep.assert_not_called()

    @mock.patch.object(server, "log_automation_event")
    @mock.patch.object(server.time, "sleep")
    @mock.patch.object(server, "_execute_crm_address_worker")
    def test_single_order_transient_failure_still_retries(
        self,
        mock_execute_worker,
        mock_sleep,
        _mock_log_event,
    ):
        mock_execute_worker.side_effect = [
            (False, "CRMAddressValidator timed out after 180 seconds.", {"success": False, "message": "timed out"}),
            (True, "ok", {"success": True, "message": "ok", "action": "validate_order"}),
        ]

        ok, message, payload = server._run_crm_address_with_retry(
            "1234567",
            "free",
            dry_run=True,
            action="validate_order",
            batch_size=1,
            parallel_workers=1,
        )

        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertEqual(payload.get("action"), "validate_order")
        self.assertEqual(mock_execute_worker.call_count, 2)
        mock_sleep.assert_called_once()

    @mock.patch.object(server, "_audit_result")
    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_persist_batch_result_keeps_batch_action_and_aggregates_manual_review(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        _mock_save_state,
        _mock_audit_result,
    ):
        mock_load_state.return_value = server._default_crm_address_state()
        payload = {
            "success": False,
            "message": "CRMAddressValidator timed out after 180 seconds.",
            "order_count": 2,
            "order_ids": ["3000001", "3000002"],
            "batch_size": 3,
            "parallel_workers": 1,
            "duration_seconds": 18.6,
            "report": [
                {**_report_row("3000001", success=True, manual_review_required=False, resolution="validated"), "duration_seconds": 7.1},
                {**_report_row("3000002", success=False, manual_review_required=True, resolution="manual_review"), "duration_seconds": 8.2},
            ],
        }

        state = server._persist_crm_address_run_result(
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            payload,
            None,
            "rush",
            dry_run=True,
            action="validate_batch",
            batch_size=3,
            parallel_workers=1,
        )

        self.assertEqual(state["last_action"], "validate_batch")
        self.assertEqual(state["run_history"][0]["action"], "validate_batch")
        self.assertEqual(state["run_history"][0]["duration_seconds"], 18.6)
        self.assertEqual(state["run_history"][0]["report"][0]["duration_seconds"], 7.1)
        self.assertTrue(state["last_manual_review_required"])
        self.assertEqual(state["last_resolution"], "batch")
        self.assertEqual(state["last_order_ids"], ["3000001", "3000002"])

    @mock.patch.object(server, "_audit_result")
    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_persist_batch_result_retains_full_fifty_order_report(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        _mock_save_state,
        _mock_audit_result,
    ):
        mock_load_state.return_value = server._default_crm_address_state()
        order_ids = [str(3000000 + index) for index in range(1, 51)]
        payload = {
            "success": True,
            "message": "Processed 50 order(s).",
            "order_count": 50,
            "order_ids": order_ids,
            "batch_size": None,
            "parallel_workers": 8,
            "report": [_report_row(order_id) for order_id in order_ids],
        }

        state = server._persist_crm_address_run_result(
            True,
            "Processed 50 order(s).",
            payload,
            None,
            "all",
            dry_run=False,
            action="validate_batch",
            batch_size=None,
            parallel_workers=8,
        )

        self.assertEqual(len(state["last_report"]), 50)
        self.assertEqual(len(state["run_history"][0]["report"]), 50)
        self.assertEqual(state["last_order_ids"], order_ids)

    @mock.patch.object(server, "_audit_result")
    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_persist_batch_result_keeps_continuous_batch_size(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        _mock_save_state,
        _mock_audit_result,
    ):
        mock_load_state.return_value = server._default_crm_address_state()
        payload = {
            "success": True,
            "message": "Processed all orders.",
            "order_count": 2,
            "order_ids": ["3000001", "3000002"],
            "batch_size": None,
            "parallel_workers": 1,
            "report": [
                _report_row("3000001", success=True, manual_review_required=False, resolution="validated"),
                _report_row("3000002", success=True, manual_review_required=False, resolution="validated"),
            ],
        }

        state = server._persist_crm_address_run_result(
            True,
            "Processed all orders.",
            payload,
            None,
            "free",
            dry_run=True,
            action="validate_batch",
            batch_size=None,
            parallel_workers=1,
        )

        self.assertIsNone(state["last_batch_size"])
        self.assertIsNone(state["run_history"][0]["batch_size"])

    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_update_crm_address_preferences_saves_continuous_batch_mode(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        mock_save_state,
    ):
        mock_load_state.return_value = server._default_crm_address_state()

        ok, message, state = server.update_crm_address_preferences(batch_size=0, parallel_workers=4)

        self.assertTrue(ok)
        self.assertIn("Batch Size Continuous", message)
        self.assertIsNone(state["saved_batch_size"])
        self.assertEqual(state["saved_parallel_workers"], 4)
        mock_save_state.assert_called_once()

    @mock.patch.object(server.threading, "Thread")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    @mock.patch.object(server, "crm_lock")
    def test_start_crm_address_run_uses_saved_preferences_when_request_omits_batch_values(
        self,
        mock_crm_lock,
        _mock_ensure_state_file,
        mock_load_state,
        mock_thread,
    ):
        state = server._default_crm_address_state()
        state["active_filter"] = "rush"
        state["saved_batch_size"] = None
        state["saved_parallel_workers"] = 4
        mock_load_state.return_value = state
        mock_crm_lock.acquire.return_value = True
        thread_instance = mock.Mock()
        mock_thread.return_value = thread_instance

        ok, message = server.start_crm_address_run(
            dry_run=True,
            action="validate_batch",
        )

        self.assertTrue(ok)
        self.assertIn("continuously until no orders remain", message)
        mock_thread.assert_called_once_with(
            target=server._crm_address_run_thread,
            args=(None, "rush", True, "validate_batch", None, 4, None),
            daemon=True,
        )
        thread_instance.start.assert_called_once()


class AutomationRuntimeTests(unittest.TestCase):
    @mock.patch.object(server.os.path, "exists", return_value=False)
    @mock.patch.object(server.subprocess, "Popen")
    def test_run_script_uses_hidden_subprocess(self, mock_popen, _mock_exists):
        proc = mock.Mock()
        proc.wait.return_value = 0
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        ok, message, payload = server._run_script("worker.py", ["--dry-run"], "Worker", timeout=5)

        self.assertTrue(ok)
        self.assertIn("completed successfully", message.lower())
        self.assertTrue(payload["success"])
        self.assertEqual(
            mock_popen.call_args.kwargs["creationflags"],
            getattr(server.subprocess, "CREATE_NO_WINDOW", 0),
        )

    @mock.patch.object(server.os.path, "exists", return_value=False)
    @mock.patch.object(server.subprocess, "Popen")
    def test_run_script_can_use_visible_terminal_subprocess(self, mock_popen, _mock_exists):
        proc = mock.Mock()
        proc.wait.return_value = 0
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        ok, _message, _payload = server._run_script("worker.py", ["--visible"], "Worker", timeout=5, show_terminal=True)

        self.assertTrue(ok)
        self.assertEqual(
            mock_popen.call_args.kwargs["creationflags"],
            getattr(server.subprocess, "CREATE_NEW_CONSOLE", 0),
        )

    def test_write_result_payload_allows_none_result_file_and_skips_audit(self):
        temp_result_path = ROOT / "tests_runtime_result.json"
        try:
            with mock.patch.object(automation_runtime, "RESULT_FILE", str(temp_result_path)):
                with mock.patch.object(automation_runtime, "log_automation_result") as mock_audit:
                    payload = automation_runtime.write_result_payload(
                        "crm.address_validator",
                        "crm_validate_address.py",
                        True,
                        "ok",
                        extra_fields={"action": "validate_batch"},
                        result_file=None,
                        audit_log=False,
                    )
        finally:
            temp_result_path.unlink(missing_ok=True)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["action"], "validate_batch")
        mock_audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
