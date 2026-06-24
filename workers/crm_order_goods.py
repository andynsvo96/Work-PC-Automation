"""
CRM Rush order-goods automation worker.

Usage:
    python crm_order_goods.py --action order_goods_batch --visible --dry-run
    python crm_order_goods.py --action order_goods_batch --visible
"""

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from automation_runtime import (
    SCRIPT_DIR,
    configure_console_utf8,
    refresh_if_crm_challenge_attempts_exceeded,
    safe_driver_quit,
    safe_take_screenshot,
    write_result_payload,
    write_status_payload,
)
from config import CRM_813_ORDER_GOODS_URL, CRM_ACTION_TIMEOUT, CRM_ORDER_GOODS_RUSH_URL, CRM_PROFILE_DIR
from crm_validate_address import (
    ALLOWED_813_ORDER_GOODS_ROW_DESCRIPTION,
    ALLOWED_813_ORDER_GOODS_ROW_LABELS,
    ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION,
    _batch_collection_limit,
    _batch_limit_reached,
    _build_crm_session_driver,
    _click_with_fallback,
    _clone_profile_for_worker,
    _collect_batch_order_ids,
    _collect_batch_order_ids_with_driver,
    _crm_attempt_modes,
    _is_retryable_exception,
    _normalize_requested_batch_size,
    _normalize_target_order_id,
    _open_target_order,
    _worker_profile_lock,
)
from crm_unlock_orders import wait_for_order_preview_panel as _wait_for_stock_unlock_preview_panel

configure_console_utf8()

AUTOMATION_NAME = "crm.order_goods"
PROFILE_PATH = os.path.join(SCRIPT_DIR, CRM_PROFILE_DIR)
RUSH_FILTER = "rush"
CONTINUOUS_ORDER_FETCH_LIMIT = 25
CRM_STATE_PATH = os.path.join(SCRIPT_DIR, "crm_state.json")
STOCK_UNLOCK_STATUS = "Stock Auto Ordering Unlocked"
ORDER_OPEN_REFRESH_ATTEMPTS = 2
ORDER_OPEN_REFRESH_DELAY_SECONDS = 2.0


def _publish_status(message, *, stage=None, current=None, total=None, order_id=None):
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
        pass


def _normalize_url_for_compare(value):
    return str(value or "").strip()


def _is_813_order_goods_list_url(value):
    return bool(
        _normalize_url_for_compare(value)
        and _normalize_url_for_compare(value) == _normalize_url_for_compare(CRM_813_ORDER_GOODS_URL)
    )


def _order_goods_allowed_row_options(target_url):
    if _is_813_order_goods_list_url(target_url):
        return {
            "allowed_row_labels": ALLOWED_813_ORDER_GOODS_ROW_LABELS,
            "allowed_row_description": ALLOWED_813_ORDER_GOODS_ROW_DESCRIPTION,
        }
    return {
        "allowed_row_labels": None,
        "allowed_row_description": None,
    }


def _elapsed_seconds(started_at):
    return round(max(0.0, time.monotonic() - started_at), 1)


def _attach_duration(payload, duration_seconds):
    if not isinstance(payload, dict):
        return payload
    duration = round(max(0.0, float(duration_seconds or 0.0)), 1)
    payload["duration_seconds"] = duration
    payload["session_duration_seconds"] = duration
    report = payload.get("report")
    if isinstance(report, list):
        for item in report:
            if isinstance(item, dict):
                item["duration_seconds"] = duration
                item["session_duration_seconds"] = duration
    return payload


def _validate_runtime_config(list_url=None):
    target_url = str(list_url or CRM_ORDER_GOODS_RUSH_URL or "").strip()
    if not target_url:
        raise RuntimeError("CRM_ORDER_GOODS_RUSH_URL is empty in config.py.")
    if str(list_url or "").strip():
        return target_url
    lowered_url = target_url.lower()
    if "shippingcharges%5blow%5d=1" not in lowered_url and "shippingcharges[low]=1" not in lowered_url:
        raise RuntimeError("Order Goods is Rush-only; the configured list URL must be a Rush CRM list.")
    return target_url


def _normalize_order_ids(raw_ids):
    cleaned = []
    seen = set()
    values = raw_ids if isinstance(raw_ids, list) else []
    for raw in values:
        text = "".join(ch for ch in str(raw or "") if ch.isdigit())
        if len(text) != 7 or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned


def _load_historical_order_goods_order_ids(state_path=CRM_STATE_PATH):
    try:
        with open(state_path, "r", encoding="utf-8-sig") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(f"Warning: could not read previous Order Goods history for skip list: {exc}")
        return set()

    history = state.get("run_history") if isinstance(state, dict) else []
    if not isinstance(history, list):
        return set()

    skipped_order_ids = set()
    for entry in history:
        if not isinstance(entry, dict):
            continue
        automation_key = str(entry.get("automation_key") or "").strip().lower()
        automation_label = str(entry.get("automation_label") or "").strip().lower()
        if automation_key != "order_goods" and "order goods" not in automation_label:
            continue
        skipped_order_ids.update(_normalize_order_ids(entry.get("order_ids")))
    return skipped_order_ids


def _find_sanmar_order_goods_button(driver, timeout=None):
    timeout = timeout or max(CRM_ACTION_TIMEOUT, 12)
    deadline = time.time() + timeout
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const lower = (value) => normalize(value).toLowerCase();
const vendorPattern = /(sanmar|s\s*&\s*s\s+activewear|s&s\s+activewear)/i;
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') {
      return false;
    }
  }
  return true;
}
function centerY(node) {
  const rect = node.getBoundingClientRect();
  return (rect.top || 0) + ((rect.height || 0) / 2);
}
const controls = Array.from(document.querySelectorAll([
  'button',
  'input[type="button"]',
  'input[type="submit"]',
  'a',
  '[ng-click]',
  '[onclick]',
  '[role="button"]',
  '.btn',
  '.button',
  '[class*="btn"]',
  '[class*="button"]'
].join(',')));
const directMatches = controls
  .filter((control) => {
    const tag = String(control.tagName || '').toLowerCase();
    if (!['button', 'input', 'a'].includes(tag) && control.getAttribute('role') !== 'button') return false;
    if (lower(control.innerText || control.textContent || control.value || control.getAttribute('aria-label')) !== 'order goods') return false;
    return isVisible(control);
  })
  .map((control) => ({
    control,
    disabled: !!control.disabled || control.getAttribute('disabled') !== null || /(?:^|\s)disabled(?:\s|$)/i.test(String(control.className || '')),
    top: centerY(control)
  }));
if (directMatches.length) {
  directMatches.sort((a, b) => Number(a.disabled) - Number(b.disabled) || a.top - b.top);
  return directMatches[0].control;
}
const matches = [];
for (const control of controls) {
  const controlText = lower(control.innerText || control.textContent || control.value || control.getAttribute('aria-label'));
  if (!controlText.includes('order goods') || !isVisible(control)) continue;
  let bestScore = null;
  let node = control;
  for (let depth = 0; node && depth < 16; depth += 1, node = node.parentElement) {
    const text = lower(node.innerText || node.textContent);
    const vendorMatch = vendorPattern.test(text);
    if (!vendorMatch) continue;
    const rect = node.getBoundingClientRect();
    bestScore = normalize(node.innerText || node.textContent).length + Math.round(rect.height || 0) + (depth * 1000);
    break;
  }
  matches.push({ control, score: bestScore, top: centerY(control) });
}
if (!matches.length) return null;
const ancestorMatches = matches.filter((item) => item.score !== null);
if (ancestorMatches.length) {
  ancestorMatches.sort((a, b) => a.score - b.score);
  return ancestorMatches[0].control;
}
if (matches.length === 1) return matches[0].control;
const vendorNodes = Array.from(document.querySelectorAll('body *')).filter((node) => {
  if (!isVisible(node)) return false;
  const ownText = normalize(Array.from(node.childNodes || [])
    .filter((child) => child.nodeType === Node.TEXT_NODE)
    .map((child) => child.textContent)
    .join(' '));
  if (!vendorPattern.test(ownText || node.textContent || '')) return false;
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0 && normalize(node.textContent).length < 240;
});
if (vendorNodes.length) {
  for (const item of matches) {
    item.score = Math.min(...vendorNodes.map((node) => Math.abs(centerY(node) - item.top)));
  }
  matches.sort((a, b) => a.score - b.score);
  return matches[0].control;
}
return matches[0].control;
"""
    while time.time() < deadline:
        try:
            button = driver.execute_script(script)
        except Exception:
            button = None
        if button is not None:
            return button
        button = _find_sanmar_order_goods_button_fallback(driver)
        if button is not None:
            return button
        time.sleep(0.25)
    return None


def _find_sanmar_order_goods_button_fallback(driver):
    selectors = [
        "button",
        "input[type='button']",
        "input[type='submit']",
        "a",
        "[role='button']",
        ".btn",
        "[class*='btn']",
        "[ng-click]",
        "[onclick]",
    ]
    try:
        controls = driver.find_elements(By.CSS_SELECTOR, ",".join(selectors))
    except Exception:
        return None

    matches = []
    for control in controls:
        try:
            displayed = bool(control.is_displayed())
        except Exception:
            displayed = False
        if not displayed:
            continue
        try:
            values = []
            for name in ("text", "value", "aria-label", "title"):
                raw = control.text if name == "text" else control.get_attribute(name)
                values.append(str(raw or ""))
            text = " ".join(" ".join(value.split()) for value in values)
        except Exception:
            text = ""
        if "order goods" not in " ".join(text.lower().split()):
            continue
        try:
            vendor_context = bool(
                driver.execute_script(
                    r"""
const node = arguments[0];
const vendorPattern = /(sanmar|s\s*&\s*s\s+activewear|s&s\s+activewear)/i;
for (let current = node; current && current !== document.body; current = current.parentElement) {
  const text = String(current.innerText || current.textContent || '');
  if (vendorPattern.test(text)) return true;
}
return false;
""",
                    control,
                )
            )
        except Exception:
            vendor_context = False
        matches.append((vendor_context, control))

    if not matches:
        return None
    vendor_matches = [control for vendor_context, control in matches if vendor_context]
    candidate = vendor_matches[0] if vendor_matches else matches[0][1]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", candidate)
        time.sleep(0.2)
    except Exception:
        pass
    return candidate


def _open_target_order_with_refresh(driver, order_id, shipping_filter=RUSH_FILTER, list_url_override=None):
    attempts = max(1, int(ORDER_OPEN_REFRESH_ATTEMPTS or 1))
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return _open_target_order(
                driver,
                order_id,
                shipping_filter=shipping_filter,
                list_url_override=list_url_override,
            )
        except TimeoutException as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            print(
                f"Order {order_id} did not open on attempt {attempt}/{attempts}; "
                "refreshing CRM and trying again..."
            )
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            try:
                driver.refresh()
            except Exception:
                pass
            time.sleep(ORDER_OPEN_REFRESH_DELAY_SECONDS)
    if last_exc is not None:
        raise last_exc
    return None


def _wait_for_order_goods_page_ready(driver, order_id, timeout=None):
    timeout = timeout or max(CRM_ACTION_TIMEOUT, 18)
    deadline = time.time() + timeout
    normalized_order_id = str(order_id or "").strip()
    script = r"""
const orderId = String(arguments[0] || '');
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const visibleText = Array.from(document.querySelectorAll('body *'))
  .filter(isVisible)
  .map((node) => node.innerText || node.textContent || node.value || '')
  .join('\n');
const text = normalize(visibleText);
return {
  ready: (!orderId || text.includes(orderId)) && (
    text.includes('stock status')
    || text.includes('order goods from vendor')
    || text.includes('sanmar')
    || text.includes('s&s activewear')
  ),
  textLength: text.length,
  hasOrderId: !orderId || text.includes(orderId),
  hasStockStatus: text.includes('stock status'),
  hasOrderGoods: text.includes('order goods'),
};
"""
    last_state = {}
    while time.time() < deadline:
        try:
            if refresh_if_crm_challenge_attempts_exceeded(driver, "Order Goods CRM order page", top_level=False):
                last_state = {"challenge_refresh": True}
                time.sleep(ORDER_OPEN_REFRESH_DELAY_SECONDS)
                continue
            state = driver.execute_script(script, normalized_order_id)
            if isinstance(state, dict):
                last_state = state
                if state.get("ready"):
                    return True
        except Exception:
            pass
        time.sleep(0.3)
    print(f"Warning: CRM order page did not report ready before Order Goods checks for {normalized_order_id}: {last_state}")
    return False


def _require_order_goods_page_ready(driver, order_id, timeout=None):
    normalized_order_id = str(order_id or "").strip()
    if _wait_for_order_goods_page_ready(driver, normalized_order_id, timeout=timeout):
        return True
    try:
        driver.switch_to.default_content()
    except Exception:
        pass
    try:
        driver.refresh()
        time.sleep(ORDER_OPEN_REFRESH_DELAY_SECONDS)
    except Exception:
        pass
    _open_target_order_with_refresh(
        driver,
        normalized_order_id,
        shipping_filter=RUSH_FILTER,
        list_url_override=None,
    )
    if _wait_for_order_goods_page_ready(driver, normalized_order_id, timeout=timeout):
        return True
    raise TimeoutException(
        f"Timed out waiting for CRM order page {normalized_order_id} to render before Order Goods checks."
    )


def _text_indicates_stock_already_ordered(text):
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    if "stock auto ordering queued" in normalized:
        return True
    false_value = r"(?:false|no|not\s+ordered|unordered|0)"
    true_value = r"(?:ordered|true|yes|1)"
    if re.search(rf"\bstock\s+ordered\s*[:=]\s*{false_value}\b", normalized):
        return False
    if re.search(rf"\bstock\s+status\s*[:=]\s*{true_value}\b", normalized):
        return True
    if re.search(rf"\bstock\s*:\s*{true_value}\b", normalized):
        return True
    if re.search(rf"\bstock\s+ordered\s*[:=]\s*{true_value}\b", normalized):
        return True
    return False


def _page_indicates_stock_already_ordered(driver):
    try:
        text = driver.execute_script(
            r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect && node.getBoundingClientRect();
  if (rect && (rect.width || 0) <= 0 && (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const parts = [document.body && (document.body.innerText || document.body.textContent)];
for (const node of Array.from(document.querySelectorAll('body *')).filter(isVisible)) {
  parts.push(node.innerText || node.textContent || node.value || '');
}
return normalize(parts.join('\n'));
"""
        )
    except Exception:
        text = ""
    return _text_indicates_stock_already_ordered(text)


def _page_indicates_stock_unavailable(driver):
    try:
        text = driver.execute_script(
            "return String((document.body && (document.body.innerText || document.body.textContent)) || '');"
        )
    except Exception:
        text = ""
    normalized = " ".join(str(text or "").lower().split())
    return (
        "all items are out of stock" in normalized
        or "order cannot proceed" in normalized
        or "out of stock. order cannot proceed" in normalized
    )


def _stock_unavailable_result(order_id):
    return {
        "order_id": order_id,
        "success": False,
        "outcome": "stock_unavailable",
        "message": "Stock could not be ordered because CRM reports all items are out of stock and the order cannot proceed.",
        "manual_review_required": True,
    }


def _stock_already_ordered_result(order_id, message=None):
    return {
        "order_id": order_id,
        "success": True,
        "outcome": "already_stock_ordered",
        "message": message or "Skipped because stock is already ordered for this order.",
        "manual_review_required": False,
    }


def _page_indicates_stock_locked_for_auto_ordering(driver):
    try:
        detected = driver.execute_script(
            r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
function visibleText(node) {
  if (!node) return '';
  const rect = node.getBoundingClientRect && node.getBoundingClientRect();
  if (rect && (rect.width || 0) <= 0 && (rect.height || 0) <= 0) return '';
  return normalize([
    node.innerText,
    node.textContent,
    node.value,
    node.getAttribute && node.getAttribute('aria-label'),
    node.getAttribute && node.getAttribute('title')
  ].join(' '));
}
const bodyText = normalize(document.body && (document.body.innerText || document.body.textContent));
if (bodyText.includes('locked for auto ordering')) return true;
for (const node of Array.from(document.querySelectorAll('body *'))) {
  const text = visibleText(node);
  if (text.includes('locked for auto ordering')) return true;
}
return false;
"""
        )
        return detected is True
    except Exception:
        return False


STOCK_UNLOCK_CONTROL_SCRIPT = r"""
const desired = 'Stock Auto Ordering Unlocked';
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const lower = (value) => normalize(value).toLowerCase();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function dispatchInput(node) {
  for (const name of ['input', 'change']) {
    node.dispatchEvent(new Event(name, { bubbles: true }));
  }
}
function findApply(scope) {
  const controls = Array.from((scope || document).querySelectorAll([
    'button',
    'input[type="button"]',
    'input[type="submit"]',
    'a',
    '[ng-click]',
    '[onclick]',
    '[role="button"]',
    '.btn',
    '[class*="btn"]'
  ].join(','))).filter(isVisible);
  return controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')) === 'apply')
    || controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')).includes('apply'))
    || null;
}
function findStatusNode() {
  const nodes = Array.from(document.querySelectorAll('body *')).filter(isVisible);
  let best = null;
  for (const node of nodes) {
    const text = lower(node.innerText || node.textContent);
    if (!text.includes('locked for auto ordering')) continue;
    const ownText = normalize(Array.from(node.childNodes || [])
      .filter((child) => child.nodeType === Node.TEXT_NODE)
      .map((child) => child.textContent)
      .join(' '));
    const rect = node.getBoundingClientRect();
    const score = (ownText ? 0 : 500) + normalize(node.textContent).length + Math.round(rect.height || 0);
    if (!best || score < best.score) best = { node, score };
  }
  return best ? best.node : null;
}
function scopesFrom(node) {
  const scopes = [];
  for (let current = node; current && current !== document.body && scopes.length < 9; current = current.parentElement) {
    scopes.push(current);
  }
  scopes.push(document);
  return scopes;
}
const statusNode = findStatusNode();
if (!statusNode) return { found: false, message: 'Locked stock status text was not found.' };
for (const scope of scopesFrom(statusNode)) {
  const select = Array.from(scope.querySelectorAll('select')).filter(isVisible).find((node) =>
    Array.from(node.options || []).some((option) => lower(option.textContent || option.value).includes('stock auto ordering unlocked'))
  );
  if (select) {
    const option = Array.from(select.options || []).find((item) => lower(item.textContent || item.value).includes('stock auto ordering unlocked'));
    if (option) {
      select.value = option.value;
      option.selected = true;
      dispatchInput(select);
      return { found: true, selected: true, method: 'select', applyButton: findApply(scope) || findApply(document) };
    }
  }
}
for (const scope of scopesFrom(statusNode)) {
  const controls = Array.from(scope.querySelectorAll([
    'input:not([type="hidden"])',
    'textarea',
    '[role="combobox"]',
    '[aria-haspopup="listbox"]',
    '[class*="select"]',
    '[class*="dropdown"]',
    'button'
  ].join(','))).filter((node) => {
    if (!isVisible(node)) return false;
    const text = lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'));
    return !text.includes('apply');
  });
  const control = controls.find((node) => lower(node.value || node.innerText || node.textContent || node.getAttribute('aria-label')).includes('locked for auto ordering'))
    || controls.find((node) => {
      const rect = node.getBoundingClientRect();
      const statusRect = statusNode.getBoundingClientRect();
      return Math.abs((rect.top || 0) - (statusRect.top || 0)) < 90;
    })
    || controls[0]
    || null;
  const applyButton = findApply(scope) || findApply(document);
  if (control || applyButton) return { found: true, selected: false, method: 'custom', control, applyButton };
}
return { found: false, message: 'Stock status controls were not found.' };
"""


FORCE_STOCK_UNLOCK_CONTROL_SCRIPT = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
const lower = (value) => normalize(value).toLowerCase();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function findApply(scope) {
  const controls = Array.from((scope || document).querySelectorAll([
    'button',
    'input[type="button"]',
    'input[type="submit"]',
    'a',
    '[ng-click]',
    '[onclick]',
    '[role="button"]',
    '.btn',
    '[class*="btn"]'
  ].join(','))).filter(isVisible);
  return controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')) === 'apply')
    || controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')).includes('apply'))
    || null;
}
function scoreInput(node, scope) {
  const rect = node.getBoundingClientRect();
  const scopeText = lower(scope && (scope.innerText || scope.textContent));
  let score = 0;
  if (scopeText.includes('unreviewed designstudio order')) score += 5000;
  if (scopeText.includes('order preview')) score += 3500;
  if (scopeText.includes('stock status')) score += 2500;
  if ((rect.top || 0) < 180) score += 1200;
  score += Math.max(0, 900 - Math.abs((rect.left || 0) - ((window.innerWidth || 1600) * 0.60)));
  score += Math.min(500, rect.width || 0);
  return score;
}
const candidates = [];
const inputs = Array.from(document.querySelectorAll([
  'input:not([type="hidden"])',
  'textarea',
  '[role="combobox"]',
  '[aria-haspopup="listbox"]'
].join(','))).filter((node) => {
  if (!isVisible(node)) return false;
  const text = lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'));
  return !text.includes('apply') && !text.includes('email customer') && !text.includes('send invoice');
});
for (const input of inputs) {
  for (let scope = input.parentElement, depth = 0; scope && scope !== document.body && depth < 10; scope = scope.parentElement, depth += 1) {
    const applyButton = findApply(scope);
    if (!applyButton) continue;
    const text = lower(scope.innerText || scope.textContent);
    const rect = scope.getBoundingClientRect();
    if (!text.includes('stock status') && !text.includes('unreviewed designstudio order') && !text.includes('order preview') && (rect.top || 0) > 220) {
      continue;
    }
    candidates.push({ control: input, applyButton, score: scoreInput(input, scope) - (depth * 50) });
    break;
  }
}
if (!candidates.length) {
  const applyButton = findApply(document);
  if (applyButton && inputs.length) {
    for (const input of inputs) {
      const rect = input.getBoundingClientRect();
      candidates.push({ control: input, applyButton, score: ((rect.top || 0) < 180 ? 1000 : 0) + Math.min(500, rect.width || 0) });
    }
  }
}
if (!candidates.length) return { found: false, message: 'Stock status controls were not found.' };
candidates.sort((a, b) => b.score - a.score);
return { found: true, selected: false, method: 'forced_custom', control: candidates[0].control, applyButton: candidates[0].applyButton };
"""


def _click_alert_ok_if_present(driver):
    try:
        alert = driver.switch_to.alert
        alert.accept()
        time.sleep(0.3)
        return True
    except Exception:
        return False


def _click_confirmation_ok_if_present(driver, timeout=2):
    deadline = time.time() + max(0.5, float(timeout or 0))
    selectors = [
        (By.XPATH, "//button[normalize-space()='OK']"),
        (By.XPATH, "//button[normalize-space()='Ok']"),
        (By.XPATH, "//button[contains(normalize-space(.), 'OK')]"),
        (By.XPATH, "//button[contains(normalize-space(.), 'Yes')]"),
    ]
    while time.time() < deadline:
        if _click_alert_ok_if_present(driver):
            return True
        for by, value in selectors:
            try:
                buttons = driver.find_elements(by, value)
            except Exception:
                continue
            for button in buttons:
                try:
                    if button.is_displayed() and button.is_enabled():
                        _click_with_fallback(driver, button)
                        time.sleep(0.3)
                        return True
                except Exception:
                    continue
        time.sleep(0.15)
    return False


def _find_stock_unlock_option(driver):
    script = r"""
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
const wanted = 'stock auto ordering unlocked';
const nodes = Array.from(document.querySelectorAll([
  '[role="option"]',
  'li',
  'a',
  'button',
  'div',
  'span'
].join(','))).filter(isVisible);
let best = null;
for (const node of nodes) {
  const text = normalize(node.innerText || node.textContent || node.value).toLowerCase();
  if (!text.includes(wanted)) continue;
  const score = text.length + Math.round((node.getBoundingClientRect().height || 0) * 5);
  if (!best || score < best.score) best = { node, score };
}
return best ? best.node : null;
"""
    try:
        return driver.execute_script(script)
    except Exception:
        return None


def _resolve_stock_unlock_text_control(driver, control):
    script = r"""
const start = arguments[0];
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
function editable(node) {
  if (!node || !isVisible(node)) return false;
  const tag = String(node.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea') return true;
  return String(node.getAttribute && node.getAttribute('contenteditable')).toLowerCase() === 'true';
}
if (editable(start)) return start;
const nested = Array.from((start || document).querySelectorAll('input:not([type="hidden"]), textarea, [contenteditable="true"]')).find(editable);
if (nested) return nested;
let scopes = [];
for (let current = start && start.parentElement; current && current !== document.body && scopes.length < 8; current = current.parentElement) {
  scopes.push(current);
}
scopes.push(document);
for (const scope of scopes) {
  const input = Array.from(scope.querySelectorAll('input:not([type="hidden"]), textarea, [contenteditable="true"]')).find(editable);
  if (input) return input;
}
return start;
"""
    try:
        resolved = driver.execute_script(script, control)
        return resolved or control
    except Exception:
        return control


def _select_native_stock_unlock_option(driver, control):
    script = r"""
const start = arguments[0];
const desired = 'stock auto ordering unlocked';
const lower = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
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
function dispatch(node) {
  for (const name of ['input', 'change']) {
    node.dispatchEvent(new Event(name, { bubbles: true }));
  }
}
function choose(select) {
  if (!select || String(select.tagName || '').toLowerCase() !== 'select' || !isVisible(select)) return false;
  const option = Array.from(select.options || []).find((item) => lower(item.textContent || item.value).includes(desired));
  if (!option) return false;
  select.value = option.value;
  option.selected = true;
  dispatch(select);
  return true;
}
if (choose(start)) return true;
const nested = Array.from((start || document).querySelectorAll('select')).find(choose);
if (nested) return true;
for (let scope = start && start.parentElement, depth = 0; scope && scope !== document.body && depth < 8; scope = scope.parentElement, depth += 1) {
  const found = Array.from(scope.querySelectorAll('select')).find(choose);
  if (found) return true;
}
return false;
"""
    try:
        return driver.execute_script(script, control) is True
    except Exception:
        return False


def _set_stock_unlock_control_text(driver, control, text):
    script = r"""
const node = arguments[0];
const value = String(arguments[1] || '');
const wanted = 'stock auto ordering unlocked';
const lower = (item) => String(item || '').replace(/\s+/g, ' ').trim().toLowerCase();
function dispatch(name, init) {
  node.dispatchEvent(new Event(name, Object.assign({ bubbles: true }, init || {})));
}
node.focus && node.focus();
const tag = String(node.tagName || '').toLowerCase();
if (tag === 'select') {
  const needle = lower(value);
  const option = Array.from(node.options || []).find((item) => {
    const text = lower(item.textContent || item.value);
    return text.includes(wanted) || (needle && text.includes(needle));
  });
  if (!option) return false;
  node.value = option.value;
  option.selected = true;
  dispatch('input');
  dispatch('change');
  return true;
}
if (tag === 'input' || tag === 'textarea') {
  const descriptor = Object.getOwnPropertyDescriptor(node.constructor.prototype, 'value')
    || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  if (descriptor && descriptor.set) descriptor.set.call(node, value);
  else node.value = value;
  dispatch('input');
  dispatch('keyup');
  dispatch('change');
  return true;
}
if (String(node.getAttribute && node.getAttribute('contenteditable')).toLowerCase() === 'true') {
  node.textContent = value;
  dispatch('input');
  dispatch('keyup');
  dispatch('change');
  return true;
}
return false;
"""
    try:
        return bool(driver.execute_script(script, control, text))
    except Exception:
        return False


def _find_preview_panel_input(driver, panel):
    script = r"""
const panel = arguments[0];
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
const inputs = Array.from(panel.querySelectorAll('input:not([type="hidden"]), textarea, [contenteditable="true"]'))
  .filter(isVisible)
  .map((node) => {
    const rect = node.getBoundingClientRect();
    return { node, score: (rect.width || 0) + ((rect.top || 0) < 180 ? 500 : 0) };
  });
inputs.sort((a, b) => b.score - a.score);
return inputs.length ? inputs[0].node : null;
"""
    try:
        return driver.execute_script(script, panel)
    except Exception:
        return None


def _find_preview_panel_apply_button(driver, panel):
    script = r"""
const panel = arguments[0];
const lower = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
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
const controls = Array.from(panel.querySelectorAll('button, input[type="button"], input[type="submit"], a, [role="button"], [ng-click], [onclick], .btn, [class*="btn"]'))
  .filter(isVisible);
return controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')) === 'apply')
  || controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')).includes('apply'))
  || null;
"""
    try:
        return driver.execute_script(script, panel)
    except Exception:
        return None


def _apply_stock_unlock_with_top_panel_script(driver):
    script = r"""
const done = arguments[arguments.length - 1];
const lower = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
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
function dispatch(node, name) {
  node.dispatchEvent(new Event(name, { bubbles: true }));
}
function dispatchKeyboard(node, name, key) {
  try {
    node.dispatchEvent(new KeyboardEvent(name, { bubbles: true, key, cancelable: true }));
  } catch (_) {}
}
function setTypedValue(node, value) {
  node.focus && node.focus();
  node.click && node.click();
  const tag = String(node.tagName || '').toLowerCase();
  if (tag === 'select') return false;
  if (tag === 'input' || tag === 'textarea') {
    const descriptor = Object.getOwnPropertyDescriptor(node.constructor.prototype, 'value')
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    if (descriptor && descriptor.set) descriptor.set.call(node, '');
    else node.value = '';
    dispatch(node, 'input');
    for (const key of value.split('')) {
      dispatchKeyboard(node, 'keydown', key);
      const nextValue = (node.value || '') + key;
      if (descriptor && descriptor.set) descriptor.set.call(node, nextValue);
      else node.value = nextValue;
      dispatch(node, 'input');
      dispatchKeyboard(node, 'keyup', key);
    }
  } else {
    node.textContent = '';
    dispatch(node, 'input');
    for (const key of value.split('')) {
      dispatchKeyboard(node, 'keydown', key);
      node.textContent = (node.textContent || '') + key;
      dispatch(node, 'input');
      dispatchKeyboard(node, 'keyup', key);
    }
  }
  dispatch(node, 'input');
  dispatch(node, 'keyup');
  dispatch(node, 'change');
  return true;
}
function chooseNativeSelect(node) {
  if (!node) return false;
  const select = String(node.tagName || '').toLowerCase() === 'select'
    ? node
    : Array.from(node.querySelectorAll && node.querySelectorAll('select') || []).find(isVisible);
  if (!select) return false;
  const option = Array.from(select.options || []).find((item) => lower(item.textContent || item.value).includes('stock auto ordering unlocked'));
  if (!option) return false;
  select.value = option.value;
  option.selected = true;
  dispatch(select, 'input');
  dispatch(select, 'change');
  return true;
}
function findApply(scope) {
  const controls = Array.from(scope.querySelectorAll('button, input[type="button"], input[type="submit"], a, [role="button"], [ng-click], [onclick], .btn, [class*="btn"]')).filter(isVisible);
  return controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')) === 'apply')
    || controls.find((node) => lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')).includes('apply'))
    || null;
}
function findTopPanelPair() {
  const inputs = Array.from(document.querySelectorAll([
    'select',
    'input:not([type="hidden"])',
    'textarea',
    '[contenteditable="true"]',
    '[role="combobox"]',
    '[aria-haspopup="listbox"]',
    '[class*="select"]',
    '[class*="dropdown"]'
  ].join(','))).filter((node) => {
    if (!isVisible(node)) return false;
    const text = lower(node.innerText || node.textContent || node.value || node.getAttribute('aria-label'));
    return !text.includes('apply') && !text.includes('send invoice') && !text.includes('email customer');
  });
  const pairs = [];
  for (const input of inputs) {
    for (let scope = input.parentElement, depth = 0; scope && scope !== document.body && depth < 10; scope = scope.parentElement, depth += 1) {
      const apply = findApply(scope);
      if (!apply) continue;
      const text = lower(scope.innerText || scope.textContent);
      const rect = input.getBoundingClientRect();
      let score = 0;
      if (text.includes('unreviewed designstudio order')) score += 5000;
      if (text.includes('stock status')) score += 3000;
      if ((rect.top || 0) < 170) score += 2000;
      score += Math.max(0, 800 - Math.abs((rect.left || 0) - ((window.innerWidth || 1600) * 0.58)));
      pairs.push({ input, apply, score: score - (depth * 50) });
      break;
    }
  }
  pairs.sort((a, b) => b.score - a.score);
  return pairs[0] || null;
}
function findSearchInput() {
  const inputs = Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea, [contenteditable="true"]')).filter(isVisible);
  const dropdownInputs = inputs.filter((node) => {
    for (let current = node.parentElement; current && current !== document.body; current = current.parentElement) {
      const text = lower(current.className || '');
      if (text.includes('dropdown') || text.includes('select') || text.includes('menu') || text.includes('popover')) return true;
    }
    return false;
  });
  return dropdownInputs[0] || inputs.find((node) => {
    const rect = node.getBoundingClientRect();
    return (rect.top || 0) < 180;
  }) || null;
}
function findUnlockOption() {
  const nodes = Array.from(document.querySelectorAll('[role="option"], li, a, button, div, span')).filter(isVisible);
  let best = null;
  for (const node of nodes) {
    const text = lower(node.innerText || node.textContent || node.value);
    if (!text.includes('stock auto ordering unlocked')) continue;
    const rect = node.getBoundingClientRect();
    const score = text.length + Math.round((rect.height || 0) * 5);
    if (!best || score < best.score) best = { node, score };
  }
  return best ? best.node : null;
}
const pair = findTopPanelPair();
if (!pair) {
  done({ success: false, message: 'Top Stock Status input and Apply button were not found.' });
  return;
}
if (chooseNativeSelect(pair.input)) {
  setTimeout(() => {
    pair.apply.click();
    done({ success: true, method: 'native_select' });
  }, 200);
  return;
}
pair.input.click && pair.input.click();
const searchInput = findSearchInput() || pair.input;
setTypedValue(searchInput, 'Stock Auto Ordering Unlocked');
let tries = 0;
const timer = setInterval(() => {
  tries += 1;
  const option = findUnlockOption();
  if (!option && tries < 20) return;
  clearInterval(timer);
  if (!option) {
    done({ success: false, message: 'Stock Auto Ordering Unlocked dropdown option did not appear after typing unlock.' });
    return;
  }
  option.click();
  setTimeout(() => {
    pair.apply.click();
    done({ success: true });
  }, 300);
}, 150);
"""
    try:
        result = driver.execute_async_script(script)
    except Exception as exc:
        return {"success": False, "message": str(exc)}
    return result if isinstance(result, dict) else {"success": bool(result)}


def _unlock_current_order_via_preview_panel(driver):
    scripted = _apply_stock_unlock_with_top_panel_script(driver)
    if scripted.get("success"):
        time.sleep(0.8)
        _click_confirmation_ok_if_present(driver, timeout=2)
        return

    panel = _wait_for_stock_unlock_preview_panel(driver)
    control = _find_preview_panel_input(driver, panel)
    if control is None:
        raise TimeoutException(scripted.get("message") or "The Stock Status input in the Order Preview panel was not found.")
    _choose_stock_unlock_status_from_control(driver, control)
    apply_button = _find_preview_panel_apply_button(driver, panel)
    if apply_button is None:
        raise TimeoutException("The Apply button in the Order Preview panel was not found.")
    _click_with_fallback(driver, apply_button)
    time.sleep(0.8)
    _click_confirmation_ok_if_present(driver, timeout=2)


def _choose_stock_unlock_status_from_control(driver, control):
    control = _resolve_stock_unlock_text_control(driver, control)
    _click_with_fallback(driver, control)
    time.sleep(0.2)
    if _select_native_stock_unlock_option(driver, control):
        time.sleep(0.3)
        return True
    typed = _set_stock_unlock_control_text(driver, control, STOCK_UNLOCK_STATUS)
    if not typed:
        try:
            control.send_keys(Keys.CONTROL, "a")
            control.send_keys(Keys.DELETE)
        except Exception:
            try:
                control.clear()
            except Exception:
                pass
        control.send_keys(STOCK_UNLOCK_STATUS)

    option = None
    deadline = time.time() + 6
    while time.time() < deadline:
        option = _find_stock_unlock_option(driver)
        if option is not None:
            break
        time.sleep(0.2)
    if option is not None:
        _click_with_fallback(driver, option)
        time.sleep(0.3)
        return True

    try:
        _set_stock_unlock_control_text(driver, control, STOCK_UNLOCK_STATUS)
        time.sleep(0.5)
        option = _find_stock_unlock_option(driver)
        if option is not None:
            _click_with_fallback(driver, option)
        else:
            control.send_keys(Keys.ENTER)
    except Exception:
        pass
    return True


def _unlock_current_order_for_auto_ordering(driver, order_id, dry_run=False, force=False):
    if not force and not _page_indicates_stock_locked_for_auto_ordering(driver):
        return None
    if dry_run:
        return {
            "order_id": order_id,
            "success": True,
            "outcome": "stock_unlock_ready",
            "message": "Order needs the Stock Auto Ordering Unlocked status before Order Goods. Dry run would set the status, refresh the order, then attempt Order Goods.",
            "manual_review_required": False,
            "stock_unlock_required": True,
        }

    if force:
        try:
            _unlock_current_order_via_preview_panel(driver)
            return {
                "order_id": order_id,
                "success": True,
                "outcome": "stock_unlocked",
                "message": "Applied Stock Auto Ordering Unlocked before attempting Order Goods.",
                "manual_review_required": False,
                "stock_unlock_required": True,
                "stock_unlocked_before_order_goods": True,
            }
        except Exception as preview_exc:
            controls = {"found": False, "message": str(preview_exc)}
    else:
        try:
            controls = driver.execute_script(STOCK_UNLOCK_CONTROL_SCRIPT)
        except Exception as exc:
            controls = {"found": False, "message": str(exc)}
    if force and (not isinstance(controls, dict) or not controls.get("found")):
        try:
            controls = driver.execute_script(FORCE_STOCK_UNLOCK_CONTROL_SCRIPT)
        except Exception as exc:
            controls = {"found": False, "message": str(exc)}
    if not isinstance(controls, dict) or not controls.get("found"):
        return {
            "order_id": order_id,
            "success": False,
            "outcome": "stock_unlock_control_not_found",
            "message": (controls or {}).get("message") or "Order needs the Stock Auto Ordering Unlocked status, but the stock status controls were not found.",
            "manual_review_required": True,
            "stock_unlock_required": True,
        }

    control = controls.get("control")
    if not controls.get("selected") and control is not None:
        try:
            _choose_stock_unlock_status_from_control(driver, control)
        except Exception as exc:
            return {
                "order_id": order_id,
                "success": False,
                "outcome": "stock_unlock_selection_failed",
                "message": f"Order needs the Stock Auto Ordering Unlocked status, but selecting that status failed: {exc}",
                "manual_review_required": True,
                "stock_unlock_required": True,
            }

    apply_button = controls.get("applyButton")
    if apply_button is None:
        return {
            "order_id": order_id,
            "success": False,
            "outcome": "stock_unlock_apply_not_found",
            "message": "Order needs the Stock Auto Ordering Unlocked status, but the Apply button was not found.",
            "manual_review_required": True,
            "stock_unlock_required": True,
        }
    try:
        _click_with_fallback(driver, apply_button)
        time.sleep(0.8)
        _click_confirmation_ok_if_present(driver, timeout=2)
    except Exception as exc:
        return {
            "order_id": order_id,
            "success": False,
            "outcome": "stock_unlock_apply_failed",
            "message": f"Order needs the Stock Auto Ordering Unlocked status, but clicking Apply failed: {exc}",
            "manual_review_required": True,
            "stock_unlock_required": True,
        }

    return {
        "order_id": order_id,
        "success": True,
        "outcome": "stock_unlocked",
        "message": "Applied Stock Auto Ordering Unlocked before attempting Order Goods.",
        "manual_review_required": False,
        "stock_unlock_required": True,
        "stock_unlocked_before_order_goods": True,
    }


def _wait_after_stock_unlock(driver, order_id, timeout=None, stock_tab_index=None):
    timeout = timeout or 12
    deadline = time.time() + timeout
    while time.time() < deadline:
        _wait_for_order_goods_page_ready(driver, order_id, timeout=4)
        if stock_tab_index is not None:
            try:
                _activate_stock_tab(driver, int(stock_tab_index))
                time.sleep(0.3)
            except Exception:
                pass
        if _page_indicates_stock_already_ordered(driver):
            return "ordered"
        button = _find_sanmar_order_goods_button(driver, timeout=1)
        if button is not None:
            try:
                if bool(button.is_enabled()):
                    return "orderable"
            except Exception:
                pass
        time.sleep(1.0)
    return "timeout"


def _stock_unlock_message(unlock_result):
    if isinstance(unlock_result, dict):
        return unlock_result.get("message") or "Applied Stock Auto Ordering Unlocked before Order Goods."
    return "Applied Stock Auto Ordering Unlocked before Order Goods."


def _mark_result_stock_unlocked(result, unlock_result):
    if not isinstance(result, dict):
        return result
    result["stock_unlocked_before_order_goods"] = True
    warnings = result.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    message = _stock_unlock_message(unlock_result)
    if message not in warnings:
        warnings.append(message)
    result["warnings"] = warnings
    return result


def _refresh_order_after_stock_unlock(driver, order_id, stock_tab_index=None):
    try:
        driver.refresh()
        time.sleep(1.0)
    except Exception:
        pass
    _require_order_goods_page_ready(driver, order_id)
    if stock_tab_index is not None:
        try:
            _activate_stock_tab(driver, int(stock_tab_index))
            time.sleep(0.3)
        except Exception:
            pass
    return _wait_after_stock_unlock(driver, order_id, stock_tab_index=stock_tab_index)


def _page_indicates_non_vendor_stock_tab(driver):
    try:
        text = driver.execute_script(
            "return String((document.body && (document.body.innerText || document.body.textContent)) || '');"
        )
    except Exception:
        text = ""
    normalized = " ".join(str(text or "").lower().split())
    return "order goods from vendor: manual order" in normalized or "vendor order #" in normalized


def _order_goods_for_open_order(
    driver,
    order_id,
    dry_run=False,
    allow_unlock_retry=True,
    stock_tab_index=None,
    ignore_already_ordered=False,
):
    if not ignore_already_ordered and _page_indicates_stock_already_ordered(driver):
        return _stock_already_ordered_result(order_id)
    button = _find_sanmar_order_goods_button(driver)
    if button is None:
        if allow_unlock_retry and _page_indicates_stock_locked_for_auto_ordering(driver):
            unlock_result = _unlock_current_order_for_auto_ordering(driver, order_id, dry_run=dry_run, force=True)
            if dry_run or not unlock_result or not unlock_result.get("success"):
                return unlock_result or {
                    "order_id": order_id,
                    "success": False,
                    "outcome": "order_goods_locked",
                    "message": "Order appears locked: Sanmar / S&S Activewear order goods button is missing or disabled.",
                    "manual_review_required": True,
                }
            post_unlock_state = _refresh_order_after_stock_unlock(driver, order_id, stock_tab_index=stock_tab_index)
            if post_unlock_state == "ordered":
                return _mark_result_stock_unlocked(
                    _stock_already_ordered_result(
                        order_id,
                        "Skipped because stock was already ordered after applying Stock Auto Ordering Unlocked.",
                    ),
                    unlock_result,
                )
            result = _order_goods_for_open_order(
                driver,
                order_id,
                dry_run=dry_run,
                allow_unlock_retry=False,
                stock_tab_index=stock_tab_index,
                ignore_already_ordered=ignore_already_ordered,
            )
            return _mark_result_stock_unlocked(result, unlock_result)
        raise TimeoutException(
            "The Sanmar / S&S Activewear order goods button was not found on this stock tab."
        )
    try:
        enabled = bool(button.is_enabled())
    except Exception:
        enabled = False
    if not enabled:
        if allow_unlock_retry:
            unlock_result = _unlock_current_order_for_auto_ordering(driver, order_id, dry_run=dry_run, force=True)
            if dry_run or not unlock_result or not unlock_result.get("success"):
                return unlock_result or {
                    "order_id": order_id,
                    "success": False,
                    "outcome": "order_goods_locked",
                    "message": "Order appears locked: Sanmar / S&S Activewear order goods button is disabled and cannot be clicked.",
                    "manual_review_required": True,
                }
            post_unlock_state = _refresh_order_after_stock_unlock(driver, order_id, stock_tab_index=stock_tab_index)
            if post_unlock_state == "ordered":
                return _mark_result_stock_unlocked(
                    _stock_already_ordered_result(
                        order_id,
                        "Skipped because stock was already ordered after applying Stock Auto Ordering Unlocked.",
                    ),
                    unlock_result,
                )
            result = _order_goods_for_open_order(
                driver,
                order_id,
                dry_run=dry_run,
                allow_unlock_retry=False,
                stock_tab_index=stock_tab_index,
                ignore_already_ordered=ignore_already_ordered,
            )
            return _mark_result_stock_unlocked(result, unlock_result)
        return {
            "order_id": order_id,
            "success": False,
            "outcome": "order_goods_locked",
            "message": "Order appears locked: Sanmar / S&S Activewear order goods button is disabled and cannot be clicked.",
            "manual_review_required": True,
        }
    if dry_run:
        return {
            "order_id": order_id,
            "success": True,
            "outcome": "order_goods_ready",
            "message": "Sanmar / S&S Activewear order goods button is available.",
            "manual_review_required": False,
        }
    _click_with_fallback(driver, button)
    time.sleep(0.5)
    return {
        "order_id": order_id,
        "success": True,
        "outcome": "order_goods_clicked",
        "message": "Clicked Sanmar / S&S Activewear order goods.",
        "manual_review_required": False,
    }


STOCK_TAB_SCRIPT = r"""
const targetIndex = arguments.length ? arguments[0] : null;
const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  if ((rect.width || 0) <= 0 || (rect.height || 0) <= 0) return false;
  for (let current = node; current; current = current.parentElement) {
    const style = window.getComputedStyle(current);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
  }
  return true;
}
function tabText(node) {
  return normalize(node && (node.innerText || node.textContent));
}
function looksLikeStockTab(node) {
  const text = tabText(node);
  if (!text || text.length > 500) return false;
  const lower = text.toLowerCase();
  return lower.includes('design previews') && /\bqty\s*:/i.test(text);
}
function clickableFor(node) {
  let best = node;
  for (let current = node; current && current !== document.body; current = current.parentElement) {
    const attrs = [
      current.getAttribute && current.getAttribute('ng-click'),
      current.getAttribute && current.getAttribute('onclick'),
      current.getAttribute && current.getAttribute('role'),
    ].join(' ').toLowerCase();
    const tag = String(current.tagName || '').toLowerCase();
    const style = window.getComputedStyle(current);
    if (tag === 'a' || tag === 'button' || attrs.includes('click') || attrs.includes('tab') || attrs.includes('button') || style.cursor === 'pointer') {
      best = current;
      break;
    }
    const rect = current.getBoundingClientRect();
    const text = tabText(current);
    if (text === tabText(node) && rect.width <= 320 && rect.height <= 180) {
      best = current;
    }
  }
  return best;
}
const minimal = [];
for (const node of Array.from(document.querySelectorAll('body *'))) {
  if (!isVisible(node) || !looksLikeStockTab(node)) continue;
  const childTab = Array.from(node.children || []).some((child) => isVisible(child) && looksLikeStockTab(child));
  if (childTab) continue;
  minimal.push(node);
}
const seen = new Set();
const tabs = [];
for (const node of minimal) {
  const element = clickableFor(node);
  const rect = element.getBoundingClientRect();
  const label = tabText(element) || tabText(node);
  const key = [Math.round(rect.top), Math.round(rect.left), label].join('|');
  if (seen.has(key)) continue;
  seen.add(key);
  tabs.push({ element, label, top: rect.top || 0, left: rect.left || 0 });
}
tabs.sort((a, b) => (a.top - b.top) || (a.left - b.left));
if (targetIndex === null || targetIndex === undefined) {
  return tabs.map((tab, index) => ({ index, label: tab.label, top: tab.top, left: tab.left }));
}
const tab = tabs[Number(targetIndex)];
return tab ? { element: tab.element, label: tab.label, count: tabs.length } : null;
"""


def _stock_tab_summary_label(label):
    text = " ".join(str(label or "").split())
    if not text:
        return ""
    prefix = text.split("Design Previews", 1)[0].strip()
    parts = [part.strip() for part in prefix.splitlines() if part.strip()]
    if parts:
        return " | ".join(parts[:2])
    return prefix[:120] if prefix else text[:120]


def _find_stock_tabs(driver):
    try:
        tabs = driver.execute_script(STOCK_TAB_SCRIPT)
    except Exception:
        return []
    return tabs if isinstance(tabs, list) else []


def _activate_stock_tab(driver, tab_index):
    try:
        tab = driver.execute_script(STOCK_TAB_SCRIPT, int(tab_index))
    except Exception:
        tab = None
    if not isinstance(tab, dict) or tab.get("element") is None:
        return None
    _click_with_fallback(driver, tab["element"])
    time.sleep(0.7)
    return tab


def _order_goods_for_all_stock_tabs(driver, order_id, dry_run=False):
    tabs = _find_stock_tabs(driver)
    tab_count = len(tabs)
    if tab_count <= 1:
        result = _order_goods_for_open_order(driver, order_id, dry_run=dry_run, stock_tab_index=0 if tabs else None)
        result["stock_tab_index"] = 1
        result["stock_tab_count"] = max(1, tab_count)
        if tabs:
            result["stock_tab_label"] = _stock_tab_summary_label(tabs[0].get("label"))
        return [result]

    results = []
    for tab_index in range(tab_count):
        tab_number = tab_index + 1
        tab = _activate_stock_tab(driver, tab_index)
        tab_label = _stock_tab_summary_label((tab or {}).get("label") or (tabs[tab_index] or {}).get("label"))
        if tab is None:
            results.append(
                {
                    "order_id": order_id,
                    "success": False,
                    "outcome": "stock_tab_not_found",
                    "message": f"Stock tab {tab_number} of {tab_count} could not be activated.",
                    "manual_review_required": True,
                    "stock_tab_index": tab_number,
                    "stock_tab_count": tab_count,
                    "stock_tab_label": tab_label,
                }
            )
            continue
        print(f"Ordering goods for stock tab {tab_number}/{tab_count}: {tab_label or 'untitled tab'}...")
        try:
            result = _order_goods_for_open_order(driver, order_id, dry_run=dry_run, stock_tab_index=tab_index)
        except TimeoutException as exc:
            if not _page_indicates_non_vendor_stock_tab(driver):
                raise
            result = {
                "order_id": order_id,
                "success": True,
                "outcome": "stock_tab_not_vendor_orderable",
                "message": "No Sanmar / S&S Activewear order goods button was present on this stock tab; continuing to the next tab.",
                "manual_review_required": False,
                "warnings": [str(exc)],
            }
        result["stock_tab_index"] = tab_number
        result["stock_tab_count"] = tab_count
        if tab_label:
            result["stock_tab_label"] = tab_label
            result["message"] = f"{result.get('message', 'Order Goods completed.')} Stock tab: {tab_label}."
        results.append(result)
        if (
            tab_index < tab_count - 1
            and isinstance(result, dict)
            and result.get("success")
            and str(result.get("outcome") or "") == "order_goods_clicked"
        ):
            _open_target_order_with_refresh(driver, order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
            _require_order_goods_page_ready(driver, order_id)
    return results


def _run_order_with_driver(driver, order_id, dry_run=False):
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or CRM order URL.")
    _publish_status(f"Opening CRM order {normalized_order_id} for Rush Order Goods.", stage="opening_order", order_id=normalized_order_id)
    _open_target_order_with_refresh(driver, normalized_order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
    _publish_status(f"Checking Order Goods page for order {normalized_order_id}.", stage="checking_order_goods", order_id=normalized_order_id)
    _require_order_goods_page_ready(driver, normalized_order_id)
    _publish_status(f"Checking stock unlock status for order {normalized_order_id}.", stage="checking_stock_status", order_id=normalized_order_id)
    unlock_result = _unlock_current_order_for_auto_ordering(driver, normalized_order_id, dry_run=dry_run)
    if unlock_result:
        if dry_run or not unlock_result.get("success"):
            return [unlock_result]
        try:
            driver.refresh()
            time.sleep(1.0)
        except Exception:
            pass
        _publish_status(f"Reloading CRM order {normalized_order_id} after stock unlock.", stage="reloading_order", order_id=normalized_order_id)
        _open_target_order_with_refresh(driver, normalized_order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
        _require_order_goods_page_ready(driver, normalized_order_id)
        unlocked_message = unlock_result.get("message") or "Applied Stock Auto Ordering Unlocked before Order Goods."
        post_unlock_state = _wait_after_stock_unlock(driver, normalized_order_id)
        if post_unlock_state == "ordered":
            result = _stock_already_ordered_result(
                normalized_order_id,
                "Skipped because stock was already ordered after applying Stock Auto Ordering Unlocked.",
            )
            result["stock_unlocked_before_order_goods"] = True
            result["warnings"] = [unlocked_message]
            return [result]
        _publish_status(f"Ordering stock tabs for order {normalized_order_id}.", stage="ordering_stock", order_id=normalized_order_id)
        results = _order_goods_for_all_stock_tabs(driver, normalized_order_id, dry_run=dry_run)
        for item in results:
            _mark_result_stock_unlocked(item, unlock_result)
        return results
    _publish_status(f"Ordering stock tabs for order {normalized_order_id}.", stage="ordering_stock", order_id=normalized_order_id)
    results = _order_goods_for_all_stock_tabs(driver, normalized_order_id, dry_run=dry_run)
    return results


def _summary_message(report_items, refresh_passes=1, order_count=0, row_description=None):
    total = len(report_items)
    row_description = str(row_description or ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION)
    order_groups = {}
    for item in report_items:
        order_id = str(item.get("order_id") or "").strip()
        if not order_id:
            continue
        order_groups.setdefault(order_id, []).append(item)
    successful_orders = sum(1 for items in order_groups.values() if all(bool(item.get("success")) for item in items))
    failed_orders = len(order_groups) - successful_orders
    if total == 0:
        if row_description == ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION:
            return f"No {row_description} Rush orders were detected in the CRM order-goods list."
        return f"No {row_description} orders were detected in the CRM order-goods list."
    parts = [
        f"Order Goods processed {max(1, int(order_count or len(order_groups) or 0))} order(s) and {total} stock tab(s) across {max(1, int(refresh_passes or 1))} CRM list refresh pass(es).",
        f"{successful_orders} order(s) succeeded.",
    ]
    if failed_orders:
        parts.append(f"{failed_orders} order(s) need attention.")
    return " ".join(parts)


def _order_goods_worker_payload(order_id, headless_mode, dry_run=False, profile_path=None, skip_stale_chrome_check=False):
    started_at = time.monotonic()
    driver = None
    report_items = []
    try:
        _publish_status(f"Loading CRM session for Rush Order Goods order {order_id}.", stage="loading_crm", order_id=order_id)
        driver = _build_crm_session_driver(
            profile_path,
            headless_mode=headless_mode,
            profile_label=f"CRM order goods worker {order_id}",
            skip_stale_chrome_check=skip_stale_chrome_check,
        )
        report_items = _run_order_with_driver(driver, order_id, dry_run=dry_run)
        duration = _elapsed_seconds(started_at)
        for item in report_items:
            if isinstance(item, dict):
                item["duration_seconds"] = duration
                item["session_duration_seconds"] = duration
        success = all(bool(item.get("success")) for item in report_items) if report_items else True
        message = _summary_message(report_items, refresh_passes=1, order_count=1)
        return {
            "action": "order_goods_batch",
            "success": success,
            "message": message,
            "order_count": 1,
            "order_ids": [order_id],
            "report": report_items,
            "dry_run": bool(dry_run),
            "headless": bool(headless_mode),
            "shipping_filter": RUSH_FILTER,
            "refresh_passes": 1,
            "duration_seconds": duration,
            "session_duration_seconds": duration,
            "manual_review_required": any(bool(item.get("manual_review_required")) for item in report_items),
            "resolution": "batch",
        }
    except Exception as exc:
        if driver is not None:
            safe_take_screenshot(driver, "crm_order_goods_error")
        duration = _elapsed_seconds(started_at)
        return {
            "action": "order_goods_batch",
            "success": False,
            "message": str(exc),
            "order_count": 1,
            "order_ids": [order_id],
            "report": [
                {
                    "order_id": order_id,
                    "success": False,
                    "outcome": "worker_exception",
                    "message": str(exc),
                    "manual_review_required": True,
                    "retryable": _is_retryable_exception(exc),
                    "error_type": type(exc).__name__,
                    "duration_seconds": duration,
                    "session_duration_seconds": duration,
                }
            ],
            "dry_run": bool(dry_run),
            "headless": bool(headless_mode),
            "shipping_filter": RUSH_FILTER,
            "duration_seconds": duration,
            "session_duration_seconds": duration,
            "manual_review_required": True,
            "resolution": "batch",
        }
    finally:
        safe_driver_quit(driver, profile_path=profile_path)


def _payload_has_retryable_order_goods_exception(payload):
    if not isinstance(payload, dict) or payload.get("success"):
        return False
    if payload.get("retryable"):
        return True
    report = payload.get("report")
    if not isinstance(report, list):
        return False
    for item in report:
        if not isinstance(item, dict) or item.get("success"):
            continue
        if item.get("outcome") != "worker_exception":
            continue
        if item.get("retryable"):
            return True
        message = item.get("message") or payload.get("message") or ""
        error = RuntimeError(str(message))
        if _is_retryable_exception(error):
            return True
    return False


def _mark_order_goods_retry_attempted(payload):
    if not isinstance(payload, dict):
        return payload
    payload["transient_retry_attempted"] = True
    report = payload.get("report")
    if isinstance(report, list):
        for item in report:
            if isinstance(item, dict):
                item["retry_attempted"] = True
                item["transient_retry_attempted"] = True
    return payload


def _run_parallel_batch_with_mode(headless_mode, dry_run=False, batch_size=None, profile_path=None, list_url=None, parallel_workers=1):
    batch_started_at = time.monotonic()
    target_url = _validate_runtime_config(list_url)
    row_options = _order_goods_allowed_row_options(target_url)
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    worker_limit = max(1, int(parallel_workers or 1))
    if requested_batch_size is not None:
        worker_limit = min(worker_limit, requested_batch_size)

    report_items = []
    attempted_order_ids = []
    historical_order_id_set = _load_historical_order_goods_order_ids()
    attempted_order_id_set = set(historical_order_id_set)
    refresh_passes = 0
    completed_order_count = 0
    total_scanned_count = 0
    if historical_order_id_set:
        print(
            "Skipping "
            f"{len(historical_order_id_set)} previously logged Rush Order Goods order(s) from shared history. "
            "Clear Stock Tools history to make them eligible again."
        )

    while not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
        refresh_passes += 1
        remaining = _batch_collection_limit(
            requested_batch_size,
            len(attempted_order_ids),
            worker_limit=worker_limit,
        )
        _publish_status(
            f"Loading CRM order-goods list pass {refresh_passes} to scan for eligible orders.",
            stage="loading_crm_list",
            current=completed_order_count if total_scanned_count else None,
            total=total_scanned_count or None,
        )
        order_ids = _collect_batch_order_ids(
            RUSH_FILTER,
            remaining,
            resolved_profile_path,
            list_url_override=target_url,
            exclude_order_ids=attempted_order_id_set,
            visible=not bool(headless_mode),
            **row_options,
        )
        if not order_ids:
            _publish_status(
                "No more eligible Rush Order Goods orders were found in the CRM list.",
                stage="scan_complete",
                current=completed_order_count if total_scanned_count else None,
                total=total_scanned_count or None,
            )
            break
        order_ids = order_ids[:remaining]
        total_scanned_count += len(order_ids)
        _publish_status(
            f"Scanned {len(order_ids)} eligible Rush Order Goods order(s) on pass {refresh_passes}; processing with {worker_limit} worker(s).",
            stage="processing_orders",
            current=completed_order_count,
            total=total_scanned_count,
        )

        finished_lock = threading.Lock()
        worker_gate = threading.BoundedSemaphore(worker_limit)
        threads = []
        chunk_payloads = []

        def _worker(order_index, order_id):
            nonlocal completed_order_count
            with worker_gate:
                session_started_at = time.monotonic()
                print(f"Launching Rush Order Goods worker {order_index + 1}/{len(order_ids)} for order {order_id}...")
                with finished_lock:
                    current_done = completed_order_count
                _publish_status(
                    f"Processing Rush Order Goods order {order_id} ({current_done}/{total_scanned_count} done).",
                    stage="processing_order",
                    current=current_done,
                    total=total_scanned_count,
                    order_id=order_id,
                )
                payload = None
                attempt_count = 0
                retry_allowed = not bool(dry_run)
                total_attempts = 2 if retry_allowed else 1
                worker_slot = (order_index % worker_limit) + 1
                for attempt in range(1, total_attempts + 1):
                    attempt_count = attempt
                    try:
                        temp_root, cloned_profile_path = _clone_profile_for_worker(
                            resolved_profile_path,
                            f"order_goods_{order_index + 1}_{order_id}_attempt_{attempt}",
                            worker_slot=worker_slot,
                            pool_name="order_goods",
                            rebuild=attempt > 1,
                        )
                    except Exception as exc:
                        payload = {
                            "success": False,
                            "order_ids": [order_id],
                            "report": [
                                {
                                    "order_id": order_id,
                                    "success": False,
                                    "outcome": "worker_exception",
                                    "message": str(exc),
                                    "manual_review_required": True,
                                    "retryable": _is_retryable_exception(exc),
                                    "error_type": type(exc).__name__,
                                    "duration_seconds": _elapsed_seconds(session_started_at),
                                    "session_duration_seconds": _elapsed_seconds(session_started_at),
                                }
                            ],
                        }
                        break
                    try:
                        if attempt > 1:
                            print(f"Retrying Rush Order Goods order {order_id} once with a fresh CRM worker profile...")
                            time.sleep(1)
                        with _worker_profile_lock(cloned_profile_path):
                            payload = _order_goods_worker_payload(
                                order_id,
                                headless_mode=headless_mode,
                                dry_run=dry_run,
                                profile_path=cloned_profile_path,
                                skip_stale_chrome_check=True,
                            )
                    finally:
                        if temp_root:
                            shutil.rmtree(temp_root, ignore_errors=True)
                    if not _payload_has_retryable_order_goods_exception(payload):
                        break
                    if attempt < total_attempts:
                        payload = _mark_order_goods_retry_attempted(payload)
                        continue
                    payload = _mark_order_goods_retry_attempted(payload)
                _attach_duration(payload, _elapsed_seconds(session_started_at))
                if isinstance(payload, dict):
                    payload["attempt_count"] = attempt_count
                with finished_lock:
                    chunk_payloads.append(payload)
                    completed_order_count += 1
                    current_done = completed_order_count
                _publish_status(
                    f"Finished Rush Order Goods order {order_id} ({current_done}/{total_scanned_count} done).",
                    stage="finished_order",
                    current=current_done,
                    total=total_scanned_count,
                    order_id=order_id,
                )

        for order_index, order_id in enumerate(order_ids):
            attempted_order_id_set.add(order_id)
            attempted_order_ids.append(order_id)
            thread = threading.Thread(target=_worker, args=(order_index, order_id), daemon=True)
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        chunk_order_position = {order_id: index for index, order_id in enumerate(order_ids)}
        chunk_payloads.sort(key=lambda payload: chunk_order_position.get(str((payload.get("order_ids") or [""])[0] or ""), 999999))
        for payload in chunk_payloads:
            payload_report = payload.get("report") if isinstance(payload, dict) else []
            if isinstance(payload_report, list):
                report_items.extend(payload_report)

        if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            break
        print("Finished Rush Order Goods list pass; reopening the list to look for more eligible orders...")

    success = all(bool(item.get("success")) for item in report_items) if report_items else True
    skipped_historical_count = len(historical_order_id_set)
    message = _summary_message(
        report_items,
        refresh_passes=refresh_passes,
        order_count=len(attempted_order_ids),
        row_description=row_options.get("allowed_row_description"),
    )
    if skipped_historical_count:
        message = (
            f"{message} Skipped {skipped_historical_count} previously logged Rush Order Goods order(s); "
            "clear Stock Tools history to make them eligible again."
        )
    return {
        "action": "order_goods_batch",
        "success": success,
        "message": message,
        "order_count": len(attempted_order_ids),
        "order_ids": attempted_order_ids,
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(headless_mode),
        "shipping_filter": RUSH_FILTER,
        "list_url": target_url,
        "batch_size": requested_batch_size,
        "parallel_workers": worker_limit,
        "refresh_passes": refresh_passes,
        "duration_seconds": _elapsed_seconds(batch_started_at),
        "manual_review_required": any(bool(item.get("manual_review_required")) for item in report_items),
        "resolution": "batch" if attempted_order_ids else "no_orders",
        "skipped_historical_order_count": skipped_historical_count,
    }


def _run_batch_with_mode(headless_mode, dry_run=False, batch_size=None, profile_path=None, list_url=None, parallel_workers=1):
    batch_started_at = time.monotonic()
    target_url = _validate_runtime_config(list_url)
    row_options = _order_goods_allowed_row_options(target_url)
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    worker_limit = max(1, int(parallel_workers or 1))
    if requested_batch_size is not None:
        worker_limit = min(worker_limit, requested_batch_size)
    if worker_limit > 1:
        return _run_parallel_batch_with_mode(
            headless_mode,
            dry_run=dry_run,
            batch_size=requested_batch_size,
            profile_path=profile_path,
            list_url=target_url,
            parallel_workers=worker_limit,
        )

    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    _publish_status("Loading CRM session for Rush Order Goods batch.", stage="loading_crm")
    driver = _build_crm_session_driver(
        resolved_profile_path,
        headless_mode=headless_mode,
        profile_label="CRM order goods",
    )
    report_items = []
    attempted_order_ids = []
    historical_order_id_set = _load_historical_order_goods_order_ids()
    attempted_order_id_set = set(historical_order_id_set)
    refresh_passes = 0
    completed_order_count = 0
    total_scanned_count = 0
    if historical_order_id_set:
        print(
            "Skipping "
            f"{len(historical_order_id_set)} previously logged Rush Order Goods order(s) from shared history. "
            "Clear Stock Tools history to make them eligible again."
        )
    try:
        while not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            refresh_passes += 1
            remaining = _batch_collection_limit(
                requested_batch_size,
                len(attempted_order_ids),
                worker_limit=CONTINUOUS_ORDER_FETCH_LIMIT,
            )
            _publish_status(
                f"Loading CRM order-goods list pass {refresh_passes} to scan for eligible orders.",
                stage="loading_crm_list",
                current=completed_order_count if total_scanned_count else None,
                total=total_scanned_count or None,
            )
            order_ids = _collect_batch_order_ids_with_driver(
                driver,
                RUSH_FILTER,
                remaining,
                list_url_override=target_url,
                exclude_order_ids=attempted_order_id_set,
                **row_options,
            )
            if not order_ids:
                _publish_status(
                    "No more eligible Rush Order Goods orders were found in the CRM list.",
                    stage="scan_complete",
                    current=completed_order_count if total_scanned_count else None,
                    total=total_scanned_count or None,
                )
                break
            total_scanned_count += len(order_ids)
            _publish_status(
                f"Scanned {len(order_ids)} eligible Rush Order Goods order(s) on pass {refresh_passes}; processing orders.",
                stage="processing_orders",
                current=completed_order_count,
                total=total_scanned_count,
            )
            for order_id in order_ids:
                if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                    break
                attempted_order_id_set.add(order_id)
                attempted_order_ids.append(order_id)
                print(f"Processing Rush Order Goods order {len(attempted_order_ids)}: {order_id}...")
                _publish_status(
                    f"Processing Rush Order Goods order {order_id} ({completed_order_count}/{total_scanned_count} done).",
                    stage="processing_order",
                    current=completed_order_count,
                    total=total_scanned_count,
                    order_id=order_id,
                )
                order_started_at = time.monotonic()
                try:
                    order_report = _run_order_with_driver(driver, order_id, dry_run=dry_run)
                    order_duration = _elapsed_seconds(order_started_at)
                    for item in order_report:
                        if isinstance(item, dict):
                            item["duration_seconds"] = order_duration
                            item["session_duration_seconds"] = order_duration
                    report_items.extend(order_report)
                except Exception as exc:
                    safe_take_screenshot(driver, "crm_order_goods_error")
                    order_duration = _elapsed_seconds(order_started_at)
                    report_items.append(
                        {
                            "order_id": order_id,
                            "success": False,
                            "outcome": "worker_exception",
                            "message": str(exc),
                            "manual_review_required": True,
                            "retryable": _is_retryable_exception(exc),
                            "error_type": type(exc).__name__,
                            "duration_seconds": order_duration,
                            "session_duration_seconds": order_duration,
                        }
                    )
                completed_order_count += 1
                _publish_status(
                    f"Finished Rush Order Goods order {order_id} ({completed_order_count}/{total_scanned_count} done).",
                    stage="finished_order",
                    current=completed_order_count,
                    total=total_scanned_count,
                    order_id=order_id,
                )
                if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                    break
            if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                break
            print("Finished Rush Order Goods list pass; reopening the list to look for more eligible orders...")
    finally:
        safe_driver_quit(driver, profile_path=resolved_profile_path)

    success = all(bool(item.get("success")) for item in report_items) if report_items else True
    skipped_historical_count = len(historical_order_id_set)
    message = _summary_message(
        report_items,
        refresh_passes=refresh_passes,
        order_count=len(attempted_order_ids),
        row_description=row_options.get("allowed_row_description"),
    )
    if skipped_historical_count:
        message = (
            f"{message} Skipped {skipped_historical_count} previously logged Rush Order Goods order(s); "
            "clear Stock Tools history to make them eligible again."
        )
    return {
        "action": "order_goods_batch",
        "success": success,
        "message": message,
        "order_count": len(attempted_order_ids),
        "order_ids": attempted_order_ids,
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(headless_mode),
        "shipping_filter": RUSH_FILTER,
        "list_url": target_url,
        "batch_size": requested_batch_size,
        "parallel_workers": worker_limit,
        "refresh_passes": refresh_passes,
        "duration_seconds": _elapsed_seconds(batch_started_at),
        "manual_review_required": any(bool(item.get("manual_review_required")) for item in report_items),
        "resolution": "batch" if attempted_order_ids else "no_orders",
        "skipped_historical_order_count": skipped_historical_count,
    }


def _run_batch(dry_run=False, batch_size=None, parallel_workers=1, profile_path=None, list_url=None, visible=False):
    batch_started_at = time.monotonic()
    modes = [False] if visible else _crm_attempt_modes()
    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        try:
            payload = _run_batch_with_mode(
                headless_mode,
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
                "action": "order_goods_batch",
                "success": False,
                "message": str(exc),
                "order_count": 0,
                "order_ids": [],
                "report": [],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": RUSH_FILTER,
                "manual_review_required": True,
                "retryable": _is_retryable_exception(exc),
                "error_type": type(exc).__name__,
                "duration_seconds": _elapsed_seconds(batch_started_at),
            }
            if not headless_mode or index == len(modes) or not _is_retryable_exception(exc):
                break
            print("Headless CRM Order Goods failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
    return last_payload or {
        "action": "order_goods_batch",
        "success": False,
        "message": "CRM Order Goods did not produce a result.",
        "order_count": 0,
        "order_ids": [],
        "report": [],
        "dry_run": bool(dry_run),
        "shipping_filter": RUSH_FILTER,
        "duration_seconds": _elapsed_seconds(batch_started_at),
        "manual_review_required": True,
    }


def _run_single_with_mode(headless_mode, order_id, dry_run=False, profile_path=None):
    started_at = time.monotonic()
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or CRM order URL.")
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    _publish_status(
        f"Loading CRM session for Rush Order Goods order {normalized_order_id}.",
        stage="loading_crm",
        current=0,
        total=1,
        order_id=normalized_order_id,
    )
    driver = _build_crm_session_driver(
        resolved_profile_path,
        headless_mode=headless_mode,
        profile_label=f"CRM order goods single {normalized_order_id}",
    )
    report_items = []
    try:
        report_items = _run_order_with_driver(driver, normalized_order_id, dry_run=dry_run)
        _publish_status(
            f"Finished Rush Order Goods order {normalized_order_id} (1/1 done).",
            stage="finished_order",
            current=1,
            total=1,
            order_id=normalized_order_id,
        )
    finally:
        safe_driver_quit(driver, profile_path=resolved_profile_path)

    success = all(bool(item.get("success")) for item in report_items) if report_items else True
    return {
        "action": "order_goods_single",
        "success": success,
        "message": _summary_message(report_items, refresh_passes=1, order_count=1),
        "target_order_id": normalized_order_id,
        "order_count": 1,
        "order_ids": [normalized_order_id],
        "report": report_items,
        "dry_run": bool(dry_run),
        "headless": bool(headless_mode),
        "shipping_filter": RUSH_FILTER,
        "batch_size": 1,
        "parallel_workers": 1,
        "refresh_passes": 1,
        "duration_seconds": _elapsed_seconds(started_at),
        "manual_review_required": any(bool(item.get("manual_review_required")) for item in report_items),
        "resolution": "single",
    }


def _run_single(order_id, dry_run=False, profile_path=None, visible=False):
    started_at = time.monotonic()
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        return {
            "action": "order_goods_single",
            "success": False,
            "message": "Order ID must be a 7-digit value or CRM order URL.",
            "target_order_id": None,
            "order_count": 0,
            "order_ids": [],
            "report": [],
            "dry_run": bool(dry_run),
            "shipping_filter": RUSH_FILTER,
            "duration_seconds": _elapsed_seconds(started_at),
            "manual_review_required": True,
            "resolution": "invalid_order_id",
        }

    modes = [False] if visible else _crm_attempt_modes()
    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        try:
            payload = _run_single_with_mode(
                headless_mode,
                normalized_order_id,
                dry_run=dry_run,
                profile_path=profile_path,
            )
            payload["headless"] = bool(headless_mode)
            return payload
        except Exception as exc:
            last_payload = {
                "action": "order_goods_single",
                "success": False,
                "message": str(exc),
                "target_order_id": normalized_order_id,
                "order_count": 1,
                "order_ids": [normalized_order_id],
                "report": [
                    {
                        "order_id": normalized_order_id,
                        "success": False,
                        "outcome": "worker_exception",
                        "message": str(exc),
                        "manual_review_required": True,
                        "retryable": _is_retryable_exception(exc),
                        "error_type": type(exc).__name__,
                        "duration_seconds": _elapsed_seconds(started_at),
                    }
                ],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": RUSH_FILTER,
                "duration_seconds": _elapsed_seconds(started_at),
                "manual_review_required": True,
                "resolution": "single",
            }
            if not headless_mode or index == len(modes) or not _is_retryable_exception(exc):
                break
            print("Headless CRM Order Goods single run failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
    return last_payload or {
        "action": "order_goods_single",
        "success": False,
        "message": "CRM Order Goods single run did not produce a result.",
        "target_order_id": normalized_order_id,
        "order_count": 1,
        "order_ids": [normalized_order_id],
        "report": [],
        "dry_run": bool(dry_run),
        "shipping_filter": RUSH_FILTER,
        "duration_seconds": _elapsed_seconds(started_at),
        "manual_review_required": True,
        "resolution": "single",
    }


def run(action="order_goods_batch", dry_run=False, batch_size=None, parallel_workers=1, profile_path=None, result_file=None, list_url=None, visible=False, order_id=None):
    if action not in {"order_goods_batch", "order_goods_single"}:
        raise RuntimeError("Unsupported CRM Order Goods action.")
    if action == "order_goods_single" or order_id:
        payload = _run_single(
            order_id,
            dry_run=dry_run,
            profile_path=profile_path,
            visible=visible,
        )
    else:
        payload = _run_batch(
            dry_run=dry_run,
            batch_size=batch_size,
            parallel_workers=parallel_workers,
            profile_path=profile_path,
            list_url=list_url,
            visible=visible,
        )
    write_result_payload(
        AUTOMATION_NAME,
        "crm_order_goods.py",
        bool(payload.get("success")),
        payload.get("message") or "CRM Order Goods completed.",
        extra_fields=payload,
        result_file=result_file,
    )
    return 0 if payload.get("success") else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Click Sanmar / S&S Activewear order goods for eligible Rush CRM orders.")
    parser.add_argument("--action", choices=["order_goods_batch", "order_goods_single"], default="order_goods_batch")
    parser.add_argument("--order-id", required=False, help="Optional single 7-digit CRM order ID or CRM order URL.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Process up to this many orders. Leave blank or use 0 through the server endpoint to run until no eligible orders remain.",
    )
    parser.add_argument("--profile-path", required=False, help="Optional Chrome user-data-dir override.")
    parser.add_argument("--result-file", required=False, help="Optional path for the JSON result payload.")
    parser.add_argument("--list-url", required=False, help="Optional Rush CRM report URL override.")
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help="Run up to this many isolated Rush Order Goods workers at the same time.",
    )
    parser.add_argument("--visible", action="store_true", help="Run Chrome visibly instead of headless for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Find the Sanmar / S&S button without clicking it.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    sys.exit(
        run(
            action=options.action,
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
