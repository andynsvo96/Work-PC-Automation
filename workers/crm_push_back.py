"""
CRM Push Back automation worker.

Push Back moves eligible CRM order production dates out by one business day,
without changing fulfillment/due dates. It only processes Rush and 813 lists.
"""

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from automation_runtime import (
    SCRIPT_DIR,
    configure_console_utf8,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
    write_status_payload,
)
from config import (
    CRM_ACTION_TIMEOUT,
    CRM_PROFILE_DIR,
    CRM_PUSH_BACK_813_URL,
    CRM_PUSH_BACK_HIGH_VALUE_URL,
    CRM_PUSH_BACK_RUSH_URL,
)
import crm_order_goods as crm_order_goods_worker
import crm_shipping_bypasser as crm_shipping_bypasser_worker
from crm_order_goods import (
    _run_order_with_driver as _order_goods_for_order_with_driver,
    _wait_for_order_goods_page_ready,
)
from crm_shipping_bypasser import (
    _change_crm_production_date,
    _extract_order_data,
    _format_date_for_crm,
    _next_business_day_after,
    _parse_crm_date,
)
from crm_validate_address import (
    _batch_collection_limit,
    _batch_limit_reached,
    _build_crm_session_driver,
    _classify_shipping_list_row_candidate,
    _clone_profile_for_worker,
    _crm_attempt_modes,
    _is_retryable_exception,
    _normalize_requested_batch_size,
    _normalize_target_order_id,
    _open_target_order,
    _record_stage_timing,
    _worker_profile_lock,
    login_if_needed,
)

configure_console_utf8()

AUTOMATION_NAME = "crm.push_back"
crm_order_goods_worker.AUTOMATION_NAME = AUTOMATION_NAME
PROFILE_PATH = os.path.join(SCRIPT_DIR, CRM_PROFILE_DIR)
CONTINUOUS_ORDER_FETCH_LIMIT = 25
VALID_FILTERS = {"rush", "813", "high_value"}
AUTO_ORDER_CONFIRMATION_TIMEOUT_SECONDS = 30
_shipping_bypasser_lock = threading.Lock()
RUSH_ROW_LABELS = ("tan", "purple")
RUSH_ROW_DESCRIPTION = "tan, natural, or purple"
ORDER_813_ROW_LABELS = ("bright_red", "dark_red", "purple")
ORDER_813_ROW_DESCRIPTION = "red or purple"
DATE_PATTERN = re.compile(r"\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
PRODUCTION_DATE_PATTERN = re.compile(
    r"Production\s+Date\s*:\s*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
FULFILLMENT_DATE_PATTERN = re.compile(
    r"(?:Fulfillment|Due)\s+Date\s*:\s*(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


def _publish_status(message, *, stage=None, current=None, total=None, order_id=None):
    try:
        write_status_payload(
            AUTOMATION_NAME,
            message,
            stage=stage,
            order_id=order_id,
            current=current,
            total=total,
        )
    except Exception:
        pass


def _elapsed_seconds(started_at):
    return round(max(0.0, time.monotonic() - started_at), 1)


def _json_safe(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _normalize_processing_filter(value):
    key = str(value or "").strip().lower()
    key = key.replace("-", "_").replace(" ", "_")
    return key if key in VALID_FILTERS else "rush"


def _list_url_for_filter(processing_filter, list_url=None):
    override = str(list_url or "").strip()
    if override:
        return override
    key = _normalize_processing_filter(processing_filter)
    if key == "813":
        return str(CRM_PUSH_BACK_813_URL).strip()
    if key == "high_value":
        return str(CRM_PUSH_BACK_HIGH_VALUE_URL).strip()
    return str(CRM_PUSH_BACK_RUSH_URL).strip()


def _allowed_row_options(processing_filter):
    if _normalize_processing_filter(processing_filter) == "813":
        return ORDER_813_ROW_LABELS, ORDER_813_ROW_DESCRIPTION
    return RUSH_ROW_LABELS, RUSH_ROW_DESCRIPTION


def _date_values_from_text(text):
    values = []
    for match in DATE_PATTERN.finditer(str(text or "")):
        parsed = _parse_crm_date(match.group(0))
        if parsed is not None:
            values.append(parsed)
    return values


def _production_date_from_row(row):
    row = row if isinstance(row, dict) else {}
    for key in ("productionText", "rowText"):
        text = str(row.get(key) or "")
        match = PRODUCTION_DATE_PATTERN.search(text)
        if match:
            parsed = _parse_crm_date(match.group(1))
            if parsed is not None:
                return parsed
    dates = _date_values_from_text(row.get("productionText"))
    return dates[0] if dates else None


def _fulfillment_date_from_row(row, production_date=None):
    row = row if isinstance(row, dict) else {}
    text = str(row.get("rowText") or "")
    match = FULFILLMENT_DATE_PATTERN.search(text)
    if match:
        parsed = _parse_crm_date(match.group(1))
        if parsed is not None:
            return parsed
    for parsed in _date_values_from_text(text):
        if production_date is not None and parsed == production_date:
            continue
        return parsed
    return None


def _date_text(value):
    return _format_date_for_crm(value) if value is not None else ""


def _result(order_id, success, outcome, message, **extra):
    payload = {
        "order_id": str(order_id or ""),
        "success": bool(success),
        "outcome": str(outcome or ""),
        "message": str(message or ""),
        "manual_review_required": bool(extra.pop("manual_review_required", not success)),
    }
    payload.update({key: _json_safe(value) for key, value in extra.items() if value is not None})
    return payload


def _run_order_goods_with_push_back_status(driver, order_id, dry_run=False):
    if dry_run:
        return _order_goods_for_order_with_driver(driver, order_id, dry_run=True)
    return _order_goods_for_order_with_driver(
        driver,
        order_id,
        dry_run=False,
        wait_for_auto_order_result=True,
        ignore_already_ordered=True,
    )


def _normalize_stock_order_results(results):
    rows = results if isinstance(results, list) else []
    cleaned = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        cleaned.append(_json_safe(dict(item)))
    return cleaned


def _stock_order_success(results):
    rows = _normalize_stock_order_results(results)
    return all(bool(item.get("success")) for item in rows) if rows else True


def _stock_order_summary(results, dry_run=False):
    rows = _normalize_stock_order_results(results)
    if not rows:
        return "No stock tabs were reported."
    successful = sum(1 for item in rows if item.get("success"))
    total = len(rows)
    if dry_run:
        return f"Stock dry run checked {total} tab(s); {successful} tab(s) looked orderable or already handled."
    summary = f"Stock ordering finished for {successful}/{total} tab(s)."
    details = []
    for item in rows:
        message = " ".join(str(item.get("message") or "").split())
        if message and message not in details:
            details.append(message)
    if details:
        summary = f"{summary} CRM result: {'; '.join(details)}"
    return summary


def _stock_order_has_outcome(results, outcome):
    wanted = str(outcome or "").strip()
    return any(str(item.get("outcome") or "").strip() == wanted for item in _normalize_stock_order_results(results))


def _text_indicates_push_back_stock_already_ordered(text):
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    stock_status_ordered = bool(re.search(r"\bstock\s+status\s*:\s*ordered\b", normalized))
    stock_ordered = bool(re.search(r"\bstock\s*:\s*ordered\b", normalized))
    return stock_status_ordered and stock_ordered


def _page_indicates_push_back_stock_already_ordered(driver):
    try:
        text = driver.execute_script(
            r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect && node.getBoundingClientRect();
  if (rect && ((rect.width || 0) <= 0 || (rect.height || 0) <= 0)) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const parts = [];
for (const node of Array.from(document.querySelectorAll('body *')).filter(isVisible)) {
  parts.push(node.innerText || node.textContent || node.value || '');
}
return normalize(parts.join('\n'));
"""
        )
    except Exception:
        text = ""
    return _text_indicates_push_back_stock_already_ordered(text)


def _wait_for_push_back_stock_confirmation(driver, order_id, timeout=None):
    timeout = max(1, int(timeout or AUTO_ORDER_CONFIRMATION_TIMEOUT_SECONDS))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _page_indicates_push_back_stock_already_ordered(driver):
            return True
        time.sleep(0.5)

    _publish_status(
        f"Refreshing CRM order {order_id} to verify Stock Status: Ordered.",
        stage="verifying_stock_order",
        order_id=order_id,
    )
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    try:
        driver.refresh()
        _wait_for_order_goods_page_ready(driver, order_id, timeout=timeout)
    except Exception:
        return False
    return _page_indicates_push_back_stock_already_ordered(driver)


def _run_shipping_bypasser_with_current_crm_driver(driver, order_id, dry_run=False):
    sanmar_driver = None
    report_items = []
    with _shipping_bypasser_lock:
        try:
            _publish_status(
                f"Starting Shipping Bypasser for CRM order {order_id} after the shipment-cost Auto Ordering failure.",
                stage="shipping_bypasser",
                order_id=order_id,
            )
            sanmar_driver = crm_shipping_bypasser_worker._build_sanmar_driver(visible=bool(dry_run))
            report_items = crm_shipping_bypasser_worker._run_order_with_drivers(
                driver,
                sanmar_driver,
                order_id,
                dry_run=dry_run,
            )
            if not any(isinstance(item, dict) and item.get("stop_run") for item in report_items):
                crm_shipping_bypasser_worker._cleanup_after_failed_order(sanmar_driver, order_id, report_items)
        finally:
            safe_driver_quit(
                sanmar_driver,
                profile_path=os.path.abspath(crm_shipping_bypasser_worker.SANMAR_PROFILE_PATH),
            )

    success = crm_shipping_bypasser_worker._report_orders_succeeded_or_partially_succeeded(report_items)
    return {
        "success": bool(success),
        "message": crm_shipping_bypasser_worker._summary_message(
            report_items,
            refresh_passes=1,
            order_count=1,
        ),
        "report": _json_safe(report_items),
        "manual_review_required": crm_shipping_bypasser_worker._report_has_fully_failed_order(report_items),
    }


def _shipping_bypass_failure_detail(shipping_bypass):
    """Return the actionable failure from an inline Shipping Bypasser run."""
    payload = shipping_bypass if isinstance(shipping_bypass, dict) else {}
    report = payload.get("report") if isinstance(payload.get("report"), list) else []
    messages = []
    for item in report:
        if not isinstance(item, dict) or item.get("success"):
            continue
        message = str(item.get("message") or "").strip()
        if message and message not in messages:
            messages.append(message)
    if messages:
        return "Shipping Bypasser error: " + "; ".join(messages[:3])
    return ""


def _precheck_row(row):
    order_id = str((row or {}).get("orderId") or "").strip()
    row_color = str((row or {}).get("colorLabel") or "").strip().lower()
    if row_color == "lime_green":
        return _result(
            order_id,
            True,
            "max_rush_lime_green_skipped",
            (
                "Skipped because the CRM list row is lime green (Max Rush); "
                "these orders are due tomorrow and cannot be pushed back."
            ),
            manual_review_required=False,
            row_color=row_color,
        )
    production_date = _production_date_from_row(row)
    due_date = _fulfillment_date_from_row(row, production_date=production_date)
    target_date = _next_business_day_after(production_date) if production_date is not None else None
    if production_date is None:
        return _result(
            order_id,
            False,
            "missing_production_date",
            "Skipped before opening because the CRM list row did not expose a production date.",
            manual_review_required=True,
            row_text=(row or {}).get("rowText"),
            row_color=(row or {}).get("colorLabel"),
        )
    if due_date is None:
        return _result(
            order_id,
            False,
            "missing_due_date",
            "Skipped before opening because the CRM list row did not expose a fulfillment/due date.",
            manual_review_required=True,
            production_date=production_date,
            target_production_date=target_date,
            row_text=(row or {}).get("rowText"),
            row_color=(row or {}).get("colorLabel"),
        )
    if target_date >= due_date:
        return _result(
            order_id,
            True,
            "due_date_guard_skipped",
            (
                "Skipped before opening because pushing production date from "
                f"{_date_text(production_date)} to {_date_text(target_date)} would be on or after due date {_date_text(due_date)}."
            ),
            manual_review_required=False,
            production_date=production_date,
            target_production_date=target_date,
            due_date=due_date,
            row_color=(row or {}).get("colorLabel"),
        )
    return None


PUSH_BACK_REPORT_ROWS_JS = r"""
const limit = Number(arguments[0] || 25);
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function isTransparent(color){
  if(!color){ return true; }
  const normalized = String(color).replace(/\s+/g, '').toLowerCase();
  return normalized === 'transparent' || normalized === 'rgba(0,0,0,0)';
}
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 && (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function addNodeColor(colors, seen, node){
  if(!node || colors.length >= 50){ return; }
  const style = window.getComputedStyle(node);
  const bg = style.backgroundColor;
  if(!isTransparent(bg)){
    const nodeRect = node.getBoundingClientRect();
    const key = [bg, node.tagName, Math.round(nodeRect.width), Math.round(nodeRect.height)].join('|');
    if(!seen.has(key)){
      seen.add(key);
      colors.push({
        backgroundColor: bg,
        tag: node.tagName,
        id: node.id || '',
        className: String(node.className || ''),
        width: nodeRect.width || 0,
        height: nodeRect.height || 0
      });
    }
  }
}
function addAncestors(colors, seen, node, maxDepth){
  let depth = 0;
  for(let current = node; current && colors.length < 50 && depth < maxDepth; current = current.parentElement, depth++){
    addNodeColor(colors, seen, current);
  }
}
function addPointColors(colors, seen, x, y){
  if(!Number.isFinite(x) || !Number.isFinite(y)){ return; }
  if(x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight){ return; }
  const elements = document.elementsFromPoint(x, y);
  for(const node of elements){
    addAncestors(colors, seen, node, 8);
    if(colors.length >= 50){ return; }
  }
}
function addChildColors(colors, seen, node){
  for(const child of Array.from((node && node.children) || [])){
    addNodeColor(colors, seen, child);
    if(colors.length >= 50){ return; }
  }
}
function reportRowFor(link, orderId) {
  const closest = link.closest ? link.closest('tr,[role="row"],li,.row,.report-row') : null;
  if (closest && clean(closest.innerText || closest.textContent).includes(orderId) && isVisible(closest)) {
    return closest;
  }
  for (let row = link; row && row !== document.body; row = row.parentElement) {
    const rect = row.getBoundingClientRect();
    const text = clean(row.innerText || row.textContent);
    if (text.includes(orderId) && rect.width > 500 && rect.height > 15 && isVisible(row)) {
      return row;
    }
  }
  return null;
}
const productionNodes = Array.from(document.querySelectorAll('body *')).filter(isVisible).map((node) => {
  const text = clean(node.innerText || node.textContent);
  if (!/Production\s+Date\s*:/i.test(text) || text.length > 500) return null;
  const rect = node.getBoundingClientRect();
  return { text, top: rect.top || 0, bottom: rect.bottom || rect.top || 0 };
}).filter(Boolean);
function productionTextFor(row) {
  if (!row) return '';
  const rowRect = row.getBoundingClientRect();
  const rowText = clean(row.innerText || row.textContent);
  if (/Production\s+Date\s*:/i.test(rowText)) return rowText;
  const candidates = productionNodes
    .filter((item) => item.top <= rowRect.top + 2)
    .sort((a, b) => b.bottom - a.bottom);
  return candidates.length ? candidates[0].text : '';
}
function collectColors(row, link) {
  const colors = [];
  const seen = new Set();
  const linkRect = link.getBoundingClientRect();
  addAncestors(colors, seen, link, 16);
  if(row){
    const rowRect = row.getBoundingClientRect();
    addAncestors(colors, seen, row, 8);
    addChildColors(colors, seen, row);
    const y = rowRect.top + Math.max(1, rowRect.height || linkRect.height || 1) / 2;
    const left = Math.max(0, rowRect.left + 4);
    const right = Math.min(window.innerWidth - 1, rowRect.right - 4);
    const middle = rowRect.left + Math.max(1, rowRect.width || linkRect.width || 1) / 2;
    for(const x of [linkRect.left + 2, linkRect.left + linkRect.width / 2, left, middle, right]){
      addPointColors(colors, seen, x, y);
    }
  }
  return colors;
}
const rows = [];
for (const link of Array.from(document.querySelectorAll('a[href],a')).filter(isVisible)) {
  const href = link.href || '';
  const text = clean(link.innerText || link.textContent);
  let match = href.match(/\/order\/(\d{7})(?:\D|$)/);
  const hrefOrderId = match ? match[1] : '';
  match = text.match(/\b(\d{7})\b/);
  const textOrderId = match ? match[1] : '';
  const orderId = hrefOrderId || textOrderId;
  if (!orderId) continue;
  const row = reportRowFor(link, orderId);
  if (!row) continue;
  const rowRect = row.getBoundingClientRect();
  rows.push({
    orderId,
    href,
    linkText: text,
    rowText: clean(row.innerText || row.textContent),
    productionText: productionTextFor(row),
    top: rowRect.top || 0,
    left: rowRect.left || 0,
    colors: collectColors(row, link)
  });
}
return rows
  .sort((a, b) => (a.top - b.top) || (a.left - b.left))
  .slice(0, Math.max(limit * 4, limit, 25));
"""


def _row_color_label(row):
    for candidate in (row or {}).get("colors") or []:
        label, rgb = _classify_shipping_list_row_candidate(candidate)
        if label:
            return label, rgb
    return None, None


def _collect_push_back_rows_with_driver(
    driver,
    processing_filter,
    limit,
    list_url,
    exclude_order_ids=None,
):
    target_limit = max(1, int(limit or 1))
    allowed_labels, allowed_description = _allowed_row_options(processing_filter)
    excluded = {str(order_id).strip() for order_id in (exclude_order_ids or []) if str(order_id).strip()}
    _publish_status(
        f"Opening Push Back CRM report for {_normalize_processing_filter(processing_filter)}.",
        stage="loading_crm_list",
    )
    safe_get_with_partial_load(driver, list_url, "CRM Push Back report")
    if login_if_needed(driver):
        safe_get_with_partial_load(driver, list_url, "CRM Push Back report after login")

    deadline = time.monotonic() + max(CRM_ACTION_TIMEOUT, 12)
    selected = []
    logged_lime_order_ids = set()
    while time.monotonic() < deadline:
        try:
            raw_rows = driver.execute_script(PUSH_BACK_REPORT_ROWS_JS, target_limit) or []
        except Exception:
            raw_rows = []
        candidates = OrderedDict()
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            order_id = str(raw.get("orderId") or "").strip()
            if not order_id or order_id in excluded or order_id in candidates:
                continue
            label, rgb = _row_color_label(raw)
            if label == "lime_green":
                if order_id not in logged_lime_order_ids:
                    print(
                        f"Skipping Push Back order {order_id} because its CRM list row is lime green (Max Rush)."
                    )
                    logged_lime_order_ids.add(order_id)
                continue
            if label not in allowed_labels:
                continue
            raw["colorLabel"] = label
            raw["rgb"] = rgb
            candidates[order_id] = raw
            if len(candidates) >= target_limit:
                break
        if candidates:
            selected = list(candidates.values())[:target_limit]
            for row in selected:
                rgb = row.get("rgb") or ()
                rgb_text = ",".join(str(part) for part in rgb) if rgb else "?"
                print(
                    f"Selected Push Back order {row.get('orderId')} from an allowed "
                    f"{row.get('colorLabel')} row ({rgb_text}); allowed colors: {allowed_description}."
                )
            break
        time.sleep(0.5)
    return selected


def _collect_push_back_rows(
    processing_filter,
    limit,
    profile_path,
    list_url,
    exclude_order_ids=None,
    visible=False,
):
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    attempt_modes = [False] if visible else _crm_attempt_modes()
    last_error = None

    for index, headless_mode in enumerate(attempt_modes, start=1):
        driver = None
        try:
            driver = _build_crm_session_driver(
                resolved_profile_path,
                headless_mode=headless_mode,
                profile_label="CRM push back batch source",
            )
            return _collect_push_back_rows_with_driver(
                driver,
                processing_filter,
                limit,
                list_url,
                exclude_order_ids=exclude_order_ids,
            )
        except Exception as exc:
            last_error = exc
            if not headless_mode or index == len(attempt_modes) or not _is_retryable_exception(exc):
                raise
            print("Headless Push Back batch source failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
        finally:
            safe_driver_quit(driver, profile_path=resolved_profile_path)

    if last_error is not None:
        raise last_error
    return []


def _open_and_read_order(driver, order_id, processing_filter, list_url):
    _open_target_order(
        driver,
        order_id,
        shipping_filter=_normalize_processing_filter(processing_filter),
        list_url_override=list_url,
    )
    _wait_for_order_goods_page_ready(driver, order_id)
    return _extract_order_data(driver, order_id)


def _refresh_and_read_order_for_save_retry(driver, order_id, processing_filter, list_url):
    _publish_status(
        f"Refreshing CRM order {order_id} after Push Back save did not complete.",
        stage="refreshing_order_retry",
        order_id=order_id,
    )
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    safe_get_with_partial_load(
        driver,
        f"https://crm2.legacy.printfly.com/order/{order_id}",
        f"CRM order {order_id} refresh before Push Back retry",
    )
    if login_if_needed(driver):
        safe_get_with_partial_load(
            driver,
            f"https://crm2.legacy.printfly.com/order/{order_id}",
            f"CRM order {order_id} refresh after login before Push Back retry",
        )
    _wait_for_order_goods_page_ready(driver, order_id)
    return _extract_order_data(driver, order_id)


def _change_crm_production_date_with_retry(driver, order_id, target_date, processing_filter, list_url):
    try:
        return _change_crm_production_date(driver, order_id, target_date), 0, None
    except Exception as first_error:
        print(
            f"Push Back save did not complete for order {order_id}: {first_error}. "
            "Refreshing the order once and retrying."
        )
        refreshed = _refresh_and_read_order_for_save_retry(driver, order_id, processing_filter, list_url)
        refreshed_date = refreshed.get("production_date")
        if refreshed_date == target_date:
            print(f"CRM order {order_id} already shows the target production date after refresh.")
            return target_date, 1, str(first_error)
        _publish_status(
            f"Retrying Push Back save for CRM order {order_id}.",
            stage="saving_order_retry",
            order_id=order_id,
        )
        return _change_crm_production_date(driver, order_id, target_date), 1, str(first_error)


def _run_order_with_driver(driver, row, processing_filter, list_url, dry_run=False):
    order_id = str((row or {}).get("orderId") or "").strip()
    if not order_id:
        raise RuntimeError("Push Back row did not include an order ID.")

    precheck = _precheck_row(row)
    if precheck is not None:
        return precheck

    row_production_date = _production_date_from_row(row)
    row_due_date = _fulfillment_date_from_row(row, production_date=row_production_date)
    row_target_date = _next_business_day_after(row_production_date)
    _publish_status(f"Opening CRM order {order_id} for Push Back.", stage="opening_order", order_id=order_id)
    data = _open_and_read_order(driver, order_id, processing_filter, list_url)
    production_date = data.get("production_date") or row_production_date
    due_date = data.get("due_date") or row_due_date
    target_date = _next_business_day_after(production_date) if production_date is not None else row_target_date

    if production_date is None:
        return _result(
            order_id,
            False,
            "missing_order_production_date",
            "Order opened, but Push Back could not read the current production date.",
            manual_review_required=True,
            row_production_date=row_production_date,
            row_due_date=row_due_date,
            row_color=(row or {}).get("colorLabel"),
        )
    if due_date is None:
        return _result(
            order_id,
            False,
            "missing_order_due_date",
            "Order opened, but Push Back could not read the due date.",
            manual_review_required=True,
            production_date=production_date,
            target_production_date=target_date,
            row_due_date=row_due_date,
            row_color=(row or {}).get("colorLabel"),
        )
    if target_date >= due_date:
        return _result(
            order_id,
            True,
            "due_date_guard_skipped",
            (
                "Skipped because pushing production date from "
                f"{_date_text(production_date)} to {_date_text(target_date)} would be on or after due date {_date_text(due_date)}."
            ),
            manual_review_required=False,
            production_date=production_date,
            target_production_date=target_date,
            due_date=due_date,
            row_color=(row or {}).get("colorLabel"),
        )

    if _page_indicates_push_back_stock_already_ordered(driver):
        return _result(
            order_id,
            True,
            "stock_already_ordered_skipped",
            (
                "Skipped because CRM shows Stock Status: Ordered and Stock: Ordered; "
                "Push Back is treating this list entry as already handled."
            ),
            manual_review_required=False,
            production_date=production_date,
            target_production_date=target_date,
            due_date=due_date,
            row_color=(row or {}).get("colorLabel"),
        )

    if dry_run:
        _publish_status(f"Checking stock order readiness for CRM order {order_id}.", stage="checking_stock_order", order_id=order_id)
        stock_results = _run_order_goods_with_push_back_status(driver, order_id, dry_run=True)
        stock_success = _stock_order_success(stock_results)
        return _result(
            order_id,
            stock_success,
            "push_back_ready_stock_ready" if stock_success else "push_back_ready_stock_failed",
            (
                f"Dry run would push production date from {_date_text(production_date)} to {_date_text(target_date)}, "
                f"then order stock. {_stock_order_summary(stock_results, dry_run=True)}"
            ),
            manual_review_required=False,
            production_date=production_date,
            target_production_date=target_date,
            due_date=due_date,
            row_color=(row or {}).get("colorLabel"),
            stock_order_attempted=True,
            stock_order_dry_run=True,
            stock_order_success=stock_success,
            stock_order_results=stock_results,
        )

    original_production_date = production_date
    current_production_date = production_date
    stock_order_attempts = []
    saved_production_dates = []
    total_save_retry_count = 0
    save_retry_errors = []

    while True:
        target_date = _next_business_day_after(current_production_date)
        if target_date >= due_date:
            return _result(
                order_id,
                False,
                "push_back_no_purchase_plan_due_date_reached",
                (
                    "Stock could not be auto ordered because no purchase plan was available, and production date "
                    f"{_date_text(current_production_date)} is already the last allowable business day before due date "
                    f"{_date_text(due_date)}."
                ),
                manual_review_required=True,
                production_date=original_production_date,
                saved_production_date=current_production_date,
                saved_production_dates=saved_production_dates,
                due_date=due_date,
                row_color=(row or {}).get("colorLabel"),
                save_retry_count=total_save_retry_count,
                save_retry_errors=save_retry_errors,
                stock_order_attempted=True,
                stock_order_success=False,
                stock_order_attempts=stock_order_attempts,
            )

        _publish_status(
            f"Pushing CRM order {order_id} production date to {_date_text(target_date)}.",
            stage="saving_order",
            order_id=order_id,
        )
        saved_date, save_retry_count, save_retry_error = _change_crm_production_date_with_retry(
            driver,
            order_id,
            target_date,
            processing_filter,
            list_url,
        )
        saved_production_dates.append(saved_date)
        total_save_retry_count += int(save_retry_count or 0)
        if save_retry_error:
            save_retry_errors.append(str(save_retry_error))

        _publish_status(
            f"Ordering stock after Push Back for CRM order {order_id}.",
            stage="ordering_stock",
            order_id=order_id,
        )
        stock_results = _run_order_goods_with_push_back_status(driver, order_id, dry_run=False)
        normalized_stock_results = _normalize_stock_order_results(stock_results)
        stock_order_attempts.append(
            {
                "production_date": _json_safe(saved_date),
                "results": normalized_stock_results,
            }
        )

        if _stock_order_has_outcome(stock_results, "auto_order_shipment_cost_exceeded"):
            shipping_bypass = _run_shipping_bypasser_with_current_crm_driver(driver, order_id, dry_run=False)
            bypass_success = bool(shipping_bypass.get("success"))
            bypass_failure_detail = "" if bypass_success else _shipping_bypass_failure_detail(shipping_bypass)
            return _result(
                order_id,
                bypass_success,
                "push_back_shipping_bypass_ordered" if bypass_success else "push_back_shipping_bypass_failed",
                (
                    f"Pushed production date from {_date_text(original_production_date)} to {_date_text(saved_date)}. "
                    "CRM rejected Auto Ordering because shipment cost exceeded the configured percentage, so Push Back "
                    f"ran Shipping Bypasser. {shipping_bypass.get('message') or ''} {bypass_failure_detail}"
                ).strip(),
                manual_review_required=bool(shipping_bypass.get("manual_review_required", not bypass_success)),
                production_date=original_production_date,
                saved_production_date=saved_date,
                saved_production_dates=saved_production_dates,
                due_date=due_date,
                row_color=(row or {}).get("colorLabel"),
                save_retry_count=total_save_retry_count,
                save_retry_errors=save_retry_errors,
                stock_order_attempted=True,
                stock_order_success=bypass_success,
                stock_order_results=normalized_stock_results,
                stock_order_attempts=stock_order_attempts,
                shipping_bypass=shipping_bypass,
            )

        if _stock_order_has_outcome(stock_results, "auto_order_no_purchase_plan"):
            current_production_date = saved_date
            next_target = _next_business_day_after(current_production_date)
            if next_target < due_date:
                _publish_status(
                    (
                        f"CRM found no purchase plan for order {order_id}; retrying after moving production date "
                        f"to {_date_text(next_target)}."
                    ),
                    stage="retrying_no_purchase_plan",
                    order_id=order_id,
                )
            continue

        stock_success = _stock_order_success(stock_results)
        stock_summary = _stock_order_summary(stock_results, dry_run=False)
        if stock_success:
            _publish_status(
                f"Waiting for CRM to confirm ordered stock for order {order_id}.",
                stage="verifying_stock_order",
                order_id=order_id,
            )
            stock_success = _wait_for_push_back_stock_confirmation(driver, order_id)
            if not stock_success:
                if _stock_order_has_outcome(stock_results, "auto_order_succeeded"):
                    stock_summary = (
                        f"{stock_summary} CRM showed the Auto Order success message but did not refresh to "
                        "Stock Status: Ordered and Stock: Ordered."
                    )
                else:
                    stock_summary = (
                        f"{stock_summary} The intermediate stock check reported success, but CRM did not confirm "
                        "Stock Status: Ordered and Stock: Ordered."
                    )

        return _result(
            order_id,
            stock_success,
            "push_back_saved_stock_ordered" if stock_success else "push_back_saved_stock_failed",
            f"Pushed production date from {_date_text(original_production_date)} to {_date_text(saved_date)}. {stock_summary}",
            manual_review_required=not stock_success,
            production_date=original_production_date,
            target_production_date=target_date,
            saved_production_date=saved_date,
            saved_production_dates=saved_production_dates,
            due_date=due_date,
            row_color=(row or {}).get("colorLabel"),
            save_retry_count=total_save_retry_count,
            save_retry_errors=save_retry_errors,
            stock_order_attempted=True,
            stock_order_success=stock_success,
            stock_order_results=normalized_stock_results,
            stock_order_attempts=stock_order_attempts,
        )


def _summary_message(report_items, refresh_passes=1, order_count=0, dry_run=False):
    if not report_items:
        return "No eligible Push Back orders were found in the CRM list."
    pushed = sum(
        1
        for item in report_items
        if str(item.get("outcome") or "").startswith(("push_back_saved", "push_back_shipping_bypass"))
    )
    ready = sum(1 for item in report_items if str(item.get("outcome") or "").startswith("push_back_ready"))
    stock_success = sum(1 for item in report_items if item.get("stock_order_attempted") and item.get("stock_order_success"))
    skipped = sum(1 for item in report_items if str(item.get("outcome") or "").endswith("_skipped"))
    failed = sum(1 for item in report_items if not item.get("success"))
    action_text = f"{ready} order(s) ready for push-back and stock order" if dry_run else f"{pushed} order(s) pushed"
    parts = [
        f"Push Back checked {max(1, int(order_count or 0))} order(s) across {max(1, int(refresh_passes or 1))} CRM list refresh pass(es).",
        action_text + ".",
        (f"Stock dry run passed on {stock_success} order(s)." if dry_run else f"Stock ordering succeeded on {stock_success} order(s)."),
        f"{skipped} order(s) skipped by due-date guard.",
    ]
    if failed:
        parts.append(f"{failed} order(s) need attention.")
    return " ".join(parts)


def _run_order_worker_payload(row, headless_mode, processing_filter, list_url, dry_run=False, profile_path=None, skip_stale_chrome_check=False):
    order_id = str((row or {}).get("orderId") or "").strip()
    driver = None
    try:
        driver = _build_crm_session_driver(
            os.path.abspath(profile_path or PROFILE_PATH),
            headless_mode=headless_mode,
            profile_label=f"CRM push back {order_id or 'order'}",
            skip_stale_chrome_check=skip_stale_chrome_check,
        )
        return _run_order_with_driver(driver, row, processing_filter, list_url, dry_run=dry_run)
    except Exception as exc:
        if driver is not None:
            safe_take_screenshot(driver, "crm_push_back_error")
        return _result(
            order_id,
            False,
            "worker_exception",
            str(exc),
            manual_review_required=True,
            retryable=_is_retryable_exception(exc),
            error_type=type(exc).__name__,
        )
    finally:
        safe_driver_quit(driver, profile_path=profile_path)


def _push_back_result_retryable(item):
    return (
        isinstance(item, dict)
        and str(item.get("outcome") or "") == "worker_exception"
        and bool(item.get("retryable"))
    )


def _run_parallel_batch_with_mode(headless_mode, processing_filter="rush", dry_run=False, batch_size=None, profile_path=None, list_url=None, parallel_workers=1):
    started_at = time.monotonic()
    normalized_filter = _normalize_processing_filter(processing_filter)
    target_url = _list_url_for_filter(normalized_filter, list_url=list_url)
    if not target_url:
        config_key = "CRM_PUSH_BACK_813_URL" if normalized_filter == "813" else "CRM_PUSH_BACK_RUSH_URL"
        raise RuntimeError(f"{config_key} is empty in config.py.")
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    worker_limit = max(1, int(parallel_workers or 1))
    if requested_batch_size is not None:
        worker_limit = min(worker_limit, requested_batch_size)

    report_items = []
    attempted_order_ids = []
    attempted_order_id_set = set()
    refresh_passes = 0
    completed_count = 0
    total_scanned_count = 0
    stage_timings = []

    while not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
        refresh_passes += 1
        remaining = _batch_collection_limit(
            requested_batch_size,
            len(attempted_order_ids),
            worker_limit=worker_limit,
        )
        _publish_status(
            f"Loading Push Back list pass {refresh_passes} to scan for eligible orders.",
            stage="loading_crm_list",
            current=completed_count if total_scanned_count else None,
            total=total_scanned_count or None,
        )
        list_scan_started_at = time.monotonic()
        rows = _collect_push_back_rows(
            normalized_filter,
            remaining,
            resolved_profile_path,
            target_url,
            exclude_order_ids=attempted_order_id_set,
            visible=not bool(headless_mode),
        )
        _record_stage_timing(
            stage_timings,
            "list_scan",
            list_scan_started_at,
            refresh_pass=refresh_passes,
            order_count=len(rows),
        )
        if not rows:
            _publish_status(
                "No more eligible Push Back orders were found in the CRM list.",
                stage="scan_complete",
                current=completed_count if total_scanned_count else None,
                total=total_scanned_count or None,
            )
            break
        rows = rows[:remaining]
        total_scanned_count += len(rows)
        _publish_status(
            f"Scanned {len(rows)} Push Back order(s) on pass {refresh_passes}; processing with {worker_limit} worker(s).",
            stage="processing_orders",
            current=completed_count,
            total=total_scanned_count,
        )

        finished_lock = threading.Lock()
        worker_gate = threading.BoundedSemaphore(worker_limit)
        threads = []
        chunk_items = []
        chunk_started_at = time.monotonic()

        def _worker(order_index, row):
            nonlocal completed_count
            order_id = str((row or {}).get("orderId") or "").strip()
            with worker_gate:
                order_started_at = time.monotonic()
                print(f"Launching Push Back worker {order_index + 1}/{len(rows)} for order {order_id}...")
                with finished_lock:
                    current_done = completed_count
                _publish_status(
                    f"Processing Push Back order {order_id} ({current_done}/{total_scanned_count} done).",
                    stage="processing_order",
                    current=current_done,
                    total=total_scanned_count,
                    order_id=order_id,
                )
                item = None
                attempt_count = 0
                total_attempts = 1 if dry_run else 2
                worker_slot = (order_index % worker_limit) + 1
                for attempt in range(1, total_attempts + 1):
                    attempt_count = attempt
                    temp_root = None
                    cloned_profile_path = None
                    try:
                        temp_root, cloned_profile_path = _clone_profile_for_worker(
                            resolved_profile_path,
                            f"push_back_{order_index + 1}_{order_id}_attempt_{attempt}",
                            worker_slot=worker_slot,
                            pool_name="push_back",
                            rebuild=attempt > 1,
                        )
                        if attempt > 1:
                            print(f"Retrying Push Back order {order_id} once with a fresh CRM worker profile...")
                            time.sleep(1)
                        with _worker_profile_lock(cloned_profile_path):
                            item = _run_order_worker_payload(
                                row,
                                headless_mode=headless_mode,
                                processing_filter=normalized_filter,
                                list_url=target_url,
                                dry_run=dry_run,
                                profile_path=cloned_profile_path,
                                skip_stale_chrome_check=True,
                            )
                    except Exception as exc:
                        item = _result(
                            order_id,
                            False,
                            "worker_exception",
                            str(exc),
                            manual_review_required=True,
                            retryable=_is_retryable_exception(exc),
                            error_type=type(exc).__name__,
                        )
                    finally:
                        if temp_root:
                            shutil.rmtree(temp_root, ignore_errors=True)
                    if not _push_back_result_retryable(item) or attempt >= total_attempts:
                        break
                item["attempt_count"] = attempt_count
                item["duration_seconds"] = _elapsed_seconds(order_started_at)
                item["session_duration_seconds"] = item["duration_seconds"]
                with finished_lock:
                    chunk_items.append(item)
                    completed_count += 1
                    current_done = completed_count
                _publish_status(
                    f"Finished Push Back order {order_id} ({current_done}/{total_scanned_count} done).",
                    stage="finished_order",
                    current=current_done,
                    total=total_scanned_count,
                    order_id=order_id,
                )

        for order_index, row in enumerate(rows):
            order_id = str(row.get("orderId") or "").strip()
            attempted_order_id_set.add(order_id)
            attempted_order_ids.append(order_id)
            thread = threading.Thread(target=_worker, args=(order_index, row), daemon=True)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()
        _record_stage_timing(
            stage_timings,
            "parallel_order_processing",
            chunk_started_at,
            refresh_pass=refresh_passes,
            order_count=len(rows),
            worker_count=worker_limit,
        )

        chunk_order_position = {
            str(row.get("orderId") or "").strip(): index
            for index, row in enumerate(rows)
        }
        chunk_items.sort(key=lambda item: chunk_order_position.get(str(item.get("order_id") or ""), 999999))
        report_items.extend(chunk_items)

        if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            break
        print("Finished Push Back list pass; reopening the list to look for more eligible orders...")

    success = all(bool(item.get("success")) for item in report_items) if report_items else True
    message = _summary_message(
        report_items,
        refresh_passes=refresh_passes,
        order_count=len(attempted_order_ids),
        dry_run=dry_run,
    )
    return {
        "action": "push_back_batch",
        "success": success,
        "message": message,
        "order_count": len(attempted_order_ids),
        "order_ids": attempted_order_ids,
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(headless_mode),
        "shipping_filter": normalized_filter,
        "list_url": target_url,
        "batch_size": requested_batch_size,
        "parallel_workers": worker_limit,
        "refresh_passes": refresh_passes,
        "duration_seconds": _elapsed_seconds(started_at),
        "stage_timings": stage_timings,
        "manual_review_required": any(bool(item.get("manual_review_required")) for item in report_items),
        "resolution": "batch" if attempted_order_ids else "no_orders",
    }


def _run_batch_with_mode(headless_mode, processing_filter="rush", dry_run=False, batch_size=None, profile_path=None, list_url=None, parallel_workers=1):
    started_at = time.monotonic()
    normalized_filter = _normalize_processing_filter(processing_filter)
    target_url = _list_url_for_filter(normalized_filter, list_url=list_url)
    if not target_url:
        config_key = "CRM_PUSH_BACK_813_URL" if normalized_filter == "813" else "CRM_PUSH_BACK_RUSH_URL"
        raise RuntimeError(f"{config_key} is empty in config.py.")
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    worker_limit = max(1, int(parallel_workers or 1))
    if requested_batch_size is not None:
        worker_limit = min(worker_limit, requested_batch_size)
    if worker_limit > 1:
        return _run_parallel_batch_with_mode(
            headless_mode,
            processing_filter=normalized_filter,
            dry_run=dry_run,
            batch_size=requested_batch_size,
            profile_path=profile_path,
            list_url=target_url,
            parallel_workers=worker_limit,
        )
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    driver = _build_crm_session_driver(
        resolved_profile_path,
        headless_mode=headless_mode,
        profile_label="CRM push back",
    )
    report_items = []
    attempted_order_ids = []
    attempted_order_id_set = set()
    refresh_passes = 0
    completed_count = 0
    total_scanned_count = 0
    stage_timings = []

    try:
        while not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            refresh_passes += 1
            remaining = _batch_collection_limit(
                requested_batch_size,
                len(attempted_order_ids),
                worker_limit=CONTINUOUS_ORDER_FETCH_LIMIT,
            )
            _publish_status(
                f"Loading Push Back list pass {refresh_passes} to scan for eligible orders.",
                stage="loading_crm_list",
                current=completed_count if total_scanned_count else None,
                total=total_scanned_count or None,
            )
            list_scan_started_at = time.monotonic()
            rows = _collect_push_back_rows_with_driver(
                driver,
                normalized_filter,
                remaining,
                target_url,
                exclude_order_ids=attempted_order_id_set,
            )
            _record_stage_timing(
                stage_timings,
                "list_scan",
                list_scan_started_at,
                refresh_pass=refresh_passes,
                order_count=len(rows),
            )
            if not rows:
                _publish_status(
                    "No more eligible Push Back orders were found in the CRM list.",
                    stage="scan_complete",
                    current=completed_count if total_scanned_count else None,
                    total=total_scanned_count or None,
                )
                break
            total_scanned_count += len(rows)
            _publish_status(
                f"Scanned {len(rows)} Push Back order(s) on pass {refresh_passes}; processing orders.",
                stage="processing_orders",
                current=completed_count,
                total=total_scanned_count,
            )
            chunk_started_at = time.monotonic()
            for row in rows:
                order_id = str(row.get("orderId") or "").strip()
                if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                    break
                attempted_order_id_set.add(order_id)
                attempted_order_ids.append(order_id)
                order_started_at = time.monotonic()
                _publish_status(
                    f"Processing Push Back order {order_id} ({completed_count}/{total_scanned_count} done).",
                    stage="processing_order",
                    current=completed_count,
                    total=total_scanned_count,
                    order_id=order_id,
                )
                try:
                    item = _run_order_with_driver(
                        driver,
                        row,
                        normalized_filter,
                        target_url,
                        dry_run=dry_run,
                    )
                    item["duration_seconds"] = _elapsed_seconds(order_started_at)
                    item["session_duration_seconds"] = item["duration_seconds"]
                    report_items.append(item)
                except Exception as exc:
                    safe_take_screenshot(driver, "crm_push_back_error")
                    report_items.append(
                        _result(
                            order_id,
                            False,
                            "worker_exception",
                            str(exc),
                            manual_review_required=True,
                            retryable=_is_retryable_exception(exc),
                            error_type=type(exc).__name__,
                            duration_seconds=_elapsed_seconds(order_started_at),
                            session_duration_seconds=_elapsed_seconds(order_started_at),
                        )
                    )
                completed_count += 1
                _publish_status(
                    f"Finished Push Back order {order_id} ({completed_count}/{total_scanned_count} done).",
                    stage="finished_order",
                    current=completed_count,
                    total=total_scanned_count,
                    order_id=order_id,
                )
            _record_stage_timing(
                stage_timings,
                "shared_session_order_processing",
                chunk_started_at,
                refresh_pass=refresh_passes,
                order_count=len(rows),
                worker_count=worker_limit,
            )
            if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                break
            print("Finished Push Back list pass; reopening the list to look for more eligible orders...")
    finally:
        safe_driver_quit(driver, profile_path=resolved_profile_path)

    success = all(bool(item.get("success")) for item in report_items) if report_items else True
    message = _summary_message(
        report_items,
        refresh_passes=refresh_passes,
        order_count=len(attempted_order_ids),
        dry_run=dry_run,
    )
    return {
        "action": "push_back_batch",
        "success": success,
        "message": message,
        "order_count": len(attempted_order_ids),
        "order_ids": attempted_order_ids,
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(headless_mode),
        "shipping_filter": normalized_filter,
        "list_url": target_url,
        "batch_size": requested_batch_size,
        "parallel_workers": worker_limit,
        "refresh_passes": refresh_passes,
        "duration_seconds": _elapsed_seconds(started_at),
        "stage_timings": stage_timings,
        "manual_review_required": any(bool(item.get("manual_review_required")) for item in report_items),
        "resolution": "batch" if attempted_order_ids else "no_orders",
    }


def _run_batch(processing_filter="rush", dry_run=False, batch_size=None, parallel_workers=1, profile_path=None, list_url=None, visible=False):
    started_at = time.monotonic()
    modes = [False] if visible else _crm_attempt_modes()
    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        try:
            payload = _run_batch_with_mode(
                headless_mode,
                processing_filter=processing_filter,
                dry_run=dry_run,
                batch_size=batch_size,
                parallel_workers=parallel_workers,
                profile_path=profile_path,
                list_url=list_url,
            )
            payload["headless"] = bool(headless_mode)
            return payload
        except Exception as exc:
            last_payload = {
                "action": "push_back_batch",
                "success": False,
                "message": str(exc),
                "order_count": 0,
                "order_ids": [],
                "report": [],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": _normalize_processing_filter(processing_filter),
                "manual_review_required": True,
                "retryable": _is_retryable_exception(exc),
                "error_type": type(exc).__name__,
                "duration_seconds": _elapsed_seconds(started_at),
            }
            if not headless_mode or index == len(modes) or not _is_retryable_exception(exc):
                break
            print("Headless Push Back failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
    return last_payload


def _run_single(order_id, processing_filter="rush", dry_run=False, profile_path=None, list_url=None, visible=False):
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or CRM order URL.")
    normalized_filter = _normalize_processing_filter(processing_filter)
    target_url = _list_url_for_filter(normalized_filter, list_url=list_url)
    started_at = time.monotonic()
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    modes = [False] if visible else _crm_attempt_modes()
    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        driver = None
        try:
            driver = _build_crm_session_driver(
                resolved_profile_path,
                headless_mode=headless_mode,
                profile_label=f"CRM push back single {normalized_order_id}",
            )
            data = _open_and_read_order(driver, normalized_order_id, normalized_filter, target_url)
            production_date = data.get("production_date")
            due_date = data.get("due_date")
            row = {
                "orderId": normalized_order_id,
                "rowText": "",
                "productionText": f"Production Date: {_date_text(production_date)}",
                "colorLabel": None,
            }
            if due_date is not None:
                row["rowText"] = f"Due Date: {_date_text(due_date)}"
            item = _run_order_with_driver(driver, row, normalized_filter, target_url, dry_run=dry_run)
            item["duration_seconds"] = _elapsed_seconds(started_at)
            payload = {
                "action": "push_back_single",
                "success": bool(item.get("success")),
                "message": item.get("message") or "Push Back single order completed.",
                "target_order_id": normalized_order_id,
                "order_count": 1,
                "order_ids": [normalized_order_id],
                "report": [item],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": normalized_filter,
                "list_url": target_url,
                "batch_size": 1,
                "parallel_workers": 1,
                "refresh_passes": 0,
                "duration_seconds": _elapsed_seconds(started_at),
                "manual_review_required": bool(item.get("manual_review_required")),
                "resolution": "single",
            }
            return payload
        except Exception as exc:
            last_payload = {
                "action": "push_back_single",
                "success": False,
                "message": str(exc),
                "target_order_id": normalized_order_id,
                "order_count": 1,
                "order_ids": [normalized_order_id],
                "report": [
                    _result(
                        normalized_order_id,
                        False,
                        "worker_exception",
                        str(exc),
                        manual_review_required=True,
                        retryable=_is_retryable_exception(exc),
                        error_type=type(exc).__name__,
                    )
                ],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": normalized_filter,
                "manual_review_required": True,
                "retryable": _is_retryable_exception(exc),
                "error_type": type(exc).__name__,
                "duration_seconds": _elapsed_seconds(started_at),
            }
            if not headless_mode or index == len(modes) or not _is_retryable_exception(exc):
                break
            print("Headless Push Back single run failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
        finally:
            safe_driver_quit(driver, profile_path=resolved_profile_path)
    return last_payload


def run(
    action="push_back_batch",
    processing_filter="rush",
    dry_run=False,
    batch_size=None,
    parallel_workers=1,
    profile_path=None,
    result_file=None,
    list_url=None,
    visible=False,
    order_id=None,
):
    if action not in {"push_back_batch", "push_back_single"}:
        raise RuntimeError("Unsupported CRM Push Back action.")
    normalized_filter = _normalize_processing_filter(processing_filter)
    if action == "push_back_single" or order_id:
        payload = _run_single(
            order_id,
            processing_filter=normalized_filter,
            dry_run=dry_run,
            profile_path=profile_path,
            list_url=list_url,
            visible=visible,
        )
    else:
        payload = _run_batch(
            processing_filter=normalized_filter,
            dry_run=dry_run,
            batch_size=batch_size,
            parallel_workers=parallel_workers,
            profile_path=profile_path,
            list_url=list_url,
            visible=visible,
        )
    write_result_payload(
        AUTOMATION_NAME,
        "crm_push_back.py",
        bool(payload.get("success")),
        payload.get("message") or "CRM Push Back completed.",
        extra_fields=payload,
        result_file=result_file,
    )
    return 0 if payload.get("success") else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Push eligible CRM production dates back by one business day.")
    parser.add_argument("--action", choices=["push_back_batch", "push_back_single"], default="push_back_batch")
    parser.add_argument("--order-id", required=False, help="Optional single 7-digit CRM order ID or CRM order URL.")
    parser.add_argument("--processing-filter", choices=["rush", "813", "high_value"], default="rush")
    parser.add_argument("--batch-size", type=int, default=None, help="Process up to this many orders; 0/unset means run until no eligible orders remain.")
    parser.add_argument("--parallel-workers", type=int, default=1, help="Number of CRM orders to process at once in batch mode.")
    parser.add_argument("--profile-path", required=False, help="Optional CRM Chrome user-data-dir override.")
    parser.add_argument("--result-file", required=False, help="Optional path for the JSON result payload.")
    parser.add_argument("--list-url", required=False, help="Optional Push Back CRM report URL override.")
    parser.add_argument("--visible", action="store_true", help="Run Chrome visibly instead of headless for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Open eligible orders and report intended production date changes without saving.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    sys.exit(
        run(
            action=options.action,
            processing_filter=options.processing_filter,
            dry_run=bool(options.dry_run),
            batch_size=options.batch_size,
            parallel_workers=options.parallel_workers,
            profile_path=options.profile_path,
            result_file=options.result_file,
            list_url=options.list_url,
            visible=bool(options.visible),
            order_id=options.order_id,
        )
    )
