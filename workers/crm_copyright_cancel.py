"""
Isolated CRM copyright-cancel automation.

Default behavior is dry-run: read the Google Sheet queue, open/inspect CRM and
Salesforce, and stop before customer email send, order cancellation, and refund
save. Use --real only after selector verification on a safe test order.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from decimal import Decimal

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from _bootstrap import ensure_project_root_on_path

PROJECT_ROOT = ensure_project_root_on_path()
WORKERS_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)

from automation_runtime import (
    RESULT_FILE,
    build_attached_chrome_driver,
    build_chrome_driver,
    configure_console_utf8,
    kill_stale_chrome,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
from config import (
    COPYRIGHT_CANCEL_ISSUE_TYPE,
    CRM_PASSWORD,
    CRM_USERNAME,
    GOOGLE_SHEET_ERROR_COLUMN,
    GOOGLE_SHEET_ID,
    GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
    GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
    GOOGLE_SHEET_WORKSHEET,
    GOOGLE_SHEETS_CREDENTIALS_FILE,
    PROCESSOR_ACTION_TIMEOUT,
    PROCESSOR_DRY_RUN,
    PROCESSOR_HEADLESS,
    PROCESSOR_ORDER_URL_TEMPLATE,
    PROCESSOR_PAGE_LOAD_TIMEOUT,
    PROCESSOR_PROFILE_DIR,
    SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL,
    SALESFORCE_COPYRIGHT_CANCEL_FROM_LABEL,
    SALESFORCE_COPYRIGHT_CANCEL_TEMPLATE,
    SALESFORCE_EMAIL_TEMPLATE_FILE,
    SALESFORCE_PASSWORD,
    SALESFORCE_USERNAME,
)
import config as _config
from workers.crm_auto_splitter import (
    ANGULAR_APPLY_JS,
    SplitterError,
    _activate_crm_context,
    _cancel_original_order,
    _clean_text,
    _click_ng_button,
    _get_order_live_state,
    _handle_login_if_needed,
    _money_text,
    _open_record_transaction,
    _order_scope,
    _parse_money,
    _read_order_totals,
    _save_order_and_wait,
    _switch_to_crm_app_frame,
    _wait_for_order_scope,
)
import crm_product_separator as _product_separator
from slack_team import run as _run_slack_team

configure_console_utf8()

AUTOMATION_NAME = "crm.copyright_cancel"
SOURCE = "crm_copyright_cancel.py"
HELD_DRIVERS = []
PROFILE_DIR_OVERRIDE = ""
MACH6_STOCK_RETURN_SLACK_URL = str(getattr(_config, "COPYRIGHT_CANCEL_MACH6_STOCK_RETURN_SLACK_URL", "") or "")
INHOUSE_CANCELLED_ORDERS_SLACK_URL = str(
    getattr(_config, "COPYRIGHT_CANCEL_INHOUSE_CANCELLED_ORDERS_SLACK_URL", "") or ""
)


class CopyrightCancelError(Exception):
    """Raised when the copyright-cancel workflow must stop."""


@dataclass
class QueueRow:
    row_number: int
    order_reference: str
    order_id: str
    issue_type: str
    error: str = ""

    @property
    def order_url(self):
        return PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=self.order_id)


def _profile_path():
    profile_dir = PROFILE_DIR_OVERRIDE or os.environ.get("COPYRIGHT_CANCEL_PROFILE_DIR") or PROCESSOR_PROFILE_DIR
    if os.path.isabs(profile_dir):
        return profile_dir
    return os.path.join(PROJECT_ROOT, profile_dir)


def _write_result(success, message, result_file=None, **extra_fields):
    return write_result_payload(
        AUTOMATION_NAME,
        SOURCE,
        success,
        message,
        extra_fields=extra_fields,
        result_file=result_file or RESULT_FILE,
    )


def _normalize_order_id(value):
    text = str(value or "")
    match = re.search(r"(?<!\d)(\d{7})(?!\d)", text)
    if not match:
        raise CopyrightCancelError("Order reference must contain a 7-digit CRM order ID.")
    return match.group(1)


def _resolve_credentials_path():
    path = GOOGLE_SHEETS_CREDENTIALS_FILE
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


def _resolve_email_template_path():
    path = SALESFORCE_EMAIL_TEMPLATE_FILE
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_ROOT, path)


def _load_email_template(template_key):
    path = _resolve_email_template_path()
    if not os.path.exists(path):
        raise CopyrightCancelError(f"Salesforce email template file was not found: {path}")
    with open(path, "r", encoding="utf-8") as handle:
        templates = json.load(handle)
    template = templates.get(template_key)
    if not isinstance(template, dict):
        raise CopyrightCancelError(f"Email template '{template_key}' was not found in {path}.")
    subject = str(template.get("subject") or "").strip()
    body = str(template.get("body") or "").strip()
    if not subject or not body:
        raise CopyrightCancelError(f"Email template '{template_key}' must contain subject and body.")
    return {"subject": subject, "body": body}


def _render_email_template(template_key, **values):
    template = _load_email_template(template_key)
    try:
        return {
            "subject": template["subject"].format(**values),
            "body": template["body"].format(**values),
        }
    except KeyError as exc:
        raise CopyrightCancelError(f"Email template '{template_key}' is missing value for {exc}.") from exc


def _load_gspread():
    try:
        import gspread
    except ImportError as exc:
        raise CopyrightCancelError(
            "Missing Google Sheets dependency. Run: python3 -m pip install -r requirements.txt"
        ) from exc
    return gspread


def _open_sheet():
    creds_path = _resolve_credentials_path()
    if not os.path.exists(creds_path):
        raise CopyrightCancelError(f"Google Sheets credential file was not found: {creds_path}")
    gspread = _load_gspread()
    client = gspread.service_account(filename=creds_path)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    worksheet = spreadsheet.worksheet(GOOGLE_SHEET_WORKSHEET)
    return spreadsheet, worksheet


def _header_indexes(headers):
    required = [
        GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
        GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
        GOOGLE_SHEET_ERROR_COLUMN,
    ]
    missing = [name for name in required if name not in headers]
    if missing:
        raise CopyrightCancelError(f"Missing Google Sheet column(s): {', '.join(missing)}")
    return {
        "order": headers.index(GOOGLE_SHEET_ORDER_REFERENCE_COLUMN),
        "issue": headers.index(GOOGLE_SHEET_ISSUE_TYPE_COLUMN),
        "error": headers.index(GOOGLE_SHEET_ERROR_COLUMN),
    }


def _scan_queue_rows(include_error_rows=False):
    spreadsheet, worksheet = _open_sheet()
    values = worksheet.get_all_values()
    headers = values[0] if values else []
    indexes = _header_indexes(headers)
    eligible = []
    skipped = []
    for row_number, row in enumerate(values[1:], start=2):
        order_ref = row[indexes["order"]].strip() if indexes["order"] < len(row) else ""
        issue_type = row[indexes["issue"]].strip() if indexes["issue"] < len(row) else ""
        error = row[indexes["error"]].strip() if indexes["error"] < len(row) else ""
        if not (order_ref or issue_type or error):
            continue
        record = {
            "row_number": row_number,
            "order_reference": order_ref,
            "issue_type": issue_type,
            "error": error,
        }
        try:
            order_id = _normalize_order_id(order_ref)
        except CopyrightCancelError as exc:
            skipped.append({**record, "reason": str(exc)})
            continue
        if not issue_type:
            skipped.append({**record, "order_id": order_id, "reason": "Issue is blank."})
            continue
        if issue_type.lower() != COPYRIGHT_CANCEL_ISSUE_TYPE.lower():
            skipped.append({**record, "order_id": order_id, "reason": "Unsupported issue type."})
            continue
        if error and not include_error_rows:
            skipped.append({**record, "order_id": order_id, "reason": "ERROR column is not blank."})
            continue
        eligible.append(QueueRow(row_number, order_ref, order_id, issue_type, error))
    return spreadsheet, worksheet, headers, eligible, skipped


def _write_sheet_error(worksheet, headers, row_number, message):
    index = headers.index(GOOGLE_SHEET_ERROR_COLUMN)
    worksheet.update_cell(row_number, index + 1, str(message or "")[:500])


def _delete_sheet_row(worksheet, row_number):
    worksheet.delete_rows(row_number)


def _visible_text(driver):
    return _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))


def _page_text(driver):
    return _clean_text(driver.execute_script("return document.body ? (document.body.innerText || document.body.textContent || '') : '';"))


def _slack_channel_id_from_url(channel_url):
    match = re.search(r"/client/[^/]+/([^/?#]+)", str(channel_url or ""))
    return match.group(1) if match else ""


def _subcontractor_from_page_text(text):
    body = _clean_text(text)
    match = re.search(
        r"\bSubcontractor:\s*(.+?)(?:\s+Preferred File Types\b|\s+Preferred Carriers\b|\s+Since\b|$)",
        body,
        flags=re.IGNORECASE,
    )
    return _clean_text(match.group(1) if match else "")


def _stock_row_summary(row, tab=None):
    return {
        "tab_number": (tab or {}).get("tab_number"),
        "tab_name": _clean_text((tab or {}).get("tab_name")),
        "vendor": _clean_text((row or {}).get("vendor")),
        "po": _clean_text((row or {}).get("po")),
        "vendor_order_number": _clean_text((row or {}).get("vendor_order_number")),
    }


def _stock_state_is_ordered(stock_state):
    return bool(_product_separator._stock_state_is_ordered(stock_state))


def _stock_state_is_local_inventory_only(stock_state):
    stock_state = stock_state or {}
    if not _stock_state_is_ordered(stock_state):
        return False
    rows = stock_state.get("manual_order_rows") or []
    if rows:
        return all(_product_separator._is_local_inventory_vendor(row.get("vendor")) for row in rows)
    vendor = _clean_text(stock_state.get("manual_order_vendor"))
    if vendor:
        return _product_separator._is_local_inventory_vendor(vendor)
    return bool(_product_separator._stock_state_should_auto_order_local_inventory(stock_state))


def _summarize_post_cancel_stock_scan(scan):
    scan = scan or {}
    tabs = scan.get("tabs") or []
    stock_rows = []
    local_inventory_rows = []
    outside_stock_rows = []
    unknown_ordered_tabs = []
    ordered_tabs = []
    for tab in tabs:
        stock_state = tab.get("stock") or {}
        if not _stock_state_is_ordered(stock_state):
            continue
        ordered_tabs.append(
            {
                "tab_number": tab.get("tab_number"),
                "tab_name": _clean_text(tab.get("tab_name")),
                "state": stock_state.get("state"),
            }
        )
        rows = stock_state.get("manual_order_rows") or []
        if not rows and (_clean_text(stock_state.get("manual_order_vendor")) or _clean_text(stock_state.get("manual_order_po"))):
            rows = [stock_state]
        if rows:
            for row in rows:
                summary = _stock_row_summary(row, tab=tab)
                stock_rows.append(summary)
                if _product_separator._is_local_inventory_vendor(summary.get("vendor")):
                    local_inventory_rows.append(summary)
                else:
                    outside_stock_rows.append(summary)
            continue
        if _stock_state_is_local_inventory_only(stock_state):
            local_inventory_rows.append(
                {
                    "tab_number": tab.get("tab_number"),
                    "tab_name": _clean_text(tab.get("tab_name")),
                    "vendor": "Local Inventory",
                    "po": "",
                    "vendor_order_number": "",
                    "detected_from": stock_state.get("state") or "stock_state",
                }
            )
            continue
        unknown_ordered_tabs.append(
            {
                "tab_number": tab.get("tab_number"),
                "tab_name": _clean_text(tab.get("tab_name")),
                "state": stock_state.get("state"),
            }
        )

    order_stock_status = scan.get("order_stock_status") if isinstance(scan.get("order_stock_status"), dict) else {}
    stock_ordered = bool(ordered_tabs or (order_stock_status or {}).get("stock_status_ordered"))
    return {
        "stock_ordered": stock_ordered,
        "ordered_tabs": ordered_tabs,
        "stock_rows": stock_rows,
        "local_inventory_rows": local_inventory_rows,
        "outside_stock_rows": outside_stock_rows,
        "unknown_ordered_tabs": unknown_ordered_tabs,
        "local_inventory_only": bool(stock_ordered and local_inventory_rows and not outside_stock_rows and not unknown_ordered_tabs),
        "order_stock_status": order_stock_status,
        "scan": scan,
    }


def _read_post_cancel_stock_state(driver, crm_handle, order_id):
    driver.switch_to.window(crm_handle)
    _activate_crm_context(driver)
    _wait_for_order_scope(driver, order_id=order_id)
    page_text = _page_text(driver)
    try:
        scan = _product_separator._scan_order(driver, expected_order_id=order_id)
    except Exception as exc:
        raise CopyrightCancelError(f"Could not inspect post-cancel stock state: {exc}") from exc
    subcontractor = _subcontractor_from_page_text(page_text)
    summary = _summarize_post_cancel_stock_scan(scan)
    summary.update(
        {
            "subcontractor": subcontractor,
            "is_subcontractor": bool(subcontractor),
            "is_mach6_subcontractor": "mach 6" in subcontractor.lower(),
        }
    )
    return summary


def _send_post_cancel_slack_message(channel_url, message, dry_run):
    channel_id = _slack_channel_id_from_url(channel_url)
    if dry_run:
        return {
            "sent": False,
            "dry_run": True,
            "channel_url": channel_url,
            "channel_id": channel_id,
            "message": message,
        }
    if not str(channel_url or "").strip():
        raise CopyrightCancelError("Post-cancel Slack channel URL is not configured.")
    ok, result_message = _run_slack_team("custom", custom_message=message, channel_url=channel_url)
    if not ok:
        raise CopyrightCancelError(f"Slack stock-return message failed for {channel_id or channel_url}: {result_message}")
    return {
        "sent": True,
        "dry_run": False,
        "channel_url": channel_url,
        "channel_id": channel_id,
        "message": message,
        "result": result_message,
    }


def _handle_post_cancel_stock_return(driver, crm_handle, order_id, order_url, dry_run, enabled=True):
    if not enabled:
        return {
            "action": "skipped",
            "slack": {"sent": False},
            "message": "Skipped post-cancel stock routing because final refund click is disabled.",
        }
    state = _read_post_cancel_stock_state(driver, crm_handle, order_id)
    if not state.get("stock_ordered"):
        return {
            "action": "complete_no_stock_ordered",
            "slack": {"sent": False},
            "stock_state": state,
        }
    if state.get("local_inventory_only"):
        return {
            "action": "complete_local_inventory",
            "slack": {"sent": False},
            "stock_state": state,
        }
    if state.get("unknown_ordered_tabs"):
        raise CopyrightCancelError(
            "Stock is marked ordered, but the vendor/local-inventory row could not be detected; manual stock return review required."
        )
    if not state.get("outside_stock_rows"):
        raise CopyrightCancelError("Stock is marked ordered, but no stock vendor row was detected; manual stock return review required.")

    if state.get("is_mach6_subcontractor"):
        channel_url = MACH6_STOCK_RETURN_SLACK_URL
        slack_message = f"{order_url} cancelled"
        action = "slack_mach6_cancelled"
    elif state.get("is_subcontractor"):
        raise CopyrightCancelError(
            f"Unsupported subcontractor for post-cancel stock routing: {state.get('subcontractor')}. Manual review required."
        )
    else:
        channel_url = INHOUSE_CANCELLED_ORDERS_SLACK_URL
        slack_message = order_url
        action = "slack_inhouse_cancelled_orders"

    return {
        "action": action,
        "slack": _send_post_cancel_slack_message(channel_url, slack_message, dry_run=dry_run),
        "stock_state": state,
    }


def _set_clipboard_text(text):
    value = str(text or "")
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(value)
        root.update()
        root.destroy()
        return
    except Exception as tk_err:
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "$text = [Console]::In.ReadToEnd(); Set-Clipboard -Value $text",
                ],
                input=value,
                text=True,
                check=True,
                timeout=10,
            )
            return
        except Exception as ps_err:
            raise CopyrightCancelError(f"Could not place Salesforce email body on clipboard: {tk_err}; {ps_err}") from ps_err


def _is_crm_login_page(driver):
    text = _visible_text(driver).lower()
    url = str(driver.current_url or "").lower()
    title = str(driver.title or "").lower()
    return "/login" in url or "crm2" in url and "login" in text or "login" in title and "salesforce" not in text


def _click_crm_login_button(driver):
    return bool(
        driver.execute_script(
            """
            const candidates = Array.from(document.querySelectorAll('button,input[type=submit],a,[role=button],div,span'));
            const visible = candidates.filter((el) => {
              const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 20 && rect.height > 15
                && style.display !== 'none' && style.visibility !== 'hidden'
                && (text === 'login' || text === 'log in' || text.includes('sign in'));
            });
            if (!visible.length) return false;
            visible.sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (br.width * br.height) - (ar.width * ar.height);
            });
            const button = visible[0];
            button.scrollIntoView({block: 'center', inline: 'center'});
            button.click();
            return true;
            """
        )
    )


def _visible_login_inputs(driver):
    inputs = []
    for element in driver.find_elements("css selector", "input"):
        try:
            if element.is_displayed() and element.size.get("width", 0) > 20 and element.size.get("height", 0) > 10:
                inputs.append(element)
        except Exception:
            pass
    return inputs


def _fill_crm_login_with_selenium(driver, username, password):
    inputs = _visible_login_inputs(driver)
    password_input = None
    for element in inputs:
        try:
            if (element.get_attribute("type") or "").lower() == "password":
                password_input = element
                break
        except Exception:
            pass
    username_input = None
    for element in inputs:
        if element == password_input:
            continue
        try:
            hint = " ".join(
                [
                    element.get_attribute("type") or "",
                    element.get_attribute("name") or "",
                    element.get_attribute("id") or "",
                    element.get_attribute("placeholder") or "",
                    element.get_attribute("autocomplete") or "",
                ]
            ).lower()
            if any(token in hint for token in ("user", "email", "login", "text")):
                username_input = element
                break
        except Exception:
            pass
    if username_input is None:
        username_input = next((element for element in inputs if element != password_input), None)
    if username_input is None or password_input is None:
        return False

    for element, value in ((username_input, username), (password_input, password)):
        try:
            element.click()
            element.send_keys(Keys.CONTROL, "a")
            element.send_keys(value)
        except Exception:
            return False
    return True


def _click_crm_login_with_selenium(driver):
    preferred = []
    fallback = []
    for element in driver.find_elements("css selector", "button,input[type=submit],a,[role=button],div,span"):
        try:
            if not element.is_displayed():
                continue
            text = _clean_text(
                " ".join(
                    [
                        element.text or "",
                        element.get_attribute("value") or "",
                        element.get_attribute("aria-label") or "",
                    ]
                )
            ).lower()
            if text not in {"login", "log in"} and "sign in" not in text:
                continue
            tag = (element.tag_name or "").lower()
            role = (element.get_attribute("role") or "").lower()
            if tag in {"button", "a", "input"} or role == "button":
                preferred.append(element)
            else:
                fallback.append(element)
        except Exception:
            pass
    for element in preferred + fallback:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
            element.click()
            return True
        except Exception:
            pass
    try:
        driver.switch_to.active_element.send_keys(Keys.ENTER)
        return True
    except Exception:
        return False


def _login_to_crm_if_needed(driver, target_url, login_wait_seconds=0):
    if not _is_crm_login_page(driver):
        return False

    username = str(CRM_USERNAME or "").strip()
    password = str(CRM_PASSWORD or "")
    if username and password:
        _fill_crm_login_with_selenium(driver, username, password)
        driver.execute_script(
            """
            const username = arguments[0];
            const password = arguments[1];
            const visibleInputs = Array.from(document.querySelectorAll('input')).filter((el) => {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 20 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
            });
            const userInput = visibleInputs.find((el) => {
              const hint = `${el.type || ''} ${el.name || ''} ${el.id || ''} ${el.placeholder || ''} ${el.autocomplete || ''}`.toLowerCase();
              return hint.includes('email') || hint.includes('user') || hint.includes('login') || hint.includes('text');
            }) || visibleInputs.find((el) => (el.type || '').toLowerCase() !== 'password');
            const passInput = visibleInputs.find((el) => (el.type || '').toLowerCase() === 'password');
            function setValue(el, value) {
              if (!el) return;
              el.focus();
              el.value = value;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('change', {bubbles: true}));
              el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true}));
            }
            setValue(userInput, username);
            setValue(passInput, password);
            return true;
            """,
            username,
            password,
        )
    clicked = _click_crm_login_with_selenium(driver) or _click_crm_login_button(driver)
    if not clicked:
        clicked = _click_exact_visible_text(driver, "Log In") or _click_exact_visible_text(driver, "Login")
    if not clicked:
        try:
            driver.switch_to.active_element.submit()
            clicked = True
        except Exception:
            clicked = bool(
                driver.execute_script(
                    """
                    const form = document.querySelector('form');
                    if (!form) return false;
                    form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
                    if (typeof form.submit === 'function') form.submit();
                    return true;
                    """
                )
            )
    if not clicked:
        raise CopyrightCancelError("CRM login page appeared, but the Login button was not found.")
    deadline = time.monotonic() + max(20, login_wait_seconds)
    while time.monotonic() < deadline:
        time.sleep(1)
        if not _is_crm_login_page(driver):
            safe_get_with_partial_load(driver, target_url, "CRM order after login")
            return True
    if username and password:
        raise CopyrightCancelError("CRM login did not complete after submitting configured credentials.")
    return _handle_login_if_needed(driver, target_url, login_wait_seconds=login_wait_seconds)


def _is_salesforce_login_page(driver):
    text = _visible_text(driver).lower()
    url = str(driver.current_url or "").lower()
    username_input, password_input = _salesforce_login_fields(driver)
    has_login_form = username_input is not None and password_input is not None
    return "salesforce login" in text or "log in to salesforce" in text or has_login_form or (
        "login.salesforce" in url and "login approval required" in text
    )


def _is_salesforce_login_approval_page(driver):
    text = _visible_text(driver).lower()
    return "login approval required" in text or "salesforce authenticator" in text


def _wait_for_salesforce_login_approval(driver, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if not _is_salesforce_login_approval_page(driver):
            return True
    raise CopyrightCancelError("Salesforce login approval was not completed before the timeout expired.")


def _wait_for_salesforce_login_transition(driver, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if _is_salesforce_login_approval_page(driver):
            _wait_for_salesforce_login_approval(driver, timeout=max(timeout, 90))
            return True
        if not _is_salesforce_login_page(driver):
            return True
    return False


def _click_exact_visible_text(driver, expected_text, root_selector=None):
    return bool(
        driver.execute_script(
            """
            const expected = arguments[0].toLowerCase();
            const root = arguments[1] ? document.querySelector(arguments[1]) : document;
            if (!root) return false;
            const nodes = Array.from(root.querySelectorAll('a,button,input,[role=button],span,div'));
            const matches = nodes.filter((el) => {
              const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return text === expected && rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden';
            });
            if (!matches.length) return false;
            matches.sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (br.width * br.height) - (ar.width * ar.height);
            });
            matches[0].scrollIntoView({block: 'center', inline: 'center'});
            matches[0].click();
            return true;
            """,
            expected_text,
            root_selector or "",
        )
    )


def _get_crm_contact_info(driver):
    _activate_crm_context(driver)
    info = driver.execute_script(
        """
        const emailPattern = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/ig;
        const panels = Array.from(document.querySelectorAll('div,section,table')).filter((el) => {
          const text = el.innerText || '';
          return text.includes('Contact Info and Send Options')
            && text.includes('Salesforce Account')
            && (text.match(emailPattern) || []).length;
        });
        const panel = panels.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0] || document.body;
        const text = panel.innerText || '';
        const emails = Array.from(new Set(text.match(emailPattern) || []));
        const sf = Array.from(panel.querySelectorAll('a,button,span'))
          .find((el) => (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase() === 'salesforce account');
        let customerName = '';
        const lines = text.split(/\\n+/).map((line) => line.trim()).filter(Boolean);
        for (const line of lines) {
          if (/contact info and send options/i.test(line)) continue;
          if (line.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i)) continue;
          if (/salesforce account|log in|prior|phone|chat|request review/i.test(line)) continue;
          customerName = line;
          break;
        }
        return {
          customer_name: customerName,
          email: emails[0] || '',
          salesforce_visible: !!sf,
          panel_text: text.slice(0, 1000)
        };
        """
    )
    if not info.get("email"):
        raise CopyrightCancelError("Could not read customer email from CRM contact panel.")
    if not info.get("salesforce_visible"):
        raise CopyrightCancelError("Could not find Salesforce Account link in CRM contact panel.")
    return info


def _click_salesforce_account(driver):
    _activate_crm_context(driver)
    opened = bool(
        driver.execute_script(
            """
            const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
            const el = nodes.find((node) => {
              const text = (node.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = node.getBoundingClientRect();
              return text === 'salesforce account' && rect.width > 0 && rect.height > 0;
            });
            if (!el) return false;
            el.scrollIntoView({block: 'center', inline: 'center'});
            const anchor = el.closest('a[href]');
            const href = anchor ? anchor.href : '';
            if (href && !href.toLowerCase().startsWith('javascript:') && href !== window.location.href) {
              window.open(href, '_blank');
              return true;
            }
            return false;
            """
        )
    )
    if opened:
        return

    for element in driver.find_elements("xpath", "//*[normalize-space(.)='Salesforce Account']"):
        try:
            if not element.is_displayed():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
            element.click()
            return
        except Exception:
            pass

    clicked = bool(
        driver.execute_script(
            """
            const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
            const el = nodes.find((node) => {
              const text = (node.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = node.getBoundingClientRect();
              return text === 'salesforce account' && rect.width > 0 && rect.height > 0;
            });
            if (!el) return false;
            el.click();
            return true;
            """
        )
    )
    if not clicked:
        raise CopyrightCancelError("Salesforce Account link was found earlier but could not be clicked.")


def _switch_to_new_or_changed_tab(driver, before_handles, timeout=20):
    deadline = time.monotonic() + timeout
    before = list(before_handles)
    while time.monotonic() < deadline:
        handles = driver.window_handles
        new_handles = [handle for handle in handles if handle not in before]
        if new_handles:
            driver.switch_to.window(new_handles[-1])
            return new_handles[-1]
        current_url = str(driver.current_url or "")
        if "salesforce" in current_url.lower() or "force.com" in current_url.lower():
            return driver.current_window_handle
        time.sleep(0.5)
    raise CopyrightCancelError("Salesforce tab did not open after clicking Salesforce Account.")


def _visible_salesforce_login_inputs(driver):
    inputs = []
    for element in driver.find_elements("css selector", "input"):
        try:
            if element.is_displayed() and element.size.get("width", 0) > 30 and element.size.get("height", 0) > 10:
                inputs.append(element)
        except Exception:
            pass
    return inputs


def _salesforce_login_fields(driver):
    inputs = _visible_salesforce_login_inputs(driver)
    username_input = None
    password_input = None
    for element in inputs:
        try:
            hint = " ".join(
                [
                    element.get_attribute("type") or "",
                    element.get_attribute("name") or "",
                    element.get_attribute("id") or "",
                    element.get_attribute("placeholder") or "",
                    element.get_attribute("autocomplete") or "",
                    element.get_attribute("aria-label") or "",
                ]
            ).lower()
            if (element.get_attribute("type") or "").lower() == "password" or "password" in hint:
                password_input = element
            elif any(token in hint for token in ("username", "email", "user")):
                username_input = element
        except Exception:
            pass
    if username_input is None:
        username_input = next((element for element in inputs if element != password_input), None)
    return username_input, password_input


def _fill_salesforce_login_with_autofill(driver):
    username_input, password_input = _salesforce_login_fields(driver)
    if username_input is None:
        return False
    def has_login_values():
        try:
            username_value = (username_input.get_attribute("value") or "").strip()
        except Exception:
            username_value = ""
        try:
            password_value = (password_input.get_attribute("value") or "") if password_input is not None else ""
        except Exception:
            password_value = ""
        if password_input is not None:
            return bool(username_value and password_value)
        return bool(username_value)

    try:
        username_input.click()
        time.sleep(0.3)
        configured_username = str(SALESFORCE_USERNAME or "").strip()
        configured_password = str(SALESFORCE_PASSWORD or "")
        if configured_username:
            username_input.send_keys(Keys.CONTROL, "a")
            username_input.send_keys(configured_username)
            if configured_password and password_input is not None:
                password_input.click()
                password_input.send_keys(Keys.CONTROL, "a")
                password_input.send_keys(configured_password)
            return has_login_values()

        # Trigger Chrome's credential/autofill dropdown and choose the first saved login.
        username_input.send_keys(Keys.ARROW_DOWN)
        time.sleep(0.2)
        username_input.send_keys(Keys.ENTER)
        time.sleep(0.8)
        if has_login_values():
            return True
        # Some Chrome autofill menus need a second focus cycle.
        username_input.click()
        time.sleep(0.2)
        username_input.send_keys(Keys.ARROW_DOWN)
        username_input.send_keys(Keys.ENTER)
        time.sleep(0.8)
        return has_login_values()
    except Exception:
        return False


def _click_salesforce_login_with_selenium(driver):
    candidates = []
    for element in driver.find_elements("css selector", "button,input[type=submit],a,[role=button],div,span"):
        try:
            if not element.is_displayed():
                continue
            text = _clean_text(
                " ".join(
                    [
                        element.text or "",
                        element.get_attribute("value") or "",
                        element.get_attribute("aria-label") or "",
                    ]
                )
            ).lower()
            if text in {"log in", "login"} or "sign in" in text:
                candidates.append(element)
        except Exception:
            pass
    for element in candidates:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
            element.click()
            return True
        except Exception:
            pass
    try:
        driver.switch_to.active_element.send_keys(Keys.ENTER)
        return True
    except Exception:
        return False


def _attempt_salesforce_login(driver, timeout=45):
    if not _is_salesforce_login_page(driver):
        return False
    if _is_salesforce_login_approval_page(driver):
        _wait_for_salesforce_login_approval(driver, timeout=max(timeout, 90))
        return True
    if not _fill_salesforce_login_with_autofill(driver):
        if _wait_for_salesforce_login_transition(driver, timeout=15):
            return True
        if _is_salesforce_login_approval_page(driver):
            _wait_for_salesforce_login_approval(driver, timeout=max(timeout, 90))
            return True
        if not str(SALESFORCE_USERNAME or "").strip() and not str(SALESFORCE_PASSWORD or ""):
            raise CopyrightCancelError(
                "Salesforce credentials are blank in config.py and Chrome autofill did not populate the login form."
            )
        raise CopyrightCancelError("Salesforce login fields could not be filled from configured credentials.")
    clicked = _click_salesforce_login_with_selenium(driver)
    if not clicked:
        clicked = _click_exact_visible_text(driver, "Log In")
    if not clicked:
        clicked = _click_exact_visible_text(driver, "Login")
    if not clicked:
        raise CopyrightCancelError("Salesforce login page appeared, but the Log In button was not found.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        if _is_salesforce_login_approval_page(driver):
            _wait_for_salesforce_login_approval(driver, timeout=max(timeout, 90))
            return True
        if not _is_salesforce_login_page(driver):
            return True
    raise CopyrightCancelError("Salesforce login did not complete after clicking Log In.")


def _open_salesforce_account(driver, crm_handle, expected_email, login_wait_seconds=0):
    driver.switch_to.window(crm_handle)
    _activate_crm_context(driver)
    before = driver.window_handles
    _click_salesforce_account(driver)
    sf_handle = _switch_to_new_or_changed_tab(driver, before)
    login_happened = _attempt_salesforce_login(driver)
    if login_happened:
        # CRM's first post-login redirect often lands on the Salesforce default page.
        # Return to CRM and click Salesforce Account again to reach the customer page.
        try:
            if driver.current_window_handle != crm_handle:
                driver.close()
        except Exception:
            pass
        driver.switch_to.window(crm_handle)
        _activate_crm_context(driver)
        before = driver.window_handles
        _click_salesforce_account(driver)
        sf_handle = _switch_to_new_or_changed_tab(driver, before)
    _wait_for_salesforce_account_page(driver, expected_email, timeout=max(30, login_wait_seconds))
    return sf_handle


def _wait_for_salesforce_account_page(driver, expected_email, timeout=45):
    deadline = time.monotonic() + timeout
    expected = str(expected_email or "").strip().lower()
    while time.monotonic() < deadline:
        text = _page_text(driver)
        if expected and expected in text.lower():
            time.sleep(2)
            return True
        if _is_salesforce_login_page(driver):
            _attempt_salesforce_login(driver)
        time.sleep(1)
    raise CopyrightCancelError(f"Salesforce account page did not show expected email {expected_email}.")


def _verify_salesforce_email(driver, expected_email):
    body = _page_text(driver)
    expected = str(expected_email or "").strip().lower()
    if expected not in body.lower():
        emails = sorted(set(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", body, re.I)))
        raise CopyrightCancelError(
            f"Salesforce email mismatch. Expected {expected_email}; visible Salesforce emails: {', '.join(emails[:8])}"
        )
    return True


def _click_salesforce_email(driver, email):
    target = str(email or "").strip().lower()
    _wait_for_salesforce_account_page(driver, target, timeout=45)
    for element in driver.find_elements("xpath", f"//a[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{target}')]"):
        try:
            if not element.is_displayed():
                continue
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
            element.click()
            return
        except Exception:
            pass
    clicked = bool(
        driver.execute_script(
            """
            const target = arguments[0].toLowerCase();
            const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
            const matches = nodes.filter((el) => {
              const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return text.includes(target) && rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden';
            });
            if (!matches.length) return false;
            matches.sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              const anchorRank = (a.closest('a') ? 0 : 1) - (b.closest('a') ? 0 : 1);
              if (anchorRank) return anchorRank;
              return (ar.y - br.y) || (ar.x - br.x);
            });
            const targetEl = matches[0].closest('a') || matches[0];
            targetEl.scrollIntoView({block: 'center', inline: 'center'});
            targetEl.click();
            return true;
            """,
            target,
        )
    )
    if not clicked:
        raise CopyrightCancelError(f"Could not click Salesforce email {email}.")


def _wait_for_email_composer(driver, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _visible_text(driver).lower()
        if "email" in text and "from" in text and "subject" in text and "send" in text:
            return True
        # Salesforce can open the composer minimized at the bottom bar.
        driver.execute_script(
            """
            const minimized = Array.from(document.querySelectorAll('button,a,span,div')).find((el) => {
              const text = `${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0 && text === 'email';
            });
            if (minimized) minimized.click();
            """
        )
        time.sleep(0.5)
    raise CopyrightCancelError("Salesforce email composer did not open.")


def _restore_salesforce_email_composer(driver):
    return bool(
        driver.execute_script(
            """
            const fullText = (document.body ? document.body.innerText || '' : '').toLowerCase();
            if (fullText.includes('from') && fullText.includes('subject') && fullText.includes('send')) return true;
            const bottomControls = Array.from(document.querySelectorAll('button,a,span,div')).filter((el) => {
              const text = `${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && rect.y > window.innerHeight * 0.65
                && style.display !== 'none' && style.visibility !== 'hidden'
                && (text.includes('maximize') || text.includes('expand') || text.includes('pop out') || text.includes('email'));
            });
            const expander = bottomControls.find((el) => {
              const text = `${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`.toLowerCase();
              return text.includes('maximize') || text.includes('expand') || text.includes('pop out');
            });
            if (expander) {
              expander.click();
              return true;
            }
            const controls = Array.from(document.querySelectorAll('button,a,span,div'));
            const emailTile = controls.find((el) => {
              const text = `${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0 && rect.y > window.innerHeight * 0.65 && text === 'email';
            });
            if (!emailTile) return false;
            let target = emailTile;
            for (let hops = 0; target.parentElement && hops < 5; hops++) {
              const rect = target.getBoundingClientRect();
              const parentRect = target.parentElement.getBoundingClientRect();
              if (parentRect.width > rect.width && parentRect.height >= rect.height && parentRect.height < 140) {
                target = target.parentElement;
              }
            }
            target.click();
            setTimeout(() => { try { emailTile.click(); } catch (err) {} }, 150);
            return true;
            """
        )
    )


def _scroll_salesforce_email_composer_to_top(driver):
    driver.execute_script(
        """
        try { window.scrollTo(0, 0); } catch (err) {}
        for (const el of Array.from(document.querySelectorAll('*'))) {
          try {
            if (el.scrollHeight > el.clientHeight + 2) {
              el.scrollTop = 0;
              el.dispatchEvent(new Event('scroll', {bubbles: true}));
            }
          } catch (err) {}
        }
        """
    )
    driver.execute_script(
        """
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden';
        }
        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = (el.innerText || '').toLowerCase();
            const rect = el.getBoundingClientRect();
            return rect.width > 300 && rect.height > 250
              && rect.right > window.innerWidth * 0.45
              && text.includes('subject') && text.includes('send');
          });
        const composer = composers.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0];
        const roots = composer ? [composer, ...Array.from(composer.querySelectorAll('*'))] : Array.from(document.querySelectorAll('*'));
        for (const el of roots) {
          try {
            if (el.scrollHeight > el.clientHeight + 4) {
              el.scrollTop = 0;
              el.dispatchEvent(new Event('scroll', {bubbles: true}));
              el.dispatchEvent(new WheelEvent('wheel', {deltaY: -1200, bubbles: true, cancelable: true}));
            }
          } catch (err) {}
        }
        if (composer) {
          const rect = composer.getBoundingClientRect();
          const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + 30) || composer;
          target.dispatchEvent(new WheelEvent('wheel', {
            deltaY: -1600,
            bubbles: true,
            cancelable: true,
            clientX: rect.left + rect.width / 2,
            clientY: rect.top + 30
          }));
        }
        """
    )
    driver.execute_script(
        """
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        const scrollers = Array.from(document.querySelectorAll('*')).filter((el) => {
          if (!visible(el)) return false;
          if (el.scrollHeight <= el.clientHeight + 8) return false;
          const rect = el.getBoundingClientRect();
          const text = (el.innerText || el.textContent || '').toLowerCase();
          return rect.right > window.innerWidth * 0.45
            && rect.height > 150
            && (text.includes('email') || text.includes('send') || text.includes('related to') || text.includes('subject'));
        });
        for (let pass = 0; pass < 4; pass += 1) {
          for (const el of scrollers) {
            try {
              el.scrollTop = 0;
              el.dispatchEvent(new Event('scroll', {bubbles: true}));
            } catch (err) {}
          }
        }
        """
    )
    try:
        click_info = driver.execute_script(
            """
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
              .filter((el) => {
                if (!visible(el)) return false;
                const rect = el.getBoundingClientRect();
                const text = (el.innerText || '').toLowerCase();
                return rect.width > 300 && rect.height > 250
                  && rect.left > window.innerWidth * 0.45
                  && text.includes('email') && text.includes('send');
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (a.innerText || '').length - (b.innerText || '').length || (br.width * br.height) - (ar.width * ar.height);
              });
            const composer = composers[0];
            if (!composer) return null;
            const rect = composer.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height * 0.48;
            const target = document.elementFromPoint(x, y) || composer;
            for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
              target.dispatchEvent(new MouseEvent(type, {
                bubbles: true,
                cancelable: true,
                view: window,
                clientX: x,
                clientY: y
              }));
            }
            target.focus && target.focus();
            target.dispatchEvent(new WheelEvent('wheel', {
              deltaY: -5000,
              bubbles: true,
              cancelable: true,
              clientX: x,
              clientY: y
            }));
            return {x, y, tag: target.tagName || '', text: (target.innerText || '').slice(0, 50)};
            """
        )
        if click_info:
            time.sleep(0.1)
            driver.switch_to.active_element.send_keys(Keys.CONTROL, Keys.HOME)
            time.sleep(0.1)
            driver.switch_to.active_element.send_keys(Keys.PAGE_UP)
            time.sleep(0.1)
    except Exception:
        pass
    try:
        driver.switch_to.active_element.send_keys(Keys.HOME)
        time.sleep(0.1)
        driver.switch_to.active_element.send_keys(Keys.PAGE_UP)
    except Exception:
        pass
    time.sleep(0.5)


def _maximize_salesforce_email_composer(driver):
    clicked = bool(
        driver.execute_script(
            """
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
              .filter((el) => {
                if (!visible(el)) return false;
                const rect = el.getBoundingClientRect();
                const text = clean(el.innerText || '');
                return rect.width > 300 && rect.height > 250
                  && rect.left > window.innerWidth * 0.45
                  && text.includes('email') && text.includes('send');
              })
              .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
            const composer = composers[0];
            if (!composer) return false;
            const cr = composer.getBoundingClientRect();
            const explicit = Array.from(composer.querySelectorAll('button,a,[role=button],span,div'))
              .filter((el) => {
                if (!visible(el)) return false;
                const rect = el.getBoundingClientRect();
                const text = clean(`${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`);
                return rect.top >= cr.top && rect.top < cr.top + 55
                  && rect.left > cr.right - 120
                  && (text.includes('expand') || text.includes('maximize') || text.includes('pop') || text.includes('full'));
              })
              .sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
            let target = explicit[0] || null;
            if (!target) {
              const x = cr.right - 55;
              const y = cr.top + 22;
              target = document.elementFromPoint(x, y);
            }
            if (!target || target === document.body) return false;
            const rect = target.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;
            for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
              target.dispatchEvent(new MouseEvent(type, {
                bubbles: true,
                cancelable: true,
                view: window,
                clientX: x,
                clientY: y
              }));
            }
            return true;
            """
        )
    )
    if clicked:
        time.sleep(1)
    return clicked


def _set_salesforce_from_orders(driver):
    _restore_salesforce_email_composer(driver)
    _wait_for_email_composer(driver, timeout=10)
    _maximize_salesforce_email_composer(driver)
    _scroll_salesforce_email_composer_to_top(driver)
    target_email = SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL.lower()
    target_label = SALESFORCE_COPYRIGHT_CANCEL_FROM_LABEL.lower()
    target_domain = target_email.partition("@")[2]

    def _current_from_text():
        return str(
            driver.execute_script(
                """
                function clean(value) {
                  return (value || '').replace(/\\s+/g, ' ').trim();
                }
                function visible(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.bottom > 0 && rect.top < window.innerHeight
                    && rect.right > 0 && rect.left < window.innerWidth;
                }
                function rendered(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
                }
                const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
                  .filter((el) => {
                    if (!visible(el)) return false;
                    const text = (el.innerText || '').toLowerCase();
                    const rect = el.getBoundingClientRect();
                    return rect.width > 300 && rect.height > 250
                      && text.includes('from') && text.includes('subject') && text.includes('send');
                  })
                  .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
                    const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
                    if (aRight !== bRight) return aRight - bRight;
                    return (a.innerText || '').length - (b.innerText || '').length;
                  });
                const composer = composers[0] || document;
                const labels = Array.from(composer.querySelectorAll('label,span,div,td,th'))
                  .filter((el) => {
                    if (!rendered(el)) return false;
                    const text = clean(el.innerText || el.textContent || '').toLowerCase();
                    return text === 'from' || text === '* from' || text.endsWith(' from');
                  })
                  .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
                for (const label of labels) {
                  const lr = label.getBoundingClientRect();
                  const rowCenter = lr.top + lr.height / 2;
                  const controls = Array.from(composer.querySelectorAll('select,input,button,[role=combobox],a,div,span'))
                    .filter((el) => {
                      if (!rendered(el) || el === label) return false;
                      const rect = el.getBoundingClientRect();
                      if (rect.left <= lr.right) return false;
                      if (Math.abs((rect.top + rect.height / 2) - rowCenter) > 36) return false;
                      if (rect.width < 120 || rect.height < 18) return false;
                      return true;
                    })
                    .sort((a, b) => {
                      const ar = a.getBoundingClientRect();
                      const br = b.getBoundingClientRect();
                      return ar.left - br.left || ((br.width * br.height) - (ar.width * ar.height));
                    });
                  const pieces = [];
                  for (const control of controls) {
                    const tag = (control.tagName || '').toLowerCase();
                    if (tag === 'select') {
                      const selected = control.options && control.selectedIndex >= 0 ? control.options[control.selectedIndex] : null;
                      if (selected) pieces.push(selected.text || selected.label || selected.value || '');
                    }
                    pieces.push(control.innerText || '');
                    pieces.push(control.value || '');
                    pieces.push(control.getAttribute('aria-label') || '');
                    pieces.push(control.getAttribute('title') || '');
                  }
                  const rowText = clean(pieces.join(' '));
                  if (rowText) {
                    return rowText;
                  }
                }
                return '';
                """
            )
            or ""
        )

    current_from = _current_from_text().lower()
    if target_email in current_from:
        return _current_from_text()

    def _from_control():
        return driver.execute_script(
            """
            const targetDomain = (arguments[0] || '').toLowerCase();
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim();
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            function rendered(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden';
            }
            function composerCandidates() {
              return Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
                .filter((el) => {
                  if (!visible(el)) return false;
                  const text = (el.innerText || '').toLowerCase();
                  const rect = el.getBoundingClientRect();
                  return rect.width > 300 && rect.height > 250
                    && text.includes('from') && text.includes('subject') && text.includes('send');
                })
                .sort((a, b) => {
                  const ar = a.getBoundingClientRect();
                  const br = b.getBoundingClientRect();
                  const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
                  const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
                  if (aRight !== bRight) return aRight - bRight;
                  return (a.innerText || '').length - (b.innerText || '').length;
                });
            }
            const composer = composerCandidates()[0] || document;
            const nodes = Array.from(composer.querySelectorAll('label,span,div,td,th'));
            const labels = nodes.filter((el) => {
              if (!rendered(el)) return false;
              const text = clean(el.innerText || el.textContent || '').toLowerCase();
              return text === 'from' || text === '* from' || text.endsWith(' from');
            });
            labels.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);

            for (const label of labels) {
              const lr = label.getBoundingClientRect();
              const controls = Array.from(composer.querySelectorAll('select,input,button,[role=combobox],a,div,span'))
                .filter((el) => {
                  if (!rendered(el) || el === label) return false;
                  const rect = el.getBoundingClientRect();
                  if (rect.left <= lr.right) return false;
                  if (Math.abs((rect.top + rect.height / 2) - (lr.top + lr.height / 2)) > 36) return false;
                  if (rect.width < 180 || rect.height < 20) return false;
                  const text = clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
                  return (targetDomain && text.includes('@' + targetDomain))
                    || (el.getAttribute('role') || '').toLowerCase() === 'combobox'
                    || el.tagName.toLowerCase() === 'select'
                    || el.tagName.toLowerCase() === 'input';
                })
                .sort((a, b) => {
                  const ar = a.getBoundingClientRect();
                  const br = b.getBoundingClientRect();
                  const aEmail = targetDomain && clean(`${a.innerText || ''} ${a.value || ''}`).includes('@' + targetDomain) ? 0 : 1;
                  const bEmail = targetDomain && clean(`${b.innerText || ''} ${b.value || ''}`).includes('@' + targetDomain) ? 0 : 1;
                  if (aEmail !== bEmail) return aEmail - bEmail;
                  return (br.width * br.height) - (ar.width * ar.height);
                });
              if (controls.length) return controls[0];
            }

            const topControls = Array.from(composer.querySelectorAll('select,input,button,[role=combobox],a,div,span'))
              .filter((el) => {
                if (!rendered(el)) return false;
                const rect = el.getBoundingClientRect();
                const text = clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
                return rect.width > 180 && rect.height > 20
                  && rect.top < window.innerHeight * 0.65
                  && targetDomain && text.includes('@' + targetDomain);
              })
              .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
            return topControls[0] || null;
            """
            ,
            target_domain,
        )

    def _select_orders_sender_native():
        return bool(
            driver.execute_script(
                """
                const targetEmail = arguments[0].toLowerCase();
                const targetLabel = arguments[1].toLowerCase();
                function clean(value) {
                  return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                }
                function visible(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.bottom > 0 && rect.top < window.innerHeight
                    && rect.right > 0 && rect.left < window.innerWidth;
                }
                const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
                  .filter((el) => {
                    if (!visible(el)) return false;
                    const text = (el.innerText || '').toLowerCase();
                    const rect = el.getBoundingClientRect();
                    return rect.width > 300 && rect.height > 250
                      && text.includes('from') && text.includes('subject') && text.includes('send');
                  })
                  .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                const composer = composers[0] || document;
                for (const select of Array.from(composer.querySelectorAll('select')).filter(visible)) {
                  const option = Array.from(select.options || []).find((item) => {
                    const text = clean(`${item.text || ''} ${item.value || ''} ${item.label || ''}`);
                    return text.includes(targetEmail) || text.includes(targetLabel);
                  });
                  if (!option) continue;
                  select.focus();
                  select.value = option.value;
                  option.selected = true;
                  select.dispatchEvent(new Event('input', {bubbles: true}));
                  select.dispatchEvent(new Event('change', {bubbles: true}));
                  return true;
                }
                return false;
                """,
                target_email,
                target_label,
            )
        )

    def _from_dropdown_is_open():
        return bool(
            driver.execute_script(
                """
                const targetDomain = (arguments[0] || '').toLowerCase();
                function clean(value) {
                  return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                }
                function visible(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.bottom > 0 && rect.top < window.innerHeight
                    && rect.right > 0 && rect.left < window.innerWidth;
                }
                return Array.from(document.querySelectorAll('[role=listbox], [role=menu], ul, div'))
                  .some((el) => {
                    if (!visible(el)) return false;
                    const text = clean(el.innerText || el.textContent || '');
                    const rect = el.getBoundingClientRect();
                    return rect.width > 220 && rect.height > 60
                      && targetDomain && text.includes('@' + targetDomain)
                      && (text.includes('--none--') || text.includes('a.vo') || text.includes('affiliate relations'));
                  });
                """
                ,
                target_domain,
            )
        )

    def _open_from_dropdown():
        control = _from_control()
        if control is not None:
            try:
                driver.execute_script(
                    "try { arguments[0].scrollIntoView({block: 'center', inline: 'nearest'}); } catch (err) {}",
                    control,
                )
                time.sleep(0.2)
                ActionChains(driver).move_to_element(control).pause(0.1).click(control).perform()
                time.sleep(0.5)
                if _from_dropdown_is_open():
                    return True
                try:
                    driver.switch_to.active_element.send_keys(Keys.ALT, Keys.ARROW_DOWN)
                    time.sleep(0.5)
                    if _from_dropdown_is_open():
                        return True
                except Exception:
                    pass
                try:
                    driver.switch_to.active_element.send_keys(Keys.SPACE)
                    time.sleep(0.5)
                    if _from_dropdown_is_open():
                        return True
                except Exception:
                    pass
            except Exception:
                pass
            try:
                driver.execute_script(
                    """
                    const control = arguments[0];
                    try { control.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (err) {}
                    const rect = control.getBoundingClientRect();
                    const clickX = Math.max(rect.left + 8, rect.right - 18);
                    const clickY = rect.top + (rect.height / 2);
                    const target = document.elementFromPoint(clickX, clickY) || control;
                    try { control.focus(); } catch (err) {}
                    for (const type of ['mousedown', 'mouseup', 'click']) {
                      target.dispatchEvent(new MouseEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: clickX,
                        clientY: clickY
                      }));
                    }
                    """,
                    control,
                )
                time.sleep(0.5)
                if _from_dropdown_is_open():
                    return True
            except Exception:
                pass
        return bool(
            driver.execute_script(
                """
                const targetDomain = (arguments[0] || '').toLowerCase();
                const controls = Array.from(document.querySelectorAll('select,input,button,[role=combobox],a,div,span'));
                const el = controls.find((node) => {
                  const text = `${node.innerText || ''} ${node.value || ''}`.toLowerCase();
                  const rect = node.getBoundingClientRect();
                  const style = window.getComputedStyle(node);
                  return rect.width > 180 && rect.height > 20
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && targetDomain && text.includes('@' + targetDomain);
                });
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const clickX = Math.max(rect.left + 8, rect.right - 18);
                const clickY = rect.top + (rect.height / 2);
                const target = document.elementFromPoint(clickX, clickY) || el;
                for (const type of ['mousedown', 'mouseup', 'click']) {
                  target.dispatchEvent(new MouseEvent(type, {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    clientX: clickX,
                    clientY: clickY
                  }));
                }
                return true;
                """
                ,
                target_domain,
            )
        ) and _from_dropdown_is_open()

    def _visible_orders_sender_option():
        return driver.execute_script(
            """
            const targetEmail = arguments[0].toLowerCase();
            const targetLabel = arguments[1].toLowerCase();

            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            function isTargetText(text) {
              return text.includes(targetLabel)
                || text.includes(targetEmail);
            }
            const nodes = Array.from(document.querySelectorAll('[role=option], [role=menuitem], .slds-listbox__option, option, li, a, button, span, div'));
            const matches = nodes.filter((el) => {
              if (!visible(el)) return false;
              const rawText = el.innerText || el.textContent || el.value || el.label || el.getAttribute('title') || el.getAttribute('aria-label') || '';
              const text = clean(rawText);
              if (!isTargetText(text)) return false;
              const rect = el.getBoundingClientRect();
              const role = (el.getAttribute('role') || '').toLowerCase();
              const tag = (el.tagName || '').toLowerCase();
              const klass = String(el.className || '').toLowerCase();
              const lineCount = String(rawText || '').split(/\\n+/).filter((line) => line.trim()).length;
              const hasOptionChild = Array.from(el.children || []).some((child) => {
                const childRole = (child.getAttribute('role') || '').toLowerCase();
                const childClass = String(child.className || '').toLowerCase();
                return childRole === 'option' || childClass.includes('listbox__option');
              });
              const selectable = role === 'option'
                || role === 'menuitem'
                || tag === 'option'
                || tag === 'li'
                || tag === 'a'
                || tag === 'button'
                || klass.includes('listbox__option')
                || ((tag === 'span' || tag === 'div') && rect.height <= 54 && lineCount <= 2 && !hasOptionChild);
              return selectable && rect.width > 80 && rect.height > 10;
            });
            if (!matches.length) return null;
            matches.sort((a, b) => {
              const at = clean(a.innerText || a.textContent || '');
              const bt = clean(b.innerText || b.textContent || '');
              const aExact = at.includes(targetLabel) ? 0 : 1;
              const bExact = bt.includes(targetLabel) ? 0 : 1;
              if (aExact !== bExact) return aExact - bExact;
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              const aRole = (a.getAttribute('role') || '').toLowerCase() === 'option' ? 0 : 1;
              const bRole = (b.getAttribute('role') || '').toLowerCase() === 'option' ? 0 : 1;
              if (aRole !== bRole) return aRole - bRole;
              return (ar.y - br.y) || (ar.x - br.x);
            });
            const option = matches[0];
            const clickable = option.closest('[role=option], [role=menuitem], .slds-listbox__option, li, a, button') || option;
            try { clickable.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (err) {}
            return clickable;
            """,
            target_email,
            target_label,
        )

    def _click_visible_orders_sender_option():
        option = _visible_orders_sender_option()
        if option is None:
            return False
        try:
            ActionChains(driver).move_to_element(option).pause(0.1).click(option).perform()
            return True
        except Exception:
            try:
                option.click()
                return True
            except Exception:
                return False

    def _select_orders_sender_from_open_dropdown():
        return bool(
            driver.execute_script(
                """
                const targetEmail = arguments[0].toLowerCase();
                const targetLabel = arguments[1].toLowerCase();

                function clean(value) {
                  return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                }
                function visible(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.bottom > 0 && rect.top < window.innerHeight;
                }
                function isTargetText(text) {
                  return text.includes(targetLabel)
                    || text.includes(targetEmail);
                }
                function clickOption() {
                  const nodes = Array.from(document.querySelectorAll('[role=option], [role=menuitem], .slds-listbox__option, option, li, a, button, span, div'));
                  const matches = nodes.filter((el) => {
                    if (!visible(el)) return false;
                    const rawText = el.innerText || el.textContent || el.value || el.label || el.getAttribute('title') || el.getAttribute('aria-label') || '';
                    const text = clean(rawText);
                    if (!isTargetText(text)) return false;
                    const rect = el.getBoundingClientRect();
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    const tag = (el.tagName || '').toLowerCase();
                    const klass = String(el.className || '').toLowerCase();
                    const lineCount = String(rawText || '').split(/\\n+/).filter((line) => line.trim()).length;
                    const containsOtherSender = /affiliate relations|customer care/i.test(String(rawText || ''));
                    const hasOptionChild = Array.from(el.children || []).some((child) => {
                      const childRole = (child.getAttribute('role') || '').toLowerCase();
                      const childClass = String(child.className || '').toLowerCase();
                      return childRole === 'option' || childClass.includes('listbox__option');
                    });
                    const selectable = role === 'option'
                      || role === 'menuitem'
                      || tag === 'option'
                      || tag === 'li'
                      || tag === 'a'
                      || tag === 'button'
                      || klass.includes('listbox__option')
                      || ((tag === 'span' || tag === 'div') && rect.height <= 54 && lineCount <= 2 && !hasOptionChild && !containsOtherSender);
                    return selectable && rect.width > 80 && rect.height > 10;
                  });
                  if (!matches.length) return false;
                  matches.sort((a, b) => {
                    const at = clean(a.innerText || a.textContent || '');
                    const bt = clean(b.innerText || b.textContent || '');
                    const aExact = at.includes(targetLabel) ? 0 : 1;
                    const bExact = bt.includes(targetLabel) ? 0 : 1;
                    if (aExact !== bExact) return aExact - bExact;
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    const aRole = (a.getAttribute('role') || '').toLowerCase() === 'option' ? 0 : 1;
                    const bRole = (b.getAttribute('role') || '').toLowerCase() === 'option' ? 0 : 1;
                    if (aRole !== bRole) return aRole - bRole;
                    return (ar.y - br.y) || (ar.x - br.x);
                  });
                  const option = matches[0];
                  const clickTarget = option.closest('[role=option], [role=menuitem], .slds-listbox__option, li, a, button') || option;
                  clickTarget.scrollIntoView({block: 'center', inline: 'nearest'});
                  const rect = clickTarget.getBoundingClientRect();
                  const clickX = rect.left + Math.min(rect.width - 5, Math.max(10, rect.width / 2));
                  const clickY = rect.top + rect.height / 2;
                  const pointTarget = document.elementFromPoint(clickX, clickY) || clickTarget;
                  for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
                    pointTarget.dispatchEvent(new MouseEvent(type, {
                      bubbles: true,
                      cancelable: true,
                      view: window,
                      clientX: clickX,
                      clientY: clickY
                    }));
                  }
                  return true;
                }
                function scrollDropdowns() {
                  const scrollers = Array.from(document.querySelectorAll('*')).filter((el) => {
                    if (!visible(el)) return false;
                    if (el.scrollHeight <= el.clientHeight + 8) return false;
                    const rect = el.getBoundingClientRect();
                    const text = clean(el.innerText || el.textContent || '');
                    return rect.width > 250 && rect.height > 80
                      && rect.top < window.innerHeight * 0.85
                      && (text.includes('@' + targetEmail.split('@').pop()) || text.includes('--none--') || text.includes('affiliate relations'));
                  });
                  if (!scrollers.length) return false;
                  scrollers.sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (ar.y - br.y) || ((br.width * br.height) - (ar.width * ar.height));
                  });
                  for (const el of scrollers) {
                    const before = el.scrollTop;
                    el.scrollTop = el.scrollTop + 180;
                    const rect = el.getBoundingClientRect();
                    el.dispatchEvent(new Event('scroll', {bubbles: true}));
                    el.dispatchEvent(new WheelEvent('wheel', {
                      deltaY: 180,
                      bubbles: true,
                      cancelable: true,
                      clientX: rect.left + rect.width / 2,
                      clientY: rect.top + rect.height / 2
                    }));
                    if (el.scrollTop !== before) return true;
                  }
                  return false;
                }
                for (let index = 0; index < 40; index += 1) {
                  if (clickOption()) return true;
                  scrollDropdowns();
                }
                return false;
                """,
                target_email,
                target_label,
            )
        )

    def _scroll_orders_sender_dropdown():
        scrolled = bool(
            driver.execute_script(
                """
                const targetDomain = (arguments[0] || '').toLowerCase();
                function clean(value) {
                  return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                }
                function visible(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.bottom > 0 && rect.top < window.innerHeight
                    && rect.right > 0 && rect.left < window.innerWidth;
                }
                const roots = Array.from(document.querySelectorAll('*'))
                  .filter((el) => {
                    if (!visible(el)) return false;
                    if (el.scrollHeight <= el.clientHeight + 8) return false;
                    const rect = el.getBoundingClientRect();
                    const text = clean(el.innerText || el.textContent || '');
                    const looksLikeFromMenu = text.includes('--none--')
                      && targetDomain && text.includes('@' + targetDomain)
                      && (text.includes('a.vo') || text.includes('customer care'));
                    return looksLikeFromMenu
                      && rect.width > 250
                      && rect.height > 90
                      && rect.top < window.innerHeight * 0.92;
                  })
                  .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    const aRole = /listbox|menu/i.test(a.getAttribute('role') || '') ? 0 : 1;
                    const bRole = /listbox|menu/i.test(b.getAttribute('role') || '') ? 0 : 1;
                    if (aRole !== bRole) return aRole - bRole;
                    const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
                    const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
                    if (aRight !== bRight) return aRight - bRight;
                    return (ar.width * ar.height) - (br.width * br.height);
                  });
                let didScroll = false;
                for (const root of roots.slice(0, 4)) {
                  const before = root.scrollTop;
                  root.scrollTop = root.scrollTop + 360;
                  const rect = root.getBoundingClientRect();
                  root.dispatchEvent(new Event('scroll', {bubbles: true}));
                  root.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: 360,
                    bubbles: true,
                    cancelable: true,
                    clientX: rect.left + rect.width / 2,
                    clientY: rect.top + rect.height / 2
                  }));
                  if (root.scrollTop !== before) didScroll = true;
                }
                return didScroll;
                """
                ,
                target_domain,
            )
        )
        if not scrolled:
            try:
                driver.switch_to.active_element.send_keys(Keys.PAGE_DOWN)
                scrolled = True
            except Exception:
                pass
        return scrolled

    if _select_orders_sender_native():
        time.sleep(0.8)
        current_from = _current_from_text()
        if target_email in current_from.lower():
            return current_from

    clicked = _open_from_dropdown()
    if not clicked:
        current_from = _current_from_text()
        if target_email in current_from.lower():
            return current_from
        raise CopyrightCancelError("Could not open Salesforce From dropdown.")

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _click_visible_orders_sender_option():
            time.sleep(0.8)
            current_from = _current_from_text()
            if target_email in current_from.lower():
                return current_from
            _scroll_orders_sender_dropdown()
            time.sleep(0.5)
            continue
        selected = _select_orders_sender_from_open_dropdown()
        if selected:
            time.sleep(0.8)
            current_from = _current_from_text()
            if target_email in current_from.lower():
                return current_from
            _scroll_orders_sender_dropdown()
            time.sleep(0.5)
            continue
        if not _scroll_orders_sender_dropdown():
            _open_from_dropdown()
        time.sleep(0.35)

    current_from = _current_from_text()
    if target_email in current_from.lower():
        return current_from
    raise CopyrightCancelError(f"Could not select {SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL} in Salesforce From dropdown.")


def _click_element_center(driver, element):
    rect = driver.execute_script(
        """
        const rect = arguments[0].getBoundingClientRect();
        return {
          x: rect.left + rect.width / 2,
          y: rect.top + rect.height / 2,
          width: rect.width,
          height: rect.height
        };
        """,
        element,
    )
    if not rect or rect.get("width", 0) <= 0 or rect.get("height", 0) <= 0:
        return False
    x = float(rect["x"])
    y = float(rect["y"])
    try:
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        return True
    except Exception:
        try:
            ActionChains(driver).move_to_element(element).pause(0.1).click(element).perform()
            return True
        except Exception:
            try:
                element.click()
                return True
            except Exception:
                return False


def _click_template_button(driver):
    _restore_salesforce_email_composer(driver)
    _wait_for_email_composer(driver, timeout=10)
    button = driver.execute_script(
        """
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || '');
            const rect = el.getBoundingClientRect();
            return rect.width > 300 && rect.height > 250
              && text.includes('from') && text.includes('subject') && text.includes('send');
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
            const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
            if (aRight !== bRight) return aRight - bRight;
            return (a.innerText || '').length - (b.innerText || '').length;
          });
        const composer = composers[0] || document;
        const cr = composer === document
          ? {left: 0, right: window.innerWidth, top: 0, bottom: window.innerHeight, width: window.innerWidth, height: window.innerHeight}
          : composer.getBoundingClientRect();
        const relatedLabels = Array.from(composer.querySelectorAll('label,span,div'))
          .filter((el) => visible(el) && clean(el.innerText || el.textContent || '') === 'related to')
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        const relatedTop = relatedLabels.length ? relatedLabels[0].getBoundingClientRect().top : cr.bottom;

        function uniqueByRect(elements) {
          const seen = new Set();
          const unique = [];
          for (const el of elements) {
            const rect = el.getBoundingClientRect();
            const key = [
              Math.round(rect.left),
              Math.round(rect.top),
              Math.round(rect.width),
              Math.round(rect.height)
            ].join(':');
            if (seen.has(key)) continue;
            seen.add(key);
            unique.push(el);
          }
          return unique;
        }

        const explicit = Array.from(composer.querySelectorAll('button,a,[role=button],span,div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(`${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`);
            const rect = el.getBoundingClientRect();
            const role = (el.getAttribute('role') || '').toLowerCase();
            const klass = String(el.className || '').toLowerCase();
            return rect.left >= cr.left && rect.right <= cr.right
              && rect.width >= 12 && rect.height >= 12
              && role !== 'tooltip'
              && !klass.includes('tooltip')
              && text.includes('template')
              && (rect.top > cr.top + cr.height * 0.55 || text.includes('insert, create, or update template'));
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const aButton = ['button', 'a'].includes(a.tagName.toLowerCase()) || (a.getAttribute('role') || '').toLowerCase() === 'button' ? 0 : 1;
            const bButton = ['button', 'a'].includes(b.tagName.toLowerCase()) || (b.getAttribute('role') || '').toLowerCase() === 'button' ? 0 : 1;
            if (aButton !== bButton) return aButton - bButton;
            return (br.y - ar.y) || (ar.x - br.x);
          });

        const bottomIconButtons = uniqueByRect(Array.from(composer.querySelectorAll('button,a,[role=button]'))
          .filter((el) => {
            if (!visible(el)) return false;
            const rect = el.getBoundingClientRect();
            if (rect.left < cr.left || rect.right > cr.right || rect.top < cr.top || rect.bottom > cr.bottom) return false;
            if (rect.top < cr.bottom - 95 || rect.left > cr.left + 210) return false;
            if (rect.width < 20 || rect.width > 70 || rect.height < 20 || rect.height > 70) return false;
            const text = clean(el.innerText || '');
            return text.length <= 12;
          }))
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const rowDelta = Math.abs(ar.y - br.y);
            if (rowDelta > 8) return br.y - ar.y;
            return ar.x - br.x;
          });

        let el = explicit[0] || bottomIconButtons[2] || bottomIconButtons[0] || null;
        if (!el) {
          const directX = cr.left + 102;
          const directY = relatedLabels.length ? relatedTop - 48 : cr.bottom - 79;
          if (directX > cr.left && directX < cr.right && directY > cr.top && directY < cr.bottom) {
            el = document.elementFromPoint(directX, directY);
          }
        }
        if (!el) return null;
        const clickable = el.closest('button,a,[role=button]') || el;
        try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
        return clickable;
        """
    )
    if button is None:
        raise CopyrightCancelError("Could not click Salesforce template button.")
    if not _click_element_center(driver, button):
        raise CopyrightCancelError("Could not click Salesforce template button.")


def _focus_salesforce_body_editor(driver):
    _restore_salesforce_email_composer(driver)
    _wait_for_email_composer(driver, timeout=10)
    target = driver.execute_script(
        """
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || '');
            const rect = el.getBoundingClientRect();
            return rect.width > 300 && rect.height > 250
              && text.includes('from') && text.includes('subject') && text.includes('send');
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
            const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
            if (aRight !== bRight) return aRight - bRight;
            return (a.innerText || '').length - (b.innerText || '').length;
          });
        const composer = composers[0] || document;
        const frames = Array.from(composer.querySelectorAll('iframe'))
          .filter(visible)
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
          });
        if (frames.length) {
          frames[0].scrollIntoView({block: 'center', inline: 'center'});
          return {kind: 'iframe', frame: frames[0]};
        }
        const fields = Array.from(composer.querySelectorAll('[contenteditable=true], textarea'))
          .filter((el) => {
            if (!visible(el)) return false;
            const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`);
            const rect = el.getBoundingClientRect();
            return !hint.includes('subject') && rect.height >= 80;
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
          });
        if (!fields.length) return null;
        fields[0].scrollIntoView({block: 'center', inline: 'center'});
        return {kind: 'element', element: fields[0]};
        """
    )
    def _dispatch_focus_click(element):
        driver.execute_script(
            """
            const el = arguments[0];
            const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
            try { el.focus(); } catch (err) {}
            let rect = el.getBoundingClientRect();
            if ((!rect.width || !rect.height) && el.ownerDocument && el.ownerDocument.body) {
              rect = el.ownerDocument.body.getBoundingClientRect();
            }
            const x = rect.left + Math.min(Math.max(rect.width / 2, 8), Math.max(rect.width - 8, 8));
            const y = rect.top + Math.min(Math.max(rect.height / 2, 8), Math.max(rect.height - 8, 8));
            for (const type of ['mouseover', 'mousedown', 'mouseup', 'click', 'focus']) {
              try {
                el.dispatchEvent(new view.MouseEvent(type, {
                  bubbles: true,
                  cancelable: true,
                  view,
                  clientX: x,
                  clientY: y
                }));
              } catch (err) {
                try { el.dispatchEvent(new view.Event(type, {bubbles: true})); } catch (eventErr) {}
              }
            }
            """,
            element,
        )

    if not target:
        return False

    if target.get("kind") == "iframe":
        frame = target.get("frame")
        if frame is None:
            return False
        driver.switch_to.frame(frame)
        try:
            body_element = driver.execute_script(
                """
                return document.querySelector('[contenteditable=true], textarea')
                  || (document.body && document.body.isContentEditable ? document.body : null)
                  || document.body;
                """
            )
            if body_element is None:
                return False
            driver.execute_script("arguments[0].focus();", body_element)
            try:
                ActionChains(driver).move_to_element_with_offset(body_element, 12, 12).click().perform()
            except Exception:
                _dispatch_focus_click(body_element)
        finally:
            driver.switch_to.default_content()
        time.sleep(0.3)
        return True

    element = target.get("element")
    if element is None:
        return False
    driver.execute_script("arguments[0].focus();", element)
    try:
        ActionChains(driver).move_to_element_with_offset(element, 12, 12).click().perform()
    except Exception:
        _dispatch_focus_click(element)
    time.sleep(0.3)
    return True


def _click_template_by_name(driver):
    target = SALESFORCE_COPYRIGHT_CANCEL_TEMPLATE.lower()
    option = driver.execute_script(
        """
        const target = arguments[0].toLowerCase();
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        const menus = Array.from(document.querySelectorAll('[role=menu],.uiMenuList,.popupTargetContainer,ul,div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || el.textContent || '');
            const rect = el.getBoundingClientRect();
            return rect.width > 120 && rect.height > 40
              && text.includes('insert a template')
              && (text.includes('recently used templates') || text.includes(target));
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (ar.width * ar.height) - (br.width * br.height);
          });
        const menu = menus[0];
        if (!menu) return null;
        const matches = Array.from(menu.querySelectorAll('a,button,[role=option],[role=menuitem],li'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || el.textContent || el.value || '');
            if (text !== target) return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 40 && rect.height > 12;
        });
        if (!matches.length) return null;
        matches.sort((a, b) => {
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          const aRole = ['a', 'button', 'li'].includes(a.tagName.toLowerCase()) || /option|menuitem/i.test(a.getAttribute('role') || '') ? 0 : 1;
          const bRole = ['a', 'button', 'li'].includes(b.tagName.toLowerCase()) || /option|menuitem/i.test(b.getAttribute('role') || '') ? 0 : 1;
          if (aRole !== bRole) return aRole - bRole;
          return (ar.y - br.y) || (ar.x - br.x);
        });
        const clickable = matches[0].closest('a,button,[role=option],[role=menuitem],li') || matches[0];
        try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
        return clickable;
        """,
        target,
    )
    if option is None:
        return False
    return _click_element_center(driver, option)


def _confirm_salesforce_template_insert(driver):
    """Confirm Salesforce's overwrite warning after a template is selected."""
    deadline = time.monotonic() + 8
    saw_warning = False
    while time.monotonic() < deadline:
        result = driver.execute_script(
            """
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim();
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            function walk(root, out = []) {
              for (const el of Array.from(root.querySelectorAll('*'))) {
                out.push(el);
                if (el.shadowRoot) walk(el.shadowRoot, out);
              }
              return out;
            }
            const nodes = walk(document);
            const text = clean(nodes.map((el) => el.innerText || el.textContent || '').join('\\n'));
            const hasWarning = /Inserting this template will overwrite the current email/i.test(text);
            const insert = nodes
              .filter((el) => /^(button|a)$/i.test(el.tagName) || (el.getAttribute('role') || '').toLowerCase() === 'button')
              .filter((el) => visible(el) && clean(el.innerText || el.textContent || el.value || el.getAttribute('title') || el.getAttribute('aria-label') || '') === 'Insert')
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (br.y - ar.y) || (br.x - ar.x);
              })[0] || null;
            if (insert) {
              try { insert.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
            }
            return {hasWarning, insert};
            """
        )
        if result and result.get("hasWarning"):
            saw_warning = True
        insert = result.get("insert") if isinstance(result, dict) else None
        if insert is not None:
            if not _click_element_center(driver, insert):
                try:
                    driver.execute_script("arguments[0].click();", insert)
                except Exception:
                    pass
            time.sleep(1)
            return True
        if saw_warning:
            time.sleep(0.4)
            continue
        # No overwrite prompt appeared; recent-template inserts can complete directly.
        time.sleep(0.4)
        if "insert email template" not in _visible_text(driver).lower():
            return True
    return not saw_warning


def _ensure_private_email_templates_folder(driver):
    target = "private email templates"
    selected = driver.execute_script(
        """
        const target = arguments[0];
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        for (const select of Array.from(document.querySelectorAll('select')).filter(visible)) {
          const options = Array.from(select.options || []);
          const option = options.find((item) => clean(`${item.text || ''} ${item.label || ''} ${item.value || ''}`).includes(target));
          if (!option) continue;
          select.value = option.value;
          option.selected = true;
          select.dispatchEvent(new Event('input', {bubbles: true}));
          select.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        }
        return clean(document.body ? document.body.innerText || '' : '').includes(target);
        """,
        target,
    )
    if selected:
        time.sleep(0.8)
        return True

    combo = driver.execute_script(
        """
        const target = arguments[0];
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        const labels = Array.from(document.querySelectorAll('label,span,div'))
          .filter((el) => visible(el) && clean(el.innerText || el.textContent || '') === 'template folders')
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        for (const label of labels) {
          const lr = label.getBoundingClientRect();
          const controls = Array.from(document.querySelectorAll('button,input,[role=combobox],a,div'))
            .filter((el) => {
              if (!visible(el) || el === label) return false;
              const rect = el.getBoundingClientRect();
              if (rect.top < lr.bottom - 5) return false;
              if (Math.abs((rect.left + rect.width / 2) - (lr.left + lr.width / 2)) > 260) return false;
              const text = clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`);
              return rect.width > 120 && rect.height > 20
                && (text.includes('template') || text.includes(target) || (el.getAttribute('role') || '').toLowerCase() === 'combobox');
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (ar.y - br.y) || (ar.x - br.x);
            });
          if (controls.length) {
            const clickable = controls[0].closest('button,a,[role=combobox]') || controls[0];
            try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
            return clickable;
          }
        }
        return null;
        """,
        target,
    )
    if combo is None or not _click_element_center(driver, combo):
        return False
    time.sleep(0.5)
    if _click_visible_text_with_action(driver, "Private Email Templates"):
        time.sleep(0.8)
        return True
    return False


def _search_full_template_modal(driver, query):
    return bool(
        driver.execute_script(
            """
            const query = arguments[0];
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            const search = Array.from(document.querySelectorAll('input')).find((el) => {
              if (!visible(el)) return false;
              const text = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`);
              return text.includes('search') || text.includes('template');
            });
            if (!search) return false;
            search.focus();
            search.value = query;
            search.dispatchEvent(new Event('input', {bubbles: true}));
            search.dispatchEvent(new Event('change', {bubbles: true}));
            search.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: query.slice(-1) || 't'}));
            return true;
            """,
            query,
        )
    )


def _scroll_full_template_modal(driver):
    return bool(
        driver.execute_script(
            """
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            const scrollers = Array.from(document.querySelectorAll('*'))
              .filter((el) => {
                if (!visible(el) || el.scrollHeight <= el.clientHeight + 12) return false;
                const rect = el.getBoundingClientRect();
                const text = clean(el.innerText || el.textContent || '');
                return rect.width > 450 && rect.height > 160
                  && (text.includes('template folders') || text.includes('name') || text.includes('private email templates'));
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (br.width * br.height) - (ar.width * ar.height);
              });
            for (const el of scrollers.slice(0, 4)) {
              const before = el.scrollTop;
              el.scrollTop = Math.min(el.scrollTop + 420, el.scrollHeight);
              const rect = el.getBoundingClientRect();
              el.dispatchEvent(new Event('scroll', {bubbles: true}));
              el.dispatchEvent(new WheelEvent('wheel', {
                deltaY: 420,
                bubbles: true,
                cancelable: true,
                clientX: rect.left + rect.width / 2,
                clientY: rect.top + rect.height / 2
              }));
              if (el.scrollTop !== before) return true;
            }
            return false;
            """
        )
    )


def _click_full_template_modal_match(driver):
    target = SALESFORCE_COPYRIGHT_CANCEL_TEMPLATE.lower()
    option = driver.execute_script(
        """
        const target = arguments[0].toLowerCase();
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function walk(root, out = []) {
          for (const el of Array.from(root.querySelectorAll('*'))) {
            out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot, out);
          }
          return out;
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        function isTargetTemplate(text) {
          return text === target || text.endsWith(target) || text.includes(target);
        }
        const nodes = walk(document).filter((el) => /^(a|button|td|tr|span|div)$/i.test(el.tagName) || (el.getAttribute('role') || '').toLowerCase() === 'button')
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || el.textContent || el.value || '');
            if (!isTargetTemplate(text)) return false;
            const rect = el.getBoundingClientRect();
            if (rect.width < 30 || rect.height < 10) return false;
            const row = el.closest('tr') || el;
            const rowText = clean(row.innerText || row.textContent || '');
            return rowText.includes('private email templates') || isTargetTemplate(text);
          });
        if (!nodes.length) return null;
        nodes.sort((a, b) => {
          const at = clean(a.innerText || a.textContent || '');
          const bt = clean(b.innerText || b.textContent || '');
          const aExact = at === target ? 0 : (at.endsWith(target) ? 1 : 2);
          const bExact = bt === target ? 0 : (bt.endsWith(target) ? 1 : 2);
          if (aExact !== bExact) return aExact - bExact;
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          const aLink = ['a', 'button'].includes(a.tagName.toLowerCase()) || (a.getAttribute('role') || '').toLowerCase() === 'button' ? 0 : 1;
          const bLink = ['a', 'button'].includes(b.tagName.toLowerCase()) || (b.getAttribute('role') || '').toLowerCase() === 'button' ? 0 : 1;
          if (aLink !== bLink) return aLink - bLink;
          return (ar.y - br.y) || (ar.x - br.x);
        });
        const clickable = nodes[0].closest('a,button,[role=button]') || nodes[0];
        try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
        return clickable;
        """,
        target,
    )
    if option is None:
        return False
    return _click_element_center(driver, option)


def _click_visible_text_with_action(driver, expected_text):
    target = driver.execute_script(
        """
        const expected = arguments[0].toLowerCase();
        const nodes = Array.from(document.querySelectorAll('a,button,input,[role=button],[role=option],[role=menuitem],li,span,div'));
        const matches = nodes.filter((el) => {
          const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
            .replace(/\\s+/g, ' ').trim().toLowerCase();
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return text === expected && rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        });
        if (!matches.length) return null;
        matches.sort((a, b) => {
          const ar = a.getBoundingClientRect();
          const br = b.getBoundingClientRect();
          const aRole = ['a', 'button', 'input', 'li'].includes(a.tagName.toLowerCase()) || /button|option|menuitem/i.test(a.getAttribute('role') || '') ? 0 : 1;
          const bRole = ['a', 'button', 'input', 'li'].includes(b.tagName.toLowerCase()) || /button|option|menuitem/i.test(b.getAttribute('role') || '') ? 0 : 1;
          if (aRole !== bRole) return aRole - bRole;
          return (ar.y - br.y) || (ar.x - br.x);
        });
        const clickable = matches[0].closest('a,button,input,[role=button],[role=option],[role=menuitem],li') || matches[0];
        try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
        return clickable;
        """,
        expected_text,
    )
    if target is None:
        return False
    return _click_element_center(driver, target)


def _open_full_template_picker_from_menu(driver):
    def _full_picker_is_open():
        return bool(
            driver.execute_script(
                """
                const text = document.body ? document.body.innerText || '' : '';
                return text.includes('Insert Email Template') && text.includes('Template Folders');
                """
            )
        )

    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if _full_picker_is_open():
            return True
        item = driver.execute_script(
            """
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            const matches = Array.from(document.querySelectorAll('a,[role=menuitem],li,button,span,div'))
              .filter((el) => {
                if (!visible(el)) return false;
                const text = clean(el.innerText || el.textContent || el.value || '');
                if (text !== 'insert a template...') return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 80 && rect.height > 14 && rect.height < 60;
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const aMenu = (a.getAttribute('role') || '').toLowerCase() === 'menuitem' || a.tagName.toLowerCase() === 'a' ? 0 : 1;
                const bMenu = (b.getAttribute('role') || '').toLowerCase() === 'menuitem' || b.tagName.toLowerCase() === 'a' ? 0 : 1;
                if (aMenu !== bMenu) return aMenu - bMenu;
                return (ar.width * ar.height) - (br.width * br.height);
              });
            const clickable = matches[0] ? (matches[0].closest('a,[role=menuitem],button') || matches[0]) : null;
            if (clickable) {
              try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
            }
            return clickable;
            """
        )
        if item is None:
            _click_template_button(driver)
            time.sleep(0.5)
            continue
        _click_element_center(driver, item)
        time.sleep(0.8)
        if _full_picker_is_open():
            return True
        try:
            driver.execute_script("arguments[0].click();", item)
        except Exception:
            pass
        time.sleep(0.8)
    return _full_picker_is_open()


def _insert_copyright_template(driver):
    _focus_salesforce_body_editor(driver)
    _click_template_button(driver)
    time.sleep(0.5)
    if not _open_full_template_picker_from_menu(driver):
        raise CopyrightCancelError("Insert a template was not found in Salesforce template menu.")
    time.sleep(1)
    _ensure_private_email_templates_folder(driver)
    _search_full_template_modal(driver, "copyright")
    time.sleep(1)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if _click_full_template_modal_match(driver):
            _confirm_salesforce_template_insert(driver)
            return True
        _search_full_template_modal(driver, "copyright")
        if not _scroll_full_template_modal(driver):
            time.sleep(0.5)
        time.sleep(0.6)
    raise CopyrightCancelError("CANCEL - Copyright template was not selectable in Salesforce.")


def _replace_subject_order_number(driver, order_id):
    replaced = bool(
        driver.execute_script(
            """
            const orderId = arguments[0];
            const fields = Array.from(document.querySelectorAll('input,textarea,[contenteditable=true]'));
            for (const el of fields) {
              const text = (el.value !== undefined ? el.value : el.innerText || '').trim();
              const placeholder = (el.placeholder || '').toLowerCase();
              if (!text.includes('XXXXXX') && !placeholder.includes('subject')) continue;
              const next = text.replace(/XXXXXX/g, orderId);
              if (el.value !== undefined) {
                el.focus();
                el.value = next;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
              } else {
                el.focus();
                el.innerText = next;
                el.dispatchEvent(new Event('input', {bubbles: true}));
              }
              return true;
            }
            return false;
            """,
            str(order_id),
        )
    )
    if not replaced:
        raise CopyrightCancelError("Could not replace XXXXXX in Salesforce email subject.")


def _verify_template_loaded(driver, order_id):
    text = _visible_text(driver)
    try:
        rich_text = driver.execute_script(
            """
            const parts = [document.body ? document.body.innerText || '' : ''];
            for (const el of Array.from(document.querySelectorAll('input,textarea,[contenteditable=true]'))) {
              parts.push(el.value || el.innerText || el.textContent || '');
            }
            for (const frame of Array.from(document.querySelectorAll('iframe'))) {
              try {
                const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
                if (doc && doc.body) parts.push(doc.body.innerText || doc.body.textContent || '');
              } catch (err) {}
            }
            return parts.join('\\n');
            """
        )
        if rich_text:
            text = f"{text}\n{rich_text}"
    except Exception:
        pass
    if str(order_id) not in text:
        raise CopyrightCancelError("Salesforce template subject does not contain the target order number after replacement.")
    lower_text = text.lower()
    if (
        "copyright" not in lower_text
        and "intellectual property" not in lower_text
        and "a refund has been issued" not in lower_text
    ):
        raise CopyrightCancelError("Salesforce email body does not look like the copyright cancellation template.")


def _fill_salesforce_email_from_local_template(driver, subject, body):
    filled = driver.execute_script(
        """
        const subject = arguments[0];
        const body = arguments[1];

        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        function emit(el, value) {
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const eventData = value === undefined ? '' : String(value || '');
          try {
            el.dispatchEvent(new view.InputEvent('beforeinput', {
              bubbles: true,
              cancelable: true,
              inputType: 'insertFromPaste',
              data: eventData
            }));
          } catch (err) {}
          try {
            el.dispatchEvent(new view.InputEvent('input', {
              bubbles: true,
              inputType: 'insertFromPaste',
              data: eventData
            }));
          } catch (err) {
            el.dispatchEvent(new view.Event('input', {bubbles: true}));
          }
          el.dispatchEvent(new view.Event('change', {bubbles: true}));
          try { el.dispatchEvent(new view.KeyboardEvent('keyup', {bubbles: true})); } catch (err) {}
        }
        function setNativeValue(el, value) {
          const tag = (el.tagName || '').toLowerCase();
          let proto = null;
          if (tag === 'textarea') proto = HTMLTextAreaElement.prototype;
          if (tag === 'input') proto = HTMLInputElement.prototype;
          const desc = proto ? Object.getOwnPropertyDescriptor(proto, 'value') : null;
          if (desc && desc.set) desc.set.call(el, value);
          else el.value = value;
        }
        function selectContents(doc, el) {
          const win = doc.defaultView || window;
          const range = doc.createRange();
          range.selectNodeContents(el);
          const selection = win.getSelection && win.getSelection();
          if (!selection) return false;
          selection.removeAllRanges();
          selection.addRange(range);
          return true;
        }
        function insertRichText(doc, el, text, html) {
          const win = doc.defaultView || window;
          el.focus();
          try {
            selectContents(doc, el);
            doc.execCommand('delete', false, null);
          } catch (err) {
            try { el.innerHTML = ''; } catch (innerErr) {}
          }
          let inserted = false;
          try {
            inserted = doc.execCommand('insertHTML', false, html);
          } catch (err) {}
          if (!inserted || clean(el.innerText || el.textContent || '').length < 10) {
            try {
              el.innerHTML = html;
              inserted = true;
            } catch (err) {}
          }
          emit(el, text);
          return clean(el.innerText || el.textContent || '');
        }
        function plainTextToHtml(text) {
          return String(text || '')
            .split(/\\n{2,}/)
            .map((paragraph) => paragraph.split(/\\n/).map((line) => {
              const div = document.createElement('div');
              div.textContent = line;
              return div.innerHTML;
            }).join('<br>'))
            .map((paragraph) => `<p>${paragraph || '<br>'}</p>`)
            .join('');
        }

        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || '');
            const rect = el.getBoundingClientRect();
            return rect.width > 300 && rect.height > 250
              && text.includes('from') && text.includes('subject') && text.includes('send');
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
            const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
            if (aRight !== bRight) return aRight - bRight;
            return (a.innerText || '').length - (b.innerText || '').length;
          });
        const composer = composers[0] || document;

        const subjectFields = Array.from(composer.querySelectorAll('input,textarea,[contenteditable=true]'))
          .filter((el) => {
            if (!visible(el)) return false;
            const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`);
            const value = clean(el.value || el.innerText || el.textContent || '');
            const rect = el.getBoundingClientRect();
            return hint.includes('subject') || value.includes('enter subject') || rect.height < 70;
          })
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        const subjectField = subjectFields.find((el) => {
          const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`);
          return hint.includes('subject');
        }) || subjectFields[0];
        if (!subjectField) return {subjectFilled: false, bodyFilled: false, reason: 'subject field not found'};

        subjectField.focus();
        if (subjectField.value !== undefined) {
          setNativeValue(subjectField, subject);
        } else {
          insertRichText(document, subjectField, subject, subject);
        }
        emit(subjectField, subject);

        const bodyHtml = plainTextToHtml(body);
        let bodyFilled = false;
        let bodyText = '';
        const frames = Array.from(composer.querySelectorAll('iframe'))
          .filter((frame) => visible(frame))
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
          });
        for (const frame of frames) {
          try {
            const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
            if (!doc || !doc.body) continue;
            const text = insertRichText(doc, doc.body, body, bodyHtml);
            if (text.includes('while reviewing your order') || text.includes('processed a refund back to your account')) {
              bodyText = text;
              bodyFilled = true;
              break;
            }
          } catch (err) {}
        }
        if (!bodyFilled) {
          const bodyFields = Array.from(composer.querySelectorAll('[contenteditable=true], textarea'))
            .filter((el) => visible(el) && el !== subjectField)
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (br.width * br.height) - (ar.width * ar.height);
            });
          const bodyField = bodyFields[0];
          if (bodyField) {
            bodyField.focus();
            if (bodyField.value !== undefined) {
              setNativeValue(bodyField, body);
              emit(bodyField, body);
              bodyText = clean(bodyField.value || '');
            } else {
              bodyText = insertRichText(document, bodyField, body, bodyHtml);
            }
            bodyFilled = true;
          }
        }
        return {
          subjectFilled: true,
          bodyFilled,
          subjectText: clean(subjectField.value || subjectField.innerText || subjectField.textContent || ''),
          bodyText
        };
        """,
        subject,
        body,
    )
    if not filled or not filled.get("subjectFilled"):
        raise CopyrightCancelError("Could not fill Salesforce email subject from local template.")
    if not filled.get("bodyFilled"):
        raise CopyrightCancelError("Could not fill Salesforce email body from local template.")
    filled_body = _clean_text(filled.get("bodyText", "")).lower()
    if "while reviewing your order" not in filled_body or "processed a refund back to your account" not in filled_body:
        raise CopyrightCancelError("Salesforce email body did not retain the rendered local template after filling.")
    typed_body = _type_salesforce_body_with_keyboard(driver, body)
    lower_typed_body = _clean_text(typed_body).lower()
    if "while reviewing your order" not in lower_typed_body or "processed a refund back to your account" not in lower_typed_body:
        raise CopyrightCancelError("Salesforce email body did not retain the typed copyright template after keyboard fill.")


def _type_salesforce_body_with_keyboard(driver, body):
    _set_clipboard_text(body)
    target = driver.execute_script(
        """
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || '');
            const rect = el.getBoundingClientRect();
            return rect.width > 300 && rect.height > 250
              && text.includes('from') && text.includes('subject') && text.includes('send');
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
            const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
            if (aRight !== bRight) return aRight - bRight;
            return (a.innerText || '').length - (b.innerText || '').length;
          });
        const composer = composers[0] || document;
        const frames = Array.from(composer.querySelectorAll('iframe'))
          .filter(visible)
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
          });
        if (frames.length) {
          frames[0].scrollIntoView({block: 'center', inline: 'center'});
          return {kind: 'iframe', frame: frames[0]};
        }
        const bodyFields = Array.from(composer.querySelectorAll('[contenteditable=true], textarea'))
          .filter((el) => {
            if (!visible(el)) return false;
            const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`);
            const rect = el.getBoundingClientRect();
            return !hint.includes('subject') && rect.height >= 80;
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
          });
        if (bodyFields.length) {
          bodyFields[0].scrollIntoView({block: 'center', inline: 'center'});
          return {kind: 'element', element: bodyFields[0]};
        }
        return null;
        """
    )
    if not target:
        raise CopyrightCancelError("Salesforce email body editor was not found for keyboard fill.")

    def _clear_and_paste(element):
        driver.execute_script("arguments[0].focus();", element)
        time.sleep(0.1)
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)
        time.sleep(0.1)
        element.send_keys(Keys.CONTROL, "a")
        time.sleep(0.1)
        element.send_keys(Keys.BACKSPACE)
        time.sleep(0.1)
        element.send_keys(Keys.CONTROL, "v")
        time.sleep(0.8)
        try:
            element.send_keys(Keys.TAB)
            time.sleep(0.2)
        except Exception:
            pass
        driver.execute_script(
            """
            const el = arguments[0];
            const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
            for (const type of ['input', 'change', 'keyup', 'blur']) {
              try { el.dispatchEvent(new view.Event(type, {bubbles: true})); } catch (err) {}
            }
            try { el.blur(); } catch (err) {}
            """,
            element,
        )

    if target.get("kind") == "iframe":
        frame = target.get("frame")
        if frame is None:
            raise CopyrightCancelError("Salesforce body iframe was not returned.")
        driver.switch_to.frame(frame)
        try:
            body_element = driver.execute_script(
                """
                return document.querySelector('[contenteditable=true], textarea')
                  || (document.body && document.body.isContentEditable ? document.body : null)
                  || document.body;
                """
            )
            if body_element is None:
                raise CopyrightCancelError("Salesforce body iframe did not contain an editable body.")
            _clear_and_paste(body_element)
            return driver.execute_script("return document.body ? (document.body.innerText || document.body.textContent || '') : '';")
        finally:
            driver.switch_to.default_content()

    element = target.get("element")
    if element is None:
        raise CopyrightCancelError("Salesforce body editor element was not returned.")
    _clear_and_paste(element)
    return driver.execute_script(
        "return arguments[0].value || arguments[0].innerText || arguments[0].textContent || '';",
        element,
    )


def _read_salesforce_email_composer_text(driver):
    return str(
        driver.execute_script(
            """
            const parts = [];
            function walk(root, out = []) {
              for (const el of Array.from(root.querySelectorAll('*'))) {
                out.push(el);
                if (el.shadowRoot) walk(el.shadowRoot, out);
              }
              return out;
            }
            function readDoc(doc, seen = new Set()) {
              if (!doc || seen.has(doc)) return;
              seen.add(doc);
              if (doc.body) parts.push(doc.body.innerText || doc.body.textContent || '');
              for (const el of walk(doc).filter((node) => /^(input|textarea)$/i.test(node.tagName) || node.isContentEditable)) {
                parts.push(el.value || el.innerText || el.textContent || '');
              }
              for (const frame of walk(doc).filter((node) => (node.tagName || '').toLowerCase() === 'iframe')) {
                try {
                  const child = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
                  readDoc(child, seen);
                } catch (err) {}
              }
            }
            readDoc(document);
            return parts.join('\\n');
            """
        )
        or ""
    )


def _verify_local_email_template_loaded(driver, order_id, subject, body):
    text = _read_salesforce_email_composer_text(driver)
    normalized_text = _clean_text(text)
    lower_text = normalized_text.lower()
    normalized_subject = _clean_text(subject)
    if str(order_id) not in normalized_text or normalized_subject not in normalized_text:
        raise CopyrightCancelError("Salesforce email subject was not filled with the rendered local template.")
    # Salesforce's rich-text editor can render correctly while hiding its body
    # text from a page-level read. If we can read the body, validate it; if not,
    # rely on the direct fill operation that just succeeded.
    if "while reviewing your order" in lower_text and "processed a refund back to your account" not in lower_text:
        raise CopyrightCancelError("Salesforce email body was not filled with the rendered local copyright template.")


def _fill_salesforce_email_from_salesforce_template(driver, order_id, subject, expected_body):
    _insert_copyright_template(driver)
    deadline = time.monotonic() + 20
    template_text = ""
    while time.monotonic() < deadline:
        time.sleep(0.5)
        template_text = _read_salesforce_email_composer_text(driver)
        lower_text = template_text.lower()
        if "while reviewing your order" in lower_text and "processed a refund back to your account" in lower_text:
            break
    else:
        raise CopyrightCancelError("Salesforce copyright template was selected, but the email body did not load.")

    _replace_subject_order_number(driver, order_id)
    time.sleep(0.5)
    state = _read_salesforce_email_state(driver)
    body_text = _clean_text(state.get("body", ""))
    subject_text = _clean_text(state.get("subject", ""))
    lower_body = body_text.lower()
    if "while reviewing your order" not in lower_body or "processed a refund back to your account" not in lower_body:
        raise CopyrightCancelError("Salesforce template body is not visible in the composer after insertion.")
    if str(order_id) not in subject_text:
        raise CopyrightCancelError(f"Salesforce template subject does not contain order {order_id}. Current subject: {subject_text or 'blank'}")
    expected_markers = [
        "while reviewing your order",
        "processed a refund back to your account",
        "800-620-1233",
    ]
    missing = [marker for marker in expected_markers if marker not in lower_body]
    if missing:
        raise CopyrightCancelError(f"Salesforce template body is missing expected text: {', '.join(missing)}")
    return {
        "source": "salesforce_template",
        "template": SALESFORCE_COPYRIGHT_CANCEL_TEMPLATE,
        "state": state,
    }


def _read_salesforce_email_state(driver):
    return driver.execute_script(
        """
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim();
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        function walk(root, out = []) {
          for (const el of Array.from(root.querySelectorAll('*'))) {
            out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot, out);
          }
          return out;
        }
        function composerCandidates() {
          return Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
            .filter((el) => {
              if (!visible(el)) return false;
              const text = (el.innerText || '').toLowerCase();
              const rect = el.getBoundingClientRect();
              return rect.width > 300 && rect.height > 250
                && text.includes('from') && text.includes('subject') && text.includes('send');
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
              const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
              if (aRight !== bRight) return aRight - bRight;
              return (a.innerText || '').length - (b.innerText || '').length;
            });
        }
        function readFrom(composer) {
          const labels = Array.from(composer.querySelectorAll('label,span,div,td,th'))
            .filter((el) => {
              if (!visible(el)) return false;
              const text = clean(el.innerText || el.textContent || '').toLowerCase();
              return text === 'from' || text === '* from' || text.endsWith(' from');
            })
            .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
          for (const label of labels) {
            const lr = label.getBoundingClientRect();
            const controls = Array.from(composer.querySelectorAll('select,input,button,[role=combobox],a,div,span'))
              .filter((el) => {
                if (!visible(el) || el === label) return false;
                const rect = el.getBoundingClientRect();
                if (rect.left <= lr.right) return false;
                if (Math.abs((rect.top + rect.height / 2) - (lr.top + lr.height / 2)) > 36) return false;
                return rect.width > 120 && rect.height > 18;
              })
              .sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
            const pieces = [];
            for (const control of controls) {
              if ((control.tagName || '').toLowerCase() === 'select') {
                const selected = control.options && control.selectedIndex >= 0 ? control.options[control.selectedIndex] : null;
                if (selected) pieces.push(selected.text || selected.label || selected.value || '');
              }
              pieces.push(control.innerText || '');
              pieces.push(control.value || '');
              pieces.push(control.getAttribute('aria-label') || '');
              pieces.push(control.getAttribute('title') || '');
            }
            const text = clean(pieces.join(' '));
            if (text) return text;
          }
          return '';
        }
        function readSubject(composer) {
          const fields = Array.from(composer.querySelectorAll('input,textarea,[contenteditable=true]'))
            .filter((el) => {
              if (!visible(el)) return false;
              const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
              const rect = el.getBoundingClientRect();
              return hint.includes('subject') || rect.height < 70;
            })
            .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
          const field = fields.find((el) => clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase().includes('subject')) || fields[0];
          return field ? clean(field.value || field.innerText || field.textContent || '') : '';
        }
        function readBody(composer) {
          const parts = [];
          function readDoc(doc, seen = new Set()) {
            if (!doc || seen.has(doc)) return;
            seen.add(doc);
            if (doc.body) parts.push(doc.body.innerText || doc.body.textContent || '');
            for (const el of walk(doc).filter((node) => /^(input|textarea)$/i.test(node.tagName) || node.isContentEditable)) {
              parts.push(el.value || el.innerText || el.textContent || '');
            }
            for (const frame of walk(doc).filter((node) => (node.tagName || '').toLowerCase() === 'iframe')) {
              try {
                const child = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
                readDoc(child, seen);
              } catch (err) {}
            }
          }
          for (const frame of walk(composer).filter((node) => (node.tagName || '').toLowerCase() === 'iframe' && visible(node))) {
            try {
              const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
              readDoc(doc);
            } catch (err) {}
          }
          for (const el of walk(composer).filter((node) => (node.isContentEditable || /^(textarea)$/i.test(node.tagName)) && visible(node))) {
            const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
            const rect = el.getBoundingClientRect();
            if (hint.includes('subject') || rect.height < 70) continue;
            parts.push(el.value || el.innerText || el.textContent || '');
          }
          return clean(parts.join('\\n'));
        }
        const composer = composerCandidates()[0] || document;
        return {
          from: readFrom(composer),
          subject: readSubject(composer),
          body: readBody(composer)
        };
        """
    ) or {}


def _verify_salesforce_email_ready_to_send(driver, order_id, subject, body):
    state = _read_salesforce_email_state(driver)
    from_text = _clean_text(state.get("from", ""))
    subject_text = _clean_text(state.get("subject", ""))
    body_text = _clean_text(state.get("body", ""))
    lower_body = body_text.lower()
    if SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL.lower() not in from_text.lower():
        raise CopyrightCancelError(f"Salesforce From is not Orders before send. Current From: {from_text or 'blank'}")
    if str(order_id) not in subject_text or _clean_text(subject) not in subject_text:
        raise CopyrightCancelError(f"Salesforce subject is not ready before send. Current subject: {subject_text or 'blank'}")
    if "while reviewing your order" not in lower_body or "processed a refund back to your account" not in lower_body:
        raise CopyrightCancelError("Salesforce body is not ready before send; refusing to send a blank/bodyless email.")
    return state


def _send_salesforce_email(driver, dry_run, order_id, subject, body, skip_ready_verify=False):
    ready_state = _read_salesforce_email_state(driver) if skip_ready_verify else _verify_salesforce_email_ready_to_send(driver, order_id, subject, body)
    if dry_run:
        return {"sent": False, "dry_run": True, "email_state": ready_state, "message": "Skipped Salesforce Send in dry-run mode."}
    if not _click_salesforce_send_button(driver):
        raise CopyrightCancelError("Salesforce Send button was not found.")
    time.sleep(4)
    return {"sent": True, "dry_run": False, "email_state": ready_state}


def _click_salesforce_send_button(driver):
    _restore_salesforce_email_composer(driver)
    _wait_for_email_composer(driver, timeout=10)
    try:
        driver.switch_to.active_element.send_keys(Keys.TAB)
        time.sleep(0.2)
    except Exception:
        pass
    try:
        driver.execute_script("if (document.activeElement && document.activeElement.blur) document.activeElement.blur();")
    except Exception:
        pass

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        send = driver.execute_script(
            """
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
              .filter((el) => {
                if (!visible(el)) return false;
                const text = clean(el.innerText || '');
                const rect = el.getBoundingClientRect();
                return rect.width > 300 && rect.height > 250
                  && text.includes('from') && text.includes('subject') && text.includes('send');
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const aRight = ar.left > window.innerWidth * 0.45 ? 0 : 1;
                const bRight = br.left > window.innerWidth * 0.45 ? 0 : 1;
                if (aRight !== bRight) return aRight - bRight;
                return (a.innerText || '').length - (b.innerText || '').length;
              });
            const composer = composers[0] || document;
            const cr = composer === document
              ? {left: 0, right: window.innerWidth, top: 0, bottom: window.innerHeight, width: window.innerWidth, height: window.innerHeight}
              : composer.getBoundingClientRect();
            const candidates = Array.from(composer.querySelectorAll('button,a,input,[role=button],span,div'))
              .filter((el) => {
                if (!visible(el)) return false;
                const text = clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`);
                const rect = el.getBoundingClientRect();
                return text === 'send'
                  && rect.left >= cr.left && rect.right <= cr.right
                  && rect.top >= cr.top && rect.bottom <= cr.bottom;
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const aButton = ['button', 'a', 'input'].includes(a.tagName.toLowerCase()) || (a.getAttribute('role') || '').toLowerCase() === 'button' ? 0 : 1;
                const bButton = ['button', 'a', 'input'].includes(b.tagName.toLowerCase()) || (b.getAttribute('role') || '').toLowerCase() === 'button' ? 0 : 1;
                if (aButton !== bButton) return aButton - bButton;
                const aBottomRight = (window.innerHeight - ar.bottom) + (window.innerWidth - ar.right);
                const bBottomRight = (window.innerHeight - br.bottom) + (window.innerWidth - br.right);
                return aBottomRight - bBottomRight;
              });
            const send = candidates[0];
            if (!send) return null;
            const clickable = send.closest('button,a,input,[role=button]') || send;
            clickable.scrollIntoView({block: 'center', inline: 'center'});
            return clickable;
            """
        )
        if send is not None:
            try:
                ActionChains(driver).move_to_element(send).pause(0.1).click(send).perform()
                return True
            except Exception:
                try:
                    send.click()
                    return True
                except Exception:
                    pass
        time.sleep(0.5)
    return False


def _prepare_and_maybe_send_salesforce_email(
    driver,
    crm_handle,
    order_id,
    customer_email,
    dry_run,
    login_wait_seconds=0,
    skip_from_selection=False,
    skip_ready_verify=False,
):
    sf_handle = _open_salesforce_account(driver, crm_handle, customer_email, login_wait_seconds=login_wait_seconds)
    _verify_salesforce_email(driver, customer_email)
    _click_salesforce_email(driver, customer_email)
    _wait_for_email_composer(driver)
    if skip_from_selection:
        selected_from = _clean_text((_read_salesforce_email_state(driver) or {}).get("from", "")) or "Skipped From selection for inspection"
    else:
        selected_from = _set_salesforce_from_orders(driver)
    rendered = _render_email_template("copyright_cancel", order_number=order_id)
    time.sleep(1)
    fill_result = _fill_salesforce_email_from_salesforce_template(
        driver,
        order_id=order_id,
        subject=rendered["subject"],
        expected_body=rendered["body"],
    )
    result = _send_salesforce_email(
        driver,
        dry_run=dry_run,
        order_id=order_id,
        subject=rendered["subject"],
        body=rendered["body"],
        skip_ready_verify=skip_ready_verify,
    )
    return {
        "salesforce_handle": sf_handle,
        "email": customer_email,
        "from": selected_from,
        "subject": rendered["subject"],
        "fill": fill_result,
        **result,
    }


def _read_payment_summary(driver):
    _activate_crm_context(driver)
    live_payment_type = ""
    live_amount = ""
    live_panel_text = ""
    try:
        state = _get_order_live_state(driver)
        transactions = state.get("transactions") or []
        transaction_amount = ""
        for transaction in transactions:
            tag = _clean_text(transaction.get("tag") or transaction.get("type"))
            amount_value = _parse_money(transaction.get("amount"))
            if amount_value > 0:
                live_payment_type = tag
                transaction_amount = _money_text(amount_value)
                break
        paid_amount = _money_text(_parse_money(state.get("amount_paid"))) if state.get("amount_paid") not in (None, "") else ""
        live_amount = paid_amount if paid_amount and _parse_money(paid_amount) > 0 else transaction_amount
        live_panel_text = _clean_text(
            " ".join(
                [
                    f"amount_paid={state.get('amount_paid')}",
                    f"amount_due={state.get('amount_due')}",
                    f"transactions={transactions}",
                ]
            )
        )
    except Exception:
        pass
    text = driver.execute_script(
        """
        const panel = Array.from(document.querySelectorAll('div,section,table'))
          .find((el) => (el.innerText || '').includes('Payments and Credits'));
        return panel ? panel.innerText : (document.body ? document.body.innerText : '');
        """
    )
    payment_type = ""
    amount = ""
    match = re.search(r"\$?\s*([0-9,]+\.\d{2})\s+([^\n\r]+?)\s+\d{1,2}/\d{1,2}/\d{2,4}", text)
    if match:
        amount = _money_text(_parse_money(match.group(1)))
        payment_type = _clean_text(match.group(2))
    elif "stripe.com" in text.lower():
        payment_type = "Stripe.com"
        lines = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
        for index, line in enumerate(lines):
            if "stripe.com" not in line.lower():
                continue
            same_line_amounts = re.findall(r"\$?\s*([0-9,]+\.\d{2})", line)
            if same_line_amounts:
                amount = _money_text(_parse_money(same_line_amounts[0]))
                break
            nearby = " ".join(lines[max(0, index - 1) : min(len(lines), index + 2)])
            nearby_amounts = re.findall(r"\$?\s*([0-9,]+\.\d{2})", nearby)
            if nearby_amounts:
                amount = _money_text(_parse_money(nearby_amounts[0]))
                break
    if not payment_type:
        payment_type = live_payment_type
    if not amount:
        amount = live_amount
    panel_text = _clean_text(" ".join([live_panel_text, text]))[:1000]
    return {"payment_type": payment_type, "amount": amount, "panel_text": panel_text}


def _validate_refundable_stripe_payment(payment):
    payment_type_text = str(payment.get("payment_type") or "")
    panel_text = str(payment.get("panel_text") or "")
    if "stripe.com" not in payment_type_text.lower() and "stripe.com" not in panel_text.lower():
        raise CopyrightCancelError(
            f"Only Stripe.com copyright refunds are supported right now. Found payment type: {payment.get('payment_type') or 'unknown'}."
        )
    amount = _parse_money(payment.get("amount"))
    if amount <= 0:
        raise CopyrightCancelError(
            "Could not find a positive Stripe.com refund amount on the CRM order. "
            f"Payment summary: {payment}"
        )
    return payment


def _has_positive_payment_amount(payment):
    amount = _parse_money((payment or {}).get("amount"))
    return amount > 0


def _refund_fee_rows(driver):
    try:
        return _order_scope(
            driver,
            """
            const rows = r.orderFees || r.fees || [];
            return rows.map((fee, index) => ({
              index,
              feeId: fee.feeId || fee.id || '',
              name: fee.name || fee.feeName || '',
              code: fee.code || '',
              amount: fee.amount || fee.price || fee.total || ''
            }));
            """,
        )
    except Exception:
        return []


def _existing_refund_fee_amount(driver):
    amounts = []
    for fee in _refund_fee_rows(driver):
        label = _clean_text(f"{fee.get('name', '')} {fee.get('code', '')}").lower()
        if "refund" in label:
            amounts.append(_parse_money(fee.get("amount")).copy_abs())
    return sum(amounts, Decimal("0.00")).quantize(Decimal("0.01"))


def _read_order_refund_fee_amount(driver):
    state = _get_order_live_state(driver)
    # Refund fee should offset the pre-tax customer charge: item subtotal plus shipping, excluding sales tax.
    subtotal = _parse_money(state.get("subtotal"))
    shipping = _parse_money(state.get("shipping_charges")).copy_abs()
    amount = (subtotal.copy_abs() + shipping).quantize(Decimal("0.01"))
    if amount <= 0:
        raise CopyrightCancelError(f"Could not determine a positive CRM subtotal for the Refund fee. Order state: {state}")
    return amount


def _add_refund_fee_to_original(driver, refund_amount=None):
    refund_amount = _parse_money(refund_amount).copy_abs() if refund_amount not in (None, "") else _read_order_refund_fee_amount(driver)
    refund_amount = refund_amount.quantize(Decimal("0.01"))
    if refund_amount <= 0:
        raise CopyrightCancelError(f"Refund fee amount must be greater than zero. Found: {refund_amount}")

    rows = _refund_fee_rows(driver)
    exact_row = None
    editable_row = None
    for fee in rows:
        label = _clean_text(f"{fee.get('name', '')} {fee.get('code', '')}").lower()
        if "refund" not in label:
            continue
        amount = _parse_money(fee.get("amount")).copy_abs()
        if amount == refund_amount:
            exact_row = fee
            break
        if editable_row is None:
            editable_row = fee
    if exact_row:
        return {
            "skipped": True,
            "reason": "already_present",
            "amount": _money_text(refund_amount),
            "fee": exact_row,
            "totals": _read_order_totals(driver),
        }

    _order_scope(driver, "runInAngular(s, () => s.editModeOn()); return true;")
    time.sleep(0.5)

    if editable_row is None:
        clicked = _click_ng_button(driver, "OrderFeesController.order.addFee(null, OrderFeesController.availableFees[0])", "add fee")
        if not clicked:
            clicked = bool(
                driver.execute_script(
                    """
                    const forbidden = /\\b(refund|issue\\s+refund|refund\\s+payment)\\b/i;
                    const button = Array.from(document.querySelectorAll('button,a')).find((el) => {
                      const text = (el.innerText || '').replace(/\\s+/g, ' ').trim();
                      if (forbidden.test(text)) throw new Error('Refusing to click refund control: ' + text);
                      return text.toLowerCase() === 'add fee';
                    });
                    if (!button) return false;
                    button.click();
                    return true;
                    """
                )
            )
        if not clicked:
            raise CopyrightCancelError("Could not click Add Fee on the original order.")
        time.sleep(0.5)

    result = _order_scope(
        driver,
        """
        const amount = arguments[0];
        const fees = r.orderFees || r.fees || [];
        if (!fees.length) throw new Error('No fee row was available');
        function feeLabel(fee) {
          return `${fee.name || fee.feeName || ''} ${fee.code || ''}`.replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        let fee = fees.find((row) => feeLabel(row).includes('refund'));
        let updatedExisting = true;
        if (!fee) {
          fee = fees[fees.length - 1];
          updatedExisting = false;
        }
        fee.feeId = 12;
        fee.name = 'Refund';
        fee.code = 'refund';
        fee.amount = amount;
        fee.crudAction = fee.crudAction || (updatedExisting ? 'u' : 'c');
        r.orderFees = fees;
        runInAngular(s, () => {});
        return {
          updatedExisting,
          feeId: fee.feeId,
          name: fee.name || '',
          code: fee.code || '',
          amount: fee.amount || ''
        };
        """,
        f"-{_money_text(refund_amount)}",
    )
    totals_before_save = _read_order_totals(driver)
    save_result = _save_order_and_wait(driver)
    totals_after_save = _read_order_totals(driver)
    return {
        "skipped": False,
        "amount": _money_text(refund_amount),
        "fee": result,
        "totals_before_save": totals_before_save,
        "save": save_result,
        "totals_after_save": totals_after_save,
    }


def _open_stripe_refund_modal(driver):
    _activate_crm_context(driver)
    def refund_modal_visible():
        try:
            return "max refund available" in _page_text(driver).lower()
        except Exception:
            return False

    def click_refund_button():
        return bool(
            driver.execute_script(
                """
                function visible(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
                }
                function clean(el) {
                  return (el.innerText || el.value || el.getAttribute('aria-label') || '')
                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                }
                const panels = Array.from(document.querySelectorAll('div,section,table'))
                  .filter((el) => {
                    const text = (el.innerText || '').toLowerCase();
                    return text.includes('payments and credits') && text.includes('stripe.com') && visible(el);
                  })
                  .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                const root = panels[0];
                if (!root) return false;
                const rows = Array.from(root.querySelectorAll('tr,tbody,div,section'))
                  .filter((el) => (el.innerText || '').toLowerCase().includes('stripe.com') && visible(el))
                  .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                const scopes = rows.concat([root]);
                for (const scope of scopes) {
                  const candidates = Array.from(scope.querySelectorAll('button,a,input,[role=button]'))
                    .filter((el) => clean(el) === 'refund' && visible(el))
                    .sort((a, b) => {
                      const ar = a.getBoundingClientRect();
                      const br = b.getBoundingClientRect();
                      return (br.width * br.height) - (ar.width * ar.height);
                    });
                  if (candidates.length) {
                    candidates[0].scrollIntoView({block: 'center', inline: 'center'});
                    candidates[0].click();
                    return true;
                  }
                }
                return false;
                """
            )
        )

    if click_refund_button():
        time.sleep(1)
        return

    clicked_view = bool(
        driver.execute_script(
            """
            const panels = Array.from(document.querySelectorAll('div,section,table'))
              .filter((el) => (el.innerText || '').toLowerCase().includes('payments and credits'));
            const stripePanels = panels.filter((el) => (el.innerText || '').toLowerCase().includes('stripe.com'));
            const root = (stripePanels.length ? stripePanels : panels)
              .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0] || document;
            const stripeText = (root.innerText || '').toLowerCase();
            if (!stripeText.includes('stripe.com')) return false;
            const rows = Array.from(root.querySelectorAll('tr,tbody,div,section')).filter((el) => {
              const text = (el.innerText || '').toLowerCase();
              const rect = el.getBoundingClientRect();
              return text.includes('stripe.com') && rect.width > 0 && rect.height > 0;
            });
            const scope = rows.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0] || root;
            const scopedButtons = Array.from(scope.querySelectorAll('button,a,input,[role=button]'));
            const rootButtons = Array.from(root.querySelectorAll('button,a,input,[role=button]'));
            const buttons = scopedButtons.concat(rootButtons.filter((el) => !scopedButtons.includes(el)));
            const control = buttons.find((el) => {
              const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return (text === 'view' || text === 'details' || text === 'refund') && rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden';
            });
            if (!control) return false;
            control.scrollIntoView({block: 'center', inline: 'center'});
            control.click();
            return true;
            """
        )
    )
    if clicked_view:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if refund_modal_visible():
                return
            if click_refund_button():
                time.sleep(1)
                if refund_modal_visible():
                    return
            time.sleep(0.5)
    if not clicked_view:
        raise CopyrightCancelError("Stripe.com payment was not found with a clickable refund button.")
    raise CopyrightCancelError("Stripe.com payment details opened, but the Refund button was not found.")


def _read_refund_modal_state(driver):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = driver.execute_script(
            """
            const modal = Array.from(document.querySelectorAll('.modal, .modal-content, [role=dialog]'))
              .find((el) => (el.innerText || '').toLowerCase().includes('max refund available'));
            if (!modal) return null;
            const text = modal.innerText || '';
            const input = Array.from(modal.querySelectorAll('input')).find((el) => {
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            });
            return {text, input_value: input ? input.value : ''};
            """
        )
        if state:
            max_match = re.search(r"Max Refund Available:\s*\$?\s*([0-9,]+\.\d{2})", state.get("text", ""), re.I)
            max_amount = _money_text(_parse_money(max_match.group(1))) if max_match else ""
            input_amount = _money_text(_parse_money(state.get("input_value", ""))) if state.get("input_value") else ""
            if max_amount and input_amount:
                return {"max_refund": max_amount, "input_amount": input_amount}
        time.sleep(0.5)
    raise CopyrightCancelError("Refund modal did not show max refund and input amount.")


def _close_refund_modal(driver):
    _click_exact_visible_text(driver, "cancel") or _click_exact_visible_text(driver, "Cancel")


def _save_refund_modal(driver, dry_run):
    state = _read_refund_modal_state(driver)
    if state["max_refund"] != state["input_amount"]:
        raise CopyrightCancelError(
            f"Refund amount mismatch. Max refund {state['max_refund']} but input has {state['input_amount']}."
        )
    if dry_run:
        _close_refund_modal(driver)
        return {"refunded": False, "dry_run": True, **state}
    clicked = bool(
        driver.execute_script(
            """
            const modal = Array.from(document.querySelectorAll('.modal, .modal-content, [role=dialog]'))
              .find((el) => (el.innerText || '').toLowerCase().includes('max refund available'));
            if (!modal) return false;
            const buttons = Array.from(modal.querySelectorAll('button,a,input,[role=button]'));
            const save = buttons.find((el) => {
              const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return text === 'save' && rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden';
            });
            if (!save) return false;
            save.scrollIntoView({block: 'center', inline: 'center'});
            save.click();
            return true;
            """
        )
    )
    if not clicked:
        raise CopyrightCancelError("Refund modal save button was not found.")
    time.sleep(3)
    return {"refunded": True, "dry_run": False, **state}


def _refund_via_stripe_payment_modal(driver, dry_run, click_refund_button=True):
    _open_stripe_refund_modal(driver)
    state = _read_refund_modal_state(driver)
    if dry_run:
        _close_refund_modal(driver)
        return {"refunded": False, "dry_run": True, "prepared": True, "refund_button_clicked": False, **state}
    if not click_refund_button:
        return {"refunded": False, "dry_run": False, "prepared": True, "refund_button_clicked": False, **state}
    return _save_refund_modal(driver, dry_run=False)


def _close_crm_modal(driver):
    return bool(
        driver.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('.modal button,.modal a,[role=dialog] button,[role=dialog] a'));
            const close = buttons.find((el) => {
              const text = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              return (text === 'close' || text === 'cancel' || text === 'x')
                && rect.width > 0 && rect.height > 0;
            });
            if (!close) return false;
            close.click();
            return true;
            """
        )
    )


def _set_transaction_modal_refund(driver, amount, note):
    amount_text = _money_text(amount)
    note_text = str(note or "")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            state = driver.execute_script(
                ANGULAR_APPLY_JS
                + """
                function findTransactionScope() {
                  const nodes = Array.from(document.querySelectorAll('.modal *, .modal, [role=dialog] *, [role=dialog]'));
                  for (const el of nodes) {
                    let scope = null;
                    try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
                    for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
                      if (scope.transaction) return scope;
                    }
                  }
                  return null;
                }
                const s = findTransactionScope();
                if (!s) return null;
                runInAngular(s, () => {
                  s.transaction.tag = 'Refund';
                  s.transaction.note = arguments[1];
                  s.transaction.amount = arguments[0];
                });
                return {
                  amount: String(s.transaction.amount || ''),
                  tag: String(s.transaction.tag || ''),
                  note: String(s.transaction.note || ''),
                  canSave: typeof s.save === 'function',
                  keys: Object.keys(s).filter((key) => /refund|save|record/i.test(key)).slice(0, 20)
                };
                """,
                amount_text,
                note_text,
            )
            if state and _money_text(_parse_money(state.get("amount"))) == amount_text:
                return state
        except Exception:
            pass
        time.sleep(0.5)
    raise CopyrightCancelError("Transaction modal did not expose a refund amount field.")


def _click_transaction_refund_button(driver):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            clicked = bool(
                driver.execute_script(
                    """
                    function clean(value) {
                      return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    }
                    function visible(el) {
                      const rect = el.getBoundingClientRect();
                      const style = window.getComputedStyle(el);
                      return rect.width > 0 && rect.height > 0
                        && style.display !== 'none' && style.visibility !== 'hidden'
                        && rect.bottom > 0 && rect.top < window.innerHeight
                        && rect.right > 0 && rect.left < window.innerWidth;
                    }
                    const modals = Array.from(document.querySelectorAll('.modal, .modal-content, [role=dialog], div'))
                      .filter((el) => visible(el) && clean(el.innerText || '').includes('refund'))
                      .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                    const root = modals[0] || document;
                    const buttons = Array.from(root.querySelectorAll('button,a,input,[role=button]'))
                      .filter((el) => {
                        if (!visible(el)) return false;
                        const text = clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`);
                        return text === 'refund';
                      })
                      .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return (br.y - ar.y) || (br.x - ar.x);
                      });
                    const button = buttons[0];
                    if (!button) return false;
                    const rect = button.getBoundingClientRect();
                    const clickX = rect.left + rect.width / 2;
                    const clickY = rect.top + rect.height / 2;
                    const target = document.elementFromPoint(clickX, clickY) || button;
                    for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
                      target.dispatchEvent(new MouseEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: clickX,
                        clientY: clickY
                      }));
                    }
                    return true;
                    """
                )
            )
            if clicked:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    raise CopyrightCancelError("Transaction modal Refund button was not found.")


def _refund_via_transaction_modal(driver, amount, note, dry_run, click_refund_button=True):
    refund_amount = _parse_money(amount).copy_abs()
    if refund_amount <= 0:
        raise CopyrightCancelError(f"Refund amount must be greater than zero. Found: {amount}")
    _open_record_transaction(driver, quote=False)
    state = _set_transaction_modal_refund(driver, -refund_amount, note)
    if dry_run:
        _close_crm_modal(driver)
        return {
            "refunded": False,
            "dry_run": True,
            "amount": _money_text(refund_amount),
            "transaction_state": state,
            "message": "Skipped clicking Refund in dry-run mode.",
        }
    if not click_refund_button:
        return {
            "refunded": False,
            "dry_run": False,
            "prepared": True,
            "refund_button_clicked": False,
            "amount": _money_text(refund_amount),
            "transaction_state": state,
            "message": "Prepared transaction refund modal and left it open, but skipped the final Refund button by request.",
        }
    _click_transaction_refund_button(driver)
    time.sleep(4)
    return {
        "refunded": True,
        "dry_run": False,
        "amount": _money_text(refund_amount),
        "transaction_state": state,
    }


def _cancel_and_refund_crm_order(driver, crm_handle, order_id, dry_run, click_refund_button=True, payment=None):
    driver.switch_to.window(crm_handle)
    _activate_crm_context(driver)
    _wait_for_order_scope(driver, order_id=order_id)
    payment = payment or _read_payment_summary(driver)
    refund_fee_amount = _read_order_refund_fee_amount(driver)
    if dry_run:
        cancel_result = {"cancelled": False, "dry_run": True, "message": "Skipped order cancellation in dry-run mode."}
        refund_fee_result = {
            "added": False,
            "dry_run": True,
            "amount": _money_text(refund_fee_amount),
            "message": "Skipped adding Refund fee in dry-run mode.",
        }
        refund_result = {"refunded": False, "dry_run": True, "message": "Skipped refund modal in dry-run mode."}
        return {"payment": payment, "cancel": cancel_result, "refund_fee": refund_fee_result, "refund": refund_result}
    else:
        _cancel_original_order(driver)
        cancel_result = {"cancelled": True, "dry_run": False}
        refund_fee_result = _add_refund_fee_to_original(driver, refund_fee_amount)
    try:
        validated_payment = _validate_refundable_stripe_payment(payment)
    except CopyrightCancelError as exc:
        if click_refund_button and _has_positive_payment_amount(payment):
            raise
        refund_result = {
            "refunded": False,
            "dry_run": False,
            "prepared": False,
            "refund_button_clicked": False,
            "message": f"Skipped payment refund modal because no refundable Stripe payment was available: {exc}",
        }
    else:
        if not click_refund_button:
            refund_result = {
                "refunded": False,
                "dry_run": bool(dry_run),
                "prepared": False,
                "refund_button_clicked": False,
                "message": "Skipped payment refund modal by request.",
            }
        else:
            refund_result = _refund_via_stripe_payment_modal(
                driver,
                dry_run=dry_run,
                click_refund_button=click_refund_button,
            )
    return {"payment": payment, "cancel": cancel_result, "refund_fee": refund_fee_result, "refund": refund_result}


def send_salesforce_email_single_order(
    order_id,
    dry_run=True,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    keep_browser_open=False,
    keep_browser_open_on_error=False,
    skip_from_selection=False,
    skip_ready_verify=False,
):
    order_id = _normalize_order_id(order_id)
    order_url = PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
    driver = None
    had_error = False
    try:
        driver = _open_driver(visible=visible, attach_browser=attach_browser, debugger_address=debugger_address)
        safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        _login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        _switch_to_crm_app_frame(driver)
        _wait_for_order_scope(driver, order_id=order_id)
        crm_handle = driver.current_window_handle
        contact = _get_crm_contact_info(driver)
        salesforce = _prepare_and_maybe_send_salesforce_email(
            driver,
            crm_handle,
            order_id,
            contact["email"],
            dry_run=dry_run,
            login_wait_seconds=login_wait_seconds,
            skip_from_selection=skip_from_selection,
            skip_ready_verify=skip_ready_verify,
        )
        return {
            "order_id": order_id,
            "order_url": order_url,
            "dry_run": bool(dry_run),
            "contact": contact,
            "salesforce": salesforce,
        }
    except Exception:
        had_error = True
        if driver is not None:
            safe_take_screenshot(driver, f"copyright_cancel_{order_id}_email_error")
        raise
    finally:
        should_keep_open = keep_browser_open or (keep_browser_open_on_error and had_error)
        if driver is not None and not attach_browser and should_keep_open:
            _retain_browser_for_inspection(driver)
        elif driver is not None and not attach_browser:
            safe_driver_quit(driver, profile_path=_profile_path())


def refund_single_order(
    order_id,
    dry_run=True,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    click_refund_button=True,
    refund_fee_amount=None,
    keep_browser_open=False,
    keep_browser_open_on_error=False,
):
    order_id = _normalize_order_id(order_id)
    order_url = PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
    driver = None
    had_error = False
    try:
        driver = _open_driver(visible=visible, attach_browser=attach_browser, debugger_address=debugger_address)
        safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        _login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        _switch_to_crm_app_frame(driver)
        _wait_for_order_scope(driver, order_id=order_id)
        payment = _read_payment_summary(driver)
        if click_refund_button and _has_positive_payment_amount(payment):
            _validate_refundable_stripe_payment(payment)
        refund_fee_amount = _parse_money(refund_fee_amount).copy_abs() if refund_fee_amount not in (None, "") else _read_order_refund_fee_amount(driver)
        if dry_run:
            refund_fee = {
                "added": False,
                "dry_run": True,
                "amount": _money_text(refund_fee_amount),
                "message": "Skipped adding Refund fee in dry-run mode.",
            }
            try:
                validated_payment = _validate_refundable_stripe_payment(payment)
            except CopyrightCancelError as exc:
                refund = {
                    "refunded": False,
                    "dry_run": True,
                    "prepared": False,
                    "message": f"Skipped refund modal in dry-run mode because no refundable Stripe payment was available: {exc}",
                }
            else:
                if not click_refund_button:
                    refund = {
                        "refunded": False,
                        "dry_run": True,
                        "prepared": False,
                        "refund_button_clicked": False,
                        "message": "Skipped payment refund modal by request.",
                    }
                else:
                    refund = _refund_via_stripe_payment_modal(
                        driver,
                        dry_run=True,
                        click_refund_button=click_refund_button,
                    )
        else:
            refund_fee = _add_refund_fee_to_original(driver, refund_fee_amount)
            try:
                validated_payment = _validate_refundable_stripe_payment(payment)
            except CopyrightCancelError as exc:
                if click_refund_button and _has_positive_payment_amount(payment):
                    raise
                refund = {
                    "refunded": False,
                    "dry_run": False,
                    "prepared": False,
                    "refund_button_clicked": False,
                    "message": f"Skipped payment refund modal because no refundable Stripe payment was available: {exc}",
                }
            else:
                if not click_refund_button:
                    refund = {
                        "refunded": False,
                        "dry_run": False,
                        "prepared": False,
                        "refund_button_clicked": False,
                        "message": "Skipped payment refund modal by request.",
                    }
                else:
                    refund = _refund_via_stripe_payment_modal(
                        driver,
                        dry_run=False,
                        click_refund_button=click_refund_button,
                    )
        return {
            "order_id": order_id,
            "order_url": order_url,
            "dry_run": bool(dry_run),
            "payment": payment,
            "refund_fee": refund_fee,
            "refund": refund,
        }
    except Exception:
        had_error = True
        if driver is not None:
            safe_take_screenshot(driver, f"copyright_cancel_{order_id}_refund_error")
        raise
    finally:
        should_keep_open = keep_browser_open or (keep_browser_open_on_error and had_error)
        if driver is not None and not attach_browser and should_keep_open:
            _retain_browser_for_inspection(driver)
        elif driver is not None and not attach_browser:
            safe_driver_quit(driver, profile_path=_profile_path())


def _open_driver(visible=False, attach_browser=False, debugger_address="127.0.0.1:9222"):
    if attach_browser:
        return build_attached_chrome_driver(debugger_address=debugger_address)
    profile = _profile_path()
    headless = bool(PROCESSOR_HEADLESS and not visible)
    kill_stale_chrome(profile, profile_label="CRM copyright cancel")
    return build_chrome_driver(
        profile,
        headless_mode=headless,
        page_load_strategy="eager",
        page_load_timeout=PROCESSOR_PAGE_LOAD_TIMEOUT,
        script_timeout=PROCESSOR_ACTION_TIMEOUT,
    )


def _retain_browser_for_inspection(driver):
    if driver is not None:
        HELD_DRIVERS.append(driver)


def _hold_retained_browsers_for_inspection():
    if not HELD_DRIVERS:
        return
    print("Keeping Chrome open for inspection. Close the Chrome window to let this worker exit.")
    while True:
        alive = False
        for driver in list(HELD_DRIVERS):
            try:
                driver.current_window_handle
                alive = True
            except Exception:
                try:
                    HELD_DRIVERS.remove(driver)
                except ValueError:
                    pass
        if not alive:
            return
        time.sleep(5)


def process_single_order(
    order_id,
    dry_run=True,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    click_refund_button=True,
    keep_browser_open=False,
    keep_browser_open_on_error=False,
):
    order_id = _normalize_order_id(order_id)
    order_url = PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
    driver = None
    had_error = False
    try:
        driver = _open_driver(visible=visible, attach_browser=attach_browser, debugger_address=debugger_address)
        safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        _login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        _switch_to_crm_app_frame(driver)
        _wait_for_order_scope(driver, order_id=order_id)
        crm_handle = driver.current_window_handle
        contact = _get_crm_contact_info(driver)
        payment = _read_payment_summary(driver)
        if not dry_run and click_refund_button and _has_positive_payment_amount(payment):
            _validate_refundable_stripe_payment(payment)
        crm_action = _cancel_and_refund_crm_order(
            driver,
            crm_handle,
            order_id,
            dry_run=dry_run,
            click_refund_button=click_refund_button,
            payment=payment,
        )
        salesforce = _prepare_and_maybe_send_salesforce_email(
            driver,
            crm_handle,
            order_id,
            contact["email"],
            dry_run=dry_run,
            login_wait_seconds=login_wait_seconds,
        )
        post_cancel_stock = _handle_post_cancel_stock_return(
            driver,
            crm_handle,
            order_id,
            order_url,
            dry_run=dry_run,
            enabled=click_refund_button,
        )
        return {
            "order_id": order_id,
            "order_url": order_url,
            "dry_run": bool(dry_run),
            "contact": contact,
            "salesforce": salesforce,
            "crm_action": crm_action,
            "post_cancel_stock": post_cancel_stock,
        }
    except Exception:
        had_error = True
        if driver is not None:
            safe_take_screenshot(driver, f"copyright_cancel_{order_id}_error")
        raise
    finally:
        should_keep_open = keep_browser_open or (keep_browser_open_on_error and had_error)
        if driver is not None and not attach_browser and should_keep_open:
            _retain_browser_for_inspection(driver)
        elif driver is not None and not attach_browser:
            safe_driver_quit(driver, profile_path=_profile_path())


def run_scan_sheet(result_file=None, include_error_rows=False):
    spreadsheet, worksheet, headers, eligible, skipped = _scan_queue_rows(include_error_rows=include_error_rows)
    payload = {
        "spreadsheet_title": spreadsheet.title,
        "worksheet_title": worksheet.title,
        "headers": headers,
        "eligible_rows": [
            {
                "row_number": row.row_number,
                "order_id": row.order_id,
                "issue_type": row.issue_type,
                "error": row.error,
                "order_url": row.order_url,
            }
            for row in eligible
        ],
        "skipped_rows": skipped,
    }
    _write_result(
        True,
        f"Found {len(eligible)} eligible copyright-cancel row(s) in Google Sheet.",
        result_file=result_file,
        action="scan_sheet",
        **payload,
    )
    return 0


def _hold_browser_after_result_if_requested(args):
    if getattr(args, "keep_browser_open", False) or getattr(args, "keep_browser_open_on_error", False):
        _hold_retained_browsers_for_inspection()


def run_process_order(args):
    started = time.monotonic()
    order_ref = args.order_id or args.order_url
    try:
        details = process_single_order(
            order_ref,
            dry_run=args.dry_run,
            visible=args.visible,
            attach_browser=args.attach_browser,
            debugger_address=args.debugger_address,
            login_wait_seconds=args.login_wait_seconds,
            click_refund_button=not args.skip_refund_click,
            keep_browser_open=args.keep_browser_open,
            keep_browser_open_on_error=args.keep_browser_open_on_error,
        )
        _write_result(
            True,
            f"Copyright-cancel {'dry run' if args.dry_run else 'automation'} complete for order {details['order_id']}.",
            result_file=args.result_file,
            action="process_order",
            duration_seconds=round(time.monotonic() - started, 2),
            **details,
        )
        _hold_browser_after_result_if_requested(args)
        return 0
    except Exception as exc:
        _write_result(
            False,
            f"Copyright-cancel failed: {exc}",
            result_file=args.result_file,
            action="process_order",
            dry_run=bool(args.dry_run),
            order_reference=order_ref,
            error_type=type(exc).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        _hold_browser_after_result_if_requested(args)
        return 1


def run_send_email_order(args):
    started = time.monotonic()
    order_ref = args.order_id or args.order_url
    try:
        details = send_salesforce_email_single_order(
            order_ref,
            dry_run=args.dry_run,
            visible=args.visible,
            attach_browser=args.attach_browser,
            debugger_address=args.debugger_address,
            login_wait_seconds=args.login_wait_seconds,
            keep_browser_open=args.keep_browser_open,
            keep_browser_open_on_error=args.keep_browser_open_on_error,
            skip_from_selection=args.skip_from_selection,
            skip_ready_verify=args.skip_ready_verify,
        )
        _write_result(
            True,
            f"Copyright-cancel Salesforce email {'dry run' if args.dry_run else 'send'} complete for order {details['order_id']}.",
            result_file=args.result_file,
            action="send_email_order",
            duration_seconds=round(time.monotonic() - started, 2),
            **details,
        )
        _hold_browser_after_result_if_requested(args)
        return 0
    except Exception as exc:
        _write_result(
            False,
            f"Copyright-cancel Salesforce email recovery failed: {exc}",
            result_file=args.result_file,
            action="send_email_order",
            dry_run=bool(args.dry_run),
            order_reference=order_ref,
            error_type=type(exc).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        _hold_browser_after_result_if_requested(args)
        return 1


def run_refund_order(args):
    started = time.monotonic()
    order_ref = args.order_id or args.order_url
    try:
        details = refund_single_order(
            order_ref,
            dry_run=args.dry_run,
            visible=args.visible,
            attach_browser=args.attach_browser,
            debugger_address=args.debugger_address,
            login_wait_seconds=args.login_wait_seconds,
            click_refund_button=not args.skip_refund_click,
            refund_fee_amount=args.refund_fee_amount,
            keep_browser_open=args.keep_browser_open,
            keep_browser_open_on_error=args.keep_browser_open_on_error,
        )
        deleted_sheet_row = False
        if args.delete_sheet_row and not args.dry_run and not args.skip_refund_click:
            spreadsheet, worksheet, headers, eligible, _skipped = _scan_queue_rows(include_error_rows=True)
            for row in sorted(eligible, key=lambda item: item.row_number, reverse=True):
                if row.order_id == details["order_id"]:
                    _delete_sheet_row(worksheet, row.row_number)
                    deleted_sheet_row = True
                    details["deleted_sheet_row_number"] = row.row_number
                    details["spreadsheet_title"] = spreadsheet.title
                    details["worksheet_title"] = worksheet.title
                    break
        _write_result(
            True,
            f"Copyright-cancel refund {'dry run' if args.dry_run else 'recovery'} complete for order {details['order_id']}.",
            result_file=args.result_file,
            action="refund_order",
            duration_seconds=round(time.monotonic() - started, 2),
            deleted_sheet_row=deleted_sheet_row,
            **details,
        )
        _hold_browser_after_result_if_requested(args)
        return 0
    except Exception as exc:
        _write_result(
            False,
            f"Copyright-cancel refund recovery failed: {exc}",
            result_file=args.result_file,
            action="refund_order",
            dry_run=bool(args.dry_run),
            order_reference=order_ref,
            error_type=type(exc).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        _hold_browser_after_result_if_requested(args)
        return 1


def run_process_queue(args):
    started = time.monotonic()
    spreadsheet, worksheet, headers, eligible, skipped = _scan_queue_rows(include_error_rows=args.retry_errors)
    limit = int(args.limit or 0)
    if limit > 0:
        eligible = eligible[:limit]
    processed = []
    failures = []
    # Delete from bottom to top so row numbers remain stable after successful runs.
    for row in sorted(eligible, key=lambda item: item.row_number, reverse=True):
        try:
            details = process_single_order(
                row.order_id,
                dry_run=args.dry_run,
                visible=args.visible,
                attach_browser=args.attach_browser,
                debugger_address=args.debugger_address,
                login_wait_seconds=args.login_wait_seconds,
                click_refund_button=not args.skip_refund_click,
                keep_browser_open=args.keep_browser_open,
                keep_browser_open_on_error=args.keep_browser_open_on_error,
            )
            processed.append({"row_number": row.row_number, "order_id": row.order_id, **details})
            if not args.dry_run and not args.skip_refund_click:
                _delete_sheet_row(worksheet, row.row_number)
        except Exception as exc:
            error_text = str(exc)
            failures.append(
                {
                    "row_number": row.row_number,
                    "order_id": row.order_id,
                    "error": error_text,
                    "error_type": type(exc).__name__,
                }
            )
            if not args.dry_run:
                _write_sheet_error(worksheet, headers, row.row_number, error_text)
    ok = not failures
    message = (
        f"Processed {len(processed)} copyright-cancel row(s); {len(failures)} failed."
        if eligible
        else "No eligible copyright-cancel rows found."
    )
    _write_result(
        ok,
        message,
        result_file=args.result_file,
        action="process_queue",
        dry_run=bool(args.dry_run),
        spreadsheet_title=spreadsheet.title,
        worksheet_title=worksheet.title,
        processed=processed,
        failures=failures,
        skipped_rows=skipped,
        duration_seconds=round(time.monotonic() - started, 2),
    )
    _hold_browser_after_result_if_requested(args)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="CRM copyright-cancel automation worker.")
    parser.add_argument("--action", choices=["scan_sheet", "process_queue", "process_order", "send_email_order", "refund_order"], default="scan_sheet")
    parser.add_argument("--order-id", default="")
    parser.add_argument("--order-url", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument("--delete-sheet-row", action="store_true")
    parser.add_argument("--refund-fee-amount", default="", help="Override the CRM Refund fee amount for one-off recovery.")
    parser.add_argument(
        "--skip-refund-click",
        action="store_true",
        help="Prepare the CRM refund flow but do not press the final Refund button.",
    )
    parser.add_argument("--login-wait-seconds", type=int, default=0)
    parser.add_argument("--attach-browser", action="store_true")
    parser.add_argument("--debugger-address", default="127.0.0.1:9222")
    parser.add_argument("--profile-dir", default="", help="Override the Chrome profile directory for this run.")
    parser.add_argument("--keep-browser-open", action="store_true", help="Leave Chrome open after the run for manual inspection.")
    parser.add_argument("--keep-browser-open-on-error", action="store_true", help="Leave Chrome open only when the run fails.")
    parser.add_argument("--skip-from-selection", action="store_true", help="Inspection-only: do not change the Salesforce From field.")
    parser.add_argument("--skip-ready-verify", action="store_true", help="Inspection-only: do not enforce pre-send From/body verification.")
    parser.add_argument("--dry-run", action="store_true", default=PROCESSOR_DRY_RUN)
    parser.add_argument("--real", action="store_true", help="Send email, cancel CRM order, save refund, and delete successful sheet rows.")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--result-file", default=RESULT_FILE)
    args = parser.parse_args(argv)
    args.dry_run = bool(args.dry_run and not args.real)
    global PROFILE_DIR_OVERRIDE
    PROFILE_DIR_OVERRIDE = str(args.profile_dir or "").strip()

    if args.action == "scan_sheet":
        return run_scan_sheet(result_file=args.result_file, include_error_rows=args.retry_errors)
    if args.action == "process_order":
        if not (args.order_id or args.order_url):
            _write_result(False, "--order-id or --order-url is required.", result_file=args.result_file, action=args.action)
            return 2
        return run_process_order(args)
    if args.action == "send_email_order":
        if not (args.order_id or args.order_url):
            _write_result(False, "--order-id or --order-url is required.", result_file=args.result_file, action=args.action)
            return 2
        return run_send_email_order(args)
    if args.action == "refund_order":
        if not (args.order_id or args.order_url):
            _write_result(False, "--order-id or --order-url is required.", result_file=args.result_file, action=args.action)
            return 2
        return run_refund_order(args)
    if args.action == "process_queue":
        return run_process_queue(args)
    _write_result(False, f"Unsupported action: {args.action}", result_file=args.result_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
