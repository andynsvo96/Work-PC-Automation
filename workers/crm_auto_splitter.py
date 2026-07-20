"""
CRM auto-splitter automation worker.

This file is safe by default:
- smoke_test only checks imports/config and optionally opens a browser.
- process_order/process_batch refuse live mode until you implement them.
- dry-run mode is the intended Mac development path.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import shutil
import sys
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse

from selenium.webdriver.common.keys import Keys

from _bootstrap import ensure_project_root_on_path

PROJECT_ROOT = ensure_project_root_on_path()

from automation_runtime import (
    RESULT_FILE,
    build_attached_chrome_driver,
    build_chrome_driver,
    configure_console_utf8,
    kill_stale_chrome,
    refresh_if_crm_challenge_attempts_exceeded,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
from runtime_paths import GENERATED_PROFILES_DIR
import config as _config
from config import (
    PROCESSOR_ACTION_TIMEOUT,
    PROCESSOR_DRY_RUN,
    PROCESSOR_HEADLESS,
    PROCESSOR_LIST_URL,
    PROCESSOR_LOGIN_URL,
    PROCESSOR_ORDER_URL_TEMPLATE,
    PROCESSOR_PAGE_LOAD_TIMEOUT,
    PROCESSOR_PROFILE_DIR,
)
import crm_product_separator as _product_separator
from slack_team import run as _run_slack_team

configure_console_utf8()

AUTOMATION_NAME = "crm.auto_splitter"
SOURCE = "crm_auto_splitter.py"
DEFAULT_MINIMUM_SPLIT_TABS = 10
DEFAULT_MAX_TABS_PER_SPLIT = DEFAULT_MINIMUM_SPLIT_TABS
AUTO_SPLITTER_SCRIPT_TIMEOUT_SECONDS = 5 * 60
ORDER_SAVE_TIMEOUT_SECONDS = 300
COPY_QUOTE_BASE_TIMEOUT_SECONDS = 90
COPY_QUOTE_SECONDS_PER_DESIGN = 6
COPY_QUOTE_MAX_TIMEOUT_SECONDS = 300
SPLIT_TOTAL_TOLERANCE = Decimal("0.01")


class SplitterError(Exception):
    """Raised when the splitter must stop before taking action."""


def _profile_path():
    if os.path.isabs(PROCESSOR_PROFILE_DIR):
        return PROCESSOR_PROFILE_DIR
    return os.path.join(PROJECT_ROOT, PROCESSOR_PROFILE_DIR)


def _normalize_parallel_workers(value, divisions=1):
    try:
        workers = int(value)
    except Exception:
        workers = 1
    workers = max(1, min(8, workers))
    try:
        divisions_count = int(divisions)
    except Exception:
        divisions_count = 1
    return max(1, min(workers, max(1, divisions_count)))


def _parallel_profile_root():
    return os.path.join(GENERATED_PROFILES_DIR, "chrome_profile_crm_auto_splitter_workers")


def _parallel_profile_path(run_id, worker_index):
    return os.path.join(_parallel_profile_root(), str(run_id), f"worker_{worker_index}")


def _clone_chrome_profile(source_profile, target_profile):
    source_abs = os.path.abspath(source_profile)
    target_abs = os.path.abspath(target_profile)
    root_abs = os.path.abspath(_parallel_profile_root())
    if not target_abs.startswith(root_abs + os.sep):
        raise SplitterError(f"Refusing to prepare worker profile outside {root_abs}.")
    if not os.path.isdir(source_abs):
        raise SplitterError(f"CRM Chrome profile was not found: {source_abs}")
    if os.path.exists(target_abs):
        shutil.rmtree(target_abs)

    ignored_exact = {
        "BrowserMetrics",
        "Crashpad",
        "Crash Reports",
        "GrShaderCache",
        "GraphiteDawnCache",
        "Safe Browsing",
        "ShaderCache",
        "SingletonCookie",
        "SingletonLock",
        "SingletonSocket",
    }
    ignored_lower = {
        "cache",
        "code cache",
        "dawncache",
        "gpucache",
        "mediacache",
        "optimization_guide_prediction_model_downloads",
    }

    def _ignore(_dir, names):
        ignored = []
        for name in names:
            lower = name.lower()
            if name in ignored_exact or lower in ignored_lower or lower.endswith(".tmp"):
                ignored.append(name)
        return ignored

    shutil.copytree(source_abs, target_abs, ignore=_ignore)
    return target_abs


def _prepare_parallel_profiles(base_profile, worker_count):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    profiles = []
    for index in range(1, int(worker_count) + 1):
        profiles.append(_clone_chrome_profile(base_profile, _parallel_profile_path(run_id, index)))
    return profiles


def _cleanup_parallel_profiles(profile_paths):
    root_abs = os.path.abspath(_parallel_profile_root())
    for profile_path in profile_paths or []:
        target_abs = os.path.abspath(profile_path)
        if target_abs.startswith(root_abs + os.sep) and os.path.exists(target_abs):
            shutil.rmtree(target_abs, ignore_errors=True)


def _build_splitter_driver(profile, visible=False):
    headless = bool(PROCESSOR_HEADLESS and not visible)
    return build_chrome_driver(
        profile,
        headless_mode=headless,
        page_load_strategy="eager",
        page_load_timeout=PROCESSOR_PAGE_LOAD_TIMEOUT,
        script_timeout=max(PROCESSOR_ACTION_TIMEOUT, AUTO_SPLITTER_SCRIPT_TIMEOUT_SECONDS),
    )


def _write_result(success, message, result_file=None, **extra_fields):
    return write_result_payload(
        AUTOMATION_NAME,
        SOURCE,
        success,
        message,
        extra_fields=extra_fields,
        result_file=result_file or RESULT_FILE,
    )


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_money(value):
    text = str(value or "").strip()
    if not text or text.lower() in {"free", "--"}:
        return Decimal("0.00")
    negative = "-" in text or "(" in text
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return Decimal("0.00")
    amount = Decimal(cleaned).quantize(Decimal("0.01"))
    return -amount if negative else amount


def _money_text(amount):
    return f"{Decimal(amount).quantize(Decimal('0.01')):.2f}"


def _signed_money_text(amount):
    value = Decimal(amount).quantize(Decimal("0.01"))
    prefix = "-" if value < 0 else ""
    return f"{prefix}${_money_text(value.copy_abs())}"


def _extract_order_id(order_id=None, order_url=None):
    if order_id:
        match = re.search(r"\d+", str(order_id))
        if match:
            return match.group(0)
    if order_url:
        match = re.search(r"/order/(\d+)", str(order_url))
        if match:
            return match.group(1)
        match = re.search(r"\b(\d{5,})\b", str(order_url))
        if match:
            return match.group(1)
    return ""


def _order_url(order_id=None, order_url=None):
    if order_url:
        parsed = urlparse(str(order_url))
        if parsed.scheme and parsed.netloc:
            return str(order_url)
    resolved_id = _extract_order_id(order_id=order_id, order_url=order_url)
    if resolved_id:
        return PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=resolved_id)
    return ""


def _format_order_list(order_numbers):
    values = [str(value) for value in order_numbers if str(value or "").strip()]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _normalize_design_name(value):
    return _clean_text(value).lower()


def _subcontractor_from_page_text(text):
    body = _clean_text(text)
    match = re.search(
        r"\bSubcontractor:\s*(.+?)(?:\s+Preferred File Types\b|\s+Preferred Carriers\b|\s+Since\b|$)",
        body,
        flags=re.IGNORECASE,
    )
    return _clean_text(match.group(1) if match else "")


def _stock_state_is_ordered(stock_state):
    return bool(_product_separator._stock_state_is_ordered(stock_state))


def _stock_row_is_local_inventory(row):
    return bool(_product_separator._is_local_inventory_vendor((row or {}).get("vendor")))


def _stock_row_is_cancelled_channel_vendor(row):
    vendor = _clean_text((row or {}).get("vendor")).lower()
    vendor = re.sub(r"\bs\s*&\s*s\b", "s and s", vendor)
    vendor = re.sub(r"\s+", " ", vendor)
    return bool("sanmar" in vendor or "s and s activewear" in vendor or "ss activewear" in vendor)


def _stock_rows_from_state(stock_state):
    stock_state = stock_state or {}
    rows = stock_state.get("manual_order_rows") or []
    if not rows and (_clean_text(stock_state.get("manual_order_vendor")) or _clean_text(stock_state.get("manual_order_po"))):
        rows = [stock_state]
    normalized_rows = []
    seen = set()
    for row in rows:
        vendor = _clean_text((row or {}).get("vendor"))
        po = _clean_text((row or {}).get("po"))
        if not vendor or not po:
            continue
        normalized = {
            "vendor": _product_separator._manual_order_vendor_label(vendor),
            "po": po,
            "vendor_order_number": _clean_text((row or {}).get("vendor_order_number")),
        }
        key = (normalized["vendor"].lower(), normalized["po"].lower())
        if key in seen:
            continue
        seen.add(key)
        normalized_rows.append(normalized)
    return normalized_rows


def _stock_state_is_header_only(stock_state):
    stock_state = stock_state or {}
    if str(stock_state.get("state") or "") != "ordered_header_only":
        return False
    if stock_state.get("has_po_row"):
        return False
    if stock_state.get("manual_order_rows"):
        return False
    return not (_clean_text(stock_state.get("manual_order_vendor")) or _clean_text(stock_state.get("manual_order_po")))


def _stock_transfer_records_for_design(design):
    stock_state = design.get("stock") or {}
    if not _stock_state_is_ordered(stock_state):
        return []
    records = []
    for row in _stock_rows_from_state(stock_state):
        records.append(
            {
                "source_tab_number": design.get("tab_number"),
                "source_design_id": _clean_text(design.get("design_id")),
                "source_design_name": _clean_text(design.get("design_name")),
                "source_quantity": design.get("quantity"),
                "source_subtotal": _clean_text(design.get("subtotal")),
                "vendor": row.get("vendor"),
                "po": row.get("po"),
                "vendor_order_number": row.get("vendor_order_number", ""),
                "local_inventory": _stock_row_is_local_inventory(row),
                "cancelled_channel_vendor": _stock_row_is_cancelled_channel_vendor(row),
            }
        )
    return records


def _summarize_original_stock(designs, order_stock_status=None):
    ordered_tabs = []
    transfer_records = []
    local_inventory_rows = []
    cancelled_channel_rows = []
    outside_stock_rows = []
    unknown_ordered_tabs = []
    header_only_ordered_tabs = []
    for design in designs or []:
        stock_state = design.get("stock") or {}
        if not _stock_state_is_ordered(stock_state):
            continue
        ordered_tabs.append(
            {
                "tab_number": design.get("tab_number"),
                "design_id": _clean_text(design.get("design_id")),
                "design_name": _clean_text(design.get("design_name")),
                "state": stock_state.get("state"),
            }
        )
        rows = _stock_transfer_records_for_design(design)
        if not rows:
            target = header_only_ordered_tabs if _stock_state_is_header_only(stock_state) else unknown_ordered_tabs
            target.append(
                {
                    "tab_number": design.get("tab_number"),
                    "design_id": _clean_text(design.get("design_id")),
                    "design_name": _clean_text(design.get("design_name")),
                    "state": stock_state.get("state"),
                }
            )
            continue
        for row in rows:
            transfer_records.append(row)
            if row.get("local_inventory"):
                local_inventory_rows.append(row)
            else:
                outside_stock_rows.append(row)
                if row.get("cancelled_channel_vendor"):
                    cancelled_channel_rows.append(row)
    order_stock_status = order_stock_status if isinstance(order_stock_status, dict) else {}
    if order_stock_status.get("stock_status_ordered") and not ordered_tabs:
        unknown_ordered_tabs.append(
            {
                "tab_number": None,
                "design_id": "",
                "design_name": "",
                "state": order_stock_status.get("state") or "order_stock_status_ordered",
            }
        )
    stock_ordered = bool(ordered_tabs or order_stock_status.get("stock_status_ordered"))
    return {
        "stock_ordered": stock_ordered,
        "ordered_tabs": ordered_tabs,
        "transfer_records": transfer_records,
        "local_inventory_rows": local_inventory_rows,
        "outside_stock_rows": outside_stock_rows,
        "cancelled_channel_rows": cancelled_channel_rows,
        "unknown_ordered_tabs": unknown_ordered_tabs,
        "header_only_ordered_tabs": header_only_ordered_tabs,
        "local_inventory_only": bool(stock_ordered and local_inventory_rows and not outside_stock_rows and not unknown_ordered_tabs),
        "order_stock_status": order_stock_status,
    }


def _planned_stock_routing(stock_summary, subcontractor):
    stock_summary = stock_summary or {}
    subcontractor = _clean_text(subcontractor)
    is_subcontractor = bool(subcontractor)
    is_mach6 = "mach 6" in subcontractor.lower()
    if not stock_summary.get("stock_ordered"):
        return {"action": "none", "reason": "no_stock_ordered", "subcontractor": subcontractor}
    if stock_summary.get("unknown_ordered_tabs"):
        return {
            "action": "manual_review",
            "reason": "stock_ordered_vendor_po_unknown",
            "subcontractor": subcontractor,
        }
    header_only_ordered_tabs = stock_summary.get("header_only_ordered_tabs") or []
    if not is_subcontractor:
        if header_only_ordered_tabs and not stock_summary.get("transfer_records"):
            return {
                "action": "header_only_no_transfer",
                "reason": "stock_ordered_header_only",
                "subcontractor": subcontractor,
            }
        return {"action": "copy_to_split_orders", "subcontractor": subcontractor}
    if header_only_ordered_tabs and not stock_summary.get("transfer_records"):
        return {
            "action": "manual_review",
            "reason": "stock_ordered_header_only_subcontractor",
            "subcontractor": subcontractor,
        }
    if is_mach6:
        if stock_summary.get("cancelled_channel_rows"):
            return {
                "action": "slack_mach6_cancelled",
                "subcontractor": subcontractor,
                "message": "<original order URL> cancelled",
            }
        if stock_summary.get("outside_stock_rows") or stock_summary.get("unknown_ordered_tabs"):
            return {
                "action": "manual_review",
                "reason": "mach6_stock_vendor_not_supported_for_auto_slack",
                "subcontractor": subcontractor,
            }
        return {
            "action": "complete_local_inventory",
            "reason": "mach6_local_inventory_only",
            "subcontractor": subcontractor,
        }
    if stock_summary.get("outside_stock_rows") or stock_summary.get("unknown_ordered_tabs"):
        return {
            "action": "manual_review",
            "reason": "unsupported_subcontractor_stock_routing",
            "subcontractor": subcontractor,
        }
    return {
        "action": "complete_local_inventory",
        "reason": "subcontractor_local_inventory_only",
        "subcontractor": subcontractor,
    }


def _split_ranges(total_tabs, divisions):
    if total_tabs <= 0:
        raise SplitterError("Tab count must be greater than zero.")
    if divisions <= 0:
        raise SplitterError("Division count must be greater than zero.")
    if divisions > total_tabs:
        raise SplitterError("Division count cannot be greater than tab count.")

    base = total_tabs // divisions
    remainder = total_tabs % divisions
    ranges = []
    cursor = 1
    for index in range(divisions):
        size = base + (1 if index < remainder else 0)
        start = cursor
        end = cursor + size - 1
        ranges.append({"split_index": index + 1, "start_tab": start, "end_tab": end, "tab_count": size})
        cursor = end + 1
    return ranges


def _auto_divisions_for_tab_count(total_tabs, max_tabs_per_split=DEFAULT_MAX_TABS_PER_SPLIT):
    try:
        total_tabs = int(total_tabs)
        max_tabs_per_split = int(max_tabs_per_split)
    except Exception as err:
        raise SplitterError(f"Tab count and max tabs per split must be numeric: {err}")
    if total_tabs <= 0:
        raise SplitterError("Tab count must be greater than zero.")
    if max_tabs_per_split <= 0:
        raise SplitterError("Max tabs per split must be greater than zero.")
    if total_tabs <= max_tabs_per_split:
        raise SplitterError(
            f"Order has {total_tabs} tab(s). Auto Splitter only splits orders with more than {max_tabs_per_split} tabs."
        )
    return (total_tabs + max_tabs_per_split - 1) // max_tabs_per_split


def _validate_split_ranges_within_limit(ranges, max_tabs_per_split=DEFAULT_MAX_TABS_PER_SPLIT):
    for split_range in ranges or []:
        if int(split_range.get("tab_count") or 0) > int(max_tabs_per_split):
            raise SplitterError(
                f"Split {split_range.get('split_index')} would contain {split_range.get('tab_count')} tabs. "
                f"Warehouse limit is {max_tabs_per_split} tabs per split order."
            )
    return True


def _allocate_money(amount, divisions):
    amount = Decimal(amount or "0.00").quantize(Decimal("0.01"))
    if divisions <= 0:
        return []
    sign = -1 if amount < 0 else 1
    cents = int((amount.copy_abs() * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base = cents // divisions
    remainder = cents % divisions
    allocated = []
    for index in range(divisions):
        split_cents = sign * (base + (1 if index < remainder else 0))
        allocated.append((Decimal(split_cents) / Decimal(100)).quantize(Decimal("0.01")))
    return allocated


def _allocate_shipping(total_shipping, divisions):
    return _allocate_money(total_shipping, divisions)


def _build_split_plan(designs, divisions, original_order_id, shipping_amount=Decimal("0.00"), promo_amount=Decimal("0.00"), promo_code=""):
    ranges = _split_ranges(len(designs), divisions)
    shipping_allocations = _allocate_shipping(shipping_amount, divisions)
    promo_allocations = _allocate_money(promo_amount, divisions)
    all_names = [design.get("design_name") for design in designs]
    duplicate_names = sorted({name for name in all_names if name and all_names.count(name) > 1})
    if duplicate_names:
        raise SplitterError(f"Duplicate design names detected before split: {', '.join(duplicate_names)}")

    plan = []
    for index, split_range in enumerate(ranges):
        keep = [
            design
            for design in designs
            if split_range["start_tab"] <= int(design.get("tab_number") or 0) <= split_range["end_tab"]
        ]
        keep_names = [design.get("design_name") for design in keep]
        keep_ids = [int(design.get("design_id")) for design in keep if str(design.get("design_id") or "").isdigit()]
        delete_names = [design.get("design_name") for design in designs if design.get("design_name") not in keep_names]
        delete_ids = [
            int(design.get("design_id"))
            for design in designs
            if design.get("design_name") not in keep_names and str(design.get("design_id") or "").isdigit()
        ]
        stock_transfer_records = []
        for design in keep:
            stock_transfer_records.extend(_stock_transfer_records_for_design(design))
        plan.append(
            {
                **split_range,
                "keep_design_names": keep_names,
                "keep_design_ids": keep_ids,
                "delete_design_names": delete_names,
                "delete_design_ids": delete_ids,
                "sales_note": f"transferred from {original_order_id}",
                "shipping_charge": _money_text(shipping_allocations[index] if index < len(shipping_allocations) else Decimal("0.00")),
                "promo_credit": _money_text(promo_allocations[index] if index < len(promo_allocations) else Decimal("0.00")),
                "promo_code": _clean_text(promo_code),
                "stock_transfer_records": stock_transfer_records,
            }
        )
    return plan


def _date_to_iso(value):
    text = _clean_text(value)
    if not text:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return text


def _browser_target_url(action, list_url=None, order_id=None):
    if action == "process_batch":
        return (list_url or PROCESSOR_LIST_URL or PROCESSOR_LOGIN_URL or "").strip()
    if order_id and PROCESSOR_LOGIN_URL:
        return PROCESSOR_LOGIN_URL.strip()
    return (PROCESSOR_LOGIN_URL or PROCESSOR_LIST_URL or "").strip()


def _open_browser_if_requested(action, dry_run=True, visible=False, list_url=None, order_id=None, open_browser=False):
    target_url = _browser_target_url(action, list_url=list_url, order_id=order_id)
    if not target_url:
        return None, "No browser URL configured."
    if not open_browser:
        return None, target_url

    profile = _profile_path()
    headless = bool(PROCESSOR_HEADLESS and not visible)
    kill_stale_chrome(profile, profile_label="new processor")
    driver = build_chrome_driver(
        profile,
        headless_mode=headless,
        page_load_strategy="eager",
        page_load_timeout=PROCESSOR_PAGE_LOAD_TIMEOUT,
        script_timeout=PROCESSOR_ACTION_TIMEOUT,
    )
    safe_get_with_partial_load(driver, target_url, "processor page")
    return driver, target_url


def _maybe_click_saved_login(driver):
    body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))
    current_url = str(driver.current_url or "").lower()
    title = str(driver.title or "").lower()
    if "login" not in body_text.lower() and "login" not in title and "/login" not in current_url:
        return False

    try:
        inputs = driver.find_elements("css selector", "input")
        for field in inputs[:2]:
            try:
                field.click()
                time.sleep(0.2)
                field.send_keys(Keys.ARROW_DOWN)
                time.sleep(0.1)
                field.send_keys(Keys.ENTER)
                time.sleep(0.2)
            except Exception:
                pass
    except Exception:
        pass

    clicked = bool(
        driver.execute_script(
            """
            const controls = Array.from(document.querySelectorAll('button,input[type=submit],a,[role=button],div,span'));
            const visible = controls.filter((el) => {
              const rect = el.getBoundingClientRect();
              return rect.width > 10 && rect.height > 10;
            });
            const login = visible.find((el) => {
              const text = `${el.innerText || ''} ${el.value || ''} ${el.getAttribute('aria-label') || ''}`.trim().toLowerCase();
              return text === 'login' || text === 'log in' || text.includes('sign in');
            });
            if (!login) return false;
            login.scrollIntoView({block: 'center', inline: 'center'});
            login.click();
            return true;
            """
        )
    )
    if not clicked:
        try:
            driver.switch_to.active_element.send_keys(Keys.ENTER)
            clicked = True
        except Exception:
            pass
    return clicked


def _is_login_page(driver):
    body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';")).lower()
    current_url = str(driver.current_url or "").lower()
    title = str(driver.title or "").lower()
    return "login" in body_text or "login" in title or "/login" in current_url


def _handle_login_if_needed(driver, target_url, login_wait_seconds=0):
    if not _is_login_page(driver):
        return False

    _maybe_click_saved_login(driver)
    time.sleep(3)
    if not _is_login_page(driver):
        safe_get_with_partial_load(driver, target_url, "original CRM order after automatic login")
        return True

    if login_wait_seconds <= 0:
        return False

    print(f"Login is required. Complete login in the Chrome window within {login_wait_seconds} seconds.")
    deadline = time.monotonic() + login_wait_seconds
    last_url = driver.current_url
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            if not _is_login_page(driver):
                safe_get_with_partial_load(driver, target_url, "original CRM order after manual login")
                return True
            if driver.current_url != last_url:
                last_url = driver.current_url
        except Exception:
            pass
    return False


def _switch_to_crm_app_frame(driver):
    driver.switch_to.default_content()
    if "/app#" in str(driver.current_url or ""):
        return False
    frames = driver.find_elements("css selector", "iframe,frame")
    for frame in frames:
        src = frame.get_attribute("src") or ""
        if "/app#" in src or "crm2.legacy.printfly.com/app" in src:
            driver.switch_to.frame(frame)
            return True
    return False


def _activate_crm_context(driver):
    driver.switch_to.default_content()
    if "/app#" in str(driver.current_url or ""):
        return "top"
    if _switch_to_crm_app_frame(driver):
        return "frame"
    return "top"


def _wait_for_crm_context(driver, timeout=45):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            _activate_crm_context(driver)
            if refresh_if_crm_challenge_attempts_exceeded(driver, "Auto Splitter CRM context"):
                last_error = "refreshed CRM challenge page"
                continue
            _activate_crm_context(driver)
            ready = driver.execute_script("return !!(window.angular && document.body && document.body.innerText.length);")
            if ready:
                return True
        except Exception as err:
            last_error = err
        time.sleep(0.5)
    raise SplitterError(f"CRM app did not become ready. Last error: {last_error}")


def _wait_for_crm_context_with_reload(driver, url, label, timeout=45):
    try:
        return _wait_for_crm_context(driver, timeout=timeout)
    except SplitterError as err:
        if "CRM app did not become ready" not in str(err):
            raise
        safe_get_with_partial_load(driver, url, f"{label} recovery reload")
        return _wait_for_crm_context(driver, timeout=timeout)


ORDER_SCOPE_BOOTSTRAP = """
function findOrderScope() {
  const nodes = Array.from(document.querySelectorAll('*'));
  for (const el of nodes) {
    let scope = null;
    try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
    for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
      if (scope.order && scope.order.getResource && typeof scope.copyOrder === 'function') return scope;
    }
  }
  return null;
}
const s = findOrderScope();
if (!s) throw new Error('Order scope not found');
const r = s.order.getResource();
"""


QUOTE_SCOPE_BOOTSTRAP = """
function findQuoteScope() {
  const nodes = Array.from(document.querySelectorAll('*'));
  for (const el of nodes) {
    let scope = null;
    try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
    for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
      if (scope.quote && typeof scope.saveQuote === 'function') return scope;
    }
  }
  return null;
}
const s = findQuoteScope();
if (!s) throw new Error('Quote scope not found');
const q = s.quote;
const op = (q.options || [])[0];
if (!op) throw new Error('Quote option not found');
"""


def _order_scope(driver, script, *args):
    return driver.execute_script(ORDER_SCOPE_BOOTSTRAP + "\n" + ANGULAR_APPLY_JS + "\n" + script, *args)


def _quote_scope(driver, script, *args):
    return driver.execute_script(QUOTE_SCOPE_BOOTSTRAP + "\n" + ANGULAR_APPLY_JS + "\n" + script, *args)


ANGULAR_APPLY_JS = """
function runInAngular(scope, fn) {
  const root = scope.$root || scope;
  if (root.$$phase) {
    return fn();
  }
  if (typeof scope.$apply === 'function') {
    return scope.$apply(fn);
  }
  const result = fn();
  if (typeof root.$digest === 'function') root.$digest();
  return result;
}
"""


def _wait_for_order_scope(driver, order_id=None, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _wait_for_crm_context(driver, timeout=3)
            summary = _order_scope(
                driver,
                """
                return {
                  id: String(r.id || ''),
                  design_count: (r.designs || []).length,
                  status: ((r.orderStatuses || [])[0] || {}).statusName || ((r.status || [])[0] || {}).statusName || ''
                };
                """,
            )
            if summary.get("design_count", 0) > 0 and (not order_id or summary.get("id") == str(order_id)):
                return summary
        except Exception:
            pass
        time.sleep(0.75)
    raise SplitterError(f"Could not find loaded CRM order scope for order {order_id or ''}.")


def _open_order_scope_with_reload(driver, order_url, order_id=None, label="CRM order", timeout=45):
    safe_get_with_partial_load(driver, order_url, label)
    try:
        return _wait_for_order_scope(driver, order_id=order_id, timeout=timeout)
    except SplitterError:
        safe_get_with_partial_load(driver, order_url, f"{label} recovery reload")
        _wait_for_crm_context_with_reload(driver, order_url, label, timeout=timeout)
        return _wait_for_order_scope(driver, order_id=order_id, timeout=timeout)


def _wait_for_quote_scope(driver, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _wait_for_crm_context(driver, timeout=3)
            summary = _quote_scope(
                driver,
                """
                return {
                  quote_id: q.id || null,
                  order_id: q.orderId || null,
                  design_count: (op.designs || []).length,
                  design_ids: (op.designs || []).map((design) => Number(design.designId))
                };
                """,
            )
            return summary
        except Exception:
            pass
        time.sleep(0.75)
    raise SplitterError("Could not find loaded CRM quote scope.")


def _copy_quote_timeout_seconds(expected_design_count):
    try:
        design_count = int(expected_design_count or 0)
    except Exception:
        design_count = 0
    scaled_timeout = design_count * COPY_QUOTE_SECONDS_PER_DESIGN
    return min(
        COPY_QUOTE_MAX_TIMEOUT_SECONDS,
        max(COPY_QUOTE_BASE_TIMEOUT_SECONDS, scaled_timeout),
    )


def _clear_copied_quote_art_notes(driver):
    return _quote_scope(
        driver,
        """
        const before = {
          artNotes: q.artNotes || '',
          addArtNotes: q.addArtNotes || '',
          artNoteOptions: q.artNoteOptions || ''
        };
        runInAngular(s, () => {
          q.artNotes = '';
          q.addArtNotes = '';
        });
        const root = document.querySelector('#quote-notes-art-notes') || document;
        const fields = Array.from(root.querySelectorAll('textarea[ng-model="quote.addArtNotes"]'));
        for (const field of fields) {
          field.value = '';
          field.dispatchEvent(new Event('input', {bubbles: true}));
          field.dispatchEvent(new Event('change', {bubbles: true}));
        }
        return {
          before,
          artNotes: q.artNotes || '',
          addArtNotes: q.addArtNotes || '',
          artNoteOptions: q.artNoteOptions || ''
        };
        """,
    )


def _append_note(existing, note):
    existing_text = str(existing or "").strip()
    if not existing_text:
        return note
    if note.lower() in existing_text.lower():
        return existing_text
    return f"{existing_text}\n\n{note}"


def _get_order_live_state(driver):
    return _order_scope(
        driver,
        """
        const txs = r.transactions || [];
        return {
          id: String(r.id || ''),
          fulfillment_date: r.fulfillmentDate || '',
          fulfillment_time: r.fulfillmentTime || '',
          shipping_charges: r.shippingCharges || '0.00',
          subtotal: s.order.getSubTotal ? s.order.getSubTotal() : null,
          grand_total: s.order.getGrandTotal ? s.order.getGrandTotal() : null,
          amount_paid: s.order.getAmountPaid ? s.order.getAmountPaid() : null,
          amount_due: s.order.getAmountDue ? s.order.getAmountDue() : null,
          sales_notes: r.salesNotes || r.filteredSalesNotes || '',
          transactions: txs.map((tx) => ({
            amount: tx.amount || '',
            tag: tx.tag || tx.type || '',
            type: tx.type || tx.tag || '',
            note: tx.note || tx.info || tx.transactionId || ''
          }))
        };
        """,
    )


def _get_original_payment_info(driver):
    state = _get_order_live_state(driver)
    for transaction in state.get("transactions", []):
        note = _clean_text(transaction.get("note"))
        tag = _clean_text(transaction.get("tag") or transaction.get("type"))
        if note:
            return {"transaction_id": note, "payment_type": tag}

    clicked = bool(
        driver.execute_script(
            """
            const root = document.querySelector('#order-payments-credits') || document;
            const button = Array.from(root.querySelectorAll('button,a')).find((el) => {
              return (el.innerText || '').trim().toLowerCase() === 'view';
            });
            if (!button) return false;
            button.scrollIntoView({block: 'center'});
            button.click();
            return true;
            """
        )
    )
    if not clicked:
        return {"transaction_id": "", "payment_type": state.get("transactions", [{}])[0].get("type", "") if state.get("transactions") else ""}

    time.sleep(1)
    text = driver.execute_script(
        """
        const modal = document.querySelector('.modal, .modal-content');
        return modal ? modal.innerText : '';
        """
    )
    try:
        driver.execute_script(
            """
            const button = Array.from(document.querySelectorAll('.modal button,.modal a')).find((el) => {
              const text = (el.innerText || '').trim().toLowerCase();
              return text === 'close' || text === 'cancel' || text === '×';
            });
            if (button) button.click();
            """
        )
    except Exception:
        pass
    match = re.search(r"\$?[0-9,]+\.\d{2}\s+([^\t\n\r]+?)\s+([A-Za-z0-9_:-]{8,})\s+\d{1,2}/\d{1,2}/\d{2}", text)
    if match:
        return {"payment_type": _clean_text(match.group(1)), "transaction_id": _clean_text(match.group(2))}
    return {"transaction_id": "", "payment_type": state.get("transactions", [{}])[0].get("type", "") if state.get("transactions") else ""}


def _payment_is_detected(order_state):
    """Return whether the order currently has money paid against it.

    ``amount_paid`` is authoritative when the CRM exposes it, including an
    explicit zero on orders that have historical or otherwise non-payable
    transaction rows. Older CRM views sometimes omit that computed value, so
    positive, non-refund transactions are used only as a fallback.
    """
    raw_amount_paid = order_state.get("amount_paid")
    if raw_amount_paid not in (None, ""):
        return _parse_money(raw_amount_paid) > Decimal("0.00")

    for transaction in order_state.get("transactions") or []:
        tag = _clean_text(transaction.get("tag") or transaction.get("type")).lower()
        if "refund" in tag:
            continue
        if _parse_money(transaction.get("amount")) > Decimal("0.00"):
            return True
    return False


def _copy_order_to_quote(driver, original_order_id, expected_design_count):
    _wait_for_order_scope(driver, order_id=original_order_id)
    _order_scope(
        driver,
        """
        runInAngular(s, () => s.copyOrder());
        return true;
        """,
    )
    deadline = time.monotonic() + _copy_quote_timeout_seconds(expected_design_count)
    last_state = {}
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            _activate_crm_context(driver)
            quote = _wait_for_quote_scope(driver, timeout=3)
            last_state = {
                "url": str(driver.current_url or ""),
                "quote_id": quote.get("quote_id"),
                "order_id": quote.get("order_id"),
                "design_count": quote.get("design_count"),
                "expected_design_count": expected_design_count,
                "design_ids": quote.get("design_ids", [])[:10],
            }
            if int(quote.get("design_count") or 0) == int(expected_design_count):
                quote["art_notes_clear"] = _clear_copied_quote_art_notes(driver)
                return quote
        except Exception as err:
            last_state = {
                "url": str(driver.current_url or ""),
                "error": str(err),
                "expected_design_count": expected_design_count,
            }
    raise SplitterError(f"Copy order did not open a complete copied quote. Last copied quote state: {last_state}")


def _configure_quote_split(driver, plan, original_state):
    keep_ids = [int(value) for value in plan.get("keep_design_ids", [])]
    if not keep_ids:
        raise SplitterError(f"Split {plan.get('split_index')} has no readable design IDs to keep.")
    due_date = _date_to_iso(original_state.get("fulfillment_date") or "")
    due_time = str(original_state.get("fulfillment_time") or "")
    _quote_scope(
        driver,
        """
        const salesNote = arguments[0];
        const dueDate = arguments[1];
        const dueTime = arguments[2];
        const shipping = arguments[3];
        runInAngular(s, () => {
          q.addNote = [q.addNote || '', salesNote].filter(Boolean).join((q.addNote || '').trim() ? '\\n\\n' : '');
          op.dueDate = dueDate || op.dueDate;
          if (dueTime && dueTime !== '23:59:59') op.dueTime = dueTime;
          op.shippingPrice = shipping;
        });
        return true;
        """,
        plan.get("sales_note", ""),
        due_date,
        due_time,
        _money_text(plan.get("shipping_charge", "0.00")),
    )
    for delete_id in plan.get("delete_design_ids", []):
        _remove_quote_design_by_id(driver, int(delete_id))

    result = _quote_scope(
        driver,
        """
        const keepIds = new Set(arguments[0].map((value) => Number(value)));
        const salesNote = arguments[1];
        const dueDate = arguments[2];
        const dueTime = arguments[3];
        const shipping = arguments[4];
        const after = (op.designs || []).map((design) => Number(design.designId));
        return {after, quote_id: q.id || null};
        """,
        keep_ids,
        plan.get("sales_note", ""),
        due_date,
        due_time,
        _money_text(plan.get("shipping_charge", "0.00")),
    )
    result["promo_config"] = {
        "promo_credit": _money_text(plan.get("promo_credit", "0.00")),
        "promo_code": plan.get("promo_code", ""),
        "apply_stage": "quote_discount_fee_before_payment",
    }
    after_ids = [int(value) for value in result.get("after", [])]
    if sorted(after_ids) != sorted(keep_ids):
        raise SplitterError(
            f"Split {plan.get('split_index')} delete check failed. Expected design IDs {keep_ids}, found {after_ids}."
        )
    return result


def _quote_design_ids(driver):
    return [
        int(value)
        for value in _quote_scope(
            driver,
            """
            return (op.designs || []).map((design) => Number(design.designId));
            """,
        )
    ]


def _quote_fee_rows(driver):
    try:
        return _quote_scope(
            driver,
            """
            const containers = [
              q.orderFees,
              q.fees,
              op.orderFees,
              op.fees
            ].filter((rows) => Array.isArray(rows));
            const rows = containers.find((items) => items.length) || [];
            return rows.map((fee) => ({
              feeId: fee.feeId || fee.id || '',
              name: fee.name || fee.feeName || '',
              code: fee.code || '',
              amount: fee.amount || fee.price || fee.total || ''
            }));
            """,
        )
    except Exception:
        return []


def _quote_fee_already_present(driver, fee_label, amount):
    wanted = _clean_text(fee_label).lower()
    for fee in _quote_fee_rows(driver):
        label = _clean_text(f"{fee.get('name', '')} {fee.get('code', '')}").lower()
        if wanted in label and _money_amount_matches(fee.get("amount"), amount):
            return True
    return False


def _add_quote_fee(driver, fee_label, amount, fallback_fee_id=None, fallback_code=None):
    amount = Decimal(str(amount or "0")).quantize(Decimal("0.01"))
    if amount == Decimal("0.00"):
        return {"skipped": True, "reason": "zero_amount", "fee_label": fee_label, "amount": _money_text(amount)}
    if _quote_fee_already_present(driver, fee_label, amount):
        return {"skipped": True, "reason": "already_present", "fee_label": fee_label, "amount": _money_text(amount)}

    if not _click_add_fee(driver, fee_label):
        raise SplitterError(f"Could not click Add Fee for quote {fee_label}.")
    time.sleep(0.5)
    result = _quote_scope(
        driver,
        """
        const feeLabel = arguments[0];
        const amount = arguments[1];
        const fallbackFeeId = arguments[2];
        const fallbackCode = arguments[3];
        const wanted = String(feeLabel || '').trim().toLowerCase();
        function feeText(fee) {
          return [
            fee && (fee.name || fee.feeName || fee.label),
            fee && fee.code
          ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function findFeeDefinition() {
          const nodes = Array.from(document.querySelectorAll('*'));
          for (const el of nodes) {
            let scope = null;
            try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
            for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
              const controller = scope.OrderFeesController || scope.orderFeesController || null;
              const candidates = [
                controller && controller.availableFees,
                scope.availableFees,
                scope.fees,
                scope.feeTypes
              ];
              for (const list of candidates) {
                if (!Array.isArray(list)) continue;
                const exact = list.find((fee) => feeText(fee) === wanted);
                if (exact) return exact;
                const partial = list.find((fee) => feeText(fee).includes(wanted));
                if (partial) return partial;
              }
            }
          }
          return null;
        }
        function addContainer(owner, prop, label, containers, seen) {
          if (!owner || !Array.isArray(owner[prop])) return;
          const key = label + ':' + prop;
          if (seen.has(key)) return;
          seen.add(key);
          containers.push({owner, prop, label});
        }
        function feeContainers() {
          const containers = [];
          const seen = new Set();
          addContainer(q, 'orderFees', 'quote.orderFees', containers, seen);
          addContainer(q, 'fees', 'quote.fees', containers, seen);
          addContainer(op, 'orderFees', 'option.orderFees', containers, seen);
          addContainer(op, 'fees', 'option.fees', containers, seen);
          const nodes = Array.from(document.querySelectorAll('*'));
          for (const el of nodes) {
            let scope = null;
            try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
            for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
              const controller = scope.OrderFeesController || scope.orderFeesController || null;
              addContainer(controller && controller.order, 'orderFees', 'controller.orderFees', containers, seen);
              addContainer(controller && controller.order, 'fees', 'controller.fees', containers, seen);
              addContainer(scope.order, 'orderFees', 'scope.orderFees', containers, seen);
              addContainer(scope.order, 'fees', 'scope.fees', containers, seen);
            }
          }
          return containers;
        }
        const containers = feeContainers();
        const target = containers.find((item) => item.owner[item.prop].length);
        if (!target) throw new Error('No quote fee row was created');
        const fees = target.owner[target.prop];
        const definition = findFeeDefinition() || {};
        const fee = fees[fees.length - 1];
        const feeId = definition.feeId || definition.id || fallbackFeeId || fee.feeId;
        if (feeId !== undefined && feeId !== null && feeId !== '') fee.feeId = feeId;
        fee.name = definition.name || definition.feeName || definition.label || feeLabel;
        fee.code = definition.code || fallbackCode || String(feeLabel || '').trim().toLowerCase();
        fee.amount = amount;
        fee.crudAction = fee.crudAction || 'c';
        runInAngular(s, () => {});
        return {feeId: fee.feeId || '', name: fee.name || '', code: fee.code || '', amount: fee.amount || '', source: target.label};
        """,
        fee_label,
        _signed_money_text(amount).replace("$", ""),
        fallback_fee_id or "",
        fallback_code or "",
    )
    return {"skipped": False, "fee": result, "save": "pending_save_quote"}


def _add_discount_fee_to_split_quote(driver, promo_credit):
    amount = Decimal(str(promo_credit or "0")).copy_abs()
    if amount == Decimal("0.00"):
        return {"skipped": True, "reason": "no_promo_credit", "amount": "0.00"}
    return _add_quote_fee(
        driver,
        "Discount",
        -amount,
        fallback_code="discount",
    )


def _remove_quote_design_by_id(driver, design_id):
    before_ids = _quote_design_ids(driver)
    if int(design_id) not in before_ids:
        return False
    removed = _quote_scope(
        driver,
        """
        const designId = Number(arguments[0]);
        const index = (op.designs || []).findIndex((design) => Number(design.designId) === designId);
        if (index < 0) return false;
        runInAngular(s, () => s.removeDesign(op.designs[index], index, op));
        return true;
        """,
        int(design_id),
    )
    if not removed:
        raise SplitterError(f"Could not start delete for design ID {design_id}.")

    deadline = time.monotonic() + 15
    accepted = False
    while time.monotonic() < deadline:
        time.sleep(0.25)
        modal_text = _find_modal_text(driver).lower()
        if "delete this design" in modal_text or "are you sure" in modal_text:
            accepted = _click_modal_choice(driver, "yes") or accepted
            break
        if int(design_id) not in _quote_design_ids(driver):
            accepted = True
            break
    if not accepted:
        raise SplitterError(f"Delete confirmation did not appear for design ID {design_id}.")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if int(design_id) not in _quote_design_ids(driver):
            return True
    raise SplitterError(f"Design ID {design_id} was not removed after confirming delete.")


def _click_ng_button(driver, ng_click, text=None):
    return bool(
        driver.execute_script(
            """
            const ngClick = arguments[0];
            const expectedText = (arguments[1] || '').toLowerCase();
            const forbidden = /\\b(refund|issue\\s+refund|refund\\s+payment)\\b/i;
            const buttons = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
            const button = buttons.find((el) => {
              const ng = el.getAttribute('ng-click') || '';
              const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              return ng === ngClick && (!expectedText || text === expectedText) && rect.width >= 0 && rect.height >= 0;
            });
            if (!button) return false;
            const label = (button.innerText || button.value || '').replace(/\\s+/g, ' ').trim();
            if (forbidden.test(label)) throw new Error('Refusing to click refund control: ' + label);
            button.scrollIntoView({block: 'center', inline: 'center'});
            button.click();
            return true;
            """,
            ng_click,
            text or "",
        )
    )


def _visible_order_save_state(driver):
    return driver.execute_script(
        """
        const controls = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
        const isVisible = (el) => {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        };
        const normalized = controls
          .filter(isVisible)
          .map((el) => ({
            text: (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase(),
            ngClick: el.getAttribute('ng-click') || '',
            disabled: !!el.disabled || el.getAttribute('disabled') !== null,
          }));
        const saveControls = normalized.filter((item) => item.text === 'save order' || item.ngClick === 'saveOrder();');
        const editControls = normalized.filter((item) => item.text === 'edit order' || item.ngClick === 'editModeOn();');
        return {
          editOrderVisible: editControls.length > 0,
          saveOrderVisible: saveControls.length > 0,
          saveOrderEnabled: saveControls.some((item) => !item.disabled),
          visibleOrderControls: normalized
            .filter((item) => item.text === 'save order' || item.text === 'edit order' || item.ngClick === 'saveOrder();' || item.ngClick === 'editModeOn();')
            .slice(0, 8),
        };
        """
    )


def _save_quote(driver):
    if not _click_ng_button(driver, "saveQuote();", "save quote"):
        _quote_scope(driver, "runInAngular(s, () => s.saveQuote()); return true;")
    deadline = time.monotonic() + 90
    last = {}
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            _activate_crm_context(driver)
            last = _wait_for_quote_scope(driver, timeout=3)
            if last.get("quote_id") or re.search(r"/quotes/\d+", str(driver.current_url)):
                return last
        except Exception:
            pass
    raise SplitterError(f"Quote save did not complete. Last quote state: {last}")


def _find_modal_text(driver):
    return driver.execute_script(
        """
        const modal = document.querySelector('.modal, .modal-content');
        return modal ? modal.innerText : '';
        """
    )


def _click_modal_choice(driver, choice_text):
    return bool(
        driver.execute_script(
            """
            const expected = arguments[0].toLowerCase();
            const button = Array.from(document.querySelectorAll('.modal button,.modal a')).find((el) => {
              return (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase() === expected;
            });
            if (!button) return false;
            button.click();
            return true;
            """,
            choice_text,
        )
    )


def _open_record_transaction(driver, quote=False):
    if quote:
        _quote_scope(driver, "runInAngular(s, () => s.recordTransaction(op)); return true;")
    else:
        _order_scope(driver, "runInAngular(s, () => s.recordTransaction()); return true;")
    time.sleep(1)
    text = _find_modal_text(driver).lower()
    if "change the due date" in text:
        _click_modal_choice(driver, "no")
        time.sleep(1)


def _save_transaction_modal(driver, tag, transaction_id):
    return _save_transaction_modal_with_amount(driver, tag, transaction_id, amount=None)


def _save_transaction_modal_with_amount(driver, tag, transaction_id, amount=None):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            saved = driver.execute_script(
                ANGULAR_APPLY_JS
                + """
                function findTransactionScope() {
                  const nodes = Array.from(document.querySelectorAll('.modal *, .modal'));
                  for (const el of nodes) {
                    let scope = null;
                    try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
                    for (let hops = 0; scope && hops < 6; scope = scope.$parent, hops++) {
                      if (scope.transaction && typeof scope.save === 'function') return scope;
                    }
                  }
                  return null;
                }
                const s = findTransactionScope();
                if (!s) return false;
                runInAngular(s, () => {
                  s.transaction.tag = arguments[0];
                  s.transaction.note = arguments[1];
                  if (arguments[2]) s.transaction.amount = arguments[2];
                });
                s.save();
                return true;
                """,
                tag,
                transaction_id,
                _money_text(amount) if amount is not None else "",
            )
            if saved:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    raise SplitterError("Transaction modal did not open with a saveable transaction form.")


def _quote_visible_total(driver):
    body_text = driver.execute_script("return document.body ? document.body.innerText : '';")
    for pattern in (
        r"Grand Total:\s*\$?\s*([0-9,]+\.\d{2})",
        r"Total:\s*\$?\s*([0-9,]+\.\d{2})\s*\|\s*\d+\s+Designs",
    ):
        match = re.search(pattern, body_text, re.IGNORECASE)
        if match:
            return _money_text(_parse_money(match.group(1)))
    return "0.00"


def _record_split_payment_and_wait_for_order(driver, tag, transaction_id):
    amount = _quote_visible_total(driver)
    _open_record_transaction(driver, quote=True)
    _save_transaction_modal_with_amount(driver, tag, transaction_id, amount=amount)
    return _wait_for_new_split_order(driver, "Split payment was saved")


def _wait_for_new_split_order(driver, action_description, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1)
        driver.switch_to.default_content()
        url = str(driver.current_url)
        match = re.search(r"/order/(\d+)", url)
        if match:
            order_id = match.group(1)
            order_url = _order_url(order_id=order_id)
            _wait_for_crm_context_with_reload(driver, order_url, f"new split order {order_id}", timeout=45)
            return order_id
        try:
            _activate_crm_context(driver)
            text = driver.execute_script("return document.body ? document.body.innerText : '';")
            match = re.search(r"\|\s*(\d{6,})\b", text)
            if match and "/order/" in url:
                return match.group(1)
        except Exception:
            pass
    raise SplitterError(f"{action_description}, but the quote did not convert to a visible order.")


def _convert_unpaid_split_quote_and_wait_for_order(driver):
    """Convert a saved quote through the CRM's normal no-payment order action."""
    conversion = _quote_scope(
        driver,
        """
        const labels = new Set([
          'create order',
          'create an order',
          'convert to order',
          'convert quote to order',
          'save as order',
          'place order'
        ]);
        const controls = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
        const isVisible = (el) => {
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        };
        const control = controls.find((el) => {
          const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
          return labels.has(text) && isVisible(el) && !el.disabled && el.getAttribute('disabled') === null;
        });
        if (control) {
          const label = (control.innerText || control.value || '').replace(/\\s+/g, ' ').trim();
          control.scrollIntoView({block: 'center', inline: 'center'});
          control.click();
          return {started: true, action: label, source: 'visible_control'};
        }

        const methods = ['createOrder', 'convertToOrder', 'convertQuoteToOrder', 'saveAsOrder', 'placeOrder'];
        const method = methods.find((name) => typeof s[name] === 'function');
        if (!method) {
          return {
            started: false,
            available_controls: controls
              .filter(isVisible)
              .map((el) => (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim())
              .filter(Boolean)
              .slice(0, 30)
          };
        }
        runInAngular(s, () => s[method](op));
        return {started: true, action: method, source: 'quote_scope'};
        """,
    )
    if not conversion or not conversion.get("started"):
        available = (conversion or {}).get("available_controls", [])
        raise SplitterError(
            "Could not find the CRM create/convert order action for an unpaid split quote. "
            f"Visible controls: {available}"
        )

    time.sleep(1)
    modal_text = _find_modal_text(driver).lower()
    if "change the due date" in modal_text:
        _click_modal_choice(driver, "no")
        time.sleep(1)
        modal_text = _find_modal_text(driver).lower()
    if modal_text and "order" in modal_text and any(word in modal_text for word in ("create", "convert", "place")):
        _click_modal_choice(driver, "yes")
    return _wait_for_new_split_order(driver, "Unpaid split quote conversion was started")


def _finalize_split_quote_and_wait_for_order(driver, payment_type, transaction_id):
    if transaction_id:
        return _record_split_payment_and_wait_for_order(
            driver,
            _transaction_tag_for_payment_type(payment_type),
            transaction_id,
        )
    return _convert_unpaid_split_quote_and_wait_for_order(driver)


def _create_split_order_in_worker(
    split,
    original_order_id,
    original_order_url,
    expected_tab_count,
    original_state,
    payment_type,
    transaction_id,
    profile_path,
    visible=False,
):
    driver = None
    try:
        kill_stale_chrome(profile_path, profile_label=f"CRM auto splitter worker {split.get('split_index')}")
        driver = _build_splitter_driver(profile_path, visible=visible)
        safe_get_with_partial_load(
            driver,
            original_order_url,
            f"worker original CRM order before split {split['split_index']}",
        )
        _handle_login_if_needed(driver, original_order_url, login_wait_seconds=0)
        if _is_login_page(driver):
            raise SplitterError(
                f"Worker {split.get('split_index')} could not use the cloned CRM login session."
            )
        _wait_for_crm_context_with_reload(
            driver,
            original_order_url,
            f"worker original CRM order before split {split['split_index']}",
        )
        _wait_for_order_scope(driver, order_id=original_order_id)
        _copy_order_to_quote(driver, original_order_id, expected_tab_count)
        configured = _configure_quote_split(driver, split, original_state)
        promo_discount_fee = _add_discount_fee_to_split_quote(driver, split.get("promo_credit", "0.00"))
        saved_quote = _save_quote(driver)
        new_order_id = _finalize_split_quote_and_wait_for_order(driver, payment_type, transaction_id)
        _open_order_scope_with_reload(
            driver,
            _order_url(order_id=new_order_id),
            order_id=new_order_id,
            label=f"new split order {new_order_id}",
        )
        totals = _read_order_totals(driver)
        return {
            "split_index": split["split_index"],
            "order_id": new_order_id,
            "existing_order": False,
            "kept_design_names": split["keep_design_names"],
            "kept_design_ids": split["keep_design_ids"],
            "deleted_design_ids": split["delete_design_ids"],
            "shipping_charge": split["shipping_charge"],
            "promo_credit": split.get("promo_credit", "0.00"),
            "promo_code": split.get("promo_code", ""),
            "stock_transfer_records": split.get("stock_transfer_records", []),
            "promo_discount_fee": promo_discount_fee,
            "quote_save": saved_quote,
            "configure_result": configured,
            "totals": totals,
        }
    finally:
        safe_driver_quit(driver, profile_path=profile_path)


def _read_order_totals(driver):
    state = _get_order_live_state(driver)
    return {
        "order_id": state.get("id"),
        "subtotal": _money_text(state.get("subtotal") or "0"),
        "grand_total": _money_text(state.get("grand_total") or "0"),
        "paid": _money_text(state.get("amount_paid") or "0"),
        "balance_due": _money_text(state.get("amount_due") or "0"),
    }


def _is_cancel_order_status(value):
    text = re.sub(r"[^a-z]+", " ", _clean_text(value).lower()).strip()
    return text in {
        "cancel order",
        "cancelled",
        "canceled",
        "cancelled order",
        "canceled order",
        "order cancelled",
        "order canceled",
    }


def _status_history_confirms_cancel_order(body_text):
    text = _clean_text(body_text)
    match = re.search(r"Status History(?: and Art Changes)?(.{0,2500})", text, re.IGNORECASE)
    if not match:
        return False
    return any(_is_cancel_order_status(value) for value in re.findall(r"Cancel Order|Cancelled|Canceled|Order Cancelled|Order Canceled", match.group(1), re.IGNORECASE))


def _money_amount_matches(value, expected):
    return _parse_money(value).copy_abs() == Decimal(str(expected or "0")).copy_abs().quantize(Decimal("0.01"))


def _split_total_mismatch_message(original_grand_total, split_total, split_total_delta):
    return (
        "Total does not match: "
        f"old/original ${_money_text(original_grand_total)} vs new/split ${_money_text(split_total)} "
        f"(difference {_signed_money_text(split_total_delta)})."
    )


def _original_refund_fee_already_present(driver, refund_amount):
    try:
        fees = _order_scope(
            driver,
            """
            const rows = r.orderFees || r.fees || [];
            return rows.map((fee) => ({
              name: fee.name || fee.feeName || '',
              code: fee.code || '',
              amount: fee.amount || fee.price || fee.total || ''
            }));
            """,
        )
    except Exception:
        return False
    for fee in fees or []:
        label = _clean_text(f"{fee.get('name', '')} {fee.get('code', '')}").lower()
        if "refund" in label and _money_amount_matches(fee.get("amount"), refund_amount):
            return True
    return False


def _existing_original_refund_fee_amount(driver):
    try:
        fees = _order_scope(
            driver,
            """
            const rows = r.orderFees || r.fees || [];
            return rows.map((fee) => ({
              name: fee.name || fee.feeName || '',
              code: fee.code || '',
              amount: fee.amount || fee.price || fee.total || ''
            }));
            """,
        )
    except Exception:
        return Decimal("0.00")
    refund_amounts = []
    for fee in fees or []:
        label = _clean_text(f"{fee.get('name', '')} {fee.get('code', '')}").lower()
        if "refund" in label:
            refund_amounts.append(_parse_money(fee.get("amount")).copy_abs())
    return sum(refund_amounts, Decimal("0.00")).quantize(Decimal("0.01"))


def _order_fee_rows(driver):
    try:
        return _order_scope(
            driver,
            """
            const rows = r.orderFees || r.fees || [];
            return rows.map((fee) => ({
              feeId: fee.feeId || fee.id || '',
              name: fee.name || fee.feeName || '',
              code: fee.code || '',
              amount: fee.amount || fee.price || fee.total || ''
            }));
            """,
        )
    except Exception:
        return []


def _order_fee_already_present(driver, fee_label, amount):
    wanted = _clean_text(fee_label).lower()
    for fee in _order_fee_rows(driver):
        label = _clean_text(f"{fee.get('name', '')} {fee.get('code', '')}").lower()
        if wanted in label and _money_amount_matches(fee.get("amount"), amount):
            return True
    return False


def _click_add_fee(driver, target_label):
    clicked = _click_ng_button(driver, "OrderFeesController.order.addFee(null, OrderFeesController.availableFees[0])", "add fee")
    if clicked:
        return True
    return bool(
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


def _add_order_fee(driver, fee_label, amount, fallback_fee_id=None, fallback_code=None):
    amount = Decimal(str(amount or "0")).quantize(Decimal("0.01"))
    if amount == Decimal("0.00"):
        return {"skipped": True, "reason": "zero_amount", "fee_label": fee_label, "amount": _money_text(amount)}
    if _order_fee_already_present(driver, fee_label, amount):
        return {"skipped": True, "reason": "already_present", "fee_label": fee_label, "amount": _money_text(amount)}

    _order_scope(driver, "runInAngular(s, () => s.editModeOn()); return true;")
    time.sleep(0.5)
    if not _click_add_fee(driver, fee_label):
        raise SplitterError(f"Could not click Add Fee for {fee_label}.")
    time.sleep(0.5)
    result = _order_scope(
        driver,
        """
        const feeLabel = arguments[0];
        const amount = arguments[1];
        const fallbackFeeId = arguments[2];
        const fallbackCode = arguments[3];
        const wanted = String(feeLabel || '').trim().toLowerCase();
        function feeText(fee) {
          return [
            fee && (fee.name || fee.feeName || fee.label),
            fee && fee.code
          ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
        }
        function findFeeDefinition() {
          const nodes = Array.from(document.querySelectorAll('*'));
          for (const el of nodes) {
            let scope = null;
            try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
            for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
              const controller = scope.OrderFeesController || scope.orderFeesController || null;
              const candidates = [
                controller && controller.availableFees,
                scope.availableFees,
                scope.fees,
                scope.feeTypes
              ];
              for (const list of candidates) {
                if (!Array.isArray(list)) continue;
                const exact = list.find((fee) => feeText(fee) === wanted);
                if (exact) return exact;
                const partial = list.find((fee) => feeText(fee).includes(wanted));
                if (partial) return partial;
              }
            }
          }
          return null;
        }
        const fees = r.orderFees || r.fees || [];
        if (!fees.length) throw new Error('No fee row was created');
        const definition = findFeeDefinition() || {};
        const fee = fees[fees.length - 1];
        const feeId = definition.feeId || definition.id || fallbackFeeId || fee.feeId;
        if (feeId !== undefined && feeId !== null && feeId !== '') fee.feeId = feeId;
        fee.name = definition.name || definition.feeName || definition.label || feeLabel;
        fee.code = definition.code || fallbackCode || String(feeLabel || '').trim().toLowerCase();
        fee.amount = amount;
        fee.crudAction = fee.crudAction || 'c';
        r.orderFees = fees;
        runInAngular(s, () => {});
        return {feeId: fee.feeId || '', name: fee.name || '', code: fee.code || '', amount: fee.amount || ''};
        """,
        fee_label,
        _signed_money_text(amount).replace("$", ""),
        fallback_fee_id or "",
        fallback_code or "",
    )
    save_result = _save_order_and_wait(driver)
    return {"skipped": False, "fee": result, "save": save_result}


def _add_discount_fee_to_split_order(driver, promo_credit):
    amount = Decimal(str(promo_credit or "0")).copy_abs()
    if amount == Decimal("0.00"):
        return {"skipped": True, "reason": "no_promo_credit", "amount": "0.00"}
    return _add_order_fee(
        driver,
        "Discount",
        -amount,
        fallback_code="discount",
    )


def _design_name_set(designs):
    return {
        _clean_text(design.get("design_name")).lower()
        for design in designs
        if _clean_text(design.get("design_name"))
    }


def _inspect_existing_split_order(driver, split_order_id, plan, used_split_indexes=None):
    split_order_id = str(split_order_id or "").strip()
    if not split_order_id:
        raise SplitterError("Existing split order ID is blank.")
    used_split_indexes = set(used_split_indexes or [])
    order_url = _order_url(order_id=split_order_id)
    _open_order_scope_with_reload(driver, order_url, order_id=split_order_id, label=f"existing split order {split_order_id}")
    scan = _scan_original_order(driver)
    existing_names = _design_name_set(scan.get("designs", []))
    matches = []
    for split in plan:
        split_index = int(split.get("split_index") or 0)
        if split_index in used_split_indexes:
            continue
        expected_names = {_clean_text(name).lower() for name in split.get("keep_design_names", []) if _clean_text(name)}
        if expected_names and expected_names == existing_names:
            matches.append(split)
    if not matches:
        raise SplitterError(
            f"Existing split order {split_order_id} did not match any remaining split plan by design names. "
            "Stopping before creating more split orders."
        )
    if len(matches) > 1:
        raise SplitterError(f"Existing split order {split_order_id} matched multiple split plans. Stopping before creating more split orders.")
    split = matches[0]
    promo_discount_fee = _add_discount_fee_to_split_order(driver, split.get("promo_credit", "0.00"))
    totals = _read_order_totals(driver)
    return {
        "split_index": split["split_index"],
        "order_id": split_order_id,
        "existing_order": True,
        "kept_design_names": split["keep_design_names"],
        "kept_design_ids": split["keep_design_ids"],
        "deleted_design_ids": split["delete_design_ids"],
        "shipping_charge": split["shipping_charge"],
        "promo_credit": split.get("promo_credit", "0.00"),
        "promo_code": split.get("promo_code", ""),
        "stock_transfer_records": split.get("stock_transfer_records", []),
        "promo_discount_fee": promo_discount_fee,
        "quote_save": None,
        "configure_result": {"existing_order_id": split_order_id, "matched_by": "design_names"},
        "totals": totals,
    }


def _record_design_key(record):
    design_id = _clean_text(record.get("source_design_id"))
    if design_id:
        return ("id", design_id)
    return ("name", _normalize_design_name(record.get("source_design_name")))


def _matching_new_design(record, new_designs):
    source_id = _clean_text(record.get("source_design_id"))
    source_name = _normalize_design_name(record.get("source_design_name"))
    candidates = []
    if source_id:
        candidates = [design for design in new_designs if _clean_text(design.get("design_id")) == source_id]
    if not candidates and source_name:
        candidates = [design for design in new_designs if _normalize_design_name(design.get("design_name")) == source_name]
    if not candidates:
        raise SplitterError(
            "Could not match stocked source design "
            f"{record.get('source_design_name') or record.get('source_design_id')} to a tab on the new split order."
        )
    if len(candidates) > 1:
        raise SplitterError(
            "Multiple new split-order tabs matched stocked source design "
            f"{record.get('source_design_name') or record.get('source_design_id')}; manual review required."
        )
    match = candidates[0]
    source_quantity = record.get("source_quantity")
    target_quantity = match.get("quantity")
    if source_quantity not in (None, "") and target_quantity not in (None, ""):
        try:
            if int(source_quantity) != int(target_quantity):
                raise SplitterError(
                    f"Matched design {record.get('source_design_name')} has quantity {target_quantity} on the new order, "
                    f"but source quantity was {source_quantity}; manual review required."
                )
        except ValueError:
            pass
    source_subtotal = _clean_text(record.get("source_subtotal"))
    target_subtotal = _clean_text(match.get("subtotal"))
    if source_subtotal and target_subtotal and _money_text(_parse_money(source_subtotal)) != _money_text(_parse_money(target_subtotal)):
        raise SplitterError(
            f"Matched design {record.get('source_design_name')} has subtotal {target_subtotal} on the new order, "
            f"but source subtotal was {source_subtotal}; manual review required."
        )
    return match


def _records_with_new_tab_matches(records, new_scan):
    records = records or []
    new_designs = new_scan.get("designs") or []
    match_cache = {}
    matched = []
    for record in records:
        key = _record_design_key(record)
        if key not in match_cache:
            match_cache[key] = _matching_new_design(record, new_designs)
        new_design = match_cache[key]
        matched.append(
            {
                **record,
                "target_tab_number": new_design.get("tab_number"),
                "target_tab_name": _clean_text(new_design.get("design_name")),
                "target_design_id": _clean_text(new_design.get("design_id")),
            }
        )
    return matched


def _copy_stock_records_to_split_orders(driver, split_orders, login_wait_seconds=0):
    results = []
    for split_order in sorted(split_orders or [], key=lambda item: int(item.get("split_index") or 0)):
        records = split_order.get("stock_transfer_records") or []
        if not records:
            continue
        order_id = str(split_order.get("order_id") or "").strip()
        if not order_id:
            raise SplitterError(f"Split {split_order.get('split_index')} has stock records but no new order ID.")
        order_url = _order_url(order_id=order_id)
        _open_order_scope_with_reload(
            driver,
            order_url,
            order_id=order_id,
            label=f"new split order {order_id} for stock transfer",
        )
        new_scan = _scan_original_order(driver)
        matched_records = _records_with_new_tab_matches(records, new_scan)
        try:
            recording = _product_separator._record_separator_manual_orders(
                driver,
                order_id,
                order_url,
                {"manual_order_records": matched_records},
                login_wait_seconds=login_wait_seconds,
            )
        except Exception as exc:
            raise SplitterError(
                f"Could not copy ordered stock records to split order {order_id}: {exc}"
            ) from exc
        results.append(
            {
                "split_index": split_order.get("split_index"),
                "order_id": order_id,
                "records": matched_records,
                "recording": recording,
            }
        )
    return {"attempted": bool(results), "orders": results}


def _send_mach6_stock_cancel_slack(order_url, dry_run=False):
    channel_url = str(getattr(_config, "COPYRIGHT_CANCEL_MACH6_STOCK_RETURN_SLACK_URL", "") or "").strip()
    message = f"{order_url} cancelled"
    channel_id_match = re.search(r"/client/[^/]+/([^/?#]+)", channel_url)
    channel_id = channel_id_match.group(1) if channel_id_match else ""
    if dry_run:
        return {
            "sent": False,
            "dry_run": True,
            "channel_url": channel_url,
            "channel_id": channel_id,
            "message": message,
        }
    if not channel_url:
        raise SplitterError("Mach6 stock-return Slack channel URL is not configured.")
    ok, result_message = _run_slack_team("custom", custom_message=message, channel_url=channel_url)
    if not ok:
        raise SplitterError(f"Mach6 stock-return Slack message failed for {channel_id or channel_url}: {result_message}")
    return {
        "sent": True,
        "dry_run": False,
        "channel_url": channel_url,
        "channel_id": channel_id,
        "message": message,
        "result": result_message,
    }


def _save_order_and_wait(driver):
    if not _click_ng_button(driver, "saveOrder();", "save order"):
        _order_scope(driver, "runInAngular(s, () => s.saveOrder()); return true;")
    deadline = time.monotonic() + ORDER_SAVE_TIMEOUT_SECONDS
    last = {}
    stable_complete_checks = 0
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            summary = _order_scope(
                driver,
                """
                return {
                  saving: !!s.saving,
                  editMode: !!s.editMode,
                  id: String(r.id || '')
                };
                """,
            )
            visible = {}
            try:
                visible = _visible_order_save_state(driver)
            except Exception:
                visible = {}
            last = {**summary, **visible}
            visible_complete = bool(visible.get("editOrderVisible")) and not bool(visible.get("saveOrderVisible"))
            scope_complete = not bool(summary.get("saving")) and not bool(summary.get("editMode"))
            if visible_complete or (scope_complete and not bool(visible.get("saveOrderEnabled"))):
                stable_complete_checks += 1
            else:
                stable_complete_checks = 0
            if stable_complete_checks >= 2:
                return summary
        except Exception as err:
            last = {"error": str(err)}
            try:
                visible = _visible_order_save_state(driver)
                last.update(visible)
                if bool(visible.get("editOrderVisible")) and not bool(visible.get("saveOrderVisible")):
                    stable_complete_checks += 1
                    if stable_complete_checks >= 2:
                        return visible
                else:
                    stable_complete_checks = 0
            except Exception:
                pass
    raise SplitterError(f"Order save did not complete. Last order save state: {last}")


def _add_refund_fee_to_original(driver, refund_amount):
    refund_amount = Decimal(str(refund_amount or "0")).copy_abs()
    if _original_refund_fee_already_present(driver, refund_amount):
        return _wait_for_order_scope(driver, timeout=10)
    _order_scope(driver, "runInAngular(s, () => s.editModeOn()); return true;")
    time.sleep(0.5)
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
        raise SplitterError("Could not click Add Fee on the original order.")
    time.sleep(0.5)
    _order_scope(
        driver,
        """
        const amount = arguments[0];
        const fees = r.orderFees || r.fees || [];
        if (!fees.length) throw new Error('No fee row was created');
        const fee = fees[fees.length - 1];
        fee.feeId = 12;
        fee.name = 'Refund';
        fee.code = 'refund';
        fee.amount = amount;
        fee.crudAction = fee.crudAction || 'c';
        r.orderFees = fees;
        runInAngular(s, () => {});
        return {feeId: fee.feeId, amount: fee.amount};
        """,
        f"-{_money_text(refund_amount)}",
    )
    return _save_order_and_wait(driver)


def _cancel_original_order(driver):
    # Prefer the same visible status controls the user uses: type "cancel", pick "cancel order", apply.
    updated = bool(
        driver.execute_script(
            """
            const input = Array.from(document.querySelectorAll('input')).find((el) => {
              const rect = el.getBoundingClientRect();
              return rect.width > 80 && rect.height > 15 && rect.top < 250;
            });
            if (!input) return false;
            input.focus();
            input.value = 'cancel';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            input.dispatchEvent(new Event('change', {bubbles: true}));
            input.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'l'}));
            return true;
            """
        )
    )
    if updated:
        time.sleep(1)
        driver.execute_script(
            """
            const option = Array.from(document.querySelectorAll('li,a,div,span')).find((el) => {
              return (el.innerText || '').replace(/\\s+/g, ' ').trim().toLowerCase() === 'cancel order';
            });
            if (option) option.click();
            """
        )
        time.sleep(0.5)
        _click_ng_button(driver, "updateOrderStatus();", "apply")
        time.sleep(1)
        _click_modal_choice(driver, "yes")
    else:
        _order_scope(
            driver,
            """
            s.orderStatusName = 'cancel order';
            runInAngular(s, () => s.updateOrderStatus());
            return true;
            """,
        )
        time.sleep(1)
        _click_modal_choice(driver, "yes")

    deadline = time.monotonic() + 45
    last_statuses = []
    while time.monotonic() < deadline:
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
            )
            last_statuses = [
                _clean_text(value)
                for value in (status_summary.get("values", []) + status_summary.get("history", []))
                if _clean_text(value)
            ]
            if any(_is_cancel_order_status(value) for value in last_statuses):
                return True
        except Exception:
            pass
        try:
            text = driver.execute_script("return document.body ? document.body.innerText : '';")
            if _status_history_confirms_cancel_order(text):
                return True
        except Exception:
            pass
        time.sleep(1)
    detail = f" Last status seen: {', '.join(last_statuses[:5])}." if last_statuses else ""
    raise SplitterError(f"Original order cancellation was not confirmed on the page.{detail}")


def _add_original_transfer_note(driver, note):
    _order_scope(
        driver,
        """
        const note = arguments[0];
        runInAngular(s, () => {
          s.editModeOn();
          const existing = r.addSalesNotes || '';
          r.addSalesNotes = existing && existing.toLowerCase().includes(note.toLowerCase())
            ? existing
            : [existing, note].filter(Boolean).join(existing.trim() ? '\\n\\n' : '');
          if (s.order.setAddSalesNotes) s.order.setAddSalesNotes(r.addSalesNotes);
        });
        return true;
        """,
        note,
    )
    return _save_order_and_wait(driver)


def _finalize_original_order_after_split(driver, payment_detected, refund_amount, original_grand_total, transfer_note):
    if payment_detected:
        _add_refund_fee_to_original(driver, refund_amount)
        refunded_totals = _read_order_totals(driver)
        _cancel_original_order(driver)
        _open_record_transaction(driver, quote=False)
        _save_transaction_modal_with_amount(driver, "Refund", transfer_note, amount=-original_grand_total)
        time.sleep(2)
    else:
        refunded_totals = None
        _cancel_original_order(driver)

    _add_original_transfer_note(driver, transfer_note)
    return {
        "refund_fee_amount": _money_text(refund_amount) if payment_detected else "0.00",
        "refund_transaction_id": transfer_note if payment_detected else "",
        "payment_actions_skipped": not payment_detected,
        "sales_note": transfer_note,
        "refunded_totals": refunded_totals,
        "final_totals": _read_order_totals(driver),
    }


def _visible_design_tab_numbers(driver):
    return driver.execute_script(
        """
        const rows = [];
        const seen = new Set();
        const nodes = Array.from(document.querySelectorAll('a,button,div,li,span'));
        for (const el of nodes) {
          const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
          const match = text.match(/(?:^|[^\\d])(\\d{1,3})\\s*-\\s*QTY\\s*:\\s*\\d+/i);
          if (!match) continue;
          const rect = el.getBoundingClientRect();
          if (rect.width < 12 || rect.height < 12) continue;
          const number = Number(match[1]);
          if (!Number.isFinite(number) || seen.has(number)) continue;
          seen.add(number);
          rows.push({tab_number: number, text, x: rect.x, y: rect.y, width: rect.width, height: rect.height});
        }
        rows.sort((a, b) => a.tab_number - b.tab_number);
        return rows;
        """
    )


def _click_design_tab(driver, tab_number):
    return bool(
        driver.execute_script(
            """
            const targetNumber = Number(arguments[0]);
            const nodes = Array.from(document.querySelectorAll('a,button,div,li,span'));
            const matches = [];
            for (const el of nodes) {
              const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
              const match = text.match(/(?:^|[^\\d])(\\d{1,3})\\s*-\\s*QTY\\s*:\\s*\\d+/i);
              if (!match || Number(match[1]) !== targetNumber) continue;
              const rect = el.getBoundingClientRect();
              if (rect.width < 12 || rect.height < 12) continue;
              matches.push({el, rect});
            }
            if (!matches.length) return false;
            matches.sort((a, b) => (a.rect.y - b.rect.y) || (a.rect.x - b.rect.x));
            matches[0].el.scrollIntoView({block: 'center', inline: 'center'});
            matches[0].el.click();
            return true;
            """,
            int(tab_number),
        )
    )


def _design_total_tab_numbers_from_page_text(driver):
    body_text = driver.execute_script("return document.body ? document.body.innerText : '';")
    numbers = []
    seen = set()
    for match in re.finditer(r"\bDesign\s+(\d{1,3})\s+Total\s*:", str(body_text or ""), re.IGNORECASE):
        number = int(match.group(1))
        if number not in seen:
            seen.add(number)
            numbers.append(number)
    return numbers


def _scan_current_design_detail(driver, tab_number):
    body_text = driver.execute_script("return document.body ? document.body.innerText : '';")
    name_match = re.search(r"Design Name:\s*([^\|\n\r]+)", body_text, re.IGNORECASE)
    id_match = re.search(r"Design ID:\s*([^\|\n\r]+)", body_text, re.IGNORECASE)
    subtotal_match = re.search(r"Subtotal:\s*\$?([0-9,]+\.\d{2})", body_text, re.IGNORECASE)
    quantity_match = re.search(r"Quantity:\s*(\d+)", body_text, re.IGNORECASE)
    price_matches = re.findall(r"Price:\s*\$?([0-9,]+\.\d{2})|(?:^|\s)\$([0-9,]+\.\d{2})", body_text, re.IGNORECASE)
    prices = []
    for first, second in price_matches:
        value = first or second
        if value:
            prices.append(_money_text(_parse_money(value)))
    return {
        "tab_number": int(tab_number),
        "design_id": _clean_text(id_match.group(1)) if id_match else "",
        "design_name": _clean_text(name_match.group(1)) if name_match else "",
        "quantity": int(quantity_match.group(1)) if quantity_match else None,
        "subtotal": _money_text(_parse_money(subtotal_match.group(1))) if subtotal_match else "",
        "visible_prices": prices[:20],
        "stock": _product_separator._stock_state_from_text(body_text),
    }


def _extract_order_totals_from_text(body_text):
    def find_money(label):
        match = re.search(rf"{re.escape(label)}\s*:?\s*(?:\$)?(-?[0-9,]+\.\d{{2}}|Free)", body_text, re.IGNORECASE)
        if not match:
            return ""
        return _money_text(_parse_money(match.group(1)))

    due_date = ""
    due_date_match = re.search(r"Due Date:\s*([0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}|[A-Za-z]+ [A-Za-z]+ Ship)", body_text, re.IGNORECASE)
    if due_date_match:
        due_date = _clean_text(due_date_match.group(1))

    due_time = ""
    due_time_match = re.search(r"Due Time:\s*([0-9]{1,2}:[0-9]{2}\s*[AP]M)", body_text, re.IGNORECASE)
    if due_time_match:
        due_time = _clean_text(due_time_match.group(1)).replace(" ", "")

    payment_type = ""
    payment_match = re.search(r"Payments and Credits\s+Amount\s+Type\s+Date\s+\$?[0-9,]+\.\d{2}\s+([^\n\r]+?)\s+[0-9]{1,2}/[0-9]{1,2}/[0-9]{2}", body_text, re.IGNORECASE)
    if payment_match:
        payment_type = _clean_text(payment_match.group(1))

    promo_amount = ""
    promo_code = ""
    promo_match = re.search(
        r"Promo(?:\(s\)|s)?\s*:?\s*(?:\$)?(-?[0-9,]+\.\d{2}|Free)(?:\s*\[([^\]]+)\])?",
        body_text,
        re.IGNORECASE,
    )
    if promo_match:
        promo_amount = _money_text(_parse_money(promo_match.group(1)))
        promo_code = _clean_text(promo_match.group(2))

    return {
        "subtotal": find_money("Subtotals"),
        "subtotal_before_tax": find_money("Subtotal before Tax"),
        "sales_tax": find_money("Sales Tax"),
        "grand_total": find_money("Grand Total"),
        "paid": find_money("Paid"),
        "balance_due": find_money("Balance Due"),
        "shipping": find_money("Shipping"),
        "promo": promo_amount,
        "promo_code": promo_code,
        "due_date": due_date,
        "due_time": due_time,
        "payment_type": payment_type,
    }


def _scan_original_order(driver, expected_tab_count=None):
    def detect_tabs():
        detected = []
        deadline = time.monotonic() + max(45, PROCESSOR_PAGE_LOAD_TIMEOUT)
        while time.monotonic() < deadline:
            detected = _visible_design_tab_numbers(driver)
            if detected:
                break
            time.sleep(1)
        if detected:
            return detected
        total_numbers = _design_total_tab_numbers_from_page_text(driver)
        if expected_tab_count is not None:
            expected_numbers = list(range(1, int(expected_tab_count) + 1))
        else:
            expected_numbers = []
            for number in total_numbers:
                if number == len(expected_numbers) + 1:
                    expected_numbers.append(number)
                else:
                    break
        if expected_numbers and total_numbers[: len(expected_numbers)] == expected_numbers:
            return [
                {
                    "tab_number": number,
                    "text": f"Design {number} Total fallback marker",
                    "fallback": "order_totals",
                }
                for number in expected_numbers
            ]
        return []

    tabs = detect_tabs()
    expected_mismatch = expected_tab_count is not None and len(tabs) != int(expected_tab_count)
    if not tabs or expected_mismatch:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        driver.refresh()
        _wait_for_crm_context(driver)
        tabs = detect_tabs()
    detected_count = len(tabs)
    if not tabs:
        body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))
        raise SplitterError(
            f"No CRM design tabs were detected after one refresh. Visible text starts: {body_text[:300]}"
        )
    if expected_tab_count is not None and detected_count != int(expected_tab_count):
        body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))
        current_url = driver.current_url
        title = driver.title
        raise SplitterError(
            f"Incorrect number of tabs. User expected {expected_tab_count}, but CRM shows {detected_count}. "
            f"Page title: {title}. URL: {current_url}. Visible text starts: {body_text[:300]}"
        )

    designs = []
    for tab in tabs:
        tab_number = int(tab["tab_number"])
        _click_design_tab(driver, tab_number)
        time.sleep(0.35)
        design = _scan_current_design_detail(driver, tab_number)
        if not design.get("design_name"):
            design["design_name"] = f"UNREAD_TAB_{tab_number}"
            design["warning"] = "Design name was not readable from the selected tab."
        designs.append(design)

    body_text = driver.execute_script("return document.body ? document.body.innerText : '';")
    totals = _extract_order_totals_from_text(body_text)
    order_stock_status = _product_separator._order_stock_status_from_text(body_text)
    stock_summary = _summarize_original_stock(designs, order_stock_status=order_stock_status)
    subcontractor = _subcontractor_from_page_text(body_text)
    return {
        "detected_tab_count": detected_count,
        "visible_tab_markers": tabs,
        "designs": designs,
        "totals": totals,
        "order_stock_status": order_stock_status,
        "stock_summary": stock_summary,
        "subcontractor": subcontractor,
        "stock_routing": _planned_stock_routing(stock_summary, subcontractor),
    }


def _transaction_tag_for_payment_type(payment_type):
    text = str(payment_type or "").lower()
    if "paypal" in text:
        return "PayPal"
    if "stripe" in text or "sezzle" in text or "affirm" in text:
        return "Stripe Manual CC Entry"
    return "Stripe Manual CC Entry"


def run_smoke_test(open_browser=False, visible=False, result_file=None):
    driver = None
    started = time.monotonic()
    try:
        driver, target = _open_browser_if_requested(
            "smoke_test",
            dry_run=True,
            visible=visible,
            open_browser=open_browser,
        )
        if driver is not None:
            safe_take_screenshot(driver, "processor_smoke_test")
        message = "Smoke test passed."
        if target:
            message = f"Smoke test passed. Browser target: {target}"
        _write_result(
            True,
            message,
            result_file=result_file,
            action="smoke_test",
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 0
    except Exception as err:
        _write_result(
            False,
            f"Smoke test failed: {err}",
            result_file=result_file,
            action="smoke_test",
            error_type=type(err).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 1
    finally:
        safe_driver_quit(driver, profile_path=_profile_path())


def run_process_order(order_id=None, dry_run=True, visible=False, result_file=None):
    started = time.monotonic()
    if not order_id:
        _write_result(False, "Order ID is required for process_order.", result_file=result_file, action="process_order")
        return 2

    if not dry_run:
        _write_result(
            False,
            "Live process_order is intentionally disabled in the template. Implement the final-click logic first.",
            result_file=result_file,
            action="process_order",
            target_order_id=str(order_id),
        )
        return 3

    driver = None
    report = None
    split_orders = []
    try:
        driver, target = _open_browser_if_requested(
            "process_order",
            dry_run=True,
            visible=visible,
            order_id=order_id,
            open_browser=True,
        )

        # TODO: Navigate to the order, inspect page state, and collect what would change.
        # Keep dry-run mode free of final submit/save/order clicks.
        report = [
            {
                "order_id": str(order_id),
                "outcome": "dry_run_template",
                "message": "Template reached dry-run mode. Add page inspection logic here.",
            }
        ]
        _write_result(
            True,
            f"Dry run complete for order {order_id}.",
            result_file=result_file,
            action="process_order",
            dry_run=True,
            target_order_id=str(order_id),
            order_count=1,
            order_ids=[str(order_id)],
            report=report,
            browser_target=target,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 0
    except Exception as err:
        if driver is not None:
            safe_take_screenshot(driver, "processor_order_error")
        _write_result(
            False,
            f"Dry run failed for order {order_id}: {err}",
            result_file=result_file,
            action="process_order",
            dry_run=True,
            target_order_id=str(order_id),
            error_type=type(err).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 1
    finally:
        safe_driver_quit(driver, profile_path=_profile_path())


PROCESS_BATCH_REPORT_ORDER_IDS_JS = r"""
const ids = new Set();
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function visible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
  return style.display !== 'none' && style.visibility !== 'hidden';
}
function addFromText(text) {
  for (const match of clean(text).matchAll(/\b\d{7}\b/g)) ids.add(match[0]);
}
for (const link of Array.from(document.querySelectorAll('a')).filter(visible)) {
  addFromText(link.innerText || link.textContent || '');
  const href = String(link.getAttribute('href') || '');
  const match = href.match(/\/order\/(\d{7})\b/);
  if (match) ids.add(match[1]);
}
for (const row of Array.from(document.querySelectorAll('tr')).filter(visible)) {
  addFromText(row.innerText || row.textContent || '');
}
return Array.from(ids);
"""


def _extract_process_batch_order_ids(driver, list_url, exclude_order_ids=None):
    target = (list_url or PROCESSOR_LIST_URL or PROCESSOR_LOGIN_URL or "").strip()
    if not target:
        raise SplitterError("Auto Splitter batch list URL is empty.")
    safe_get_with_partial_load(driver, target, "auto splitter batch list")
    _handle_login_if_needed(driver, target)
    excluded = {str(order_id).strip() for order_id in (exclude_order_ids or []) if str(order_id).strip()}
    deadline = time.monotonic() + max(45, PROCESSOR_PAGE_LOAD_TIMEOUT)
    order_ids = []
    while time.monotonic() < deadline:
        try:
            order_ids = driver.execute_script(PROCESS_BATCH_REPORT_ORDER_IDS_JS) or []
        except Exception:
            order_ids = []
        cleaned = []
        seen = set()
        for raw in order_ids:
            order_id = str(raw or "").strip()
            if not re.fullmatch(r"\d{7}", order_id) or order_id in excluded or order_id in seen:
                continue
            seen.add(order_id)
            cleaned.append(order_id)
        if cleaned:
            return cleaned
        time.sleep(1)
    return []


def run_process_batch(list_url=None, dry_run=True, visible=False, result_file=None):
    started = time.monotonic()
    if not dry_run:
        _write_result(
            False,
            "Live process_batch is intentionally disabled in the template. Implement the final-click logic first.",
            result_file=result_file,
            action="process_batch",
        )
        return 3

    driver = None
    try:
        driver, target = _open_browser_if_requested(
            "process_batch",
            dry_run=True,
            visible=visible,
            list_url=list_url,
            open_browser=True,
        )

        order_ids = []
        report = []
        attempted_order_ids = set()
        refresh_passes = 0
        while True:
            pass_order_ids = _extract_process_batch_order_ids(driver, target, exclude_order_ids=set(attempted_order_ids))
            if not pass_order_ids:
                break
            refresh_passes += 1
            for order_id in pass_order_ids:
                attempted_order_ids.add(order_id)
                order_ids.append(order_id)
                report.append(
                    {
                        "order_id": str(order_id),
                        "outcome": "dry_run_template",
                        "message": "Template reached batch dry-run mode. Add page inspection logic here.",
                    }
                )
            print(f"Finished Auto Splitter batch refresh pass {refresh_passes}; reopening the list to look for additional orders...")

        _write_result(
            True,
            f"Batch dry run complete. Found {len(order_ids)} order(s) across {max(1, refresh_passes)} list refresh pass(es).",
            result_file=result_file,
            action="process_batch",
            dry_run=True,
            order_count=len(order_ids),
            order_ids=order_ids,
            report=report,
            browser_target=target,
            duration_seconds=round(time.monotonic() - started, 2),
            refresh_passes=refresh_passes,
        )
        return 0
    except Exception as err:
        if driver is not None:
            safe_take_screenshot(driver, "processor_batch_error")
        _write_result(
            False,
            f"Batch dry run failed: {err}",
            result_file=result_file,
            action="process_batch",
            dry_run=True,
            error_type=type(err).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 1
    finally:
        safe_driver_quit(driver, profile_path=_profile_path())


def run_split_order(
    order_id=None,
    order_url=None,
    expected_tab_count=None,
    divisions=None,
    minimum_tabs=DEFAULT_MINIMUM_SPLIT_TABS,
    login_wait_seconds=0,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    dry_run=True,
    visible=False,
    result_file=None,
    resume_existing_order_ids=None,
    parallel_workers=1,
):
    started = time.monotonic()
    resolved_order_id = _extract_order_id(order_id=order_id, order_url=order_url)
    target_url = _order_url(order_id=order_id, order_url=order_url)

    if not target_url:
        _write_result(False, "Order ID or CRM order URL is required for split_order.", result_file=result_file, action="split_order")
        return 2
    try:
        expected_tab_count = int(expected_tab_count) if expected_tab_count is not None else None
        divisions = int(divisions) if divisions is not None else None
        minimum_tabs = int(minimum_tabs)
        if expected_tab_count is not None and expected_tab_count <= minimum_tabs:
            raise SplitterError(
                f"Order has {expected_tab_count} tab(s). Auto Splitter only splits orders with more than {minimum_tabs} tabs."
            )
        if expected_tab_count is not None and divisions is not None:
            _validate_split_ranges_within_limit(_split_ranges(expected_tab_count, divisions), minimum_tabs)
    except Exception as err:
        _write_result(
            False,
            f"Invalid split request: {err}",
            result_file=result_file,
            action="split_order",
            target_order_id=resolved_order_id,
            order_url=target_url,
            error_type=type(err).__name__,
        )
        return 2

    driver = None
    report = None
    split_orders = []
    worker_profiles = []
    resume_existing_order_ids = [
        str(value or "").strip()
        for value in (resume_existing_order_ids or [])
        if str(value or "").strip()
    ]
    try:
        profile = _profile_path()
        if attach_browser:
            parallel_workers = 1
            driver = build_attached_chrome_driver(debugger_address=debugger_address)
            driver.set_script_timeout(max(PROCESSOR_ACTION_TIMEOUT, AUTO_SPLITTER_SCRIPT_TIMEOUT_SECONDS))
        else:
            kill_stale_chrome(profile, profile_label="CRM auto splitter")
            if not dry_run and divisions is not None and int(parallel_workers or 1) > 1:
                parallel_workers = _normalize_parallel_workers(parallel_workers, divisions=divisions)
                worker_profiles = _prepare_parallel_profiles(profile, parallel_workers)
            driver = _build_splitter_driver(profile, visible=visible)
        safe_get_with_partial_load(driver, target_url, "original CRM order")
        _handle_login_if_needed(driver, target_url, login_wait_seconds=login_wait_seconds)
        _switch_to_crm_app_frame(driver)

        scan = _scan_original_order(driver, expected_tab_count=expected_tab_count)
        detected_tab_count = int(scan["detected_tab_count"])
        if expected_tab_count is None:
            expected_tab_count = detected_tab_count
        if detected_tab_count <= minimum_tabs:
            raise SplitterError(
                f"Order has {detected_tab_count} tab(s). Auto Splitter only splits orders with more than {minimum_tabs} tabs."
            )
        if divisions is None:
            divisions = _auto_divisions_for_tab_count(detected_tab_count, minimum_tabs)
        ranges = _split_ranges(detected_tab_count, divisions)
        _validate_split_ranges_within_limit(ranges, minimum_tabs)
        parallel_workers = _normalize_parallel_workers(parallel_workers, divisions=divisions)
        if not worker_profiles:
            parallel_workers = 1
        shipping_amount = _parse_money(scan.get("totals", {}).get("shipping"))
        promo_amount = _parse_money(scan.get("totals", {}).get("promo")).copy_abs()
        promo_code = scan.get("totals", {}).get("promo_code", "")
        plan = _build_split_plan(
            scan["designs"],
            divisions,
            resolved_order_id or "UNKNOWN",
            shipping_amount=shipping_amount,
            promo_amount=promo_amount,
            promo_code=promo_code,
        )
        stock_summary = scan.get("stock_summary") or {}
        subcontractor = _clean_text(scan.get("subcontractor"))
        stock_routing = _planned_stock_routing(stock_summary, subcontractor)
        if stock_routing.get("action") == "slack_mach6_cancelled":
            stock_routing["message"] = f"{target_url} cancelled"
        original_note_after_split = f"transferred to {_format_order_list(['<split order #>' for _ in range(divisions)])}"
        payment_type = scan.get("totals", {}).get("payment_type", "")
        payment_detected = _parse_money(scan.get("totals", {}).get("paid")) > Decimal("0.00")
        report = {
            "original_order_id": resolved_order_id,
            "order_url": target_url,
            "detected_tab_count": scan["detected_tab_count"],
            "expected_tab_count": expected_tab_count,
            "divisions": divisions,
            "minimum_tabs": minimum_tabs,
            "parallel_workers": parallel_workers if not dry_run else 1,
            "designs": scan["designs"],
            "totals": scan["totals"],
            "order_stock_status": scan.get("order_stock_status", {}),
            "stock_summary": stock_summary,
            "subcontractor": subcontractor,
            "stock_routing": stock_routing,
            "split_plan": plan,
            "promo_allocation_total": _money_text(promo_amount),
            "promo_code": _clean_text(promo_code),
            "payment_transfer": {
                "payment_detected": payment_detected,
                "mode": "split_payment" if payment_detected else "no_payment",
                "original_payment_type": payment_type,
                "split_transaction_tag": _transaction_tag_for_payment_type(payment_type) if payment_detected else "",
                "transaction_id": "<read from original payment view popup during live run>" if payment_detected else "",
            },
            "original_order_final_steps": {
                "refund_fee_amount": (
                    scan.get("totals", {}).get("subtotal") or scan.get("totals", {}).get("subtotal_before_tax")
                ) if payment_detected else "0.00",
                "cancel_status": "cancel order",
                "refund_transaction_tag": "Refund" if payment_detected else "",
                "refund_transaction_id": original_note_after_split if payment_detected else "",
                "payment_actions_skipped": not payment_detected,
                "sales_note": original_note_after_split,
                "never_click_payment_refund_button": True,
            },
        }

        if not dry_run:
            if stock_routing.get("action") == "manual_review":
                raise SplitterError(
                    f"Auto Splitter stock routing requires manual review: {stock_routing.get('reason') or 'unknown reason'}."
                )
            original_state = _get_order_live_state(driver)
            payment_detected = _payment_is_detected(original_state)
            transaction_id = ""
            if payment_detected:
                payment_info = _get_original_payment_info(driver)
                payment_type = payment_info.get("payment_type") or payment_type
                transaction_id = payment_info.get("transaction_id", "")
                if not transaction_id:
                    raise SplitterError("Original payment transaction ID could not be read from the payment view popup.")

            split_total = Decimal("0.00")
            report.update(
                {
                    "dry_run": False,
                    "payment_transfer": {
                        "payment_detected": payment_detected,
                        "mode": "split_payment" if payment_detected else "no_payment",
                        "original_payment_type": payment_type,
                        "split_transaction_tag": _transaction_tag_for_payment_type(payment_type) if payment_detected else "",
                        "transaction_id": transaction_id,
                    },
                    "split_orders": split_orders,
                    "completed_split_count": 0,
                    "remaining_split_count": len(plan),
                    "partial": True,
                    "parallel_workers": parallel_workers,
                    "resume_existing_order_ids": resume_existing_order_ids,
                }
            )
            completed_split_indexes = set()
            for existing_order_id in resume_existing_order_ids:
                existing_split = _inspect_existing_split_order(driver, existing_order_id, plan, used_split_indexes=completed_split_indexes)
                split_orders.append(existing_split)
                completed_split_indexes.add(int(existing_split["split_index"]))
                split_total += Decimal(existing_split["totals"]["grand_total"])
                report["completed_split_count"] = len(split_orders)
                report["remaining_split_count"] = max(len(plan) - len(split_orders), 0)
                report["split_total_so_far"] = _money_text(split_total)

            pending_splits = [
                split
                for split in plan
                if int(split.get("split_index") or 0) not in completed_split_indexes
            ]
            if parallel_workers > 1 and len(pending_splits) > 1:
                worker_count = min(parallel_workers, len(pending_splits), len(worker_profiles))
                pending_iter = iter(pending_splits)
                futures = {}

                def _submit_next(executor, profile_for_split):
                    next_split = next(pending_iter, None)
                    if next_split is None:
                        return False
                    future = executor.submit(
                        _create_split_order_in_worker,
                        next_split,
                        resolved_order_id,
                        target_url,
                        expected_tab_count,
                        original_state,
                        payment_type,
                        transaction_id,
                        profile_for_split,
                        visible,
                    )
                    futures[future] = profile_for_split
                    return True

                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    for profile_for_split in worker_profiles[:worker_count]:
                        _submit_next(executor, profile_for_split)
                    worker_errors = []
                    while futures:
                        future = next(as_completed(list(futures)))
                        profile_for_split = futures.pop(future)
                        try:
                            split_order = future.result()
                        except Exception as err:
                            worker_errors.append(
                                {
                                    "profile": profile_for_split,
                                    "error_type": type(err).__name__,
                                    "message": str(err),
                                }
                            )
                            report["parallel_worker_errors"] = worker_errors
                            continue
                        split_total += Decimal(split_order["totals"]["grand_total"])
                        split_orders.append(split_order)
                        report["split_orders"] = sorted(split_orders, key=lambda item: int(item.get("split_index") or 0))
                        report["completed_split_count"] = len(split_orders)
                        report["remaining_split_count"] = max(len(plan) - len(split_orders), 0)
                        report["split_total_so_far"] = _money_text(split_total)
                        if not worker_errors:
                            _submit_next(executor, profile_for_split)
                    if worker_errors:
                        first_error = worker_errors[0]
                        raise SplitterError(
                            f"Parallel split worker failed after {len(split_orders)}/{len(plan)} split order(s) were recorded. "
                            f"{first_error.get('error_type')}: {first_error.get('message')}"
                        )
                split_orders.sort(key=lambda item: int(item.get("split_index") or 0))
            else:
                for split in pending_splits:
                    _open_order_scope_with_reload(
                        driver,
                        target_url,
                        order_id=resolved_order_id,
                        label=f"original CRM order before split {split['split_index']}",
                    )
                    _copy_order_to_quote(driver, resolved_order_id, expected_tab_count)
                    configured = _configure_quote_split(driver, split, original_state)
                    promo_discount_fee = _add_discount_fee_to_split_quote(driver, split.get("promo_credit", "0.00"))
                    saved_quote = _save_quote(driver)
                    new_order_id = _finalize_split_quote_and_wait_for_order(driver, payment_type, transaction_id)
                    _open_order_scope_with_reload(
                        driver,
                        _order_url(order_id=new_order_id),
                        order_id=new_order_id,
                        label=f"new split order {new_order_id}",
                    )
                    totals = _read_order_totals(driver)
                    split_total += Decimal(totals["grand_total"])
                    split_orders.append(
                        {
                            "split_index": split["split_index"],
                            "order_id": new_order_id,
                            "existing_order": False,
                            "kept_design_names": split["keep_design_names"],
                            "kept_design_ids": split["keep_design_ids"],
                            "deleted_design_ids": split["delete_design_ids"],
                            "shipping_charge": split["shipping_charge"],
                            "promo_credit": split.get("promo_credit", "0.00"),
                            "promo_code": split.get("promo_code", ""),
                            "stock_transfer_records": split.get("stock_transfer_records", []),
                            "promo_discount_fee": promo_discount_fee,
                            "quote_save": saved_quote,
                            "configure_result": configured,
                            "totals": totals,
                        }
                    )
                    report["completed_split_count"] = len(split_orders)
                    report["remaining_split_count"] = max(len(plan) - len(split_orders), 0)
                    report["split_total_so_far"] = _money_text(split_total)

            if stock_routing.get("action") == "copy_to_split_orders":
                stock_transfer_result = _copy_stock_records_to_split_orders(
                    driver,
                    split_orders,
                    login_wait_seconds=login_wait_seconds,
                )
                report["stock_transfer"] = stock_transfer_result

            original_grand_total = Decimal(_money_text(original_state.get("grand_total") or scan.get("totals", {}).get("grand_total") or "0"))
            if original_grand_total == Decimal("0.00") and resume_existing_order_ids and split_total > Decimal("0.00"):
                original_grand_total = split_total.quantize(Decimal("0.01"))
            split_total_delta = (split_total - original_grand_total).quantize(Decimal("0.01"))
            split_total_mismatch_warning = ""
            if split_total_delta.copy_abs() > SPLIT_TOTAL_TOLERANCE:
                split_total_mismatch_warning = _split_total_mismatch_message(
                    original_grand_total,
                    split_total,
                    split_total_delta,
                )
                report["split_total_mismatch"] = {
                    "old_original_total": _money_text(original_grand_total),
                    "new_split_total": _money_text(split_total),
                    "difference": _money_text(split_total_delta),
                    "message": split_total_mismatch_warning,
                }
            elif split_total_delta:
                report["split_total_rounding_delta"] = _money_text(split_total_delta)

            transfer_note = f"transferred to {_format_order_list([item['order_id'] for item in split_orders])}"
            refund_amount = Decimal(
                _money_text(
                    scan.get("totals", {}).get("subtotal_before_tax")
                    or original_state.get("subtotal")
                    or scan.get("totals", {}).get("subtotal")
                    or "0"
                )
            )
            existing_refund_amount = _existing_original_refund_fee_amount(driver)
            if refund_amount == Decimal("0.00") and existing_refund_amount > Decimal("0.00"):
                refund_amount = existing_refund_amount
            elif refund_amount == Decimal("0.00") and resume_existing_order_ids:
                refund_amount = split_total.quantize(Decimal("0.01"))
            _open_order_scope_with_reload(
                driver,
                target_url,
                order_id=resolved_order_id,
                label="original CRM order for refund and cancellation",
            )
            original_finalization = _finalize_original_order_after_split(
                driver,
                payment_detected,
                refund_amount,
                original_grand_total,
                transfer_note,
            )
            if stock_routing.get("action") == "slack_mach6_cancelled":
                report["stock_cancel_slack"] = _send_mach6_stock_cancel_slack(target_url, dry_run=False)

            report.update(
                {
                    "dry_run": False,
                    "payment_transfer": {
                        "payment_detected": payment_detected,
                        "mode": "split_payment" if payment_detected else "no_payment",
                        "original_payment_type": payment_type,
                        "split_transaction_tag": _transaction_tag_for_payment_type(payment_type) if payment_detected else "",
                        "transaction_id": transaction_id,
                    },
                    "split_orders": split_orders,
                    "split_total": _money_text(split_total),
                    "original_grand_total": _money_text(original_grand_total),
                    "split_total_delta": _money_text(split_total_delta),
                    "completed_split_count": len(split_orders),
                    "remaining_split_count": 0,
                    "parallel_workers": parallel_workers,
                    "partial": False,
                    "original_order_final_steps": {
                        **report["original_order_final_steps"],
                        **original_finalization,
                    },
                }
            )
            completion_message = (
                f"Auto-split complete for order {resolved_order_id}. "
                f"New split orders: {_format_order_list([item['order_id'] for item in split_orders])}."
            )
            if split_total_mismatch_warning:
                report["total_mismatch_warning"] = split_total_mismatch_warning
                completion_message = f"{completion_message} {split_total_mismatch_warning}"
            _write_result(
                True,
                completion_message,
                result_file=result_file,
                action="split_order",
                dry_run=False,
                status="completed",
                target_order_id=resolved_order_id,
                order_url=target_url,
                detected_tab_count=scan["detected_tab_count"],
                expected_tab_count=expected_tab_count,
                divisions=divisions,
                new_order_ids=[item["order_id"] for item in split_orders],
                total_mismatch_warning=split_total_mismatch_warning,
                report=report,
                duration_seconds=round(time.monotonic() - started, 2),
            )
            return 0

        _write_result(
            True,
            f"Auto-split dry run complete for order {resolved_order_id or target_url}. No CRM changes were made.",
            result_file=result_file,
            action="split_order",
            dry_run=True,
            target_order_id=resolved_order_id,
            order_url=target_url,
            detected_tab_count=scan["detected_tab_count"],
            expected_tab_count=expected_tab_count,
            divisions=divisions,
            report=report,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 0
    except SplitterError as err:
        if driver is not None:
            safe_take_screenshot(driver, "auto_split_stopped")
        extra = {
            "action": "split_order",
            "dry_run": bool(dry_run),
            "target_order_id": resolved_order_id,
            "order_url": target_url,
            "error_type": type(err).__name__,
            "duration_seconds": round(time.monotonic() - started, 2),
        }
        if report is not None:
            extra["report"] = report
            extra["new_order_ids"] = [item.get("order_id") for item in split_orders if item.get("order_id")]
            extra["completed_split_count"] = len(split_orders)
            extra["remaining_split_count"] = max(len(report.get("split_plan", [])) - len(split_orders), 0)
        _write_result(False, str(err), result_file=result_file, **extra)
        return 4
    except Exception as err:
        if driver is not None:
            safe_take_screenshot(driver, "auto_split_order_error")
        extra = {
            "action": "split_order",
            "dry_run": bool(dry_run),
            "target_order_id": resolved_order_id,
            "order_url": target_url,
            "error_type": type(err).__name__,
            "duration_seconds": round(time.monotonic() - started, 2),
        }
        if report is not None:
            extra["report"] = report
            extra["new_order_ids"] = [item.get("order_id") for item in split_orders if item.get("order_id")]
            extra["completed_split_count"] = len(split_orders)
            extra["remaining_split_count"] = max(len(report.get("split_plan", [])) - len(split_orders), 0)
        _write_result(False, f"Auto-split failed for order {resolved_order_id or target_url}: {err}", result_file=result_file, **extra)
        return 1
    finally:
        if attach_browser:
            pass
        else:
            safe_driver_quit(driver, profile_path=_profile_path())
        _cleanup_parallel_profiles(worker_profiles)


def main(argv=None):
    parser = argparse.ArgumentParser(description="CRM processor automation worker.")
    parser.add_argument("--action", choices=["smoke_test", "process_order", "process_batch", "split_order"], default="smoke_test")
    parser.add_argument("--order-id", default="")
    parser.add_argument("--order-url", default="")
    parser.add_argument("--list-url", default="")
    parser.add_argument("--tab-count", type=int, default=None, help="Expected number of design tabs on the original order.")
    parser.add_argument("--divisions", type=int, default=None, help="Number of split orders to create.")
    parser.add_argument("--minimum-tabs", type=int, default=DEFAULT_MINIMUM_SPLIT_TABS)
    parser.add_argument("--parallel-workers", type=int, default=1, help="Live split workers for creating split orders. Original cleanup remains serial.")
    parser.add_argument("--login-wait-seconds", type=int, default=0, help="Wait this long for manual login if CRM opens the login page.")
    parser.add_argument("--attach-browser", action="store_true", help="Attach to Chrome already opened by open_crm_profile.command.")
    parser.add_argument("--debugger-address", default="127.0.0.1:9222")
    parser.add_argument("--dry-run", action="store_true", default=PROCESSOR_DRY_RUN)
    parser.add_argument("--real", action="store_true", help="Use live mode. The template refuses live actions until implemented.")
    parser.add_argument("--visible", action="store_true", help="Force visible Chrome even if config enables headless mode.")
    parser.add_argument("--open-browser", action="store_true", help="For smoke_test, open the configured page in Chrome.")
    parser.add_argument("--result-file", default=RESULT_FILE)
    parser.add_argument(
        "--resume-existing-order-id",
        action="append",
        default=[],
        help="Existing split order ID to count as already completed before creating remaining split orders. Repeat for multiple orders.",
    )
    args = parser.parse_args(argv)

    dry_run = bool(args.dry_run and not args.real)
    if args.action == "smoke_test":
        return run_smoke_test(open_browser=args.open_browser, visible=args.visible, result_file=args.result_file)
    if args.action == "process_order":
        return run_process_order(order_id=args.order_id, dry_run=dry_run, visible=args.visible, result_file=args.result_file)
    if args.action == "process_batch":
        return run_process_batch(list_url=args.list_url, dry_run=dry_run, visible=args.visible, result_file=args.result_file)
    if args.action == "split_order":
        return run_split_order(
            order_id=args.order_id,
            order_url=args.order_url,
            expected_tab_count=args.tab_count,
            divisions=args.divisions,
            minimum_tabs=args.minimum_tabs,
            login_wait_seconds=args.login_wait_seconds,
            attach_browser=args.attach_browser,
            debugger_address=args.debugger_address,
            dry_run=dry_run,
            visible=args.visible,
            result_file=args.result_file,
            resume_existing_order_ids=args.resume_existing_order_id,
            parallel_workers=args.parallel_workers,
        )
    _write_result(False, f"Unsupported action: {args.action}", result_file=args.result_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
