"""
Isolated CRM copyright-cancel automation.

Default behavior is dry-run: read the Google Sheet queue, open/inspect CRM and
Salesforce, and stop before customer email send, order cancellation, and refund
save. Use --real only after selector verification on a safe test order.
"""

import argparse
import ctypes
import html
import json
import os
import re
import subprocess
import sys
import tempfile
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
    write_status_payload,
    write_result_payload,
)
from config import (
    CONTENT_VIOLATION_CANCEL_ISSUE_TYPE,
    COPYRIGHT_CANCEL_ISSUE_TYPE,
    EXISTING_DESIGNS_CANCEL_ISSUE_TYPE,
    GOOGLE_SHEET_ERROR_COLUMN,
    GOOGLE_SHEET_ID,
    GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
    GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
    GOOGLE_SHEET_REASON_COLUMN,
    GOOGLE_SHEET_WORKSHEET,
    PROCESSOR_ACTION_TIMEOUT,
    PROCESSOR_DRY_RUN,
    PROCESSOR_HEADLESS,
    PROCESSOR_ORDER_URL_TEMPLATE,
    PROCESSOR_PAGE_LOAD_TIMEOUT,
    PROCESSOR_PROFILE_DIR,
    SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL,
    SALESFORCE_COPYRIGHT_CANCEL_FROM_LABEL,
    SALESFORCE_COPYRIGHT_CANCEL_TEMPLATE,
    SALESFORCE_CONTENT_VIOLATION_CANCEL_TEMPLATE,
    SALESFORCE_EXISTING_DESIGNS_CANCEL_TEMPLATE,
    SALESFORCE_EMAIL_TEMPLATE_FILE,
    SALESFORCE_OUTSIDE_LIMIT_CANCEL_TEMPLATE,
    OUTSIDE_LIMIT_CANCEL_ISSUE_TYPE,
)
import config as _config
from runtime_paths import STATE_DIR, resolve_runtime_file
from credential_store import (
    CRM_CREDENTIAL_TARGET,
    GOOGLE_SHEETS_CREDENTIAL_TARGET,
    SALESFORCE_CREDENTIAL_TARGET,
    read_json_credential,
    read_windows_credential,
)
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
    run_split_order as _run_auto_split_order,
)
import crm_product_separator as _product_separator
from crm_shipping_bypasser import run as _run_shipping_bypasser
from slack_team import run as _run_slack_team

configure_console_utf8()

AUTOMATION_NAME = "crm.copyright_cancel"
SOURCE = "crm_copyright_cancel.py"
MISSING_REASON_ERROR = "Missing Reason. Copyright cancel must have a reason."
ORDER_NUMBER_PLACEHOLDER_LABEL = "[ORDER-NUMBER]"
REASON_PLACEHOLDER_LABEL = "[REASON]"
ORDER_NUMBER_PLACEHOLDER_RE = re.compile(r"\[\s*ORDER[\s_-]*NUMBER\s*\]", re.IGNORECASE)
REASON_PLACEHOLDER_RE = re.compile(r"\[\s*REASON\s*\]", re.IGNORECASE)
LEGACY_PLACEHOLDER_RE = re.compile(r"XXXXXX", re.IGNORECASE)
HELD_DRIVERS = []
PROFILE_DIR_OVERRIDE = ""
COMPLICATED_EMB_ISSUE_TYPE = str(
    getattr(_config, "COMPLICATED_EMB_ISSUE_TYPE", "Complicated EMB to HDD") or "Complicated EMB to HDD"
)
OVERSIZE_EMB_TO_HDD_ISSUE_TYPE = str(
    getattr(_config, "OVERSIZE_EMB_TO_HDD_ISSUE_TYPE", "Oversize EMB to HDD") or "Oversize EMB to HDD"
)
COPYRIGHT_REACHOUT_ISSUE_TYPE = str(
    getattr(_config, "COPYRIGHT_REACHOUT_ISSUE_TYPE", "Copyright - Reachout") or "Copyright - Reachout"
)
COPYRIGHT_REMOVAL_ISSUE_TYPE = str(
    getattr(_config, "COPYRIGHT_REMOVAL_ISSUE_TYPE", "Copyright Removal") or "Copyright Removal"
)
AUTO_SPLITTER_ISSUE_TYPE = str(getattr(_config, "AUTO_SPLITTER_ISSUE_TYPE", "Auto Splitter") or "Auto Splitter")
MANUAL_STOCK_ORDER_ISSUE_TYPE = str(
    getattr(_config, "MANUAL_STOCK_ORDER_ISSUE_TYPE", "Manual Stock Order") or "Manual Stock Order"
)
SALESFORCE_COMPLICATED_EMB_TO_HDD_TEMPLATE = str(
    getattr(_config, "SALESFORCE_COMPLICATED_EMB_TO_HDD_TEMPLATE", "[AUTO] Complicated EMB to HDD")
    or "[AUTO] Complicated EMB to HDD"
)
SALESFORCE_OVERSIZE_EMBROIDERY_TEMPLATE = str(
    getattr(_config, "SALESFORCE_OVERSIZE_EMBROIDERY_TEMPLATE", "[AUTO] Oversize Embroidery")
    or "[AUTO] Oversize Embroidery"
)
SALESFORCE_COPYRIGHT_REACHOUT_TEMPLATE = str(
    getattr(_config, "SALESFORCE_COPYRIGHT_REACHOUT_TEMPLATE", "[AUTO] Copyright Reachout") or "[AUTO] Copyright Reachout"
)
SALESFORCE_COPYRIGHT_REMOVAL_TEMPLATE = str(
    getattr(
        _config,
        "SALESFORCE_COPYRIGHT_REMOVAL_TEMPLATE",
        "[AUTO] Copyright Removal",
    )
    or "[AUTO] Copyright Removal"
)
COPYRIGHT_REACHOUT_CRM_STATUS = str(
    getattr(_config, "COPYRIGHT_REACHOUT_CRM_STATUS", "issue - copyright") or "issue - copyright"
)
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
    reason: str
    process_key: str = "copyright_cancel"
    error: str = ""

    @property
    def order_url(self):
        return PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=self.order_id)

    @property
    def cancel_process(self):
        return _cancel_process_for_key(self.process_key)

    @property
    def process(self):
        return _cancel_process_for_key(self.process_key)


@dataclass(frozen=True)
class CancelProcess:
    key: str
    issue_type: str
    salesforce_template: str
    template_search: str
    sales_note_reason_label: str
    sales_note_email_line: str
    subject_markers: tuple
    body_markers: tuple
    display_name: str
    requires_reason: bool = True
    cancel_and_refund: bool = True
    fixed_sales_note: str = ""
    sales_note_template: str = ""
    template_aliases: tuple = ()
    replace_body_placeholder_with_reason: bool = False
    refund_case_subject: str = "Copyright"


COPYRIGHT_CANCEL_PROCESS = CancelProcess(
    key="copyright_cancel",
    issue_type=COPYRIGHT_CANCEL_ISSUE_TYPE,
    salesforce_template=SALESFORCE_COPYRIGHT_CANCEL_TEMPLATE,
    template_search="copyright",
    sales_note_reason_label="copyright",
    sales_note_email_line="emailed copyright cancellation",
    subject_markers=("refund has been issued",),
    body_markers=("while reviewing your order", "processed a refund back to your account"),
    display_name="Copyright cancel",
    replace_body_placeholder_with_reason=True,
)
CONTENT_VIOLATION_CANCEL_PROCESS = CancelProcess(
    key="content_violation_cancel",
    issue_type=CONTENT_VIOLATION_CANCEL_ISSUE_TYPE,
    salesforce_template=SALESFORCE_CONTENT_VIOLATION_CANCEL_TEMPLATE,
    template_search="content violation",
    sales_note_reason_label="content violation",
    sales_note_email_line="emailed content violation cancellation",
    subject_markers=(),
    body_markers=("content policy", "refund"),
    display_name="Content violation cancel",
    refund_case_subject="Content Violation",
)
EXISTING_DESIGNS_CANCEL_PROCESS = CancelProcess(
    key="existing_designs_cancel",
    issue_type=EXISTING_DESIGNS_CANCEL_ISSUE_TYPE,
    salesforce_template=SALESFORCE_EXISTING_DESIGNS_CANCEL_TEMPLATE,
    template_search="existing t-shirt",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=(),
    body_markers=("photograph or screenshot of artwork printed on another shirt",),
    display_name="Existing designs cancel",
    requires_reason=False,
    fixed_sales_note="Cannot print an screenshot/photograph of a design on a t-shirt\nCancelled",
    refund_case_subject="Existing design",
)
OUTSIDE_LIMIT_CANCEL_PROCESS = CancelProcess(
    key="outside_limit_cancel",
    issue_type=OUTSIDE_LIMIT_CANCEL_ISSUE_TYPE,
    salesforce_template=SALESFORCE_OUTSIDE_LIMIT_CANCEL_TEMPLATE,
    template_search="outside limit",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=(),
    body_markers=("part of the artwork extends outside the designated print area",),
    display_name="Outside limit cancel",
    requires_reason=False,
    fixed_sales_note="Cannot print beyond the designated area limit\nCancelled",
)
COMPLICATED_EMB_TO_HDD_PROCESS = CancelProcess(
    key="complicated_emb_to_hdd",
    issue_type=COMPLICATED_EMB_ISSUE_TYPE,
    salesforce_template=SALESFORCE_COMPLICATED_EMB_TO_HDD_TEMPLATE,
    template_search="complicated",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=(),
    body_markers=("unable to fulfill your request to embroider", "updated to ink printing"),
    display_name="Complicated EMB to HDD",
    requires_reason=False,
    cancel_and_refund=False,
    fixed_sales_note="Complicated embroidery. Switched to HDD to keep the details. Emailed",
)
OVERSIZE_EMB_TO_HDD_PROCESS = CancelProcess(
    key="oversize_emb_to_hdd",
    issue_type=OVERSIZE_EMB_TO_HDD_ISSUE_TYPE,
    salesforce_template=SALESFORCE_OVERSIZE_EMBROIDERY_TEMPLATE,
    template_search="oversize embroidery",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=(),
    body_markers=("embroidery",),
    display_name="Oversize EMB to HDD",
    requires_reason=False,
    cancel_and_refund=False,
    fixed_sales_note="Oversize embroidery. Switch to HDD to keep the design size. Emailed",
)
COPYRIGHT_REACHOUT_PROCESS = CancelProcess(
    key="copyright_reachout",
    issue_type=COPYRIGHT_REACHOUT_ISSUE_TYPE,
    salesforce_template=SALESFORCE_COPYRIGHT_REACHOUT_TEMPLATE,
    template_search="copyright",
    sales_note_reason_label="Copyright",
    sales_note_email_line="Emailed txted",
    subject_markers=(),
    body_markers=("protected by copyright",),
    display_name="Copyright reachout",
    requires_reason=True,
    cancel_and_refund=False,
    replace_body_placeholder_with_reason=True,
)
COPYRIGHT_REMOVAL_PROCESS = CancelProcess(
    key="copyright_removal",
    issue_type=COPYRIGHT_REMOVAL_ISSUE_TYPE,
    salesforce_template=SALESFORCE_COPYRIGHT_REMOVAL_TEMPLATE,
    template_search="copyright",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=("copyrighted element removed",),
    body_markers=("copyright", "removed"),
    display_name="Copyright removal",
    requires_reason=True,
    cancel_and_refund=False,
    sales_note_template="Removed {reason} copyright\nemailed",
    replace_body_placeholder_with_reason=True,
)
AUTO_SPLITTER_PROCESS = CancelProcess(
    key="auto_splitter",
    issue_type=AUTO_SPLITTER_ISSUE_TYPE,
    salesforce_template="",
    template_search="",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=(),
    body_markers=(),
    display_name="Auto Splitter",
    requires_reason=False,
    cancel_and_refund=False,
    fixed_sales_note="Auto Splitter",
)
MANUAL_STOCK_ORDER_PROCESS = CancelProcess(
    key="manual_stock_order",
    issue_type=MANUAL_STOCK_ORDER_ISSUE_TYPE,
    salesforce_template="",
    template_search="",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=(),
    body_markers=(),
    display_name="Manual Stock Order",
    requires_reason=False,
    cancel_and_refund=False,
    fixed_sales_note="Manual Stock Order",
)
CANCEL_PROCESSES = (
    COPYRIGHT_CANCEL_PROCESS,
    CONTENT_VIOLATION_CANCEL_PROCESS,
    EXISTING_DESIGNS_CANCEL_PROCESS,
    OUTSIDE_LIMIT_CANCEL_PROCESS,
    COMPLICATED_EMB_TO_HDD_PROCESS,
    OVERSIZE_EMB_TO_HDD_PROCESS,
    COPYRIGHT_REACHOUT_PROCESS,
    COPYRIGHT_REMOVAL_PROCESS,
    AUTO_SPLITTER_PROCESS,
    MANUAL_STOCK_ORDER_PROCESS,
)
CANCEL_PROCESSES_BY_KEY = {process.key: process for process in CANCEL_PROCESSES}
SALESFORCE_CASE_STATUS_REFUND_PENDING = "Refund Pending"


def _cancel_process_for_key(key):
    process = CANCEL_PROCESSES_BY_KEY.get(str(key or "").strip())
    if process is None:
        raise CopyrightCancelError(f"Unsupported cancellation process: {key}")
    return process


def _cancel_process_for_issue_type(issue_type):
    clean_issue = str(issue_type or "").strip().lower()
    for process in CANCEL_PROCESSES:
        if clean_issue == process.issue_type.lower():
            return process
    return None


def _missing_reason_error(process):
    return f"Missing Reason. {process.display_name} must have a reason."


def _salesforce_refund_case_subject(process):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
    return _clean_text(process.refund_case_subject) or "Copyright"


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


def _publish_status(message, *, stage=None, current=None, total=None, order_id=None):
    """Publish Sheets Scanner progress for the control-panel status poll."""
    try:
        write_status_payload(
            AUTOMATION_NAME,
            message,
            stage=stage,
            current=current,
            total=total,
            order_id=order_id,
        )
    except Exception:
        # Live-status reporting must never interrupt the actual sheet workflow.
        pass


def _normalize_order_id(value):
    text = str(value or "")
    match = re.search(r"(?<!\d)(\d{7})(?!\d)", text)
    if not match:
        raise CopyrightCancelError("Order reference must contain a 7-digit CRM order ID.")
    return match.group(1)


def _resolve_email_template_path():
    path = SALESFORCE_EMAIL_TEMPLATE_FILE
    return resolve_runtime_file(path, STATE_DIR)


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
    gspread = _load_gspread()
    credentials = read_json_credential(GOOGLE_SHEETS_CREDENTIAL_TARGET)
    client = gspread.service_account_from_dict(credentials)
    spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
    worksheet = spreadsheet.worksheet(GOOGLE_SHEET_WORKSHEET)
    return spreadsheet, worksheet


def _header_indexes(headers):
    required = [
        GOOGLE_SHEET_ORDER_REFERENCE_COLUMN,
        GOOGLE_SHEET_ISSUE_TYPE_COLUMN,
        GOOGLE_SHEET_REASON_COLUMN,
        GOOGLE_SHEET_ERROR_COLUMN,
    ]
    missing = [name for name in required if name not in headers]
    if missing:
        raise CopyrightCancelError(f"Missing Google Sheet column(s): {', '.join(missing)}")
    return {
        "order": headers.index(GOOGLE_SHEET_ORDER_REFERENCE_COLUMN),
        "issue": headers.index(GOOGLE_SHEET_ISSUE_TYPE_COLUMN),
        "reason": headers.index(GOOGLE_SHEET_REASON_COLUMN),
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
        reason = row[indexes["reason"]].strip() if indexes["reason"] < len(row) else ""
        error = row[indexes["error"]].strip() if indexes["error"] < len(row) else ""
        if not (order_ref or issue_type or reason or error):
            continue
        record = {
            "row_number": row_number,
            "order_reference": order_ref,
            "issue_type": issue_type,
            "cancellation_reason": reason,
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
        process = _cancel_process_for_issue_type(issue_type)
        if process is None:
            skipped.append({**record, "order_id": order_id, "reason": "Unsupported issue type."})
            continue
        if process.requires_reason and not reason:
            skipped.append({**record, "order_id": order_id, "reason": _missing_reason_error(process)})
            continue
        if error and not include_error_rows:
            skipped.append({**record, "order_id": order_id, "reason": "ERROR column is not blank."})
            continue
        eligible.append(QueueRow(row_number, order_ref, order_id, issue_type, reason, process.key, error))
    return spreadsheet, worksheet, headers, eligible, skipped


def _write_sheet_error(worksheet, headers, row_number, message):
    index = headers.index(GOOGLE_SHEET_ERROR_COLUMN)
    lines = [line.strip() for line in str(message or "").splitlines() if line.strip()]
    concise_message = lines[0] if lines else ""
    worksheet.update_cell(row_number, index + 1, concise_message[:500])


def _write_missing_reason_errors(worksheet, headers, skipped_rows):
    written = 0
    for row in skipped_rows or []:
        if not str(row.get("reason") or "").startswith("Missing Reason."):
            continue
        row_number = row.get("row_number")
        if not row_number:
            continue
        _write_sheet_error(worksheet, headers, row_number, row.get("reason") or MISSING_REASON_ERROR)
        written += 1
    return written


def _clear_sheet_queue_row(worksheet, row_number):
    range_name = f"A{int(row_number)}:D{int(row_number)}"
    if hasattr(worksheet, "batch_clear"):
        worksheet.batch_clear([range_name])
        return
    worksheet.update(range_name, [["", "", "", ""]])


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
    row = row or {}
    return {
        "tab_number": (tab or {}).get("tab_number"),
        "tab_name": _clean_text((tab or {}).get("tab_name")),
        "vendor": _clean_text(row.get("vendor") or row.get("manual_order_vendor")),
        "po": _clean_text(row.get("po") or row.get("manual_order_po")),
        "vendor_order_number": _clean_text(row.get("vendor_order_number")),
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


def _stock_row_is_cancelled_channel_vendor(row):
    vendor = _clean_text((row or {}).get("vendor")).lower()
    vendor = re.sub(r"\bs\s*&\s*s\b", "s and s", vendor)
    vendor = re.sub(r"\s+", " ", vendor)
    return bool("sanmar" in vendor or "s and s activewear" in vendor or "ss activewear" in vendor)


def _summarize_post_cancel_stock_scan(scan):
    scan = scan or {}
    tabs = scan.get("tabs") or []
    stock_rows = []
    local_inventory_rows = []
    outside_stock_rows = []
    cancelled_channel_rows = []
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
                    if _stock_row_is_cancelled_channel_vendor(summary):
                        cancelled_channel_rows.append(summary)
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
        "cancelled_channel_rows": cancelled_channel_rows,
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
    if state.get("unknown_ordered_tabs") and not state.get("cancelled_channel_rows"):
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


def _build_cf_html(fragment):
    html_doc = f"<html><body><!--StartFragment-->{fragment}<!--EndFragment--></body></html>"
    header_template = (
        "Version:0.9\r\n"
        "StartHTML:{start_html:010d}\r\n"
        "EndHTML:{end_html:010d}\r\n"
        "StartFragment:{start_fragment:010d}\r\n"
        "EndFragment:{end_fragment:010d}\r\n"
    )
    empty_header = header_template.format(
        start_html=0,
        end_html=0,
        start_fragment=0,
        end_fragment=0,
    )
    start_html = len(empty_header.encode("utf-8"))
    start_fragment = start_html + len("<html><body><!--StartFragment-->".encode("utf-8"))
    end_fragment = start_fragment + len(str(fragment or "").encode("utf-8"))
    end_html = start_html + len(html_doc.encode("utf-8"))
    header = header_template.format(
        start_html=start_html,
        end_html=end_html,
        start_fragment=start_fragment,
        end_fragment=end_fragment,
    )
    return (header + html_doc).encode("utf-8") + b"\0"


def _set_clipboard_html(html_fragment, plain_text):
    if os.name != "nt":
        raise CopyrightCancelError("Rich clipboard paste for Salesforce is only implemented on Windows.")
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    cf_html = user32.RegisterClipboardFormatW("HTML Format")
    if not cf_html:
        raise CopyrightCancelError("Could not register Windows HTML clipboard format.")
    gmem_moveable = 0x0002
    cf_unicode_text = 13
    handles_to_keep = []

    def _alloc_bytes(data):
        handle = kernel32.GlobalAlloc(gmem_moveable, len(data))
        if not handle:
            raise CopyrightCancelError("Could not allocate Windows clipboard memory.")
        locked = kernel32.GlobalLock(handle)
        if not locked:
            kernel32.GlobalFree(handle)
            raise CopyrightCancelError("Could not lock Windows clipboard memory.")
        try:
            ctypes.memmove(locked, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        return handle

    html_data = _build_cf_html(html_fragment)
    text_data = (str(plain_text or "") + "\0").encode("utf-16le")
    html_handle = _alloc_bytes(html_data)
    text_handle = _alloc_bytes(text_data)
    for _attempt in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.2)
    else:
        kernel32.GlobalFree(html_handle)
        kernel32.GlobalFree(text_handle)
        raise CopyrightCancelError("Could not open Windows clipboard for Salesforce rich body paste.")
    try:
        if not user32.EmptyClipboard():
            raise CopyrightCancelError("Could not clear Windows clipboard.")
        if not user32.SetClipboardData(cf_html, html_handle):
            raise CopyrightCancelError("Could not place Salesforce HTML body on clipboard.")
        handles_to_keep.append(html_handle)
        if not user32.SetClipboardData(cf_unicode_text, text_handle):
            raise CopyrightCancelError("Could not place Salesforce plain body on clipboard.")
        handles_to_keep.append(text_handle)
    finally:
        user32.CloseClipboard()
        if html_handle not in handles_to_keep:
            kernel32.GlobalFree(html_handle)
        if text_handle not in handles_to_keep:
            kernel32.GlobalFree(text_handle)


def _format_copyright_reachout_body_text(body_text):
    text = _clean_text(body_text)
    hello_index = text.find("Hello,")
    if hello_index >= 0:
        text = text[hello_index:]
    end_marker = "We appreciate your business."
    end_index = text.find(end_marker)
    if end_index >= 0:
        text = text[: end_index + len(end_marker)]
    replacements = [
        ("Hello, Thank you", "Hello,\n\nThank you"),
        ("RushOrderTees! While", "RushOrderTees!\n\nWhile"),
        ("this order: 1.", "this order:\n\n1."),
        (" 2. ", "\n2. "),
        (" 3. ", "\n3. "),
        (" 4. ", "\n4. "),
        ("refund. Please", "refund.\n\nPlease"),
    ]
    for before, after in replacements:
        text = text.replace(before, after)
    return text.strip()


def _format_copyright_removal_body_text(body_text):
    text = _clean_text(body_text)
    hello_index = text.find("Hello,")
    if hello_index >= 0:
        text = text[hello_index:]
    end_marker = "We appreciate your business."
    end_index = text.find(end_marker)
    if end_index >= 0:
        text = text[: end_index + len(end_marker)]
    replacements = [
        ("Hello, Thank you", "Hello,\n\nThank you"),
        ("RushOrderTees! We wanted", "RushOrderTees!\n\nWe wanted"),
        ("from the design. Your updated", "from the design.\n\nYour updated"),
        ("happy to assist. We are", "happy to assist.\n\nWe are"),
        ("800-620-1233. Thank you", "800-620-1233.\n\nThank you"),
    ]
    for before, after in replacements:
        text = text.replace(before, after)
    return text.strip()


def _format_placeholder_body_text(body_text, process=COPYRIGHT_REACHOUT_PROCESS):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
    if process.key == COPYRIGHT_REMOVAL_PROCESS.key:
        return _format_copyright_removal_body_text(body_text)
    return _format_copyright_reachout_body_text(body_text)


def _subject_has_order_placeholder(text):
    return bool(LEGACY_PLACEHOLDER_RE.search(str(text or "")) or ORDER_NUMBER_PLACEHOLDER_RE.search(str(text or "")))


def _replace_order_placeholders(text, order_id):
    return re.sub(
        r"XXXXXX|\[\s*ORDER[\s_-]*NUMBER\s*\]",
        str(order_id),
        str(text or ""),
        flags=re.IGNORECASE,
    )


def _body_has_reason_placeholder(text):
    return bool(LEGACY_PLACEHOLDER_RE.search(str(text or "")) or REASON_PLACEHOLDER_RE.search(str(text or "")))


def _replace_reason_placeholders(text, reason):
    return re.sub(
        r"XXXXXX|\[\s*REASON\s*\]",
        str(reason),
        str(text or ""),
        flags=re.IGNORECASE,
    )


def _unresolved_placeholder_labels(text):
    value = str(text or "")
    labels = []
    checks = (
        (ORDER_NUMBER_PLACEHOLDER_RE, ORDER_NUMBER_PLACEHOLDER_LABEL),
        (REASON_PLACEHOLDER_RE, REASON_PLACEHOLDER_LABEL),
        (LEGACY_PLACEHOLDER_RE, "XXXXXX"),
    )
    for pattern, label in checks:
        if pattern.search(value) and label not in labels:
            labels.append(label)
    return labels


def _validate_no_unresolved_email_placeholders(subject_text, body_text, body_placeholder_state=None):
    subject_labels = _unresolved_placeholder_labels(subject_text)
    body_labels = _unresolved_placeholder_labels(body_text)
    hidden_body_count = int((body_placeholder_state or {}).get("count") or 0)
    if hidden_body_count > 0 and REASON_PLACEHOLDER_LABEL not in body_labels:
        body_labels.append(REASON_PLACEHOLDER_LABEL)
    if subject_labels or body_labels:
        pieces = []
        if subject_labels:
            pieces.append(f"subject: {', '.join(subject_labels)}")
        if body_labels:
            pieces.append(f"body: {', '.join(body_labels)}")
        raise CopyrightCancelError(
            "Salesforce email still contains unresolved placeholder(s) before send; refusing to send. "
            + "; ".join(pieces)
        )


def _html_with_bold_placeholder_reason(body_text, reason):
    clean_reason = _clean_text(reason)
    if not clean_reason:
        raise CopyrightCancelError("Copyright reachout body replacement requires a reason.")
    if not _body_has_reason_placeholder(body_text):
        raise CopyrightCancelError(f"Salesforce copyright reachout template body does not contain {REASON_PLACEHOLDER_LABEL}.")
    token = "\0COPYRIGHT_REASON\0"
    escaped = html.escape(_replace_reason_placeholders(body_text, token))
    bold_reason = f"<strong>{html.escape(clean_reason)}</strong>"
    escaped = escaped.replace(token, bold_reason)
    paragraphs = re.split(r"\n{2,}", escaped)
    return "".join(
        f"<p>{paragraph.replace(chr(10), '<br>') or '<br>'}</p>"
        for paragraph in paragraphs
    )


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
            element.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
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

    credential = read_windows_credential(CRM_CREDENTIAL_TARGET)
    username = credential.username.strip()
    password = credential.secret
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
    return "salesforce login" in text or "log in to salesforce" in text or has_login_form or ("hello" in text and "login" in text) or (
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
            && (text.includes('Salesforce Account') || text.includes('Salesforce Contact'))
            && (text.match(emailPattern) || []).length;
        });
        const panel = panels.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0] || document.body;
        const text = panel.innerText || '';
        const emails = Array.from(new Set(text.match(emailPattern) || []));
        const sf = Array.from(panel.querySelectorAll('a,button,span'))
          .find((el) => {
            const label = (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (label === 'salesforce account' || label === 'salesforce contact')
              && rect.width > 0 && rect.height > 0
              && style.display !== 'none' && style.visibility !== 'hidden';
          });
        let customerName = '';
        const lines = text.split(/\\n+/).map((line) => line.trim()).filter(Boolean);
        for (const line of lines) {
          if (/contact info and send options/i.test(line)) continue;
          if (line.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i)) continue;
          if (/salesforce (?:account|contact)|log in|prior|phone|chat|request review/i.test(line)) continue;
          customerName = line;
          break;
        }
        return {
          customer_name: customerName,
          email: emails[0] || '',
          salesforce_visible: !!sf,
          salesforce_label: sf ? (sf.innerText || '').replace(/\\s+/g, ' ').trim() : '',
          panel_text: text.slice(0, 1000)
        };
        """
    )
    if not info.get("email"):
        raise CopyrightCancelError("Could not read customer email from CRM contact panel.")
    if not info.get("salesforce_visible"):
        raise CopyrightCancelError("Could not find Salesforce Account or Salesforce Contact link in CRM contact panel.")
    return info


def _wait_for_crm_contact_info(driver, order_id=None, timeout=90):
    last_error = None
    for attempt in range(2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                _activate_crm_context(driver)
                if order_id:
                    _wait_for_order_scope(driver, order_id=order_id, timeout=3)
                return _get_crm_contact_info(driver)
            except Exception as exc:
                last_error = exc
            time.sleep(1)
        if attempt == 0:
            driver.switch_to.default_content()
            driver.refresh()
            _activate_crm_context(driver)
            if order_id:
                _wait_for_order_scope(driver, order_id=order_id, timeout=30)
    raise CopyrightCancelError(f"CRM contact panel did not finish loading. Last error: {last_error}")


def _click_salesforce_account(driver, order_id=None, timeout=90):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            _activate_crm_context(driver)
            if order_id:
                _wait_for_order_scope(driver, order_id=order_id, timeout=3)
            opened = bool(
                driver.execute_script(
                    """
                    const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
                    const el = nodes.find((node) => {
                      const text = (node.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const rect = node.getBoundingClientRect();
                      return (text === 'salesforce account' || text === 'salesforce contact')
                        && rect.width > 0 && rect.height > 0;
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

            for element in driver.find_elements(
                "xpath",
                "//*[normalize-space(.)='Salesforce Account' or normalize-space(.)='Salesforce Contact']",
            ):
                try:
                    if not element.is_displayed():
                        continue
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
                    element.click()
                    return
                except Exception as exc:
                    last_error = exc

            clicked = bool(
                driver.execute_script(
                    """
                    const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
                    const el = nodes.find((node) => {
                      const text = (node.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                      const rect = node.getBoundingClientRect();
                      return (text === 'salesforce account' || text === 'salesforce contact')
                        && rect.width > 0 && rect.height > 0;
                    });
                    if (!el) return false;
                    el.click();
                    return true;
                    """
                )
            )
            if clicked:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    raise CopyrightCancelError(
        f"Salesforce Account/Contact link did not become clickable before timeout. Last error: {last_error}"
    )


def _salesforce_account_href(driver, order_id=None):
    _activate_crm_context(driver)
    if order_id:
        _wait_for_order_scope(driver, order_id=order_id, timeout=3)
    href = driver.execute_script(
        """
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function rendered(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden';
        }
        const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
        const label = nodes.find((node) => {
          const text = clean(node.innerText || node.value || '');
          return (text === 'salesforce account' || text === 'salesforce contact') && rendered(node);
        });
        if (!label) return '';
        const anchor = label.closest('a[href]');
        if (!anchor) return '';
        const href = anchor.href || '';
        if (!href || href.toLowerCase().startsWith('javascript:') || href === window.location.href) return '';
        return href;
        """
    )
    return str(href or "").strip()


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
    raise CopyrightCancelError("Salesforce tab did not open after clicking Salesforce Account/Contact.")


def _open_url_in_new_tab(driver, url):
    before_handles = list(driver.window_handles)
    if hasattr(driver.switch_to, "new_window"):
        driver.switch_to.new_window("tab")
        driver.get(url)
        return driver.current_window_handle
    driver.execute_script("window.open(arguments[0], '_blank');", url)
    return _switch_to_new_or_changed_tab(driver, before_handles, timeout=10)


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
    if password_input is None and len(inputs) >= 2:
        password_input = inputs[1]
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
        credential = read_windows_credential(SALESFORCE_CREDENTIAL_TARGET, required=False)
        configured_username = credential.username.strip() if credential else ""
        configured_password = credential.secret if credential else ""
        if configured_username:
            username_input.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
            username_input.send_keys(configured_username)
            if configured_password and password_input is not None:
                password_input.click()
                password_input.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
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
        if read_windows_credential(SALESFORCE_CREDENTIAL_TARGET, required=False) is None:
            raise CopyrightCancelError(
                f"Salesforce credential '{SALESFORCE_CREDENTIAL_TARGET}' is missing from Windows "
                "Credential Manager and Chrome autofill did not populate the login form."
            )
        raise CopyrightCancelError("Salesforce login fields could not be filled from Windows Credential Manager.")
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


def _open_salesforce_account(driver, crm_handle, expected_email, login_wait_seconds=0, order_id=None):
    """Open the CRM account with one refresh if its dynamic link stalls."""
    refresh_used = False

    def refresh_crm_order_once():
        nonlocal refresh_used
        if refresh_used:
            return False
        refresh_used = True
        driver.switch_to.window(crm_handle)
        driver.refresh()
        _activate_crm_context(driver)
        if order_id:
            _wait_for_order_scope(driver, order_id=order_id, timeout=30)
        return True

    def open_account_tab():
        while True:
            driver.switch_to.window(crm_handle)
            _activate_crm_context(driver)
            account_href = _salesforce_account_href(driver, order_id=order_id)
            before = list(driver.window_handles)
            try:
                _click_salesforce_account(driver, order_id=order_id)
            except CopyrightCancelError:
                if refresh_crm_order_once():
                    continue
                raise
            try:
                return _switch_to_new_or_changed_tab(driver, before)
            except CopyrightCancelError:
                if account_href:
                    return _open_url_in_new_tab(driver, account_href)
                if refresh_crm_order_once():
                    continue
                raise

    sf_handle = open_account_tab()
    login_happened = _attempt_salesforce_login(driver)
    if login_happened:
        # CRM's first post-login redirect often lands on the Salesforce default page.
        # Return to CRM and click Salesforce Account/Contact again to reach the customer page.
        try:
            if driver.current_window_handle != crm_handle:
                driver.close()
        except Exception:
            pass
        sf_handle = open_account_tab()
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
            driver.switch_to.active_element.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, Keys.HOME)
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
                    || text.includes('--none--')
                    || text === 'none'
                    || text.includes('from')
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
                  && (
                    targetDomain && text.includes('@' + targetDomain)
                    || text.includes('--none--')
                    || text === 'none'
                    || text.includes('from')
                  );
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
                function all(selector, root) {
                  const start = root || document;
                  const found = [];
                  const seen = new Set();
                  function add(node) {
                    if (node && !seen.has(node)) {
                      seen.add(node);
                      found.push(node);
                    }
                  }
                  function walk(node) {
                    if (!node) return;
                    try {
                      if (node.querySelectorAll) {
                        Array.from(node.querySelectorAll(selector)).forEach(add);
                        Array.from(node.querySelectorAll('*')).forEach((child) => {
                          if (child.shadowRoot) walk(child.shadowRoot);
                        });
                      }
                    } catch (err) {}
                    if (node.shadowRoot) walk(node.shadowRoot);
                  }
                  walk(start);
                  return found;
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
                for key in (Keys.ENTER, Keys.ARROW_DOWN):
                    try:
                        driver.switch_to.active_element.send_keys(key)
                        time.sleep(0.5)
                        if _from_dropdown_is_open():
                            return True
                    except Exception:
                        pass
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
                try:
                    driver.switch_to.active_element.send_keys(Keys.ENTER)
                    time.sleep(0.5)
                    if _from_dropdown_is_open():
                        return True
                except Exception:
                    pass
            except Exception:
                pass
            try:
                opened = bool(
                    driver.execute_script(
                        """
                        const control = arguments[0];
                        function rendered(el) {
                          const rect = el.getBoundingClientRect();
                          const style = window.getComputedStyle(el);
                          return rect.width > 0 && rect.height > 0
                            && style.display !== 'none' && style.visibility !== 'hidden';
                        }
                        const root = control.closest('.slds-combobox, .slds-form-element__control, .uiInput, [role=combobox]')
                          || control.parentElement
                          || control;
                        const candidates = Array.from(root.querySelectorAll('button,input,[role=combobox],.slds-combobox__input,.slds-input_faux,a,span,div'))
                          .filter(rendered)
                          .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            const aRight = ar.right;
                            const bRight = br.right;
                            if (Math.abs(aRight - bRight) > 4) return bRight - aRight;
                            return (br.width * br.height) - (ar.width * ar.height);
                          });
                        for (const el of candidates.slice(0, 4)) {
                          const rect = el.getBoundingClientRect();
                          const points = [
                            [rect.right - 10, rect.top + rect.height / 2],
                            [rect.left + rect.width / 2, rect.top + rect.height / 2],
                          ];
                          for (const point of points) {
                            const x = Math.max(rect.left + 2, Math.min(rect.right - 2, point[0]));
                            const y = Math.max(rect.top + 2, Math.min(rect.bottom - 2, point[1]));
                            const target = document.elementFromPoint(x, y) || el;
                            try { el.focus(); } catch (err) {}
                            for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
                              target.dispatchEvent(new MouseEvent(type, {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                                clientX: x,
                                clientY: y
                              }));
                            }
                          }
                        }
                        return candidates.length > 0;
                        """,
                        control,
                    )
                )
                if opened:
                    time.sleep(0.5)
                    if _from_dropdown_is_open():
                        return True
                    try:
                        driver.switch_to.active_element.send_keys(Keys.ENTER)
                        time.sleep(0.5)
                        if _from_dropdown_is_open():
                            return True
                    except Exception:
                        pass
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
            function all(selector, root) {
              const start = root || document;
              const found = [];
              const seen = new Set();
              function add(node) {
                if (node && !seen.has(node)) {
                  seen.add(node);
                  found.push(node);
                }
              }
              function walk(node) {
                if (!node) return;
                try {
                  if (node.querySelectorAll) {
                    Array.from(node.querySelectorAll(selector)).forEach(add);
                    Array.from(node.querySelectorAll('*')).forEach((child) => {
                      if (child.shadowRoot) walk(child.shadowRoot);
                    });
                  }
                } catch (err) {}
                if (node.shadowRoot) walk(node.shadowRoot);
              }
              walk(start);
              return found;
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


def _click_viewport_point(driver, x, y):
    try:
        x = float(x)
        y = float(y)
    except Exception:
        return False
    clicked = False
    try:
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        clicked = True
    except Exception:
        pass
    if os.name == "nt":
        try:
            metrics = driver.execute_script(
                """
                return {
                  screenX: window.screenX,
                  screenY: window.screenY,
                  outerWidth: window.outerWidth,
                  outerHeight: window.outerHeight,
                  innerWidth: window.innerWidth,
                  innerHeight: window.innerHeight
                };
                """
            )
            border_x = max(0, (float(metrics.get("outerWidth") or 0) - float(metrics.get("innerWidth") or 0)) / 2)
            chrome_y = max(0, float(metrics.get("outerHeight") or 0) - float(metrics.get("innerHeight") or 0) - border_x)
            screen_x = int(round(float(metrics.get("screenX") or 0) + border_x + x))
            screen_y = int(round(float(metrics.get("screenY") or 0) + chrome_y + y))
            user32 = ctypes.windll.user32
            user32.SetCursorPos(screen_x, screen_y)
            time.sleep(0.05)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            clicked = True
        except Exception:
            pass
    return clicked


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
        function frameScore(frame) {
          const rect = frame.getBoundingClientRect();
          const attrs = clean(`${frame.className || ''} ${frame.title || ''} ${frame.getAttribute('aria-label') || ''}`);
          let text = '';
          let html = '';
          try {
            const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
            if (doc && doc.body) {
              text = clean(doc.body.innerText || doc.body.textContent || '');
              html = clean(doc.body.innerHTML || '');
            }
          } catch (err) {}
          let score = Math.min(4000, rect.width * rect.height / 100);
          if (attrs.includes('email body')) score += 50000;
          if (attrs.includes('cke_wysiwyg_frame')) score += 50000;
          if (text.includes('xxxxxx') || html.includes('xxxxxx')) score += 60000;
          if (text.includes('while reviewing your order') || html.includes('while reviewing your order')) score += 60000;
          if (text === 'font size' || (text.includes('font size') && !text.includes('while reviewing your order'))) score -= 100000;
          if (rect.height < 100) score -= 10000;
          return score;
        }
        const frames = Array.from(composer.querySelectorAll('iframe'))
          .filter(visible)
          .sort((a, b) => frameScore(b) - frameScore(a));
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


def _template_match_labels(process=COPYRIGHT_CANCEL_PROCESS):
    labels = []
    for label in (process.salesforce_template, *(process.template_aliases or ())):
        label = _clean_text(label)
        if label and label.lower() not in [item.lower() for item in labels]:
            labels.append(label)
    return labels


def _click_template_by_name(driver, process=COPYRIGHT_CANCEL_PROCESS):
    targets = [label.lower() for label in _template_match_labels(process)]
    option = driver.execute_script(
        """
        const targets = arguments[0].map((value) => String(value || '').toLowerCase()).filter(Boolean);
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
              && (text.includes('recently used templates') || targets.some((target) => text.includes(target)));
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (ar.width * ar.height) - (br.width * br.height);
          });
        const menu = menus[0];
        if (!menu) return null;
        const matches = Array.from(menu.querySelectorAll('a,button,[role=option],[role=menuitem],li,span,div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || el.textContent || el.value || '');
            if (!targets.includes(text)) return false;
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
        const clickable = matches[0].closest('a,button,[role=option],[role=menuitem],li,div,span') || matches[0];
        try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
        return clickable;
        """,
        targets,
    )
    if option is None:
        return False
    return _click_element_center(driver, option)


def _salesforce_template_appears_inserted(driver, process=COPYRIGHT_CANCEL_PROCESS):
    try:
        state = _read_salesforce_email_state(driver) or {}
    except Exception:
        state = {}
    subject = _clean_text(state.get("subject", ""))
    body = _clean_text(state.get("body", ""))
    if process.body_markers:
        return bool(subject and body and not _missing_body_markers(body.lower(), process))
    return bool(subject and "enter subject" not in subject.lower())


def _wait_for_salesforce_template_markers(driver, process=COPYRIGHT_CANCEL_PROCESS, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _salesforce_template_appears_inserted(driver, process):
            return True
        time.sleep(0.4)
    return False


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
            function all(selector, root) {
              const start = root || document;
              const found = [];
              const seen = new Set();
              function add(node) {
                if (node && !seen.has(node)) {
                  seen.add(node);
                  found.push(node);
                }
              }
              function walk(node) {
                if (!node) return;
                try {
                  if (node.querySelectorAll) {
                    Array.from(node.querySelectorAll(selector)).forEach(add);
                    Array.from(node.querySelectorAll('*')).forEach((child) => {
                      if (child.shadowRoot) walk(child.shadowRoot);
                    });
                  }
                } catch (err) {}
                if (node.shadowRoot) walk(node.shadowRoot);
              }
              walk(start);
              return found;
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


def _template_search_queries(process=COPYRIGHT_CANCEL_PROCESS):
    queries = []
    candidates = []
    if "[AUTO]" in str(process.salesforce_template).upper():
        candidates.append("[AUTO]")
    candidates.extend(
        (
            process.salesforce_template,
            *(process.template_aliases or ()),
            process.salesforce_template.replace("NO REPLY -", ""),
            process.template_search,
        )
    )
    for query in candidates:
        query = _clean_text(query)
        if query and query.lower() not in [item.lower() for item in queries]:
            queries.append(query)
    return queries


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


def _click_full_template_modal_match(driver, process=COPYRIGHT_CANCEL_PROCESS):
    targets = [label.lower() for label in _template_match_labels(process)]
    option = driver.execute_script(
        """
        const targets = arguments[0].map((value) => String(value || '').toLowerCase()).filter(Boolean);
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
          return targets.some((target) => text === target || text.endsWith(target) || text.includes(target));
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
          const aExact = targets.includes(at) ? 0 : (targets.some((target) => at.endsWith(target)) ? 1 : 2);
          const bExact = targets.includes(bt) ? 0 : (targets.some((target) => bt.endsWith(target)) ? 1 : 2);
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
        targets,
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


def _insert_cancel_template(driver, process=COPYRIGHT_CANCEL_PROCESS):
    _focus_salesforce_body_editor(driver)
    _click_template_button(driver)
    time.sleep(0.5)
    if _click_template_by_name(driver, process):
        _confirm_salesforce_template_insert(driver)
        if _wait_for_salesforce_template_markers(driver, process):
            return True
        _click_template_button(driver)
        time.sleep(0.5)
    if not _open_full_template_picker_from_menu(driver):
        raise CopyrightCancelError("Insert a template was not found in Salesforce template menu.")
    time.sleep(1)
    _ensure_private_email_templates_folder(driver)
    deadline = time.monotonic() + 35
    search_queries = _template_search_queries(process)
    while time.monotonic() < deadline:
        for query in search_queries:
            _search_full_template_modal(driver, query)
            time.sleep(1)
            if _click_full_template_modal_match(driver, process):
                _confirm_salesforce_template_insert(driver)
                if _wait_for_salesforce_template_markers(driver, process):
                    return True
                _click_template_button(driver)
                time.sleep(0.5)
                if not _open_full_template_picker_from_menu(driver):
                    raise CopyrightCancelError("Insert a template was not found in Salesforce template menu.")
                time.sleep(1)
                _ensure_private_email_templates_folder(driver)
            if not _scroll_full_template_modal(driver):
                time.sleep(0.5)
            time.sleep(0.4)
    raise CopyrightCancelError(
        f"{process.display_name} template was not selectable in Salesforce. Tried: {', '.join(_template_match_labels(process))}"
    )


def _insert_copyright_template(driver):
    return _insert_cancel_template(driver, COPYRIGHT_CANCEL_PROCESS)


def _replace_subject_order_number(driver, order_id):
    result = driver.execute_script(
        """
        const orderId = arguments[0];
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
          if (!root) return out;
          const nodes = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
          for (const el of nodes) {
            out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot, out);
          }
          return out;
        }
        function fieldText(el) {
          return clean(el.value !== undefined ? el.value : el.innerText || el.textContent || '');
        }
        function hasOrderPlaceholder(value) {
          return /XXXXXX|\\[\\s*ORDER[\\s_-]*NUMBER\\s*\\]/i.test(String(value || ''));
        }
        function replaceOrderPlaceholder(value) {
          return String(value || '').replace(/XXXXXX|\\[\\s*ORDER[\\s_-]*NUMBER\\s*\\]/gi, orderId);
        }
        function hintText(el) {
          return clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
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
        function emit(el, value) {
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          try {
            el.dispatchEvent(new view.InputEvent('input', {bubbles: true, inputType: 'insertReplacementText', data: value}));
          } catch (err) {
            el.dispatchEvent(new view.Event('input', {bubbles: true}));
          }
          el.dispatchEvent(new view.Event('change', {bubbles: true}));
          try { el.dispatchEvent(new view.KeyboardEvent('keyup', {bubbles: true})); } catch (err) {}
          try { el.blur(); } catch (err) {}
        }
        const fields = walk(document)
          .filter((el) => /^(input|textarea)$/i.test(el.tagName || '') || el.isContentEditable)
          .filter((el) => visible(el));
        const candidates = fields
          .filter((el) => hasOrderPlaceholder(fieldText(el)))
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        const subjectFields = fields
          .filter((el) => hintText(el).includes('subject'))
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        const field = candidates[0] || subjectFields.find((el) => hasOrderPlaceholder(fieldText(el)));
        if (!field) {
          const subjectText = subjectFields.length ? fieldText(subjectFields[0]) : '';
          return {replaced: false, subjectText, reason: 'subject placeholder not found'};
        }
        const before = fieldText(field);
        const next = replaceOrderPlaceholder(before);
        if (!next.includes(orderId)) {
          return {replaced: false, subjectText: before, reason: 'subject did not contain placeholder'};
        }
        field.focus();
        if (field.value !== undefined) {
          setNativeValue(field, next);
        } else {
          field.innerText = next;
        }
        emit(field, next);
        return {replaced: true, subjectText: fieldText(field)};
        """,
        str(order_id),
    )
    if not result or not result.get("replaced"):
        subject_text = _clean_text((result or {}).get("subjectText", ""))
        reason = _clean_text((result or {}).get("reason", ""))
        detail = f" Current subject: {subject_text or 'blank'}." if subject_text or reason else ""
        raise CopyrightCancelError(f"Could not replace {ORDER_NUMBER_PLACEHOLDER_LABEL} in Salesforce email subject.{detail}")


def _missing_body_markers(text, process):
    lower_text = _clean_text(text).lower()
    return [marker for marker in process.body_markers if marker not in lower_text]


def _verify_template_loaded(driver, order_id, process=COPYRIGHT_CANCEL_PROCESS):
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
    if _missing_body_markers(lower_text, process):
        raise CopyrightCancelError(f"Salesforce email body does not look like the {process.display_name.lower()} template.")


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
        function frameScore(frame) {
          const rect = frame.getBoundingClientRect();
          const attrs = clean(`${frame.className || ''} ${frame.title || ''} ${frame.getAttribute('aria-label') || ''}`);
          let text = '';
          let html = '';
          try {
            const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
            if (doc && doc.body) {
              text = clean(doc.body.innerText || doc.body.textContent || '');
              html = clean(doc.body.innerHTML || '');
            }
          } catch (err) {}
          let score = Math.min(4000, rect.width * rect.height / 100);
          if (attrs.includes('email body')) score += 50000;
          if (attrs.includes('cke_wysiwyg_frame')) score += 50000;
          if (text.includes('xxxxxx') || html.includes('xxxxxx')) score += 60000;
          if (text.includes('while reviewing your order') || html.includes('while reviewing your order')) score += 60000;
          if (text === 'font size' || (text.includes('font size') && !text.includes('while reviewing your order'))) score -= 100000;
          if (rect.height < 100) score -= 10000;
          return score;
        }
        const frames = Array.from(composer.querySelectorAll('iframe'))
          .filter((frame) => visible(frame))
          .sort((a, b) => frameScore(b) - frameScore(a));
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


def _type_salesforce_body_with_keyboard(driver, body, html_body=""):
    if html_body:
        _set_clipboard_html(html_body, body)
    else:
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
        function frameScore(frame) {
          const rect = frame.getBoundingClientRect();
          const attrs = clean(`${frame.className || ''} ${frame.title || ''} ${frame.getAttribute('aria-label') || ''}`);
          let text = '';
          let html = '';
          try {
            const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
            if (doc && doc.body) {
              text = clean(doc.body.innerText || doc.body.textContent || '');
              html = clean(doc.body.innerHTML || '');
            }
          } catch (err) {}
          let score = Math.min(4000, rect.width * rect.height / 100);
          if (attrs.includes('email body')) score += 50000;
          if (attrs.includes('cke_wysiwyg_frame')) score += 50000;
          if (text.includes('xxxxxx') || html.includes('xxxxxx')) score += 60000;
          if (text.includes('while reviewing your order') || html.includes('while reviewing your order')) score += 60000;
          if (text === 'font size' || (text.includes('font size') && !text.includes('while reviewing your order'))) score -= 100000;
          if (rect.height < 100) score -= 10000;
          return score;
        }
        const frames = Array.from(composer.querySelectorAll('iframe'))
          .filter(visible)
          .sort((a, b) => frameScore(b) - frameScore(a));
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

    def _direct_fill_body(element):
        return driver.execute_script(
            """
            const el = arguments[0];
            const text = arguments[1];
            const html = arguments[2];
            const doc = el.ownerDocument || document;
            const win = doc.defaultView || window;
            function emit(target) {
              for (const type of ['beforeinput', 'input', 'change', 'keyup', 'blur']) {
                try { target.dispatchEvent(new win.Event(type, {bubbles: true})); } catch (err) {}
              }
            }
            try { el.focus(); } catch (err) {}
            if (el.value !== undefined) {
              el.value = text;
              emit(el);
              return el.value || '';
            }
            let inserted = false;
            try {
              const range = doc.createRange();
              range.selectNodeContents(el);
              const selection = win.getSelection && win.getSelection();
              if (selection) {
                selection.removeAllRanges();
                selection.addRange(range);
              }
              doc.execCommand('delete', false, null);
              if (html) inserted = doc.execCommand('insertHTML', false, html);
              else inserted = doc.execCommand('insertText', false, text);
            } catch (err) {}
            if (!inserted) {
              if (html) el.innerHTML = html;
              else el.textContent = text;
            }
            emit(el);
            try { el.blur(); } catch (err) {}
            return el.innerText || el.textContent || '';
            """,
            element,
            body,
            html_body,
        )

    def _clear_and_paste(element):
        filled = _direct_fill_body(element)
        time.sleep(0.8)
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
        return filled

    if target.get("kind") == "iframe":
        frame = target.get("frame")
        if frame is None:
            raise CopyrightCancelError("Salesforce body iframe was not returned.")
        driver.switch_to.frame(frame)
        try:
            editor_result = driver.execute_script(
                """
                const text = arguments[0] || '';
                const suppliedHtml = arguments[1] || '';
                function escapeHtml(value) {
                  return String(value || '').replace(/[&<>"']/g, (ch) => ({
                    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
                  }[ch]));
                }
                const html = suppliedHtml || escapeHtml(text).replace(/\\n/g, '<br>');
                function emit(target, win) {
                  if (!target) return;
                  const view = win || (target.ownerDocument && target.ownerDocument.defaultView) || window;
                  for (const type of ['beforeinput', 'input', 'change', 'keyup', 'blur']) {
                    try { target.dispatchEvent(new view.Event(type, {bubbles: true})); } catch (err) {}
                  }
                }
                function setValue(el, value, win) {
                  if (!el) return false;
                  try {
                    const proto = el.tagName && el.tagName.toLowerCase() === 'textarea'
                      ? (win || window).HTMLTextAreaElement.prototype
                      : (win || window).HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(el, value);
                    else el.value = value;
                  } catch (err) {
                    try { el.value = value; } catch (innerErr) {}
                  }
                  emit(el, win);
                  return true;
                }
                function setEditableBody(doc, win) {
                  if (!doc || !doc.body) return false;
                  const attrs = `${doc.body.className || ''} ${doc.body.getAttribute('aria-label') || ''} ${doc.body.getAttribute('role') || ''}`.toLowerCase();
                  if (!doc.body.isContentEditable && !attrs.includes('email body') && !attrs.includes('cke_editable')) return false;
                  try { doc.body.focus(); } catch (err) {}
                  doc.body.innerHTML = html;
                  emit(doc.body, win);
                  try { doc.body.blur(); } catch (err) {}
                  return true;
                }
                function updateCkEditor(win) {
                  let count = 0;
                  try {
                    const instances = Object.values((win.CKEDITOR && win.CKEDITOR.instances) || {});
                    for (const editor of instances) {
                      try {
                        editor.setData(html);
                        if (editor.updateElement) editor.updateElement();
                        if (editor.fire) editor.fire('change');
                        count += 1;
                      } catch (err) {}
                    }
                  } catch (err) {}
                  return count;
                }
                function syncDoc(doc, win, seen) {
                  if (!doc || seen.has(doc)) return {editables: 0, textareas: 0, ckeditors: 0};
                  seen.add(doc);
                  const result = {editables: 0, textareas: 0, ckeditors: updateCkEditor(win || doc.defaultView || window)};
                  if (setEditableBody(doc, win || doc.defaultView || window)) result.editables += 1;
                  for (const textarea of Array.from(doc.querySelectorAll('textarea#editor, textarea[name="editor"], textarea.cke_source'))) {
                    if (setValue(textarea, html, win || doc.defaultView || window)) result.textareas += 1;
                  }
                  for (const frame of Array.from(doc.querySelectorAll('iframe'))) {
                    try {
                      const childWin = frame.contentWindow;
                      const childDoc = frame.contentDocument || (childWin && childWin.document);
                      const child = syncDoc(childDoc, childWin, seen);
                      result.editables += child.editables;
                      result.textareas += child.textareas;
                      result.ckeditors += child.ckeditors;
                    } catch (err) {}
                  }
                  return result;
                }
                const result = syncDoc(document, window, new Set());
                return {
                  ...result,
                  text: document.body ? (document.body.innerText || document.body.textContent || '') : '',
                  html: document.body ? (document.body.innerHTML || '') : ''
                };
                """,
                body,
                html_body,
            )
            if (
                editor_result
                and (
                    int(editor_result.get("editables") or 0) > 0
                    or int(editor_result.get("textareas") or 0) > 0
                    or int(editor_result.get("ckeditors") or 0) > 0
                )
            ):
                time.sleep(0.8)
                body_text = driver.execute_script(
                    """
                    const pieces = [];
                    function readDoc(doc, seen = new Set()) {
                      if (!doc || seen.has(doc)) return;
                      seen.add(doc);
                      if (doc.body) pieces.push(doc.body.innerText || doc.body.textContent || '');
                      for (const frame of Array.from(doc.querySelectorAll('iframe'))) {
                        try { readDoc(frame.contentDocument || (frame.contentWindow && frame.contentWindow.document), seen); } catch (err) {}
                      }
                    }
                    readDoc(document);
                    return pieces.join('\\n');
                    """
                )
                return body_text or str(editor_result.get("text") or "")
            body_element = driver.execute_script(
                """
                return document.querySelector('[contenteditable=true], textarea')
                  || (document.body && document.body.isContentEditable ? document.body : null)
                  || document.body;
                """
            )
            if body_element is None:
                raise CopyrightCancelError("Salesforce body iframe did not contain an editable body.")
            filled = _clear_and_paste(body_element)
            body_text = driver.execute_script("return document.body ? (document.body.innerText || document.body.textContent || '') : '';")
            return body_text or filled
        finally:
            driver.switch_to.default_content()

    element = target.get("element")
    if element is None:
        raise CopyrightCancelError("Salesforce body editor element was not returned.")
    filled = _clear_and_paste(element)
    body_text = driver.execute_script(
        "return arguments[0].value || arguments[0].innerText || arguments[0].textContent || '';",
        element,
    )
    return body_text or filled


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


def _replace_salesforce_body_placeholder_with_reason(driver, reason, process=COPYRIGHT_REACHOUT_PROCESS):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
    clean_reason = _clean_text(reason)
    if not clean_reason:
        raise CopyrightCancelError(f"{process.display_name} body replacement requires a reason.")
    state = _read_salesforce_email_state(driver)
    current_body = _clean_text((state or {}).get("body", ""))
    if not _body_has_reason_placeholder(current_body):
        raise CopyrightCancelError(
            f"Salesforce {process.display_name.lower()} body does not contain {REASON_PLACEHOLDER_LABEL} before replacement."
        )
    broad_result = driver.execute_script(
        """
        const reason = arguments[0];
        return {count: 0, text: ''};
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
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
        function walkElements(root, out = []) {
          if (!root || !root.querySelectorAll) return out;
          for (const el of Array.from(root.querySelectorAll('*'))) {
            out.push(el);
            if (el.shadowRoot) walkElements(el.shadowRoot, out);
          }
          return out;
        }
        function hasReasonPlaceholder(value) {
          return /XXXXXX|\\[\\s*REASON\\s*\\]/i.test(String(value || ''));
        }
        function replaceTextNode(node, doc) {
          const value = node.nodeValue || '';
          if (!hasReasonPlaceholder(value)) return 0;
          const parent = node.parentNode;
          if (!parent || /^(script|style)$/i.test(parent.nodeName || '')) return 0;
          const parts = value.split(/XXXXXX|\\[\\s*REASON\\s*\\]/gi);
          const placeholders = value.match(/XXXXXX|\\[\\s*REASON\\s*\\]/gi) || [];
          const fragment = doc.createDocumentFragment();
          parts.forEach((part, index) => {
            if (part) fragment.appendChild(doc.createTextNode(part));
            if (index < placeholders.length) {
              const strong = doc.createElement('strong');
              strong.textContent = reason;
              fragment.appendChild(strong);
            }
          });
          parent.replaceChild(fragment, node);
          return placeholders.length;
        }
        function walkNode(node, doc, seen) {
          if (!node || seen.has(node)) return 0;
          seen.add(node);
          if (node.nodeType === 3) return replaceTextNode(node, doc);
          if (node.nodeType !== 1 && node.nodeType !== 9 && node.nodeType !== 11) return 0;
          let count = 0;
          const childDoc = node.ownerDocument || doc;
          for (const child of Array.from(node.childNodes || [])) {
            count += walkNode(child, childDoc, seen);
          }
          if (node.shadowRoot) {
            count += walkNode(node.shadowRoot, childDoc, seen);
          }
          if ((node.tagName || '').toLowerCase() === 'iframe') {
            try {
              const frameDoc = node.contentDocument || (node.contentWindow && node.contentWindow.document);
              if (frameDoc && frameDoc.body) count += walkNode(frameDoc.body, frameDoc, seen);
            } catch (err) {}
          }
          return count;
        }
        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || el.textContent || '');
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
        const roots = composers.length ? [composers[0]] : [document.body || document];
        let count = 0;
        const seen = new Set();
        for (const root of roots) {
          count += walkNode(root, root.ownerDocument || document, seen);
        }
        for (const type of ['input', 'change', 'keyup', 'blur']) {
          try { roots[0].dispatchEvent(new Event(type, {bubbles: true})); } catch (err) {}
        }
        return {
          count,
          text: roots[0] ? (roots[0].innerText || roots[0].textContent || '') : ''
        };
        """,
        clean_reason,
    )
    if int((broad_result or {}).get("count") or 0) > 0:
        time.sleep(0.8)
        updated_state = _read_salesforce_email_state(driver)
        updated_body = _clean_text((updated_state or {}).get("body", ""))
        placeholder_state = _salesforce_email_body_placeholder_state(driver, REASON_PLACEHOLDER_LABEL)
        broad_text = _clean_text((broad_result or {}).get("text", ""))
        if (
            clean_reason.lower() in f"{updated_body} {broad_text}".lower()
            and not _body_has_reason_placeholder(updated_body)
            and not _body_has_reason_placeholder(broad_text)
        ):
            return {
                "placeholder": REASON_PLACEHOLDER_LABEL,
                "replacement": clean_reason,
                "bold_html_applied": True,
                "method": "broad_composer_dom",
                "state": updated_state,
                "placeholder_state": placeholder_state,
                "broad_result": {"count": int((broad_result or {}).get("count") or 0)},
            }
    targeted_result = driver.execute_script(
        """
        const reason = arguments[0];
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
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
        function walkElements(root, out = []) {
          if (!root || !root.querySelectorAll) return out;
          for (const el of Array.from(root.querySelectorAll('*'))) {
            out.push(el);
            if (el.shadowRoot) walkElements(el.shadowRoot, out);
          }
          return out;
        }
        function hasReasonPlaceholder(value) {
          return /XXXXXX|\\[\\s*REASON\\s*\\]/i.test(String(value || ''));
        }
        function frameState(frame) {
          const rect = frame.getBoundingClientRect();
          const attrs = clean(`${frame.className || ''} ${frame.title || ''} ${frame.getAttribute('aria-label') || ''}`);
          let doc = null;
          let text = '';
          let html = '';
          try {
            doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
            if (doc && doc.body) {
              text = clean(doc.body.innerText || doc.body.textContent || '');
              html = clean(doc.body.innerHTML || '');
            }
          } catch (err) {}
          let score = Math.min(4000, rect.width * rect.height / 100);
          if (attrs.includes('email body')) score += 50000;
          if (attrs.includes('cke_wysiwyg_frame')) score += 50000;
          if (hasReasonPlaceholder(text) || hasReasonPlaceholder(html)) score += 60000;
          if (text.includes('while reviewing your order') || html.includes('while reviewing your order')) score += 60000;
          if (text === 'font size' || (text.includes('font size') && !text.includes('while reviewing your order'))) score -= 100000;
          if (rect.height < 100) score -= 10000;
          return {frame, doc, root: doc && doc.body, text, html, score};
        }
        function fieldState(field) {
          const rect = field.getBoundingClientRect();
          const doc = field.ownerDocument || document;
          const hint = clean(`${field.placeholder || ''} ${field.getAttribute('aria-label') || ''} ${field.getAttribute('name') || ''} ${field.getAttribute('title') || ''} ${field.className || ''}`);
          const text = clean(field.value || field.innerText || field.textContent || '');
          const html = clean(field.innerHTML || '');
          let score = Math.min(4000, rect.width * rect.height / 100);
          if (hint.includes('subject') || rect.height < 70) score -= 100000;
          if (hint.includes('email body') || hint.includes('cke_editable')) score += 50000;
          if (hasReasonPlaceholder(text) || hasReasonPlaceholder(html)) score += 60000;
          if (text.includes('while reviewing your order') || html.includes('while reviewing your order')) score += 60000;
          if (text === 'font size' || (text.includes('font size') && !text.includes('while reviewing your order'))) score -= 100000;
          return {field, doc, root: field, text, html, score};
        }
        function replaceTextNode(node, doc) {
          const value = node.nodeValue || '';
          if (!hasReasonPlaceholder(value)) return 0;
          const parent = node.parentNode;
          if (!parent || /^(script|style)$/i.test(parent.nodeName || '')) return 0;
          const parts = value.split(/XXXXXX|\\[\\s*REASON\\s*\\]/gi);
          const placeholders = value.match(/XXXXXX|\\[\\s*REASON\\s*\\]/gi) || [];
          const fragment = doc.createDocumentFragment();
          parts.forEach((part, index) => {
            if (part) fragment.appendChild(doc.createTextNode(part));
            if (index < placeholders.length) {
              const strong = doc.createElement('strong');
              strong.textContent = reason;
              fragment.appendChild(strong);
            }
          });
          parent.replaceChild(fragment, node);
          return placeholders.length;
        }
        function walkReplace(root, doc) {
          if (!root) return 0;
          if (root.nodeType === 3) return replaceTextNode(root, doc);
          if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return 0;
          let count = 0;
          for (const child of Array.from(root.childNodes || [])) {
            count += walkReplace(child, doc);
          }
          return count;
        }
        function collectFrames(doc, out = [], seen = new Set()) {
          if (!doc || seen.has(doc)) return out;
          seen.add(doc);
          for (const frame of walkElements(doc).filter((node) => (node.tagName || '').toLowerCase() === 'iframe')) {
            out.push(frame);
            try {
              const childDoc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
              collectFrames(childDoc, out, seen);
            } catch (err) {}
          }
          return out;
        }
        function emit(target, win) {
          if (!target) return;
          const view = win || (target.ownerDocument && target.ownerDocument.defaultView) || window;
          for (const type of ['beforeinput', 'input', 'change', 'keyup', 'blur']) {
            try { target.dispatchEvent(new view.Event(type, {bubbles: true})); } catch (err) {}
          }
        }
        function setValue(el, value, win) {
          if (!el) return false;
          try {
            const proto = el.tagName && el.tagName.toLowerCase() === 'textarea'
              ? (win || window).HTMLTextAreaElement.prototype
              : (win || window).HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, value);
            else el.value = value;
          } catch (err) {
            try { el.value = value; } catch (innerErr) {}
          }
          emit(el, win);
          return true;
        }
        function replaceHtml(value) {
          return String(value || '').replace(/XXXXXX|\\[\\s*REASON\\s*\\]/gi, `<strong>${reason.replace(/[&<>"']/g, (ch) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
          }[ch]))}</strong>`);
        }
        function emitEditorInput(target, win) {
          const view = win || (target && target.ownerDocument && target.ownerDocument.defaultView) || window;
          if (!target) return;
          for (const type of ['beforeinput', 'input', 'change', 'keyup']) {
            try { target.dispatchEvent(new view.Event(type, {bubbles: true})); } catch (err) {}
          }
        }
        function replaceWithEditorSelection(root, doc) {
          if (!root || !doc) return 0;
          const win = doc.defaultView || window;
          let count = 0;
          try {
            root.scrollIntoView({block: 'center', inline: 'nearest'});
            root.focus();
          } catch (err) {}
          for (let guard = 0; guard < 20; guard += 1) {
            const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
              acceptNode(node) {
                if (!hasReasonPlaceholder(node.nodeValue || '')) return NodeFilter.FILTER_REJECT;
                const parent = node.parentNode;
                if (!parent || /^(script|style)$/i.test(parent.nodeName || '')) return NodeFilter.FILTER_REJECT;
                return NodeFilter.FILTER_ACCEPT;
              }
            });
            const node = walker.nextNode();
            if (!node) break;
            const value = node.nodeValue || '';
            const match = /XXXXXX|\\[\\s*REASON\\s*\\]/i.exec(value);
            if (!match) break;
            const range = doc.createRange();
            range.setStart(node, match.index);
            range.setEnd(node, match.index + match[0].length);
            const selection = win.getSelection && win.getSelection();
            if (selection) {
              selection.removeAllRanges();
              selection.addRange(range);
            }
            let inserted = false;
            try {
              inserted = doc.execCommand('insertHTML', false, replaceHtml(match[0]));
            } catch (err) {
              inserted = false;
            }
            if (!inserted) {
              const strong = doc.createElement('strong');
              strong.textContent = reason;
              range.deleteContents();
              range.insertNode(strong);
            }
            count += 1;
            emitEditorInput(root, win);
          }
          try { root.blur(); } catch (err) {}
          return count;
        }
        function syncEditorSurfaces(startWin, bodyHtml) {
          const result = {ckeditors: 0, textareas: 0, editables: 0};
          let win = startWin || window;
          const seenWins = new Set();
          for (let depth = 0; win && !seenWins.has(win) && depth < 8; depth += 1) {
            seenWins.add(win);
            let doc = null;
            try { doc = win.document; } catch (err) { doc = null; }
            try {
              const instances = Object.values((win.CKEDITOR && win.CKEDITOR.instances) || {});
              for (const editor of instances) {
                try {
                  const data = editor.getData ? String(editor.getData() || '') : '';
                  const nextData = hasReasonPlaceholder(data) ? replaceHtml(data) : bodyHtml;
                  editor.setData(nextData);
                  if (editor.updateElement) editor.updateElement();
                  if (editor.fire) editor.fire('change');
                  result.ckeditors += 1;
                } catch (err) {}
              }
            } catch (err) {}
            if (doc) {
              for (const field of Array.from(doc.querySelectorAll('textarea#editor, textarea[name="editor"], textarea.cke_source, input[type="hidden"]'))) {
                const value = String(field.value || '');
                const nextValue = hasReasonPlaceholder(value) ? replaceHtml(value) : bodyHtml;
                if (setValue(field, nextValue, win)) result.textareas += 1;
              }
              for (const frame of Array.from(doc.querySelectorAll('iframe'))) {
                try {
                  const childWin = frame.contentWindow;
                  const childDoc = frame.contentDocument || (childWin && childWin.document);
                  const attrs = clean(`${frame.className || ''} ${frame.title || ''} ${frame.getAttribute('aria-label') || ''}`);
                  if (childDoc && childDoc.body && (attrs.includes('email body') || attrs.includes('cke_wysiwyg_frame'))) {
                    childDoc.body.innerHTML = bodyHtml;
                    emit(childDoc.body, childWin);
                    result.editables += 1;
                  }
                } catch (err) {}
              }
            }
            try {
              if (win.parent === win) break;
              win = win.parent;
            } catch (err) {
              break;
            }
          }
          return result;
        }
        const frameCandidates = collectFrames(document).filter((frame) => {
            try { return visible(frame) || frameState(frame).score > 0; } catch (err) { return false; }
          }).map(frameState)
          .filter((item) => item.doc && item.root);
        const fieldCandidates = walkElements(document)
          .filter((field) => {
            try { return visible(field) && (field.isContentEditable || /^(textarea)$/i.test(field.tagName || '')); } catch (err) { return false; }
          })
          .map(fieldState)
          .filter((item) => item.root);
        const candidates = frameCandidates.concat(fieldCandidates).sort((a, b) => b.score - a.score);
        const target = candidates[0];
        if (!target || target.score < 0) return {count: 0, reason: 'no_body_frame', candidates: candidates.slice(0, 5).map((item) => ({score: item.score, text: item.text.slice(0, 80)}))};
        let count = replaceWithEditorSelection(target.root, target.doc);
        if (count <= 0) count = walkReplace(target.root, target.doc);
        const bodyHtml = target.root.innerHTML || target.root.value || '';
        const sync = count > 0 ? syncEditorSurfaces(target.doc.defaultView || window, bodyHtml) : {ckeditors: 0, textareas: 0, editables: 0};
        for (const type of ['beforeinput', 'input', 'change', 'keyup', 'blur']) {
          try { target.root.dispatchEvent(new target.doc.defaultView.Event(type, {bubbles: true})); } catch (err) {}
        }
        try { target.root.blur(); } catch (err) {}
        return {
          count,
          score: target.score,
          sync,
          body: target.root.innerText || target.root.textContent || target.root.value || '',
          html: bodyHtml
        };
        """,
        clean_reason,
    )
    if int((targeted_result or {}).get("count") or 0) > 0:
        time.sleep(0.8)
        updated_state = _read_salesforce_email_state(driver)
        updated_body = _clean_text((updated_state or {}).get("body", ""))
        placeholder_state = _salesforce_email_body_placeholder_state(driver, REASON_PLACEHOLDER_LABEL)
        targeted_body = _clean_text((targeted_result or {}).get("body", ""))
        targeted_html = str((targeted_result or {}).get("html") or "")
        if (
            clean_reason.lower() in updated_body.lower()
            and not _body_has_reason_placeholder(updated_body)
            and int(placeholder_state.get("count") or 0) == 0
        ):
            return {
                "placeholder": REASON_PLACEHOLDER_LABEL,
                "replacement": clean_reason,
                "bold_html_applied": True,
                "method": "targeted_ckeditor_body",
                "state": updated_state,
                "placeholder_state": placeholder_state,
                "targeted_result": {
                    "count": int((targeted_result or {}).get("count") or 0),
                    "score": (targeted_result or {}).get("score"),
                    "sync": (targeted_result or {}).get("sync") or {},
                },
            }
        if (
            clean_reason.lower() in f"{targeted_body} {targeted_html}".lower()
            and not _body_has_reason_placeholder(targeted_body)
            and not _body_has_reason_placeholder(targeted_html)
        ):
            return {
                "placeholder": REASON_PLACEHOLDER_LABEL,
                "replacement": clean_reason,
                "bold_html_applied": True,
                "method": "targeted_ckeditor_body_editor_verified",
                "state": updated_state,
                "placeholder_state": placeholder_state,
                "targeted_result": {
                    "count": int((targeted_result or {}).get("count") or 0),
                    "score": (targeted_result or {}).get("score"),
                    "sync": (targeted_result or {}).get("sync") or {},
                },
            }

    raise CopyrightCancelError(
        f"Salesforce {process.display_name.lower()} body placeholder could not be replaced inside the loaded template; "
        "refusing to type or paste fallback body content."
    )


def _fill_salesforce_email_from_salesforce_template(
    driver,
    order_id,
    subject="",
    expected_body="",
    process=COPYRIGHT_CANCEL_PROCESS,
    reason="",
):
    _insert_cancel_template(driver, process)
    deadline = time.monotonic() + 20
    template_text = ""
    subject_text = ""
    while time.monotonic() < deadline:
        time.sleep(0.5)
        state = _read_salesforce_email_state(driver)
        subject_text = _clean_text(state.get("subject", ""))
        template_text = _clean_text(state.get("body", ""))
        lower_text = template_text.lower()
        subject_ready = _subject_has_order_placeholder(subject_text) or str(order_id) in subject_text
        if not _missing_body_markers(lower_text, process) and subject_ready:
            break
    else:
        if not subject_text:
            raise CopyrightCancelError(
                f"Salesforce {process.display_name.lower()} template was selected, but the email subject did not load."
            )
        raise CopyrightCancelError(
            f"Salesforce {process.display_name.lower()} template subject did not contain {ORDER_NUMBER_PLACEHOLDER_LABEL} or order {order_id}. "
            f"Current subject: {subject_text}"
        )

    if str(order_id) not in subject_text:
        _replace_subject_order_number(driver, order_id)
    state = {}
    for _attempt in range(20):
        time.sleep(0.5)
        state = _read_salesforce_email_state(driver)
        if str(order_id) in _clean_text(state.get("subject", "")):
            break
    body_text = _clean_text(state.get("body", ""))
    subject_text = _clean_text(state.get("subject", ""))
    lower_body = body_text.lower()
    if _missing_body_markers(lower_body, process):
        raise CopyrightCancelError("Salesforce template body is not visible in the composer after insertion.")
    if str(order_id) not in subject_text:
        raise CopyrightCancelError(f"Salesforce template subject does not contain order {order_id}. Current subject: {subject_text or 'blank'}")
    missing = _missing_body_markers(lower_body, process)
    if missing:
        raise CopyrightCancelError(f"Salesforce template body is missing expected text: {', '.join(missing)}")
    body_replacement = None
    if process.replace_body_placeholder_with_reason:
        body_replacement = _replace_salesforce_body_placeholder_with_reason(driver, reason, process=process)
        state = body_replacement.get("state") or _read_salesforce_email_state(driver)
    return {
        "source": "salesforce_template",
        "template": process.salesforce_template,
        "process": process.key,
        "state": state,
        "body_placeholder_replacement": body_replacement,
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
          const fields = walk(composer)
            .filter((node) => /^(input|textarea)$/i.test(node.tagName || '') || node.isContentEditable)
            .filter((el) => {
              if (!visible(el)) return false;
              const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
              const value = clean(el.value || el.innerText || el.textContent || '');
              const rect = el.getBoundingClientRect();
              return hint.includes('subject') || ((/XXXXXX|\\[\\s*ORDER[\\s_-]*NUMBER\\s*\\]/i.test(value) || /\\b\\d{6,}\\b/.test(value)) && rect.height < 90) || rect.height < 70;
            })
            .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
          const field = fields.find((el) => {
            const value = clean(el.value || el.innerText || el.textContent || '');
            const rect = el.getBoundingClientRect();
            return (/XXXXXX|\\[\\s*ORDER[\\s_-]*NUMBER\\s*\\]/i.test(value) || /\\b\\d{6,}\\b/.test(value)) && rect.height < 90;
          }) || fields.find((el) => clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase().includes('subject')) || fields[0];
          return field ? clean(field.value || field.innerText || field.textContent || '') : '';
        }
        function readBody(composer) {
          const candidates = [];
          function addCandidate(kind, text, html, score) {
            const cleanText = clean(text || '');
            const cleanHtml = clean(html || '');
            if (!cleanText && !cleanHtml) return;
            if (cleanText.toLowerCase() === 'font size' || (cleanText.toLowerCase().includes('font size') && !cleanText.toLowerCase().includes('while reviewing your order'))) {
              score -= 100000;
            }
            if (/XXXXXX|\\[\\s*REASON\\s*\\]/i.test(cleanText) || /XXXXXX|\\[\\s*REASON\\s*\\]/i.test(cleanHtml)) score += 60000;
            if (cleanText.toLowerCase().includes('while reviewing your order') || cleanHtml.toLowerCase().includes('while reviewing your order')) score += 60000;
            candidates.push({kind, text: cleanText || cleanHtml, score});
          }
          function readDoc(doc, frame, seen = new Set()) {
            if (!doc || seen.has(doc)) return;
            seen.add(doc);
            let frameScore = 0;
            if (frame) {
              const attrs = clean(`${frame.className || ''} ${frame.title || ''} ${frame.getAttribute('aria-label') || ''}`).toLowerCase();
              if (attrs.includes('email body')) frameScore += 50000;
              if (attrs.includes('cke_wysiwyg_frame')) frameScore += 50000;
            }
            if (doc.body) {
              const bodyAttrs = clean(`${doc.body.className || ''} ${doc.body.getAttribute('aria-label') || ''} ${doc.body.getAttribute('role') || ''}`).toLowerCase();
              let bodyScore = frameScore;
              if (bodyAttrs.includes('email body')) bodyScore += 50000;
              if (bodyAttrs.includes('cke_editable')) bodyScore += 50000;
              addCandidate('iframe_body', doc.body.innerText || doc.body.textContent || '', doc.body.innerHTML || '', bodyScore);
            }
            for (const el of walk(doc).filter((node) => /^(input|textarea)$/i.test(node.tagName) || node.isContentEditable)) {
              addCandidate('iframe_editor', el.value || el.innerText || el.textContent || '', el.innerHTML || '', frameScore);
            }
            for (const frame of walk(doc).filter((node) => (node.tagName || '').toLowerCase() === 'iframe')) {
              try {
                const child = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
                readDoc(child, frame, seen);
              } catch (err) {}
            }
          }
          for (const frame of walk(composer).filter((node) => (node.tagName || '').toLowerCase() === 'iframe' && visible(node))) {
            try {
              const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
              readDoc(doc, frame);
            } catch (err) {}
          }
          for (const el of walk(composer).filter((node) => (node.isContentEditable || /^(textarea)$/i.test(node.tagName)) && visible(node))) {
            const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
            const rect = el.getBoundingClientRect();
            if (hint.includes('subject') || rect.height < 70) continue;
            addCandidate('body_field', el.value || el.innerText || el.textContent || '', el.innerHTML || '', rect.height >= 100 ? 1000 : 0);
          }
          candidates.sort((a, b) => b.score - a.score);
          return candidates.length ? candidates[0].text : '';
        }
        const composer = composerCandidates()[0] || document;
        return {
          from: readFrom(composer),
          subject: readSubject(composer),
          body: readBody(composer)
        };
        """
    ) or {}


def _salesforce_email_body_placeholder_state(driver, placeholder="XXXXXX"):
    return driver.execute_script(
        """
        const placeholder = String(arguments[0] || '');
        const reasonMode = /^\\[\\s*REASON\\s*\\]$/i.test(placeholder);
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').trim();
        }
        function hasPlaceholder(value) {
          const text = String(value || '');
          if (reasonMode) return /XXXXXX|\\[\\s*REASON\\s*\\]/i.test(text);
          return text.includes(placeholder);
        }
        function walk(root, out = []) {
          if (!root || !root.querySelectorAll) return out;
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
              const text = clean(el.innerText || el.textContent || '').toLowerCase();
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
        function addIfMatch(matches, kind, value) {
          const text = String(value || '');
          if (!hasPlaceholder(text)) return;
          matches.push({kind, snippet: clean(text).slice(0, 240)});
        }
        function inspectDoc(doc, matches, seen = new Set()) {
          if (!doc || seen.has(doc)) return;
          seen.add(doc);
          try {
            const win = doc.defaultView || window;
            const instances = Object.values((win.CKEDITOR && win.CKEDITOR.instances) || {});
            for (const editor of instances) {
              try {
                const container = editor.container && editor.container.$;
                if (container && !visible(container)) continue;
                addIfMatch(matches, 'ckeditor_data', editor.getData ? editor.getData() : '');
              } catch (err) {}
            }
          } catch (err) {}
          if (doc.body) {
            addIfMatch(matches, 'iframe_body_text', doc.body.innerText || doc.body.textContent || '');
            addIfMatch(matches, 'iframe_body_html', doc.body.innerHTML || '');
          }
          for (const el of walk(doc).filter((node) => /^(textarea)$/i.test(node.tagName || '') || node.isContentEditable)) {
            addIfMatch(matches, 'editor_value', el.value || '');
            addIfMatch(matches, 'editor_text', el.innerText || el.textContent || '');
            addIfMatch(matches, 'editor_html', el.innerHTML || '');
          }
          for (const frame of walk(doc).filter((node) => (node.tagName || '').toLowerCase() === 'iframe' && visible(node))) {
            try {
              inspectDoc(frame.contentDocument || (frame.contentWindow && frame.contentWindow.document), matches, seen);
            } catch (err) {}
          }
        }
        const matches = [];
        const roots = composerCandidates();
        if (!roots.length) roots.push(document);
        const seenFrames = new Set();
        const seenFields = new Set();
        for (const root of roots) {
          for (const frame of walk(root).filter((node) => (node.tagName || '').toLowerCase() === 'iframe' && visible(node))) {
            if (seenFrames.has(frame)) continue;
            seenFrames.add(frame);
            try {
              inspectDoc(frame.contentDocument || (frame.contentWindow && frame.contentWindow.document), matches);
            } catch (err) {}
          }
        }
        for (const root of roots) {
          for (const el of walk(root).filter((node) => (node.isContentEditable || /^(textarea)$/i.test(node.tagName || '')) && visible(node))) {
            if (seenFields.has(el)) continue;
            seenFields.add(el);
            const hint = clean(`${el.placeholder || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('name') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
            const rect = el.getBoundingClientRect();
            if (hint.includes('subject') || rect.height < 70) continue;
            addIfMatch(matches, 'body_value', el.value || '');
            addIfMatch(matches, 'body_text', el.innerText || el.textContent || '');
            addIfMatch(matches, 'body_html', el.innerHTML || '');
          }
        }
        return {placeholder, count: matches.length, matches};
        """,
        placeholder,
    ) or {"placeholder": placeholder, "count": 0, "matches": []}


def _verify_salesforce_email_ready_to_send(driver, order_id, subject, body, process=COPYRIGHT_CANCEL_PROCESS):
    state = _read_salesforce_email_state(driver)
    from_text = _clean_text(state.get("from", ""))
    subject_text = _clean_text(state.get("subject", ""))
    body_text = _clean_text(state.get("body", ""))
    lower_body = body_text.lower()
    if SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL.lower() not in from_text.lower():
        raise CopyrightCancelError(f"Salesforce From is not Orders before send. Current From: {from_text or 'blank'}")
    placeholder_state = _salesforce_email_body_placeholder_state(driver, REASON_PLACEHOLDER_LABEL)
    _validate_no_unresolved_email_placeholders(subject_text, body_text, body_placeholder_state=placeholder_state)
    lower_subject = subject_text.lower()
    if str(order_id) not in subject_text or any(marker not in lower_subject for marker in process.subject_markers):
        raise CopyrightCancelError(f"Salesforce subject is not ready before send. Current subject: {subject_text or 'blank'}")
    if _missing_body_markers(lower_body, process):
        raise CopyrightCancelError("Salesforce body is not ready before send; refusing to send a blank/bodyless email.")
    return state


def _send_salesforce_email(driver, dry_run, order_id, subject, body, skip_ready_verify=False, process=COPYRIGHT_CANCEL_PROCESS):
    ready_state = _read_salesforce_email_state(driver) if skip_ready_verify else _verify_salesforce_email_ready_to_send(driver, order_id, subject, body, process=process)
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
    process=COPYRIGHT_CANCEL_PROCESS,
    reason="",
    login_wait_seconds=0,
    skip_from_selection=False,
    skip_ready_verify=False,
):
    preparation_retry_error = None
    for attempt in range(2):
        try:
            sf_handle = _open_salesforce_account(
                driver,
                crm_handle,
                customer_email,
                login_wait_seconds=login_wait_seconds,
                order_id=order_id,
            )
            _verify_salesforce_email(driver, customer_email)
            _click_salesforce_email(driver, customer_email)
            _wait_for_email_composer(driver)
            if skip_from_selection:
                selected_from = _clean_text((_read_salesforce_email_state(driver) or {}).get("from", "")) or "Skipped From selection for inspection"
            else:
                selected_from = _set_salesforce_from_orders(driver)
            time.sleep(1)
            fill_result = _fill_salesforce_email_from_salesforce_template(
                driver,
                order_id=order_id,
                process=process,
                reason=reason,
            )
            email_state = fill_result.get("state") or _read_salesforce_email_state(driver)
            subject = _clean_text(email_state.get("subject", ""))
            body = _clean_text(email_state.get("body", ""))
            break
        except Exception as exc:
            if attempt:
                raise
            preparation_retry_error = str(exc)
            try:
                if driver.current_window_handle != crm_handle:
                    driver.close()
            except Exception:
                pass
            driver.switch_to.window(crm_handle)
            driver.refresh()
            _activate_crm_context(driver)
            _wait_for_order_scope(driver, order_id=order_id, timeout=30)
    result = _send_salesforce_email(
        driver,
        dry_run=dry_run,
        order_id=order_id,
        subject=subject,
        body=body,
        skip_ready_verify=skip_ready_verify,
        process=process,
    )
    return {
        "salesforce_handle": sf_handle,
        "email": customer_email,
        "from": selected_from,
        "subject": subject,
        "process": process.key,
        "fill": fill_result,
        "preparation_retried": bool(preparation_retry_error),
        "first_preparation_error": preparation_retry_error,
        **result,
    }


def _visible_salesforce_text(driver):
    try:
        return _visible_text(driver)
    except Exception:
        return _page_text(driver)


def _click_salesforce_cases_new_button(driver):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        clicked = bool(
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
                const directCaseNew = document.querySelector('[data-target-selection-name="sfdc:StandardButton.Case.NewCase"] button, [data-target-selection-name="sfdc:StandardButton.Case.NewCase"] a, [data-target-selection-name="sfdc:StandardButton.Case.NewCase"] [role=button]');
                if (directCaseNew && rendered(directCaseNew)) {
                  directCaseNew.scrollIntoView({block: 'center', inline: 'center'});
                  directCaseNew.click();
                  return true;
                }
                const roots = Array.from(document.querySelectorAll('article,section,div'))
                  .filter((el) => {
                    if (!rendered(el)) return false;
                    const text = clean(el.innerText || '');
                    const label = clean(`${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`);
                    return (/\\bCases\\s*(\\(|$)/i.test(text) || /^Cases$/i.test(label)) && /\\bNew\\b/i.test(text);
                  })
                  .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                for (const root of roots) {
                  const exact = root.querySelector('[data-target-selection-name="sfdc:StandardButton.Case.NewCase"] button, [data-target-selection-name="sfdc:StandardButton.Case.NewCase"] a, [data-target-selection-name="sfdc:StandardButton.Case.NewCase"] [role=button]');
                  if (exact && rendered(exact)) {
                    exact.scrollIntoView({block: 'center', inline: 'center'});
                    exact.click();
                    return true;
                  }
                  const controls = Array.from(root.querySelectorAll('button,a,[role=button],input'))
                    .filter((el) => {
                      if (!rendered(el)) return false;
                      const text = clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`).toLowerCase();
                      return text === 'new' || text === 'new case';
                    })
                    .sort((a, b) => {
                      const ar = a.getBoundingClientRect();
                      const br = b.getBoundingClientRect();
                      return (br.right - ar.right) || (ar.top - br.top);
                    });
                  if (!controls.length) continue;
                  controls[0].scrollIntoView({block: 'center', inline: 'center'});
                  controls[0].click();
                  return true;
                }
                return false;
                """
            )
        )
        if clicked:
            return True
        driver.execute_script(
            """
            function rendered(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden';
            }
            const scrollers = [document.scrollingElement, document.documentElement, document.body]
              .concat(Array.from(document.querySelectorAll('*')).filter((el) => {
                if (!rendered(el)) return false;
                return el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 300;
              }))
              .filter(Boolean);
            scrollers.sort((a, b) => {
              const ar = a.getBoundingClientRect ? a.getBoundingClientRect() : {width: window.innerWidth, height: window.innerHeight};
              const br = b.getBoundingClientRect ? b.getBoundingClientRect() : {width: window.innerWidth, height: window.innerHeight};
              return (br.width * br.height) - (ar.width * ar.height);
            });
            for (const scroller of scrollers.slice(0, 4)) {
              try {
                scroller.scrollTop = scroller.scrollTop + 650;
                scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
              } catch (err) {}
            }
            try { window.scrollBy(0, 650); } catch (err) {}
            """
        )
        time.sleep(0.5)
    raise CopyrightCancelError("Salesforce Cases New button was not found.")


def _wait_for_salesforce_new_case_form(driver, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _visible_salesforce_text(driver).lower()
        if "new case" in text and "case information" in text and "order number" in text:
            return True
        if _is_salesforce_login_page(driver):
            _attempt_salesforce_login(driver, timeout=max(30, min(90, int(deadline - time.monotonic()) or 30)))
        time.sleep(0.5)
    raise CopyrightCancelError("Salesforce New Case form did not open.")


def _salesforce_field_control(driver, label_text, selectors):
    return driver.execute_script(
        """
        const labelText = String(arguments[0] || '').replace(/^\\*/, '').trim().toLowerCase();
        const selectors = arguments[1];
        function clean(value) {
          return (value || '').replace(/\\s+/g, ' ').replace(/^\\*/, '').trim();
        }
        function cleanLower(value) {
          return clean(value).toLowerCase();
        }
        function labelMatches(value) {
          const text = cleanLower(value);
          if (!text) return false;
          return text === labelText
            || text.startsWith(labelText + ' ')
            || text.endsWith(' ' + labelText)
            || text.includes(' ' + labelText + ' ');
        }
        function all(selector, root) {
          const start = root || document;
          const found = [];
          const seen = new Set();
          function add(node) {
            if (node && !seen.has(node)) {
              seen.add(node);
              found.push(node);
            }
          }
          function walk(node) {
            if (!node) return;
            try {
              if (node.querySelectorAll) {
                Array.from(node.querySelectorAll(selector)).forEach(add);
                Array.from(node.querySelectorAll('*')).forEach((child) => {
                  if (child.shadowRoot) walk(child.shadowRoot);
                });
              }
            } catch (err) {}
            if (node.shadowRoot) walk(node.shadowRoot);
          }
          walk(start);
          return found;
        }
        function visible(el) {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        const fieldContainers = all('records-record-layout-item[field-label], lightning-input-field[field-label], [data-target-selection-name]')
          .filter((el) => {
            if (!visible(el)) return false;
            const fieldLabel = el.getAttribute('field-label') || '';
            const targetName = el.getAttribute('data-target-selection-name') || '';
            return labelMatches(fieldLabel) || targetName.toLowerCase().endsWith('.' + labelText.replace(/\\s+/g, '_') + '__c') || targetName.toLowerCase().endsWith('.' + labelText.replace(/\\s+/g, ''));
          })
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        for (const container of fieldContainers) {
          const controls = all(selectors, container).filter((el) => visible(el));
          if (controls.length) {
            controls.sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              const aAria = labelMatches(`${a.getAttribute('aria-label') || ''} ${a.getAttribute('name') || ''}`) ? 0 : 1;
              const bAria = labelMatches(`${b.getAttribute('aria-label') || ''} ${b.getAttribute('name') || ''}`) ? 0 : 1;
              if (aAria !== bAria) return aAria - bAria;
              const aInput = ['input', 'button', 'textarea'].includes((a.tagName || '').toLowerCase()) ? 0 : 1;
              const bInput = ['input', 'button', 'textarea'].includes((b.tagName || '').toLowerCase()) ? 0 : 1;
              if (aInput !== bInput) return aInput - bInput;
              return (ar.left - br.left) || (ar.top - br.top);
            });
            return controls[0];
          }
        }
        const labels = all('records-record-layout-item,label,span,div,lightning-input-field,lightning-grouped-combobox')
          .filter((el) => {
            if (!visible(el)) return false;
            const rect = el.getBoundingClientRect();
            const tag = (el.tagName || '').toLowerCase();
            const text = `${el.innerText || ''} ${el.textContent || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('field-label') || ''}`;
            const compactText = clean(text);
            if (!['records-record-layout-item', 'lightning-input-field', 'lightning-grouped-combobox'].includes(tag)
                && (compactText.length > 140 || rect.width > 420 || rect.height > 90)) {
              return false;
            }
            if (labelMatches(text)) return true;
            const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
            return labelledBy.some((id) => {
              const ref = document.getElementById(id);
              return ref && labelMatches(`${ref.innerText || ''} ${ref.textContent || ''}`);
            });
          })
          .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);
        for (const label of labels) {
          if (label.tagName && label.tagName.toLowerCase() === 'label') {
            const forId = label.getAttribute('for');
            if (forId) {
              const target = document.getElementById(forId);
              if (target && visible(target) && target.matches(selectors)) return target;
              const wrapped = target && target.closest('.slds-form-element, lightning-input-field, .uiInput, .forcePageBlockSectionRow');
              if (wrapped) {
                const wrappedControls = all(selectors, wrapped).filter((el) => visible(el) && el !== label);
                if (wrappedControls.length) return wrappedControls[0];
              }
            }
          }
          const formElement = label.closest('.slds-form-element, lightning-input-field, .uiInput, .forcePageBlockSectionRow')
            || label.parentElement
            || document;
          const scoped = all(selectors, formElement).filter((el) => visible(el) && el !== label);
          if (scoped.length) {
            scoped.sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (ar.left - br.left) || (ar.top - br.top);
            });
            return scoped[0];
          }
          const lr = label.getBoundingClientRect();
          const nearby = all(selectors)
            .filter((el) => {
              if (!visible(el)) return false;
              const rect = el.getBoundingClientRect();
              const sameRow = Math.abs((rect.top + rect.height / 2) - (lr.top + lr.height / 2)) < 48 && rect.left > lr.left;
              const below = rect.top >= lr.bottom - 8 && rect.top < lr.bottom + 95 && Math.abs(rect.left - lr.left) < 360;
              return sameRow || below;
            })
            .sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              const aDistance = Math.abs(ar.top - lr.top) + Math.abs(ar.left - lr.left);
              const bDistance = Math.abs(br.top - lr.top) + Math.abs(br.left - lr.left);
              return aDistance - bDistance;
            });
          if (nearby.length) return nearby[0];
        }
        return null;
        """,
        label_text,
        selectors,
    )


def _salesforce_order_search_dialog_visible(driver):
    return bool(
        driver.execute_script(
            """
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim();
            }
            function all(selector, root) {
              const start = root || document;
              const found = [];
              const seen = new Set();
              function add(node) {
                if (node && !seen.has(node)) {
                  seen.add(node);
                  found.push(node);
                }
              }
              function walk(node) {
                if (!node) return;
                try {
                  if (node.querySelectorAll) {
                    Array.from(node.querySelectorAll(selector)).forEach(add);
                    Array.from(node.querySelectorAll('*')).forEach((child) => {
                      if (child.shadowRoot) walk(child.shadowRoot);
                    });
                  }
                } catch (err) {}
                if (node.shadowRoot) walk(node.shadowRoot);
              }
              walk(start);
              return found;
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            function textOf(el) {
              if (!el) return '';
              const parts = [el.innerText, el.textContent];
              try {
                all('*', el).slice(0, 80).forEach((child) => {
                  parts.push(child.innerText, child.textContent);
                  if (child.getAttribute) {
                    parts.push(child.getAttribute('title'), child.getAttribute('aria-label'));
                  }
                });
              } catch (err) {}
              return clean(parts.filter(Boolean).join(' '));
            }
            return all('[role=dialog],section,div')
              .some((el) => visible(el) && /(Advanced Search|Search Orders)/i.test(textOf(el)));
            """
        )
    )


def _fill_salesforce_case_order_lookup(driver, order_id):
    field = _salesforce_field_control(driver, "Order Number", "input,button,[role=combobox],.slds-combobox__input")
    if field is None:
        raise CopyrightCancelError("Salesforce Case Order Number lookup field was not found.")
    field.click()
    time.sleep(0.2)
    try:
        field.clear()
    except Exception:
        field.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
        field.send_keys(Keys.BACKSPACE)
    field.send_keys(str(order_id))
    time.sleep(1)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        option = driver.execute_script(
            """
            const orderId = String(arguments[0] || '').trim().toLowerCase();
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function all(selector, root) {
              const start = root || document;
              const found = [];
              const seen = new Set();
              function add(node) {
                if (node && !seen.has(node)) {
                  seen.add(node);
                  found.push(node);
                }
              }
              function walk(node) {
                if (!node) return;
                try {
                  if (node.querySelectorAll) {
                    Array.from(node.querySelectorAll(selector)).forEach(add);
                    Array.from(node.querySelectorAll('*')).forEach((child) => {
                      if (child.shadowRoot) walk(child.shadowRoot);
                    });
                  }
                } catch (err) {}
                if (node.shadowRoot) walk(node.shadowRoot);
              }
              walk(start);
              return found;
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            // Quick lookup suggestions match Salesforce Order Number. They do
            // not prove that the record belongs to this CRM order, so never
            // select one here. Advanced Search is mandatory because it exposes
            // the authoritative Printfly Order Id column.
            return null;
            """,
            str(order_id),
        )
        more = driver.execute_script(
            """
            const orderId = String(arguments[0] || '').trim().toLowerCase();
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
            const nodes = Array.from(document.querySelectorAll('lightning-base-combobox-item,[role=option],li,a,button,div,span'));
            const moreOptions = nodes
              .filter((el) => {
                if (!visible(el)) return false;
                const text = clean(`${el.innerText || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''}`);
                return text.includes('show more results') && (!orderId || text.includes(orderId));
              })
              .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
            const more = moreOptions[0];
            if (!more) return null;
            const clickable = more.closest('lightning-base-combobox-item,a,li,[role=option]') || more;
            clickable.scrollIntoView({block: 'center', inline: 'center'});
            return clickable;
            """,
            str(order_id),
        )
        if more is not None:
            for activate_more in ("enter", "events", "center"):
                try:
                    if activate_more == "enter":
                        field.send_keys(Keys.ENTER)
                    elif activate_more == "events":
                        driver.execute_script(
                            """
                            const option = arguments[0];
                            const target = option.closest('lightning-base-combobox-item,[role=option],a,li,button') || option;
                            target.scrollIntoView({block: 'center', inline: 'center'});
                            for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                              target.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                            }
                            """,
                            more,
                        )
                    elif not _click_element_center(driver, more):
                        continue
                except Exception:
                    continue
                deadline_open = time.monotonic() + 5
                while time.monotonic() < deadline_open:
                    if _salesforce_order_search_dialog_visible(driver):
                        return "advanced"
                    time.sleep(0.25)
        search_button = driver.execute_script(
            """
            const field = arguments[0];
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function all(selector, root) {
              const start = root || document;
              const found = [];
              const seen = new Set();
              function add(node) {
                if (node && !seen.has(node)) {
                  seen.add(node);
                  found.push(node);
                }
              }
              function walk(node) {
                if (!node) return;
                try {
                  if (node.querySelectorAll) {
                    Array.from(node.querySelectorAll(selector)).forEach(add);
                    Array.from(node.querySelectorAll('*')).forEach((child) => {
                      if (child.shadowRoot) walk(child.shadowRoot);
                    });
                  }
                } catch (err) {}
                if (node.shadowRoot) walk(node.shadowRoot);
              }
              walk(start);
              return found;
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            const form = field.closest('.slds-form-element, lightning-input-field, .uiInput, .forcePageBlockSectionRow')
              || field.parentElement
              || document;
            const fr = field.getBoundingClientRect();
            const controls = all('button,a,[role=button],lightning-button-icon,lightning-icon,.slds-input__icon', form)
              .filter((el) => {
                if (!visible(el) || el === field) return false;
                const text = clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('title') || ''} ${el.getAttribute('aria-label') || ''} ${el.getAttribute('alternative-text') || ''} ${el.getAttribute('icon-name') || ''}`);
                const rect = el.getBoundingClientRect();
                const searchText = text.includes('search') || text.includes('lookup') || text.includes('utility:search');
                const besideLookup = rect.left >= fr.right - 12
                  && rect.left <= fr.right + 80
                  && Math.abs((rect.top + rect.height / 2) - (fr.top + fr.height / 2)) < 36;
                return searchText || besideLookup;
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const ad = Math.abs(ar.left - fr.right) + Math.abs((ar.top + ar.height / 2) - (fr.top + fr.height / 2));
                const bd = Math.abs(br.left - fr.right) + Math.abs((br.top + br.height / 2) - (fr.top + fr.height / 2));
                return ad - bd;
              });
            const target = controls[0];
            if (!target) return null;
            const clickable = target.closest('button,a,[role=button]') || target;
            clickable.scrollIntoView({block: 'center', inline: 'center'});
            return clickable;
            """,
            field,
        )
        if search_button is not None:
            if _click_element_center(driver, search_button):
                deadline_open = time.monotonic() + 5
                while time.monotonic() < deadline_open:
                    if _salesforce_order_search_dialog_visible(driver):
                        return "advanced"
                    time.sleep(0.25)
        try:
            field.send_keys(Keys.ENTER)
            deadline_open = time.monotonic() + 3
            while time.monotonic() < deadline_open:
                if _salesforce_order_search_dialog_visible(driver):
                    return "advanced"
                time.sleep(0.25)
        except Exception:
            pass
        time.sleep(0.5)
    raise CopyrightCancelError(f'Salesforce order lookup did not show more results for "{order_id}".')


def _select_salesforce_advanced_search_order(driver, order_id):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        selected = driver.execute_script(
            """
            const orderId = String(arguments[0] || '').replace(/^0+/, '').trim();
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim();
            }
            function all(selector, root) {
              const start = root || document;
              const found = [];
              const seen = new Set();
              function add(node) {
                if (node && !seen.has(node)) {
                  seen.add(node);
                  found.push(node);
                }
              }
              function walk(node) {
                if (!node) return;
                try {
                  if (node.querySelectorAll) {
                    Array.from(node.querySelectorAll(selector)).forEach(add);
                    Array.from(node.querySelectorAll('*')).forEach((child) => {
                      if (child.shadowRoot) walk(child.shadowRoot);
                    });
                  }
                } catch (err) {}
                if (node.shadowRoot) walk(node.shadowRoot);
              }
              walk(start);
              return found;
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            function textOf(el) {
              if (!el) return '';
              const parts = [
                el.innerText,
                el.textContent,
                el.value,
                el.getAttribute && el.getAttribute('title'),
                el.getAttribute && el.getAttribute('aria-label'),
                el.getAttribute && el.getAttribute('data-value')
              ];
              try {
                all('*', el).slice(0, 120).forEach((child) => {
                  parts.push(child.innerText, child.textContent, child.value);
                  if (child.getAttribute) {
                    parts.push(child.getAttribute('title'), child.getAttribute('aria-label'), child.getAttribute('data-value'));
                  }
                });
              } catch (err) {}
              return clean(parts.filter(Boolean).join(' '));
            }
            function orderMatches(text) {
              const value = String(text || '');
              const compact = value.replace(/\\D+/g, ' ');
              const normalized = value.replace(/^0+/, '');
              return normalized === orderId || new RegExp(`(^|\\\\D)0*${orderId}(\\\\D|$)`).test(value)
                || compact.split(/\\s+/).some((part) => part.replace(/^0+/, '') === orderId);
            }
            function selectDatatableRow(row, reference) {
              const rowText = textOf(row);
              const firstCell = all('td,th,[role=gridcell]', row)[0] || row;
              const input = all('input[type=radio],input[type=checkbox]', firstCell)[0] || all('input[type=radio],input[type=checkbox]', row)[0];
              const label = input && input.id ? all(`label[for="${CSS.escape(input.id)}"]`, firstCell)[0] || all(`label[for="${CSS.escape(input.id)}"]`, row)[0] : null;
              const faux = (label ? all('.slds-radio_faux,.slds-radio--faux', label)[0] : null)
                || all('.slds-radio_faux,.slds-radio--faux', firstCell)[0]
                || all('.slds-radio_faux,.slds-radio--faux', row)[0];
              if (input) {
                try { input.click(); } catch (err) {}
                try { input.focus(); } catch (err) {}
                try { input.checked = true; } catch (err) {}
                try { input.dispatchEvent(new KeyboardEvent('keydown', {key: ' ', code: 'Space', bubbles: true, cancelable: true})); } catch (err) {}
                try { input.dispatchEvent(new KeyboardEvent('keyup', {key: ' ', code: 'Space', bubbles: true, cancelable: true})); } catch (err) {}
                try { input.dispatchEvent(new Event('input', {bubbles: true})); } catch (err) {}
                try { input.dispatchEvent(new Event('change', {bubbles: true})); } catch (err) {}
              }
              if (label) {
                try { label.click(); } catch (err) {}
              }
              const clickTarget = faux || label || input || firstCell;
              const rect = clickTarget.getBoundingClientRect();
              return {
                row_text: rowText || textOf(reference) || '',
                row_key: row.getAttribute('data-row-key-value') || '',
                click_point: {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2},
                datatable_row: true
              };
            }
            const directOrderCells = all('[data-label="Printfly Order Id"],[data-col-key-value*="Printfly_Order_Id"]')
              .filter((cell) => {
                const value = `${cell.getAttribute('data-cell-value') || ''} ${textOf(cell)}`;
                return visible(cell) && orderMatches(value);
              })
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const aPrintfly = /Printfly Order Id/i.test(`${a.getAttribute('data-label') || ''} ${a.getAttribute('data-col-key-value') || ''}`) ? 0 : 1;
                const bPrintfly = /Printfly Order Id/i.test(`${b.getAttribute('data-label') || ''} ${b.getAttribute('data-col-key-value') || ''}`) ? 0 : 1;
                if (aPrintfly !== bPrintfly) return aPrintfly - bPrintfly;
                return (ar.width * ar.height) - (br.width * br.height);
              });
            for (const cell of directOrderCells) {
              const row = cell.closest('tr[role=row],.slds-hint-parent');
              if (row) {
                const result = selectDatatableRow(row, cell);
                result.printfly_order_id = orderId;
                result.match_source = 'printfly-order-id-column';
                return result;
              }
            }
            const root = all('[role=dialog],section,div')
              .filter((el) => visible(el) && /(Advanced Search|Search Orders|Order Number)/i.test(textOf(el)))
              .sort((a, b) => {
                const at = textOf(a);
                const bt = textOf(b);
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const aHasTable = /Printfly Order Id/i.test(at) ? 0 : 1;
                const bHasTable = /Printfly Order Id/i.test(bt) ? 0 : 1;
                if (aHasTable !== bHasTable) return aHasTable - bHasTable;
                return (br.width * br.height) - (ar.width * ar.height);
              })[0] || document;
            const grids = all('table[role=grid]', root)
              .filter((grid) => visible(grid) && orderMatches(textOf(grid)))
              .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                const aSelected = Number(a.getAttribute('data-num-selected-rows') || 0) > 0 ? 0 : 1;
                const bSelected = Number(b.getAttribute('data-num-selected-rows') || 0) > 0 ? 0 : 1;
                if (aSelected !== bSelected) return aSelected - bSelected;
                return (br.width * br.height) - (ar.width * ar.height);
              });
            for (const grid of grids) {
              const gridRows = all('tr[role=row],.slds-hint-parent', grid).filter((row) => visible(row) && !/HEADER/i.test(row.getAttribute('data-row-key-value') || ''));
              for (const row of gridRows) {
                const orderCell = all('[data-label="Printfly Order Id"],[data-col-key-value*="Printfly_Order_Id"]', row)
                  .find((cell) => orderMatches(textOf(cell)) || orderMatches(cell.getAttribute('data-cell-value') || ''));
                if (orderCell) {
                  const result = selectDatatableRow(row, orderCell);
                  result.printfly_order_id = orderId;
                  result.match_source = 'printfly-order-id-column';
                  return result;
                }
              }
            }
            function radioPointFor(reference, row) {
              const refRect = reference.getBoundingClientRect();
              const rowRect = row ? row.getBoundingClientRect() : refRect;
              const cy = refRect.top + refRect.height / 2;
              const rowCenter = rowRect.top + rowRect.height / 2;
              const sameRow = all('*', root)
                .map((el) => ({el, rect: el.getBoundingClientRect()}))
                .filter((item) => {
                  if (!visible(item.el)) return false;
                  const center = item.rect.top + item.rect.height / 2;
                  return item.rect.width > 8
                    && item.rect.height > 8
                    && Math.abs(center - rowCenter) < 24
                    && item.rect.left < refRect.left;
                })
                .sort((a, b) => a.rect.left - b.rect.left);
              if (sameRow.length) {
                return {x: sameRow[0].rect.left + 18, y: cy};
              }
              return {x: rowRect.left + 24, y: cy};
            }
            const exactCells = all('[data-label="Printfly Order Id"],[data-col-key-value*="Printfly_Order_Id"]', root)
              .filter((el) => {
                if (!visible(el)) return false;
                const tag = (el.tagName || '').toLowerCase();
                const direct = clean(el.innerText || el.textContent);
                if (tag === 'span' && direct.replace(/^0+/, '') !== orderId) return false;
                return orderMatches(textOf(el));
              })
              .sort((a, b) => {
                const at = textOf(a);
                const bt = textOf(b);
                const aExact = at.replace(/^0+/, '') === orderId ? 0 : 1;
                const bExact = bt.replace(/^0+/, '') === orderId ? 0 : 1;
                if (aExact !== bExact) return aExact - bExact;
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.width * ar.height) - (br.width * br.height);
              });
            for (const cell of exactCells) {
              const cr = cell.getBoundingClientRect();
              const cy = cr.top + cr.height / 2;
              const radios = all('input[type=radio],input[type=checkbox],[role=radio],.slds-radio_faux,.slds-radio--faux,.slds-radio__label,.slds-radio,.slds-checkbox_faux', root)
                .filter((el) => {
                  if (!visible(el)) return false;
                  const rr = el.getBoundingClientRect();
                  const ry = rr.top + rr.height / 2;
                  return Math.abs(ry - cy) < 28 && rr.left < cr.left;
                })
                .sort((a, b) => {
                  const ar = a.getBoundingClientRect();
                  const br = b.getBoundingClientRect();
                  return Math.abs(ar.right - cr.left) - Math.abs(br.right - cr.left);
              });
              const row = cell.closest('tr,[role=row],.slds-hint-parent') || cell.parentElement || cell;
              const clickPoint = radioPointFor(cell, row);
              const input = (row ? all('input[type=radio],input[type=checkbox]', row)[0] : null)
                || all('input[type=radio],input[type=checkbox]', root)[0];
              if (input) {
                try { input.click(); } catch (err) {}
                try { input.checked = true; } catch (err) {}
                try { input.dispatchEvent(new Event('input', {bubbles: true})); } catch (err) {}
                try { input.dispatchEvent(new Event('change', {bubbles: true})); } catch (err) {}
                return {row_text: textOf(row) || textOf(cell), dom_selected: true, click_point: clickPoint, printfly_order_id: orderId, match_source: 'printfly-order-id-column'};
              }
              return {row_text: textOf(row) || textOf(cell), click_point: clickPoint, printfly_order_id: orderId, match_source: 'printfly-order-id-column'};
            }
            const rows = all('tr,[role=row],.slds-hint-parent', root).filter(visible);
            let headerTexts = [];
            const headerRow = rows.find((row) => /Printfly Order Id/i.test(textOf(row)));
            if (headerRow) {
              headerTexts = all('th,[role=columnheader],td', headerRow)
                .map((cell) => textOf(cell).toLowerCase());
            }
            const printflyIndex = headerTexts.findIndex((text) => text.includes('printfly order id'));
            for (const row of rows) {
              const rowText = textOf(row);
              if (!rowText || /Printfly Order Id/i.test(rowText)) continue;
              const cells = all('td,[role=gridcell]', row);
              if (printflyIndex < 0 || !cells[printflyIndex] || !orderMatches(textOf(cells[printflyIndex]))) continue;
              const input = all('input[type=radio],input[type=checkbox]', row)[0] || all('input[type=radio],input[type=checkbox]', root)[0];
              if (input) {
                try { input.click(); } catch (err) {}
                try { input.checked = true; } catch (err) {}
                try { input.dispatchEvent(new Event('input', {bubbles: true})); } catch (err) {}
                try { input.dispatchEvent(new Event('change', {bubbles: true})); } catch (err) {}
                const rr = row.getBoundingClientRect();
                return {row_text: rowText, dom_selected: true, click_point: radioPointFor(row, row), printfly_order_id: orderId, match_source: 'printfly-order-id-column'};
              }
              const rr = row.getBoundingClientRect();
              return {row_text: rowText, click_point: radioPointFor(row, row), printfly_order_id: orderId, match_source: 'printfly-order-id-column'};
            }
            const rootText = textOf(root);
            if (new RegExp(`1 result found for ["“]${orderId}["”]`, 'i').test(rootText) || rootText.includes(`1 result found for "${orderId}"`)) {
              return null;
            }
            return null;
            """,
            str(order_id),
        )
        if selected:
            click_point = selected.get("click_point") if isinstance(selected, dict) else None
            if click_point:
                _click_viewport_point(driver, click_point.get("x"), click_point.get("y"))
                time.sleep(0.8)
            break
        time.sleep(0.5)
    else:
        raise CopyrightCancelError(f"Salesforce Advanced Search did not find Printfly Order Id {order_id}.")

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        clicked = bool(
            driver.execute_script(
                """
                function clean(value) {
                  return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                }
                function all(selector, root) {
                  const start = root || document;
                  const found = [];
                  const seen = new Set();
                  function add(node) {
                    if (node && !seen.has(node)) {
                      seen.add(node);
                      found.push(node);
                    }
                  }
                  function walk(node) {
                    if (!node) return;
                    try {
                      if (node.querySelectorAll) {
                        Array.from(node.querySelectorAll(selector)).forEach(add);
                        Array.from(node.querySelectorAll('*')).forEach((child) => {
                          if (child.shadowRoot) walk(child.shadowRoot);
                        });
                      }
                    } catch (err) {}
                    if (node.shadowRoot) walk(node.shadowRoot);
                  }
                  walk(start);
                  return found;
                }
                function visible(el) {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.bottom > 0 && rect.top < window.innerHeight
                    && rect.right > 0 && rect.left < window.innerWidth;
                }
                const root = all('[role=dialog],section,div')
                  .filter((el) => visible(el) && /(Advanced Search|Search Orders|Order Number)/i.test(el.innerText || ''))
                  .sort((a, b) => {
                    const at = a.innerText || '';
                    const bt = b.innerText || '';
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    const aHasTable = /Printfly Order Id/i.test(at) ? 0 : 1;
                    const bHasTable = /Printfly Order Id/i.test(bt) ? 0 : 1;
                    if (aHasTable !== bHasTable) return aHasTable - bHasTable;
                    return (br.width * br.height) - (ar.width * ar.height);
                  })[0] || document;
                const buttons = all('button,a,[role=button],input', root)
                  .filter((el) => {
                    if (!visible(el)) return false;
                    if (el.disabled || clean(el.getAttribute('aria-disabled') || '') === 'true') return false;
                    if (/disabled/i.test(String(el.className || ''))) return false;
                    return clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''}`) === 'select';
                  })
                  .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (br.bottom - ar.bottom) || (br.right - ar.right);
                  });
                if (!buttons.length) return false;
                buttons[0].scrollIntoView({block: 'center', inline: 'center'});
                buttons[0].click();
                return true;
                """
            )
        )
        if clicked:
            deadline_closed = time.monotonic() + 5
            while time.monotonic() < deadline_closed:
                if not _salesforce_order_search_dialog_visible(driver):
                    return selected
                time.sleep(0.25)
        time.sleep(0.5)
    raise CopyrightCancelError("Salesforce Advanced Search Select button was not found.")


def _click_salesforce_case_picklist_option(driver, value):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        option = driver.execute_script(
            """
            const expected = String(arguments[0] || '').trim().toLowerCase();
            function clean(value) {
              return (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            }
            function all(selector, root) {
              const start = root || document;
              const found = [];
              const seen = new Set();
              function add(node) {
                if (node && !seen.has(node)) {
                  seen.add(node);
                  found.push(node);
                }
              }
              function walk(node) {
                if (!node) return;
                try {
                  if (node.querySelectorAll) {
                    Array.from(node.querySelectorAll(selector)).forEach(add);
                    Array.from(node.querySelectorAll('*')).forEach((child) => {
                      if (child.shadowRoot) walk(child.shadowRoot);
                    });
                  }
                } catch (err) {}
                if (node.shadowRoot) walk(node.shadowRoot);
              }
              walk(start);
              return found;
            }
            function visible(el) {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && rect.bottom > 0 && rect.top < window.innerHeight
                && rect.right > 0 && rect.left < window.innerWidth;
            }
            const options = all('[role=option],lightning-base-combobox-item,li,a,span,div')
              .filter((el) => visible(el) && clean(`${el.innerText || ''} ${el.getAttribute('title') || ''}`) === expected)
              .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
            if (!options.length) return null;
            const option = options[0].closest('[role=option],lightning-base-combobox-item,li,a') || options[0];
            option.scrollIntoView({block: 'center', inline: 'center'});
            return option;
            """,
            value,
        )
        if option is not None and _click_element_center(driver, option):
            return True
        time.sleep(0.5)
    return False


def _set_salesforce_case_status(driver):
    field = _salesforce_field_control(driver, "Status", "button,[role=combobox],a,input,.slds-combobox__input,.slds-input_faux")
    if field is None:
        raise CopyrightCancelError("Salesforce Case Status field was not found.")
    field.click()
    time.sleep(0.5)
    if not _click_salesforce_case_picklist_option(driver, SALESFORCE_CASE_STATUS_REFUND_PENDING):
        try:
            driver.switch_to.active_element.send_keys(Keys.ARROW_DOWN)
            time.sleep(0.2)
            driver.switch_to.active_element.send_keys(Keys.ARROW_DOWN)
            time.sleep(0.2)
        except Exception:
            pass
        if not _click_salesforce_case_picklist_option(driver, SALESFORCE_CASE_STATUS_REFUND_PENDING):
            raise CopyrightCancelError("Salesforce Case Status option Refund Pending was not found.")
    time.sleep(0.5)


def _fill_salesforce_case_subject(driver, subject):
    field = _salesforce_field_control(driver, "Subject", "input,textarea")
    for _ in range(6):
        if field is not None:
            break
        driver.execute_script(
            """
            const scrollers = Array.from(document.querySelectorAll('*')).filter((el) => {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0
                && style.display !== 'none' && style.visibility !== 'hidden'
                && el.scrollHeight > el.clientHeight + 80
                && rect.top < window.innerHeight
                && rect.bottom > 0;
            }).sort((a, b) => {
              const ar = a.getBoundingClientRect();
              const br = b.getBoundingClientRect();
              return (br.width * br.height) - (ar.width * ar.height);
            });
            for (const el of scrollers.slice(0, 4)) {
              try {
                el.scrollTop = el.scrollTop + 520;
                el.dispatchEvent(new Event('scroll', {bubbles: true}));
              } catch (err) {}
            }
            try { window.scrollBy(0, 520); } catch (err) {}
            """
        )
        time.sleep(0.4)
        field = _salesforce_field_control(driver, "Subject", "input,textarea")
    if field is None:
        raise CopyrightCancelError("Salesforce Case Subject field was not found.")
    driver.execute_script(
        """
        const field = arguments[0];
        const value = arguments[1];
        field.scrollIntoView({block: 'center', inline: 'center'});
        field.focus();
        const proto = field.tagName && field.tagName.toLowerCase() === 'textarea'
          ? window.HTMLTextAreaElement.prototype
          : window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        setter.call(field, value);
        field.dispatchEvent(new Event('input', {bubbles: true}));
        field.dispatchEvent(new Event('change', {bubbles: true}));
        field.blur();
        """,
        field,
        subject,
    )
    time.sleep(0.3)


def _click_salesforce_case_save(driver):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
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
                const root = Array.from(document.querySelectorAll('[role=dialog],section,div'))
                  .filter((el) => visible(el) && /New Case/i.test(el.innerText || '') && /Subject/i.test(el.innerText || ''))
                  .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0] || document;
                const buttons = Array.from(root.querySelectorAll('button,a,[role=button],input'))
                  .filter((el) => visible(el) && clean(`${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''}`) === 'save')
                  .sort((a, b) => {
                    const ar = a.getBoundingClientRect();
                    const br = b.getBoundingClientRect();
                    return (br.bottom - ar.bottom) || (br.right - ar.right);
                  });
                if (!buttons.length) return false;
                buttons[0].scrollIntoView({block: 'center', inline: 'center'});
                buttons[0].click();
                return true;
                """
            )
        )
        if clicked:
            return True
        time.sleep(0.5)
    raise CopyrightCancelError("Salesforce Case Save button was not found.")


def _wait_for_salesforce_refund_case_saved(driver, subject, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = _visible_salesforce_text(driver)
        lower = text.lower()
        case_number_match = re.search(r"Case Number\s+([0-9]+)", text, re.I)
        saved_signal = bool(case_number_match) or "case created" in lower
        if subject.lower() in lower and SALESFORCE_CASE_STATUS_REFUND_PENDING.lower() in lower and saved_signal:
            return {
                "saved": True,
                "subject": subject,
                "status": SALESFORCE_CASE_STATUS_REFUND_PENDING,
                "case_number": case_number_match.group(1) if case_number_match else "",
            }
        time.sleep(1)
    raise CopyrightCancelError("Salesforce refund-pending Case did not show as saved.")


def _create_salesforce_refund_pending_case(driver, order_id, process=COPYRIGHT_CANCEL_PROCESS, dry_run=False):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
    subject = _salesforce_refund_case_subject(process)
    if dry_run:
        return {
            "created": False,
            "dry_run": True,
            "order_lookup": str(order_id),
            "subject": subject,
            "status": SALESFORCE_CASE_STATUS_REFUND_PENDING,
            "message": "Skipped Salesforce refund-pending Case creation in dry-run mode.",
        }
    _click_salesforce_cases_new_button(driver)
    _wait_for_salesforce_new_case_form(driver)
    lookup_mode = _fill_salesforce_case_order_lookup(driver, order_id)
    if lookup_mode != "advanced":
        raise CopyrightCancelError("Salesforce Order Number lookup did not open Advanced Search.")
    selected_order = _select_salesforce_advanced_search_order(driver, order_id)
    _set_salesforce_case_status(driver)
    _fill_salesforce_case_subject(driver, subject)
    _click_salesforce_case_save(driver)
    saved = _wait_for_salesforce_refund_case_saved(driver, subject)
    return {
        "created": True,
        "dry_run": False,
        "order_lookup": str(order_id),
        "selected_order": selected_order,
        **saved,
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


def _is_stripe_payment(payment):
    payment = payment or {}
    text = " ".join(
        [
            str(payment.get("payment_type") or ""),
            str(payment.get("panel_text") or ""),
        ]
    ).lower()
    return "stripe.com" in text or "stripe" in text


def _has_positive_payment_amount(payment):
    amount = _parse_money((payment or {}).get("amount"))
    return amount > 0


def _requires_salesforce_refund_case(payment):
    return _has_positive_payment_amount(payment) and not _is_stripe_payment(payment)


def _cancel_sales_note(reason, process=COPYRIGHT_CANCEL_PROCESS):
    if process.fixed_sales_note:
        return process.fixed_sales_note
    clean_reason = _clean_text(reason)
    if not clean_reason:
        raise CopyrightCancelError(_missing_reason_error(process))
    if process.sales_note_template:
        return process.sales_note_template.format(reason=clean_reason)
    return f"{clean_reason} {process.sales_note_reason_label}\n{process.sales_note_email_line}"


def _copyright_cancel_sales_note(reason):
    return _cancel_sales_note(reason, COPYRIGHT_CANCEL_PROCESS)


def _append_copyright_cancel_sales_note(driver, reason, dry_run=False, process=COPYRIGHT_CANCEL_PROCESS):
    note = _cancel_sales_note(reason, process)
    existing = _order_scope(
        driver,
        """
        return String(r.addSalesNotes || r.salesNotes || r.filteredSalesNotes || '');
        """,
    )
    if note.lower() in str(existing or "").lower():
        return {
            "updated": False,
            "already_present": True,
            "dry_run": bool(dry_run),
            "note": note,
        }
    if dry_run:
        return {
            "updated": False,
            "already_present": False,
            "dry_run": True,
            "note": note,
            "message": "Skipped updating CRM Sales Notes in dry-run mode.",
        }
    update_result = _order_scope(
        driver,
        """
        const note = arguments[0];
        const existingDraft = String(r.addSalesNotes || '').trim();
        const alreadyPresent = existingDraft.toLowerCase().includes(note.toLowerCase());
        const after = alreadyPresent ? existingDraft : note;
        runInAngular(s, () => {
          s.editModeOn();
          r.addSalesNotes = after;
          if (s.order.setAddSalesNotes) s.order.setAddSalesNotes(r.addSalesNotes);
        });
        return {
          updated: after !== existingDraft,
          already_present: alreadyPresent,
          note: note
        };
        """,
        note,
    )
    save_result = _save_order_and_wait(driver)
    return {
        "updated": bool((update_result or {}).get("updated")),
        "already_present": bool((update_result or {}).get("already_present")),
        "dry_run": False,
        "note": note,
        "save": save_result,
    }


def _normalize_order_status_text(value):
    return re.sub(r"[\s\-]+", " ", _clean_text(value).lower()).strip()


def _order_status_matches(value, expected):
    return _normalize_order_status_text(value) == _normalize_order_status_text(expected)


def _read_order_status_summary(driver):
    return _order_scope(
        driver,
        """
        const values = [
          s.orderStatusName,
          s.statusName,
          r.orderStatusName,
          r.statusName,
          (r.orderStatus || {}).statusName,
          ((r.orderStatuses || [])[0] || {}).statusName,
          ((r.status || [])[0] || {}).statusName
        ];
        const history = [];
        for (const rows of [r.orderStatuses, r.status, r.statusHistory, r.orderStatusHistory]) {
          if (!Array.isArray(rows)) continue;
          for (const row of rows) {
            history.push(row.statusName || row.name || row.status || '');
          }
        }
        return {values, history};
        """,
    )


def _order_status_values(driver):
    summary = _read_order_status_summary(driver) or {}
    return [
        _clean_text(value)
        for value in (summary.get("values", []) + summary.get("history", []))
        if _clean_text(value)
    ]


def _order_status_already_applied(driver, status_text):
    return any(_order_status_matches(value, status_text) for value in _order_status_values(driver))


def _body_text_confirms_order_status(body_text, status_text):
    clean = _clean_text(body_text)
    escaped = re.escape(status_text).replace(r"\ ", r"\s+").replace(r"\-", r"\s*-\s*")
    return bool(
        re.search(rf"Order's status updated to include\s+{escaped}", clean, flags=re.IGNORECASE)
        or re.search(rf"\bStatus:\s*{escaped}\b", clean, flags=re.IGNORECASE)
    )


def _click_order_status_apply(driver):
    if _click_ng_button(driver, "updateOrderStatus();", "apply"):
        return True
    return bool(
        driver.execute_script(
            """
            function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
            function visible(el) {
              if (!el) return false;
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
              return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            }
            const controls = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]')).filter(visible);
            const apply = controls.find((el) => (el.getAttribute('ng-click') || '') === 'updateOrderStatus();')
              || controls.find((el) => clean(el.innerText || el.value).toLowerCase() === 'apply');
            if (!apply) return false;
            apply.scrollIntoView({block: 'center', inline: 'center'});
            apply.click();
            return true;
            """
        )
    )


def _apply_order_status(driver, status_text, dry_run=False):
    status_text = _clean_text(status_text)
    if _order_status_already_applied(driver, status_text):
        return {
            "status_applied": False,
            "already_applied": True,
            "dry_run": bool(dry_run),
            "status": status_text,
        }
    if dry_run:
        return {
            "status_applied": False,
            "already_applied": False,
            "dry_run": True,
            "status": status_text,
            "message": f"Skipped applying {status_text} in dry-run mode.",
        }

    result = driver.execute_script(
        """
        function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
        function visible(el) {
          if (!el) return false;
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        function emit(el, name) {
          el.dispatchEvent(new Event(name, {bubbles: true}));
        }
        const statusText = String(arguments[0] || '').trim();
        const inputs = Array.from(document.querySelectorAll('input[type=text], input:not([type]), textarea')).filter(visible);
        const statusInput = inputs.find((input) => input.getAttribute('ng-model') === 'orderStatusName') || inputs.find((input) => {
          const rect = input.getBoundingClientRect();
          const nearby = Array.from(document.querySelectorAll('body *')).some((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || el.textContent);
            if (!/^Status:/i.test(text)) return false;
            const other = el.getBoundingClientRect();
            return Math.abs(other.top - rect.top) < 140 && Math.abs(other.left - rect.left) < 700;
          });
          return nearby || rect.top < 350;
        }) || inputs[0];
        if (!statusInput) return {success: false, message: 'Order status input was not found.'};
        statusInput.scrollIntoView({block: 'center', inline: 'center'});
        statusInput.focus();
        statusInput.value = statusText;
        emit(statusInput, 'input');
        emit(statusInput, 'change');
        statusInput.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: statusText.slice(-1) || 't'}));
        statusInput.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'ArrowDown'}));
        statusInput.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'ArrowDown'}));
        return {success: true, typed: true};
        """,
        status_text,
    )
    if not isinstance(result, dict) or not result.get("success"):
        raise CopyrightCancelError((result or {}).get("message") or f"Could not type {status_text} status.")

    time.sleep(1)
    driver.execute_script(
        """
        function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase(); }
        function visible(el) {
          if (!el) return false;
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        const expected = clean(arguments[0]);
        const option = Array.from(document.querySelectorAll('li,a,button,div,span'))
          .filter(visible)
          .find((el) => clean(el.innerText || el.textContent) === expected);
        if (option) option.click();
        """,
        status_text,
    )
    time.sleep(0.3)
    if not _click_order_status_apply(driver):
        raise CopyrightCancelError(f"{status_text} apply button was not found.")

    deadline = time.monotonic() + 35
    last_statuses = []
    last_text = ""
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            last_statuses = _order_status_values(driver)
            if any(_order_status_matches(value, status_text) for value in last_statuses):
                return {
                    "status_applied": True,
                    "already_applied": False,
                    "dry_run": False,
                    "status": status_text,
                    "confirmation": "order_scope",
                }
        except Exception:
            pass
        try:
            last_text = driver.execute_script("return document.body ? document.body.innerText : '';")
            if _body_text_confirms_order_status(last_text, status_text):
                return {
                    "status_applied": True,
                    "already_applied": False,
                    "dry_run": False,
                    "status": status_text,
                    "confirmation": "page_text",
                }
        except Exception:
            pass
    detail = f" Last status seen: {', '.join(last_statuses[:5])}." if last_statuses else ""
    raise CopyrightCancelError(
        f"{status_text} status was not confirmed after Apply.{detail} "
        f"Visible text starts: {_clean_text(last_text)[:300]}"
    )


def _prepare_no_cancel_crm_action(driver, reason, dry_run=False, process=COMPLICATED_EMB_TO_HDD_PROCESS):
    sales_note_result = _append_copyright_cancel_sales_note(driver, reason, dry_run=dry_run, process=process)
    status_result = None
    if process.key == COPYRIGHT_REACHOUT_PROCESS.key:
        status_result = _apply_order_status(driver, COPYRIGHT_REACHOUT_CRM_STATUS, dry_run=dry_run)
    return {
        "sales_note": sales_note_result,
        "order_status": status_result,
        "cancel": {
            "cancelled": False,
            "skipped": True,
            "message": f"{process.display_name} does not cancel the CRM order.",
        },
        "refund_fee": {
            "added": False,
            "skipped": True,
            "message": f"{process.display_name} does not add a refund fee.",
        },
        "refund": {
            "refunded": False,
            "skipped": True,
            "refund_button_clicked": False,
            "message": f"{process.display_name} does not refund the payment.",
        },
    }


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
    return _refund_fee_amount_from_order_state(state)


def _refund_fee_amount_from_order_state(state):
    # Refund fee should offset the pre-tax customer charge: item subtotal plus shipping, excluding sales tax.
    subtotal = _parse_money(state.get("subtotal"))
    shipping = _parse_money(state.get("shipping_charges")).copy_abs()
    amount = (subtotal.copy_abs() + shipping).quantize(Decimal("0.01"))
    if amount <= 0:
        raise CopyrightCancelError(
            "Could not determine a positive CRM subtotal for the Refund fee. "
            f"Subtotal: {_money_text(subtotal)}; shipping: {_money_text(shipping)}."
        )
    return amount


def _zero_charge_cancel_refund_result(dry_run=False):
    return {
        "refund_fee": {
            "added": False,
            "skipped": True,
            "dry_run": bool(dry_run),
            "amount": "0.00",
            "reason": "no_refundable_customer_charge",
            "message": "Skipped Refund fee because the order has no refundable customer charge.",
        },
        "refund": {
            "refunded": False,
            "skipped": True,
            "dry_run": bool(dry_run),
            "prepared": False,
            "refund_button_clicked": False,
            "reason": "no_refundable_customer_charge",
            "message": "Skipped payment refund because the order has no refundable customer charge.",
        },
    }


def _crm_order_already_cancelled(driver):
    try:
        status_summary = _order_scope(
            driver,
            """
            const values = [
              s.orderStatusName,
              s.statusName,
              r.orderStatusName,
              r.statusName,
              (r.orderStatus || {}).statusName,
              ((r.orderStatuses || [])[0] || {}).statusName,
              ((r.status || [])[0] || {}).statusName
            ];
            const history = [];
            for (const rows of [r.orderStatuses, r.status, r.statusHistory, r.orderStatusHistory]) {
              if (!Array.isArray(rows)) continue;
              for (const row of rows) {
                history.push(row.statusName || row.name || row.status || '');
              }
            }
            return {values, history};
            """,
        ) or {}
    except Exception:
        status_summary = {}
    statuses = [
        _clean_text(value).lower()
        for value in ((status_summary.get("values") or []) + (status_summary.get("history") or []))
        if _clean_text(value)
    ]
    if any(status in {"cancelled", "canceled", "cancel order", "order cancelled", "order canceled"} for status in statuses):
        return True
    try:
        text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';")).lower()
    except Exception:
        text = ""
    return bool(re.search(r"\b(?:order\s+)?status\s*[:\-]?\s*(?:cancelled|canceled)\b", text))


def _completed_stripe_refund_state(driver):
    try:
        state = _get_order_live_state(driver)
    except Exception:
        return {"refunded": False}
    transactions = state.get("transactions") or []
    stripe_total = Decimal("0.00")
    refund_total = Decimal("0.00")
    for transaction in transactions:
        label = _clean_text(f"{transaction.get('tag', '')} {transaction.get('type', '')}").lower()
        amount = _parse_money(transaction.get("amount"))
        if amount > 0 and "stripe" in label:
            stripe_total += amount
        if amount < 0 and "refund" in label:
            refund_total += amount.copy_abs()
    refunded = bool(stripe_total > 0 and refund_total >= stripe_total)
    return {
        "refunded": refunded,
        "stripe_total": _money_text(stripe_total),
        "refund_total": _money_text(refund_total),
        "order_state": state,
    }


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


def _cancel_and_refund_crm_order(driver, crm_handle, order_id, dry_run, click_refund_button=True, payment=None, reason="", process=COPYRIGHT_CANCEL_PROCESS):
    driver.switch_to.window(crm_handle)
    _activate_crm_context(driver)
    _wait_for_order_scope(driver, order_id=order_id)
    sales_note_result = _append_copyright_cancel_sales_note(driver, reason, dry_run=dry_run, process=process)
    payment = payment or _read_payment_summary(driver)
    completed_refund = _completed_stripe_refund_state(driver)
    if not dry_run and completed_refund.get("refunded"):
        return {
            "payment": payment,
            "sales_note": sales_note_result,
            "cancel": {
                "cancelled": False,
                "skipped": True,
                "dry_run": False,
                "reason": "already_refunded",
                "message": "Skipped CRM cancel/refund duplicate work because a matching Stripe refund is already recorded.",
            },
            "refund_fee": {
                "added": False,
                "skipped": True,
                "dry_run": False,
                "reason": "already_refunded",
                "message": "Skipped adding Refund fee because the order already has a completed Stripe refund.",
            },
            "refund": {
                "refunded": True,
                "already_refunded": True,
                "dry_run": False,
                "prepared": False,
                "refund_button_clicked": False,
                **completed_refund,
            },
        }
    order_state = _get_order_live_state(driver)
    try:
        refund_fee_amount = _refund_fee_amount_from_order_state(order_state)
    except CopyrightCancelError:
        refund_fee_amount = Decimal("0.00")
    if refund_fee_amount <= 0:
        if dry_run:
            cancel_result = {"cancelled": False, "dry_run": True, "message": "Skipped order cancellation in dry-run mode."}
        elif _crm_order_already_cancelled(driver):
            cancel_result = {
                "cancelled": False,
                "skipped": True,
                "dry_run": False,
                "reason": "already_cancelled",
                "message": "Skipped CRM cancellation because the order is already cancelled.",
            }
        else:
            _cancel_original_order(driver)
            cancel_result = {"cancelled": True, "dry_run": False}
        zero_charge = _zero_charge_cancel_refund_result(dry_run=dry_run)
        return {
            "payment": payment,
            "sales_note": sales_note_result,
            "cancel": cancel_result,
            "refund_fee": zero_charge["refund_fee"],
            "refund": zero_charge["refund"],
        }
    if dry_run:
        cancel_result = {"cancelled": False, "dry_run": True, "message": "Skipped order cancellation in dry-run mode."}
        refund_fee_result = {
            "added": False,
            "dry_run": True,
            "amount": _money_text(refund_fee_amount),
            "message": "Skipped adding Refund fee in dry-run mode.",
        }
        if _requires_salesforce_refund_case(payment):
            refund_result = {
                "refunded": False,
                "dry_run": True,
                "prepared": False,
                "refund_button_clicked": False,
                "case_required": True,
                "method": "salesforce_case",
                "message": "Skipped Salesforce refund-pending Case creation in dry-run mode.",
            }
        else:
            refund_result = {"refunded": False, "dry_run": True, "message": "Skipped refund modal in dry-run mode."}
        return {
            "payment": payment,
            "sales_note": sales_note_result,
            "cancel": cancel_result,
            "refund_fee": refund_fee_result,
            "refund": refund_result,
        }
    else:
        if _crm_order_already_cancelled(driver):
            cancel_result = {
                "cancelled": False,
                "skipped": True,
                "dry_run": False,
                "reason": "already_cancelled",
                "message": "Skipped CRM cancellation because the order is already cancelled.",
            }
        else:
            _cancel_original_order(driver)
            cancel_result = {"cancelled": True, "dry_run": False}
        refund_fee_result = _add_refund_fee_to_original(driver, refund_fee_amount)
    if _requires_salesforce_refund_case(payment):
        refund_result = {
            "refunded": False,
            "dry_run": False,
            "prepared": True,
            "refund_button_clicked": False,
            "case_required": True,
            "method": "salesforce_case",
            "message": "Non-Stripe payment requires a Salesforce refund-pending Case.",
        }
        return {
            "payment": payment,
            "sales_note": sales_note_result,
            "cancel": cancel_result,
            "refund_fee": refund_fee_result,
            "refund": refund_result,
        }
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
    return {
        "payment": payment,
        "sales_note": sales_note_result,
        "cancel": cancel_result,
        "refund_fee": refund_fee_result,
        "refund": refund_result,
    }


def send_salesforce_email_single_order(
    order_id,
    dry_run=True,
    process=COPYRIGHT_CANCEL_PROCESS,
    reason="",
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    keep_browser_open=False,
    keep_browser_open_on_error=False,
    skip_from_selection=False,
    skip_ready_verify=False,
):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
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
        contact = _wait_for_crm_contact_info(driver, order_id=order_id)
        salesforce = _prepare_and_maybe_send_salesforce_email(
            driver,
            crm_handle,
            order_id,
            contact["email"],
            dry_run=dry_run,
            process=process,
            reason=reason,
            login_wait_seconds=login_wait_seconds,
            skip_from_selection=skip_from_selection,
            skip_ready_verify=skip_ready_verify,
        )
        return {
            "order_id": order_id,
            "order_url": order_url,
            "dry_run": bool(dry_run),
            "process": process.key,
            "issue_type": process.issue_type,
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
    process=COPYRIGHT_CANCEL_PROCESS,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    click_refund_button=True,
    refund_fee_amount=None,
    keep_browser_open=False,
    keep_browser_open_on_error=False,
):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
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
        payment = _read_payment_summary(driver)
        if click_refund_button and _has_positive_payment_amount(payment) and _is_stripe_payment(payment):
            _validate_refundable_stripe_payment(payment)
        refund_fee_amount = _parse_money(refund_fee_amount).copy_abs() if refund_fee_amount not in (None, "") else _read_order_refund_fee_amount(driver)
        refund_case = None
        if dry_run:
            refund_fee = {
                "added": False,
                "dry_run": True,
                "amount": _money_text(refund_fee_amount),
                "message": "Skipped adding Refund fee in dry-run mode.",
            }
            if _requires_salesforce_refund_case(payment):
                refund = {
                    "refunded": False,
                    "dry_run": True,
                    "prepared": False,
                    "refund_button_clicked": False,
                    "case_required": True,
                    "method": "salesforce_case",
                    "message": "Skipped Salesforce refund-pending Case creation in dry-run mode.",
                }
                refund_case = _create_salesforce_refund_pending_case(driver, order_id, process=process, dry_run=True)
            else:
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
            if _requires_salesforce_refund_case(payment):
                refund = {
                    "refunded": False,
                    "dry_run": False,
                    "prepared": True,
                    "refund_button_clicked": False,
                    "case_required": True,
                    "method": "salesforce_case",
                    "message": "Non-Stripe payment requires a Salesforce refund-pending Case.",
                }
                if click_refund_button:
                    contact = _wait_for_crm_contact_info(driver, order_id=order_id)
                    sf_handle = _open_salesforce_account(
                        driver,
                        crm_handle,
                        contact["email"],
                        login_wait_seconds=login_wait_seconds,
                        order_id=order_id,
                    )
                    _verify_salesforce_email(driver, contact["email"])
                    refund_case = _create_salesforce_refund_pending_case(
                        driver,
                        order_id,
                        process=process,
                        dry_run=False,
                    )
                    refund_case["salesforce_handle"] = sf_handle
                    refund_case["email"] = contact["email"]
                else:
                    refund_case = {
                        "created": False,
                        "dry_run": False,
                        "order_lookup": str(order_id),
                        "subject": _salesforce_refund_case_subject(process),
                        "status": SALESFORCE_CASE_STATUS_REFUND_PENDING,
                        "message": "Skipped Salesforce refund-pending Case creation because refund click/action was skipped.",
                    }
            else:
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
            "process": process.key,
            "issue_type": process.issue_type,
            "payment": payment,
            "refund_fee": refund_fee,
            "refund": refund,
            "salesforce_refund_case": refund_case,
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
    reason,
    dry_run=True,
    process=COPYRIGHT_CANCEL_PROCESS,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    click_refund_button=True,
    keep_browser_open=False,
    keep_browser_open_on_error=False,
):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
    if process.key == AUTO_SPLITTER_PROCESS.key:
        return process_auto_splitter_order(
            order_id,
            dry_run=dry_run,
            visible=visible,
            attach_browser=attach_browser,
            debugger_address=debugger_address,
            login_wait_seconds=login_wait_seconds,
        )
    if process.key == MANUAL_STOCK_ORDER_PROCESS.key:
        return process_manual_stock_order(
            order_id,
            dry_run=dry_run,
            visible=visible,
        )
    order_id = _normalize_order_id(order_id)
    _cancel_sales_note(reason, process)
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
        contact = _wait_for_crm_contact_info(driver, order_id=order_id)
        if not process.cancel_and_refund:
            crm_action = _prepare_no_cancel_crm_action(
                driver,
                reason,
                dry_run=dry_run,
                process=process,
            )
            salesforce = _prepare_and_maybe_send_salesforce_email(
                driver,
                crm_handle,
                order_id,
                contact["email"],
                dry_run=dry_run,
                process=process,
                reason=reason,
                login_wait_seconds=login_wait_seconds,
            )
            return {
                "order_id": order_id,
                "order_url": order_url,
                "dry_run": bool(dry_run),
                "process": process.key,
                "issue_type": process.issue_type,
                "contact": contact,
                "salesforce": salesforce,
                "crm_action": crm_action,
                "salesforce_refund_case": None,
                "post_cancel_stock": {
                    "action": "skipped",
                    "message": f"{process.display_name} does not use post-cancel stock routing.",
                },
            }
        payment = _read_payment_summary(driver)
        if not dry_run and click_refund_button and _has_positive_payment_amount(payment) and _is_stripe_payment(payment):
            _validate_refundable_stripe_payment(payment)
        crm_action = _cancel_and_refund_crm_order(
            driver,
            crm_handle,
            order_id,
            dry_run=dry_run,
            click_refund_button=click_refund_button,
            payment=payment,
            reason=reason,
            process=process,
        )
        salesforce = _prepare_and_maybe_send_salesforce_email(
            driver,
            crm_handle,
            order_id,
            contact["email"],
            dry_run=dry_run,
            process=process,
            reason=reason,
            login_wait_seconds=login_wait_seconds,
        )
        refund_case = None
        refund_state = crm_action.get("refund") or {}
        if refund_state.get("case_required"):
            if not click_refund_button:
                refund_case = {
                    "created": False,
                    "dry_run": bool(dry_run),
                    "order_lookup": str(order_id),
                    "subject": _salesforce_refund_case_subject(process),
                    "status": SALESFORCE_CASE_STATUS_REFUND_PENDING,
                    "message": "Skipped Salesforce refund-pending Case creation because refund click/action was skipped.",
                }
            else:
                refund_case = _create_salesforce_refund_pending_case(
                    driver,
                    order_id,
                    process=process,
                    dry_run=dry_run,
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
            "process": process.key,
            "issue_type": process.issue_type,
            "contact": contact,
            "salesforce": salesforce,
            "crm_action": crm_action,
            "salesforce_refund_case": refund_case,
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


def post_cancel_stock_slack_single_order(
    order_id,
    dry_run=True,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
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
        post_cancel_stock = _handle_post_cancel_stock_return(
            driver,
            crm_handle,
            order_id,
            order_url,
            dry_run=dry_run,
            enabled=True,
        )
        return {
            "order_id": order_id,
            "order_url": order_url,
            "dry_run": bool(dry_run),
            "post_cancel_stock": post_cancel_stock,
        }
    except Exception:
        had_error = True
        if driver is not None:
            safe_take_screenshot(driver, f"copyright_cancel_{order_id}_post_cancel_stock_error")
        raise
    finally:
        should_keep_open = keep_browser_open or (keep_browser_open_on_error and had_error)
        if driver is not None and not attach_browser and should_keep_open:
            _retain_browser_for_inspection(driver)
        elif driver is not None and not attach_browser:
            safe_driver_quit(driver, profile_path=_profile_path())


def create_salesforce_refund_case_single_order(
    order_id,
    process=COPYRIGHT_CANCEL_PROCESS,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    keep_browser_open=False,
    keep_browser_open_on_error=False,
    force_case=False,
):
    process = _cancel_process_for_key(process) if isinstance(process, str) else process
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
        contact = _wait_for_crm_contact_info(driver, order_id=order_id)
        payment = _read_payment_summary(driver)
        if not force_case and not _requires_salesforce_refund_case(payment):
            raise CopyrightCancelError("Current payment method does not require a Salesforce refund-pending Case.")
        sf_handle = _open_salesforce_account(
            driver,
            crm_handle,
            contact["email"],
            login_wait_seconds=login_wait_seconds,
            order_id=order_id,
        )
        _verify_salesforce_email(driver, contact["email"])
        refund_case = _create_salesforce_refund_pending_case(
            driver,
            order_id,
            process=process,
            dry_run=False,
        )
        refund_case["salesforce_handle"] = sf_handle
        refund_case["email"] = contact["email"]
        return {
            "order_id": order_id,
            "order_url": order_url,
            "dry_run": False,
            "process": process.key,
            "issue_type": process.issue_type,
            "contact": contact,
            "payment": payment,
            "eligibility_overridden": bool(force_case),
            "salesforce_refund_case": refund_case,
        }
    except Exception:
        had_error = True
        if driver is not None:
            safe_take_screenshot(driver, f"copyright_cancel_{order_id}_refund_case_error")
        raise
    finally:
        should_keep_open = keep_browser_open or (keep_browser_open_on_error and had_error)
        if driver is not None and not attach_browser and should_keep_open:
            _retain_browser_for_inspection(driver)
        elif driver is not None and not attach_browser:
            safe_driver_quit(driver, profile_path=_profile_path())


def run_create_refund_case_order(args):
    started = time.monotonic()
    order_ref = args.order_id or args.order_url
    try:
        details = create_salesforce_refund_case_single_order(
            order_ref,
            process=args.process,
            visible=args.visible,
            attach_browser=args.attach_browser,
            debugger_address=args.debugger_address,
            login_wait_seconds=args.login_wait_seconds,
            keep_browser_open=args.keep_browser_open,
            keep_browser_open_on_error=args.keep_browser_open_on_error,
            force_case=args.force_case,
        )
        _write_result(
            True,
            f"Salesforce refund-pending Case recovery complete for order {details['order_id']}.",
            result_file=args.result_file,
            action="create_refund_case_order",
            duration_seconds=round(time.monotonic() - started, 2),
            **details,
        )
        _hold_browser_after_result_if_requested(args)
        return 0
    except Exception as exc:
        _write_result(
            False,
            f"Salesforce refund-pending Case recovery failed: {exc}",
            result_file=args.result_file,
            action="create_refund_case_order",
            dry_run=False,
            order_reference=order_ref,
            error_type=type(exc).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        _hold_browser_after_result_if_requested(args)
        return 1


def run_scan_sheet(result_file=None, include_error_rows=False):
    try:
        spreadsheet, worksheet, headers, eligible, skipped = _scan_queue_rows(include_error_rows=include_error_rows)
    except Exception as exc:
        _write_result(
            False,
            f"Sheet scanner could not read the Google Sheet: {exc}",
            result_file=result_file,
            action="scan_sheet",
            error_type=type(exc).__name__,
            eligible_rows=[],
            skipped_rows=[],
        )
        return 1

    # A scan is strictly read-only.  In particular, do not fill the ERROR
    # column for rows with a missing reason; the operator may only be
    # inspecting the queue.
    missing_reason_error_count = 0
    payload = {
        "spreadsheet_title": spreadsheet.title,
        "worksheet_title": worksheet.title,
        "headers": headers,
        "missing_reason_error_count": missing_reason_error_count,
        "eligible_rows": [
            {
                "row_number": row.row_number,
                "order_id": row.order_id,
                "issue_type": row.issue_type,
                "process": row.process_key,
                "reason": row.reason,
                "error": row.error,
                "order_url": row.order_url,
            }
            for row in eligible
        ],
        "skipped_rows": skipped,
    }
    _write_result(
        True,
        f"Found {len(eligible)} eligible sheet scanner row(s) in Google Sheet.",
        result_file=result_file,
        action="scan_sheet",
        **payload,
    )
    return 0


def _run_auto_splitter_once(order_id, dry_run, result_file, visible=False, attach_browser=False, debugger_address="127.0.0.1:9222", login_wait_seconds=0, tab_count=None, divisions=None):
    exit_code = _run_auto_split_order(
        order_id=order_id,
        expected_tab_count=tab_count,
        divisions=divisions,
        login_wait_seconds=login_wait_seconds,
        attach_browser=attach_browser,
        debugger_address=debugger_address,
        dry_run=bool(dry_run),
        visible=visible,
        result_file=result_file,
        parallel_workers=1,
    )
    try:
        with open(result_file, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
    except Exception as exc:
        raise CopyrightCancelError(f"Auto Splitter did not write a readable result payload: {exc}") from exc
    payload = payload if isinstance(payload, dict) else {}
    if exit_code != 0 or not payload.get("success"):
        raise CopyrightCancelError(payload.get("message") or f"Auto Splitter exited with code {exit_code}.")
    return payload


def process_auto_splitter_order(
    order_id,
    dry_run=True,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
):
    order_id = _normalize_order_id(order_id)
    tmp = tempfile.NamedTemporaryFile(prefix="auto_splitter_sheet_", suffix=".json", delete=False)
    result_file = tmp.name
    tmp.close()
    try:
        preflight = _run_auto_splitter_once(
            order_id,
            True,
            result_file,
            visible=visible,
            attach_browser=attach_browser,
            debugger_address=debugger_address,
            login_wait_seconds=login_wait_seconds,
        )
        if dry_run:
            return {
                "order_id": order_id,
                "order_url": PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id),
                "dry_run": True,
                "process": AUTO_SPLITTER_PROCESS.key,
                "issue_type": AUTO_SPLITTER_PROCESS.issue_type,
                "auto_splitter": preflight,
                "preflight_dry_run": preflight,
            }
        live_payload = _run_auto_splitter_once(
            order_id,
            False,
            result_file,
            visible=visible,
            attach_browser=attach_browser,
            debugger_address=debugger_address,
            login_wait_seconds=login_wait_seconds,
            tab_count=preflight.get("expected_tab_count") or preflight.get("detected_tab_count"),
            divisions=preflight.get("divisions"),
        )
        live_payload["preflight_dry_run"] = preflight
        return {
            "order_id": order_id,
            "order_url": PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id),
            "dry_run": False,
            "process": AUTO_SPLITTER_PROCESS.key,
            "issue_type": AUTO_SPLITTER_PROCESS.issue_type,
            "auto_splitter": live_payload,
            "preflight_dry_run": preflight,
            "new_order_ids": live_payload.get("new_order_ids", []),
        }
    finally:
        try:
            os.remove(result_file)
        except OSError:
            pass


def process_manual_stock_order(
    order_id,
    dry_run=True,
    visible=False,
):
    order_id = _normalize_order_id(order_id)
    tmp = tempfile.NamedTemporaryFile(prefix="shipping_bypasser_sheet_", suffix=".json", delete=False)
    result_file = tmp.name
    tmp.close()
    try:
        exit_code = _run_shipping_bypasser(
            action="shipping_bypass_single",
            dry_run=bool(dry_run),
            result_file=result_file,
            visible=visible,
            order_id=order_id,
        )
        try:
            with open(result_file, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except Exception as exc:
            raise CopyrightCancelError(f"Shipping Bypasser did not write a readable result payload: {exc}") from exc
        payload = payload if isinstance(payload, dict) else {}
        if exit_code != 0 or not payload.get("success"):
            raise CopyrightCancelError(payload.get("message") or f"Shipping Bypasser exited with code {exit_code}.")
        return {
            "order_id": order_id,
            "order_url": PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id),
            "dry_run": bool(dry_run),
            "process": MANUAL_STOCK_ORDER_PROCESS.key,
            "issue_type": MANUAL_STOCK_ORDER_PROCESS.issue_type,
            "shipping_bypasser": payload,
        }
    finally:
        try:
            os.remove(result_file)
        except OSError:
            pass


def _hold_browser_after_result_if_requested(args):
    if getattr(args, "keep_browser_open", False) or getattr(args, "keep_browser_open_on_error", False):
        _hold_retained_browsers_for_inspection()


def run_process_order(args):
    started = time.monotonic()
    order_ref = args.order_id or args.order_url
    try:
        details = process_single_order(
            order_ref,
            args.reason,
            dry_run=args.dry_run,
            process=args.process,
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
            f"Sheet scanner {'dry run' if args.dry_run else 'automation'} complete for order {details['order_id']}.",
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
            f"Sheet scanner order failed: {exc}",
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
            process=args.process,
            reason=args.reason,
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
            process=args.process,
            visible=args.visible,
            attach_browser=args.attach_browser,
            debugger_address=args.debugger_address,
            login_wait_seconds=args.login_wait_seconds,
            click_refund_button=not args.skip_refund_click,
            refund_fee_amount=args.refund_fee_amount,
            keep_browser_open=args.keep_browser_open,
            keep_browser_open_on_error=args.keep_browser_open_on_error,
        )
        cleared_sheet_queue_row = False
        if args.delete_sheet_row and not args.dry_run and not args.skip_refund_click:
            spreadsheet, worksheet, headers, eligible, _skipped = _scan_queue_rows(include_error_rows=True)
            for row in sorted(eligible, key=lambda item: item.row_number, reverse=True):
                if row.order_id == details["order_id"]:
                    _clear_sheet_queue_row(worksheet, row.row_number)
                    cleared_sheet_queue_row = True
                    details["cleared_sheet_queue_row_number"] = row.row_number
                    details["spreadsheet_title"] = spreadsheet.title
                    details["worksheet_title"] = worksheet.title
                    break
        _write_result(
            True,
            f"Copyright-cancel refund {'dry run' if args.dry_run else 'recovery'} complete for order {details['order_id']}.",
            result_file=args.result_file,
            action="refund_order",
            duration_seconds=round(time.monotonic() - started, 2),
            cleared_sheet_queue_row=cleared_sheet_queue_row,
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


def run_post_cancel_stock_slack_order(args):
    started = time.monotonic()
    order_ref = args.order_id or args.order_url
    try:
        details = post_cancel_stock_slack_single_order(
            order_ref,
            dry_run=args.dry_run,
            visible=args.visible,
            attach_browser=args.attach_browser,
            debugger_address=args.debugger_address,
            login_wait_seconds=args.login_wait_seconds,
            keep_browser_open=args.keep_browser_open,
            keep_browser_open_on_error=args.keep_browser_open_on_error,
        )
        _write_result(
            True,
            f"Copyright-cancel post-cancel stock Slack {'dry run' if args.dry_run else 'recovery'} complete for order {details['order_id']}.",
            result_file=args.result_file,
            action="post_cancel_stock_slack_order",
            duration_seconds=round(time.monotonic() - started, 2),
            **details,
        )
        _hold_browser_after_result_if_requested(args)
        return 0
    except Exception as exc:
        _write_result(
            False,
            f"Copyright-cancel post-cancel stock Slack recovery failed: {exc}",
            result_file=args.result_file,
            action="post_cancel_stock_slack_order",
            dry_run=bool(args.dry_run),
            order_reference=order_ref,
            error_type=type(exc).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        _hold_browser_after_result_if_requested(args)
        return 1


def run_process_queue(args):
    started = time.monotonic()
    try:
        spreadsheet, worksheet, headers, eligible, skipped = _scan_queue_rows(include_error_rows=args.retry_errors)
    except Exception as exc:
        _write_result(
            False,
            f"Sheet scanner could not read the Google Sheet: {exc}",
            result_file=args.result_file,
            action="process_queue",
            dry_run=bool(args.dry_run),
            error_type=type(exc).__name__,
            processed=[],
            failures=[],
            skipped_rows=[],
            duration_seconds=round(time.monotonic() - started, 2),
        )
        _hold_browser_after_result_if_requested(args)
        return 1

    # Dry runs must not modify the queue, including validation feedback in
    # the ERROR column.  Live runs retain the existing operator feedback.
    missing_reason_error_count = 0
    if not args.dry_run:
        missing_reason_error_count = _write_missing_reason_errors(worksheet, headers, skipped)
    limit = int(args.limit or 0)
    if limit > 0:
        eligible = eligible[:limit]
    processed = []
    failures = []
    total_orders = len(eligible)
    _publish_status(
        "No eligible Sheets Scanner orders found." if total_orders == 0 else f"Found {total_orders} eligible Sheets Scanner order(s).",
        stage="scanned",
        current=0,
        total=total_orders,
    )
    # Clear queue cells only; keep any operator instructions in later columns fixed.
    for row in sorted(eligible, key=lambda item: item.row_number, reverse=True):
        current_order = len(processed) + len(failures) + 1
        _publish_status(
            f"Processing Sheets Scanner order {row.order_id} ({current_order}/{total_orders}).",
            stage="processing_order",
            current=current_order,
            total=total_orders,
            order_id=row.order_id,
        )
        try:
            if row.process_key == AUTO_SPLITTER_PROCESS.key:
                details = process_auto_splitter_order(
                    row.order_id,
                    dry_run=args.dry_run,
                    visible=args.visible,
                    attach_browser=args.attach_browser,
                    debugger_address=args.debugger_address,
                    login_wait_seconds=args.login_wait_seconds,
                )
            elif row.process_key == MANUAL_STOCK_ORDER_PROCESS.key:
                details = process_manual_stock_order(
                    row.order_id,
                    dry_run=args.dry_run,
                    visible=args.visible,
                )
            else:
                details = process_single_order(
                    row.order_id,
                    row.reason,
                    dry_run=args.dry_run,
                    process=row.process_key,
                    visible=args.visible,
                    attach_browser=args.attach_browser,
                    debugger_address=args.debugger_address,
                    login_wait_seconds=args.login_wait_seconds,
                    click_refund_button=not args.skip_refund_click,
                    keep_browser_open=args.keep_browser_open,
                    keep_browser_open_on_error=args.keep_browser_open_on_error,
                )
            processed.append(
                {
                    "row_number": row.row_number,
                    "order_id": row.order_id,
                    "issue_type": row.issue_type,
                    "reason": row.reason,
                    **details,
                }
            )
            process = row.process
            should_delete_row = not args.dry_run and (
                row.process_key == AUTO_SPLITTER_PROCESS.key
                or row.process_key == MANUAL_STOCK_ORDER_PROCESS.key
                or not process.cancel_and_refund
                or not args.skip_refund_click
            )
            if should_delete_row:
                _clear_sheet_queue_row(worksheet, row.row_number)
        except Exception as exc:
            error_text = str(exc)
            failures.append(
                {
                    "row_number": row.row_number,
                    "order_id": row.order_id,
                    "issue_type": row.issue_type,
                    "process": row.process_key,
                    "reason": row.reason,
                    "error": error_text,
                    "error_type": type(exc).__name__,
                }
            )
            if not args.dry_run:
                _write_sheet_error(worksheet, headers, row.row_number, error_text)
    ok = not failures
    message = (
        f"Processed {len(processed)} sheet scanner row(s); {len(failures)} failed."
        if eligible
        else "No eligible sheet scanner rows found."
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
        missing_reason_error_count=missing_reason_error_count,
        duration_seconds=round(time.monotonic() - started, 2),
    )
    _hold_browser_after_result_if_requested(args)
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="CRM sheet scanner automation worker.")
    parser.add_argument(
        "--action",
        choices=[
            "scan_sheet",
            "process_queue",
            "process_order",
            "send_email_order",
            "refund_order",
            "post_cancel_stock_slack_order",
            "create_refund_case_order",
        ],
        default="scan_sheet",
    )
    parser.add_argument("--order-id", default="")
    parser.add_argument("--order-url", default="")
    parser.add_argument("--reason", default="", help="Required for full cancellation processing; written to CRM Sales Notes.")
    parser.add_argument("--process", choices=sorted(CANCEL_PROCESSES_BY_KEY), default=COPYRIGHT_CANCEL_PROCESS.key)
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
    parser.add_argument(
        "--force-case",
        action="store_true",
        help="Explicit one-order recovery override: create the Salesforce refund case even when CRM has no eligible payment row.",
    )
    parser.add_argument("--skip-from-selection", action="store_true", help="Inspection-only: do not change the Salesforce From field.")
    parser.add_argument("--skip-ready-verify", action="store_true", help="Inspection-only: do not enforce pre-send From/body verification.")
    parser.add_argument("--dry-run", action="store_true", default=PROCESSOR_DRY_RUN)
    parser.add_argument("--real", action="store_true", help="Process sheet rows live and delete successful sheet rows.")
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
    if args.action == "post_cancel_stock_slack_order":
        if not (args.order_id or args.order_url):
            _write_result(False, "--order-id or --order-url is required.", result_file=args.result_file, action=args.action)
            return 2
        return run_post_cancel_stock_slack_order(args)
    if args.action == "create_refund_case_order":
        if not (args.order_id or args.order_url):
            _write_result(False, "--order-id or --order-url is required.", result_file=args.result_file, action=args.action)
            return 2
        return run_create_refund_case_order(args)
    if args.action == "process_queue":
        return run_process_queue(args)
    _write_result(False, f"Unsupported action: {args.action}", result_file=args.result_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
