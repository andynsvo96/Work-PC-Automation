"""
CRM Shipping Cost Bypass automation worker.

This worker handles orders where CRM stock is available but the normal stock
order button is blocked by shipping-cost rules. It manually places a SanMar
order, then records the same PO in CRM under Manual Order.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from automation_runtime import (
    SCRIPT_DIR,
    build_chrome_driver,
    configure_console_utf8,
    kill_stale_chrome,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
from config import (
    CRM_ACTION_TIMEOUT,
    CRM_PAGE_LOAD_TIMEOUT,
    CRM_PROFILE_DIR,
    CRM_SHIPPING_BYPASS_URL,
    SANMAR_CART_URL,
    SANMAR_PROFILE_DIR,
    SANMAR_URL,
)
from crm_validate_address import (
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
from crm_order_goods import _wait_for_order_goods_page_ready

configure_console_utf8()

AUTOMATION_NAME = "crm.shipping_bypasser"
PROFILE_PATH = os.path.join(SCRIPT_DIR, CRM_PROFILE_DIR)
SANMAR_PROFILE_PATH = os.path.join(SCRIPT_DIR, SANMAR_PROFILE_DIR)
RUSH_FILTER = "rush"
CONTINUOUS_ORDER_FETCH_LIMIT = 25
CRM_STATE_PATH = os.path.join(SCRIPT_DIR, "crm_state.json")
WAREHOUSE_DISTANCE = {
    "inhouse": [
        "Robbinsville, NJ",
        "Richmond, VA",
        "Cincinnati, OH",
        "Jacksonville, FL",
        "Minneapolis, MN",
        "Dallas, TX",
        "Phoenix, AZ",
        "Reno, NV",
        "Seattle, WA",
    ],
    "mach6": [
        "Phoenix, AZ",
        "Reno, NV",
        "Seattle, WA",
        "Dallas, TX",
        "Minneapolis, MN",
        "Cincinnati, OH",
        "Jacksonville, FL",
        "Richmond, VA",
        "Robbinsville, NJ",
    ],
}
WAREHOUSE_ALIASES = {
    "robbinsville": "Robbinsville, NJ",
    "richmond": "Richmond, VA",
    "cincinnati": "Cincinnati, OH",
    "jacksonville": "Jacksonville, FL",
    "minneapolis": "Minneapolis, MN",
    "dallas": "Dallas, TX",
    "phoenix": "Phoenix, AZ",
    "reno": "Reno, NV",
    "seattle": "Seattle, WA",
}
SIZE_TOKENS = {
    "XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL",
    "YXS", "YS", "YM", "YL", "YXL", "ONE SIZE", "ONESIZE", "OSFA",
}


def _elapsed_seconds(started_at):
    return round(max(0.0, time.monotonic() - started_at), 1)


def _validate_runtime_config(list_url=None):
    target_url = str(list_url or CRM_SHIPPING_BYPASS_URL or "").strip()
    if not target_url:
        raise RuntimeError("CRM_SHIPPING_BYPASS_URL is empty in config.py.")
    lowered_url = target_url.lower()
    if "shipping+is+too+expensive" not in lowered_url and "shipping is too expensive" not in lowered_url:
        raise RuntimeError("Shipping Bypasser list URL must target Sales Notes = Shipping is too expensive.")
    if "tabs%5bhigh%5d=1" not in lowered_url and "tabs[high]=1" not in lowered_url:
        raise RuntimeError("Shipping Bypasser list URL must be limited to single-tab orders.")
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


def _load_historical_shipping_bypass_order_ids(state_path=CRM_STATE_PATH):
    try:
        with open(state_path, "r", encoding="utf-8-sig") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return set()
    except Exception as exc:
        print(f"Warning: could not read previous Shipping Bypasser history for skip list: {exc}")
        return set()
    history = state.get("run_history") if isinstance(state, dict) else []
    skipped = set()
    for entry in history if isinstance(history, list) else []:
        if not isinstance(entry, dict):
            continue
        automation_key = str(entry.get("automation_key") or "").strip().lower()
        automation_label = str(entry.get("automation_label") or "").strip().lower()
        if automation_key != "shipping_bypasser" and "shipping bypass" not in automation_label:
            continue
        skipped.update(_normalize_order_ids(entry.get("order_ids")))
    return skipped


def _normalize_text(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _upper_key(value):
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _parse_crm_date(value):
    text = _normalize_text(value)
    for pattern in (r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"):
        match = re.search(pattern, text)
        if not match:
            continue
        try:
            if pattern.startswith(r"\b(\d{4})"):
                year, month, day = [int(part) for part in match.groups()]
            else:
                month, day, year = [int(part) for part in match.groups()]
                if year < 100:
                    year += 2000
            return datetime(year, month, day).date()
        except ValueError:
            continue
    return None


def _format_date_for_crm(date_value):
    return date_value.strftime("%Y-%m-%d")


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat") and value.__class__.__name__ == "date":
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _result(order_id, success, outcome, message, **extra):
    payload = {
        "order_id": order_id,
        "success": bool(success),
        "outcome": outcome,
        "message": str(message),
        "manual_review_required": not bool(success),
    }
    payload.update(_json_safe(extra))
    return payload


CRM_ORDER_DATA_SCRIPT = r"""
const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
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
function ownText(node) {
  return normalize(Array.from(node.childNodes || [])
    .filter((child) => child.nodeType === Node.TEXT_NODE)
    .map((child) => child.textContent)
    .join(' '));
}
const bodyText = normalize(document.body && (document.body.innerText || document.body.textContent));
const all = Array.from(document.querySelectorAll('body *')).filter(isVisible).map((node) => {
  const rect = node.getBoundingClientRect();
  return {
    text: normalize(node.innerText || node.textContent || node.value || ''),
    own: ownText(node),
    tag: String(node.tagName || '').toLowerCase(),
    x: rect.left || 0,
    y: rect.top || 0,
    w: rect.width || 0,
    h: rect.height || 0,
  };
});
const pieces = all
  .map((item) => ({...item, text: item.own || item.text}))
  .filter((item) => item.text && item.text.length <= 160);
const heading = pieces.find((item) => /items on this design/i.test(item.text));
const headingY = heading ? heading.y : 0;
const after = pieces.filter((item) => item.y >= headingY - 20);
function findLabel(label) {
  const rx = new RegExp('^' + label + ':?$', 'i');
  return after.filter((item) => rx.test(item.text)).sort((a, b) => a.y - b.y || a.x - b.x)[0] || null;
}
function extractItemBlock(stockLine, blockEndY) {
  const block = after.filter((item) => item.y >= stockLine.y - 5 && item.y < blockEndY);
  const sizeLabel = block.find((item) => /^Size:?$/i.test(item.text) && item.y >= stockLine.y)
    || block.find((item) => /^Size:?$/i.test(item.text));
  const qtyLabel = block.find((item) => /^Quantity:?$/i.test(item.text) && item.y >= (sizeLabel ? sizeLabel.y : stockLine.y))
    || block.find((item) => /^Quantity:?$/i.test(item.text));
  const priceLabel = block.find((item) => /^Price:?$/i.test(item.text) && item.y >= (qtyLabel ? qtyLabel.y : stockLine.y))
    || block.find((item) => /^Price:?$/i.test(item.text));
  let sizes = [];
  let quantities = {};
  if (sizeLabel && qtyLabel) {
    const sizeRow = block.filter((item) =>
      Math.abs(item.y - sizeLabel.y) < 18 && item.x > sizeLabel.x + 20 && /^[A-Z0-9/ -]{1,12}$/.test(item.text)
    ).sort((a, b) => a.x - b.x);
    sizes = sizeRow.map((item) => ({ size: item.text.toUpperCase().replace(/\s+/g, ' '), x: item.x + item.w / 2 }));
    const qtyFloor = qtyLabel.y - 16;
    const qtyCeiling = priceLabel ? priceLabel.y - 4 : qtyLabel.y + 40;
    const qtyCells = block.filter((item) =>
      item.x > qtyLabel.x + 20 && item.y >= qtyFloor && item.y <= qtyCeiling && /^\d+$/.test(item.text)
    ).sort((a, b) => a.x - b.x);
    for (const cell of qtyCells) {
      if (!sizes.length) continue;
      const cx = cell.x + cell.w / 2;
      let best = sizes[0];
      let bestDistance = Math.abs(cx - best.x);
      for (const size of sizes) {
        const distance = Math.abs(cx - size.x);
        if (distance < bestDistance) {
          best = size;
          bestDistance = distance;
        }
      }
      if (best && bestDistance < 45) quantities[best.size] = Number(cell.text);
    }
  }
  let color = '';
  const total = after.find((item) => /total quantity/i.test(item.text) && Math.abs(item.y - stockLine.y) < 80);
  const colorCandidates = block.filter((item) => {
    if (item.y < stockLine.y - 20 || item.y > stockLine.y + 80) return false;
    if (item.x <= stockLine.x + 50) return false;
    if (/total quantity|quantity|subtotal|ink colors|impressions/i.test(item.text)) return false;
    if (total && item.x >= total.x - 20) return false;
    return /^[A-Z][A-Za-z0-9 /-]{1,40}$/.test(item.text);
  }).sort((a, b) => a.y - b.y || a.x - b.x);
  color = colorCandidates.length ? colorCandidates[0].text : '';
  if (!color) {
    const richLine = after.find((item) =>
      Math.abs(item.y - stockLine.y) < 40
      && item.text.includes(stockLine.text)
      && /Alpha Stock\s+(.+?)\s+Total Quantity/i.test(item.text)
    );
    const richMatch = richLine ? richLine.text.match(/Alpha Stock\s+(.+?)\s+Total Quantity/i) : null;
    if (richMatch) color = normalize(richMatch[1]);
  }
  return {
    stockLine: stockLine.text,
    color,
    quantities,
    sizes: sizes.map((item) => item.size),
  };
}
const rawStockLines = after.filter((item) => /\b[A-Z0-9]{2,}\b.*-\s*Alpha Stock/i.test(item.text) || /\b[A-Z0-9]{2,}\b.*Alpha Stock/i.test(item.text))
  .filter((item) => !/^Items on this design\b/i.test(item.text))
  .filter((item) => !/\b(Size|Quantity|Price):/i.test(item.text))
  .sort((a, b) => a.y - b.y || a.x - b.x);
const stockLines = [];
for (const line of rawStockLines) {
  const duplicate = stockLines.find((existing) =>
    existing.text === line.text && Math.abs(existing.y - line.y) < 28
  );
  if (!duplicate) stockLines.push(line);
}
const items = [];
for (let i = 0; i < stockLines.length; i += 1) {
  const next = stockLines[i + 1];
  items.push(extractItemBlock(stockLines[i], next ? next.y - 8 : Number.POSITIVE_INFINITY));
}
const firstItem = items[0] || { stockLine: '', color: '', quantities: {}, sizes: [] };
const activeTab = pieces.find((item) => /Design Previews/i.test(item.text) && /\bQTY\s*:/i.test(item.text));
return {
  bodyText,
  stockLine: firstItem.stockLine,
  color: firstItem.color,
  quantities: firstItem.quantities,
  sizes: firstItem.sizes,
  items,
  activeTabText: activeTab ? activeTab.text : '',
};
"""


def _extract_order_data(driver, order_id):
    data = driver.execute_script(CRM_ORDER_DATA_SCRIPT)
    if not isinstance(data, dict):
        data = {}
    body_text = _normalize_text(data.get("bodyText"))
    due_match = re.search(r"Due Date:\s*([0-9/-]+)", body_text, flags=re.I)
    prod_match = re.search(r"Production Date:\s*([0-9/-]+)", body_text, flags=re.I)
    due_date = _parse_crm_date(due_match.group(1) if due_match else "")
    production_date = _parse_crm_date(prod_match.group(1) if prod_match else "")
    tab_text = _normalize_text(data.get("activeTabText"))
    po_match = re.search(r"\b(H-[A-Za-z0-9]+)\b", tab_text, flags=re.I) or re.search(r"\b(H-[A-Za-z0-9]+)\b", body_text, flags=re.I)
    raw_items = data.get("items") if isinstance(data.get("items"), list) else []
    if not raw_items:
        raw_items = [
            {
                "stockLine": data.get("stockLine"),
                "color": data.get("color"),
                "quantities": data.get("quantities"),
                "sizes": data.get("sizes"),
            }
        ]
    products = []
    seen_products = set()
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            continue
        stock_line = _normalize_text(raw_item.get("stockLine"))
        product_match = re.search(r"\b([A-Z0-9]{2,})\b\s+(.*?)(?:\s*-\s*Alpha Stock|$)", stock_line, flags=re.I)
        product_id = product_match.group(1).upper() if product_match else ""
        if not product_id or product_id == "ITEMS":
            continue
        product_name = _normalize_text(product_match.group(2) if product_match else stock_line)
        quantities = {
            str(size).upper().replace(" ", ""): int(qty)
            for size, qty in (raw_item.get("quantities") or {}).items()
            if int(qty or 0) > 0
        }
        dedupe_key = (product_id, _normalize_text(raw_item.get("color")).upper(), tuple(sorted(quantities.items())))
        if dedupe_key in seen_products:
            continue
        seen_products.add(dedupe_key)
        products.append(
            {
                "index": index,
                "stock_line": stock_line,
                "product_id": product_id,
                "product_name": product_name,
                "is_a4": bool(re.search(r"\bA4\b", product_name, flags=re.I) or product_id.upper().startswith("N")),
                "color": _normalize_text(raw_item.get("color")),
                "quantities": quantities,
            }
        )
    subcontractor_match = re.search(r"Subcontractor:\s*([^|]+?)(?:\s*Preferred|\s*$)", body_text, flags=re.I)
    subcontractor = _normalize_text(subcontractor_match.group(1) if subcontractor_match else "")
    order_type = "mach6" if "mach 6" in subcontractor.lower() else "inhouse"
    first_product = products[0] if products else {}
    return {
        "order_id": order_id,
        "due_date": due_date,
        "production_date": production_date,
        "po": po_match.group(1) if po_match else "",
        "product_id": first_product.get("product_id", ""),
        "product_name": first_product.get("product_name", ""),
        "is_a4": bool(first_product.get("is_a4")),
        "color": first_product.get("color", ""),
        "quantities": first_product.get("quantities", {}),
        "products": products,
        "subcontractor": subcontractor,
        "order_type": order_type,
    }


def _build_sanmar_driver(visible=False):
    resolved = os.path.abspath(SANMAR_PROFILE_PATH)
    if not visible:
        kill_stale_chrome(resolved, profile_label="SanMar")
    driver = build_chrome_driver(
        resolved,
        headless_mode=not bool(visible),
        page_load_timeout=CRM_PAGE_LOAD_TIMEOUT,
        script_timeout=max(CRM_ACTION_TIMEOUT, 20),
    )
    try:
        setattr(driver, "_shipping_bypasser_visible", bool(visible))
    except Exception:
        pass
    return driver


def _open_visible_sanmar_cart():
    profile = os.path.abspath(SANMAR_PROFILE_PATH)
    chrome = shutil.which("chrome") or shutil.which("chrome.exe")
    args = []
    if chrome:
        args = [chrome, f"--user-data-dir={profile}", SANMAR_CART_URL]
    else:
        args = ["cmd", "/c", "start", "", SANMAR_CART_URL]
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _wait_for_text(driver, pattern, timeout=15):
    deadline = time.time() + timeout
    rx = re.compile(pattern, flags=re.I)
    while time.time() < deadline:
        try:
            text = driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');")
        except Exception:
            text = ""
        if rx.search(text):
            return text
        time.sleep(0.3)
    return ""


def _ensure_sanmar_logged_in(driver):
    safe_get_with_partial_load(driver, SANMAR_URL, label="SanMar home")
    text = _wait_for_text(driver, r"(Welcome,\s*EZONLINE1|Log In|Shopping Box)", timeout=15)
    if re.search(r"Welcome,\s*EZONLINE1", text, flags=re.I):
        return True
    login = _find_clickable_by_text(driver, r"^Log In$")
    if login is not None:
        _click_with_fallback(driver, login)
        time.sleep(1.0)
    button = _find_clickable_by_text(driver, r"^Log In$")
    if button is not None:
        _click_with_fallback(driver, button)
    text = _wait_for_text(driver, r"Welcome,\s*EZONLINE1", timeout=20)
    if not re.search(r"Welcome,\s*EZONLINE1", text, flags=re.I) and bool(getattr(driver, "_shipping_bypasser_visible", False)):
        print("SanMar login is visible. Sign in as EZONLINE1 in the Chrome window; the worker will continue after login is confirmed.")
        text = _wait_for_text(driver, r"Welcome,\s*EZONLINE1", timeout=180)
    if not re.search(r"Welcome,\s*EZONLINE1", text, flags=re.I):
        if not bool(getattr(driver, "_shipping_bypasser_visible", False)):
            _open_visible_sanmar_cart()
        raise RuntimeError("SanMar login was not confirmed as EZONLINE1. Opened SanMar visibly if possible; sign in and rerun Shipping Bypasser.")
    return True


def _find_clickable_by_text(driver, pattern):
    script = r"""
const pattern = new RegExp(arguments[0], 'i');
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
const controls = Array.from(document.querySelectorAll('button,a,input[type="button"],input[type="submit"],[role="button"],.btn,[class*="btn"]')).filter(isVisible);
return controls.find((node) => pattern.test(normalize(node.innerText || node.textContent || node.value || node.getAttribute('aria-label')))) || null;
"""
    try:
        element = driver.execute_script(script, pattern)
        if element is not None:
            return element
    except Exception:
        pass
    return None


def _sanmar_cart_has_items(driver):
    script = r"""
const text = String(document.body && (document.body.innerText || document.body.textContent) || '');
const normalized = text.replace(/\s+/g, ' ').trim();
const checkout = /\bCheckout\b/i.test(normalized);
const amount = /\bShopping Box\b\s*\$?\s*(?!0\.00)(\d+|\d+\.\d{2})/i.test(normalized);
const badge = Array.from(document.querySelectorAll('body *')).some((node) => {
  const t = String(node.innerText || node.textContent || '').trim();
  if (!/^\d+$/.test(t) || Number(t) <= 0) return false;
  const rect = node.getBoundingClientRect();
  return rect.top < 160 && rect.left > (window.innerWidth || 1200) * 0.45;
});
return { hasItems: Boolean(checkout && (amount || badge)), text: normalized.slice(0, 1000) };
"""
    state = driver.execute_script(script)
    return state if isinstance(state, dict) else {"hasItems": False}


def _search_sanmar_product(driver, search_id):
    input_el = None
    deadline = time.time() + 12
    while time.time() < deadline and input_el is None:
        try:
            inputs = driver.find_elements(By.CSS_SELECTOR, "input")
            for item in inputs:
                placeholder = str(item.get_attribute("placeholder") or "")
                if item.is_displayed() and ("product" in placeholder.lower() or "style" in placeholder.lower() or "pms" in placeholder.lower()):
                    input_el = item
                    break
        except Exception:
            pass
        if input_el is None:
            time.sleep(0.2)
    if input_el is None:
        raise RuntimeError("SanMar search input was not found.")
    input_el.send_keys(Keys.CONTROL, "a")
    input_el.send_keys(search_id)
    input_el.send_keys(Keys.ENTER)
    deadline = time.time() + 20
    style_key = _upper_key(search_id)
    while time.time() < deadline:
        try:
            text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
            url = str(driver.current_url or "")
        except Exception:
            text = ""
            url = ""
        product_page = bool(re.search(r"Inventory\s+and\s+Pricing|Color\s+selected|Add\s+to\s+shopping\s+box", text, flags=re.I))
        cart_page = bool(re.search(r"My\s+Shopping\s+Box|Shopping\s+Details|Continue\s+Checkout", text, flags=re.I))
        if style_key in _upper_key(text) and product_page and not (cart_page and not product_page):
            return True
        if style_key in _upper_key(url) and product_page:
            return True
        time.sleep(0.3)
    raise RuntimeError(f"SanMar product search did not open the inventory page for {search_id}.")


def _select_sanmar_color(driver, color):
    wanted = _upper_key(color)
    if not wanted:
        raise RuntimeError("CRM stock color was not detected.")
    script = r"""
const wanted = arguments[0];
const key = (value) => String(value || '').toUpperCase().replace(/[^A-Z0-9]+/g, '');
function isMatch(value) {
  const actual = key(value);
  if (!actual) return false;
  if (actual === wanted) return true;
  if (wanted === 'NAVY' && actual.endsWith('NAVY')) return true;
  if (wanted === 'ROYAL' && actual.endsWith('ROYAL')) return true;
  return false;
}
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
const nodes = Array.from(document.querySelectorAll('button,a,label,span,div,input')).filter(isVisible);
let best = null;
for (const node of nodes) {
  const text = node.getAttribute('title') || node.getAttribute('aria-label') || node.value || node.innerText || node.textContent || '';
  if (!isMatch(text)) continue;
  const rect = node.getBoundingClientRect();
  const clickable = node.closest('button,a,label') || node;
  const tag = String(clickable.tagName || '').toLowerCase();
  const exact = key(text) === wanted ? 0 : 100000;
  const tagScore = tag === 'a' || tag === 'button' || tag === 'label' ? 0 : 10000;
  const area = Math.max(1, Math.round((rect.width || 1) * (rect.height || 1)));
  const score = exact + tagScore + area + Math.round(rect.top || 0) + String(text).length;
  if (!best || score < best.score) best = { node: clickable, score };
}
return best ? best.node : null;
"""
    node = None
    deadline = time.time() + 12
    while time.time() < deadline and node is None:
        node = driver.execute_script(script, wanted)
        if node is None:
            time.sleep(0.3)
    if node is None:
        raise RuntimeError(f"SanMar color '{color}' was not found.")
    _click_with_fallback(driver, node)
    selected_pattern = re.escape(_normalize_text(color))
    text = _wait_for_text(driver, rf"Color\s+selected:\s*{selected_pattern}|Color\s+selected:.*{selected_pattern}", timeout=8)
    if not text and wanted in {"NAVY", "ROYAL"}:
        text = _wait_for_text(driver, rf"Color\s+selected:.*{wanted.title()}", timeout=3)
    time.sleep(0.7)


def _sanmar_inventory(driver):
    script = r"""
const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
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
const warehousePattern = /(Robbinsville,\s*NJ|Richmond,\s*VA|Cincinnati,\s*OH|Jacksonville,\s*FL|Minneapolis,\s*MN|Dallas,\s*TX|Phoenix,\s*AZ|Reno,\s*NV|Seattle,\s*WA)/i;
const sizePattern = /^(XS|S|M|L|XL|[2-6]XL|YXS|YS|YM|YL|YXL|ONE SIZE|OSFA)$/;
const warehouseRows = [];
for (const tr of Array.from(document.querySelectorAll('tr')).filter(isVisible)) {
  const text = normalize(tr.innerText || tr.textContent);
  const match = text.match(warehousePattern);
  if (!match) continue;
  const rect = tr.getBoundingClientRect();
  warehouseRows.push({
    warehouse: normalize(match[1]).replace(/\s*,\s*/, ', '),
    y: rect.top + rect.height / 2,
    stock: {},
  });
}
function nearestWarehouse(y) {
  let best = null;
  let bestDistance = Infinity;
  for (const row of warehouseRows) {
    const distance = Math.abs(row.y - y);
    if (distance < bestDistance) {
      best = row;
      bestDistance = distance;
    }
  }
  return bestDistance <= 12 ? best : null;
}
for (const table of Array.from(document.querySelectorAll('table')).filter(isVisible)) {
  if (!table.querySelector('input:not([type="hidden"])')) continue;
  const headerCells = Array.from(table.querySelectorAll('thead th, tr.headings td, tr.headings th, th.size-header, td.size-header'))
    .filter(isVisible)
    .map((cell) => normalize(cell.innerText || cell.textContent).toUpperCase().replace(/\s+/g, ''));
  const header = headerCells.filter((text) => sizePattern.test(text));
  if (!header.length) continue;
  for (const tr of Array.from(table.querySelectorAll('tbody tr')).filter(isVisible)) {
    const inputs = Array.from(tr.querySelectorAll('input:not([type="hidden"])')).filter(isVisible);
    if (!inputs.length) continue;
    const rect = tr.getBoundingClientRect();
    const warehouse = nearestWarehouse(rect.top + rect.height / 2);
    if (!warehouse) continue;
    const cells = Array.from(tr.children || []).filter(isVisible);
    for (let i = 0; i < cells.length && i < header.length; i += 1) {
      const size = header[i];
      if (!sizePattern.test(size)) continue;
      const raw = normalize(cells[i].innerText || cells[i].textContent).replace(/,/g, '');
      const numberMatch = raw.match(/\d+/);
      warehouse.stock[size] = numberMatch ? Number(numberMatch[0]) : 0;
    }
  }
}
return warehouseRows.map((row) => ({ warehouse: row.warehouse, stock: row.stock }));
"""
    rows = driver.execute_script(script)
    return rows if isinstance(rows, list) else []


def _choose_warehouse(inventory, required, order_type):
    order = WAREHOUSE_DISTANCE.get(order_type) or WAREHOUSE_DISTANCE["inhouse"]
    by_name = {}
    for row in inventory:
        if not isinstance(row, dict):
            continue
        raw_name = _normalize_text(row.get("warehouse"))
        canonical = None
        for key, alias in WAREHOUSE_ALIASES.items():
            if key in raw_name.lower():
                canonical = alias
                break
        if canonical:
            by_name[canonical] = row
    for warehouse in order:
        row = by_name.get(warehouse)
        if not row:
            continue
        stock = row.get("stock") if isinstance(row.get("stock"), dict) else {}
        if all(int(stock.get(size, 0) or 0) >= int(qty) for size, qty in required.items()):
            return warehouse
    return None


def _inventory_by_warehouse(inventory):
    by_name = {}
    for row in inventory if isinstance(inventory, list) else []:
        if not isinstance(row, dict):
            continue
        raw_name = _normalize_text(row.get("warehouse"))
        canonical = None
        for key, alias in WAREHOUSE_ALIASES.items():
            if key in raw_name.lower():
                canonical = alias
                break
        if canonical:
            by_name[canonical] = row
    return by_name


def _choose_common_warehouse(product_lines, order_type):
    order = WAREHOUSE_DISTANCE.get(order_type) or WAREHOUSE_DISTANCE["inhouse"]
    lines = product_lines if isinstance(product_lines, list) else []
    for warehouse in order:
        can_fulfill = True
        for line in lines:
            inventory = _inventory_by_warehouse(line.get("inventory"))
            row = inventory.get(warehouse)
            if not row:
                can_fulfill = False
                break
            stock = row.get("stock") if isinstance(row.get("stock"), dict) else {}
            required = line.get("quantities") if isinstance(line.get("quantities"), dict) else {}
            if not all(int(stock.get(size, 0) or 0) >= int(qty) for size, qty in required.items()):
                can_fulfill = False
                break
        if can_fulfill:
            return warehouse
    return None


def _sanmar_search_id_for_product(product):
    search_id = str((product or {}).get("product_id") or "").strip().upper()
    if (product or {}).get("is_a4"):
        return f"a4{search_id}"
    return search_id


def _fill_sanmar_quantities(driver, warehouse, required):
    script = r"""
const warehouse = arguments[0];
const required = arguments[1] || {};
const normalize = (value) => String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
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
function dispatch(node, name) {
  node.dispatchEvent(new Event(name, { bubbles: true }));
}
const sizePattern = /^(XS|S|M|L|XL|[2-6]XL|YXS|YS|YM|YL|YXL|ONE SIZE|OSFA)$/;
let targetY = null;
for (const tr of Array.from(document.querySelectorAll('tr')).filter(isVisible)) {
  const rowText = normalize(tr.innerText || tr.textContent).toLowerCase();
  if (!rowText.includes(String(warehouse).toLowerCase())) continue;
  const rect = tr.getBoundingClientRect();
  targetY = rect.top + rect.height / 2;
  break;
}
if (targetY === null) return { success: false, message: `Warehouse row not found: ${warehouse}` };
const filled = [];
for (const table of Array.from(document.querySelectorAll('table')).filter(isVisible)) {
  if (!table.querySelector('input:not([type="hidden"])')) continue;
  const headerCells = Array.from(table.querySelectorAll('thead th, tr.headings td, tr.headings th, th.size-header, td.size-header'))
    .filter(isVisible)
    .map((cell) => normalize(cell.innerText || cell.textContent).toUpperCase().replace(/\s+/g, ''));
  const header = headerCells.filter((text) => sizePattern.test(text));
  if (!header.length) continue;
  for (const tr of Array.from(table.querySelectorAll('tbody tr')).filter(isVisible)) {
    const rect = tr.getBoundingClientRect();
    if (Math.abs((rect.top + rect.height / 2) - targetY) > 12) continue;
    const cells = Array.from(tr.children || []).filter(isVisible);
    for (let i = 0; i < cells.length && i < header.length; i += 1) {
      const size = header[i];
      if (!size || required[size] === undefined) continue;
      const input = Array.from(cells[i].querySelectorAll('input:not([type="hidden"])')).find(isVisible);
    if (!input) return { success: false, message: `Quantity input missing for ${size}` };
    const value = String(required[size]);
    input.focus();
    input.value = value;
    dispatch(input, 'input');
    dispatch(input, 'change');
    dispatch(input, 'blur');
      filled.push(size);
    }
  }
}
const missing = Object.keys(required).filter((size) => !filled.includes(size));
if (missing.length) return { success: false, message: `Quantity input missing for ${missing.join(', ')}` };
return { success: true };
"""
    result = driver.execute_script(script, warehouse, required)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "SanMar quantities could not be filled.")
    time.sleep(0.7)


def _click_sanmar_button(driver, text_pattern, timeout=12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        node = _find_clickable_by_text(driver, text_pattern)
        if node is not None:
            _click_with_fallback(driver, node)
            time.sleep(1.0)
            return True
        time.sleep(0.3)
    raise RuntimeError(f"SanMar button not found: {text_pattern}")


def _add_current_product_to_box(driver):
    _click_sanmar_button(driver, r"Add\s+to\s+shopping\s+box")
    _wait_for_text(driver, r"Shopping\s+Box|Saved\s+Shopping\s+Box|Checkout|Proceed\s+to\s+checkout", timeout=15)
    time.sleep(0.8)


def _select_shipping_destination(driver, order_type, warehouse):
    if order_type == "inhouse" and warehouse == "Robbinsville, NJ":
        _click_radio_near_text(driver, "Pick Up at warehouse")
        _select_dropdown_option_containing(driver, "Robbinsville")
        _click_sanmar_button(driver, r"Proceed\s+To\s+Payment")
        return {"ship_mode": "pickup", "address": "Robbinsville, NJ"}
    _click_radio_near_text(driver, "Ship to an address")
    target = "Mach 6 Manufacturing" if order_type == "mach6" else "123 EZ TEES INC"
    _click_radio_near_text(driver, target)
    _wait_for_text(driver, re.escape(target), timeout=5)
    _click_sanmar_button(driver, r"Confirm\s+Address")
    time.sleep(2.0)
    return {"ship_mode": "ship", "address": target}


def _click_radio_near_text(driver, text):
    script = r"""
const wanted = String(arguments[0] || '').toLowerCase();
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
const radios = Array.from(document.querySelectorAll('input[type="radio"]')).filter(isVisible);
let best = null;
for (const radio of radios) {
  for (let current = radio.parentElement, depth = 0; current && depth < 4; current = current.parentElement, depth += 1) {
    const scopeText = normalize(current.innerText || current.textContent);
    if (!scopeText.toLowerCase().includes(wanted)) continue;
    const rect = radio.getBoundingClientRect();
    const currentRect = current.getBoundingClientRect();
    const area = Math.max(1, (currentRect.width || 1) * (currentRect.height || 1));
    const score = (depth * 100000000) + area + Math.round(rect.top || 0);
    if (!best || score < best.score) best = { radio, score };
    break;
  }
}
return best ? best.radio : null;
"""
    radio = driver.execute_script(script, text)
    if radio is None:
        raise RuntimeError(f"Radio option not found: {text}")
    _click_with_fallback(driver, radio)
    time.sleep(0.5)


def _select_dropdown_option_containing(driver, text):
    script = r"""
const wanted = String(arguments[0] || '').toLowerCase();
const selects = Array.from(document.querySelectorAll('select')).filter((node) => {
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0;
});
for (const select of selects) {
  const option = Array.from(select.options || []).find((item) => String(item.textContent || item.value || '').toLowerCase().includes(wanted));
  if (!option) continue;
  select.value = option.value;
  option.selected = true;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
  return true;
}
return false;
"""
    if driver.execute_script(script, text) is not True:
        raise RuntimeError(f"Dropdown option not found: {text}")
    time.sleep(0.5)


def _select_ups_and_eta(driver, order_type):
    if order_type == "inhouse":
        text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        if "Pick Up at warehouse" in text and "Shipping Method" not in text:
            return None
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
const radios = Array.from(document.querySelectorAll('input[type="radio"]')).filter(isVisible);
for (const radio of radios) {
  let scope = radio.parentElement;
  let text = '';
  for (let depth = 0; scope && depth < 5; scope = scope.parentElement, depth += 1) text += ' ' + normalize(scope.innerText || scope.textContent);
  if (!/\bUPS\b/i.test(text)) continue;
  radio.click();
  radio.dispatchEvent(new Event('change', { bubbles: true }));
  const dateMatch = text.match(/(?:Jan|Feb|Mar|Apr|May|Jun|June|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?|\d{1,2}\/\d{1,2}\/\d{2,4}/i);
  return { success: true, etaText: dateMatch ? dateMatch[0] : '' };
}
return { success: false };
"""
    result = driver.execute_script(script)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError("UPS shipping option was not available.")
    time.sleep(0.5)
    eta = _parse_sanmar_eta(result.get("etaText"))
    if eta is None:
        text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        ups_area = re.search(r"\bUPS\b(.{0,160})", text, flags=re.I)
        eta = _parse_sanmar_eta(ups_area.group(1) if ups_area else text)
    if eta is None:
        raise RuntimeError("UPS estimated delivery date could not be read.")
    return eta


def _parse_sanmar_eta(value):
    text = _normalize_text(value)
    direct = _parse_crm_date(text)
    if direct:
        return direct
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
        "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    match = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,\s*(\d{4}))?\b", text)
    if not match:
        return None
    month = months.get(match.group(1).lower())
    if not month:
        return None
    year = int(match.group(3) or datetime.now().year)
    try:
        return datetime(year, month, int(match.group(2))).date()
    except ValueError:
        return None


def _change_crm_production_date(driver, order_id, target_date):
    edit = _find_clickable_by_text(driver, r"edit\s+order")
    if edit is None:
        raise RuntimeError("CRM edit order button was not found for production date update.")
    _click_with_fallback(driver, edit)
    time.sleep(1.0)
    script = r"""
const target = arguments[0];
const labels = Array.from(document.querySelectorAll('body *')).filter((node) => /Production Date:/i.test(node.innerText || node.textContent || ''));
let input = null;
for (const label of labels) {
  for (let scope = label; scope && scope !== document.body; scope = scope.parentElement) {
    input = Array.from(scope.querySelectorAll('input')).find((node) => {
      const rect = node.getBoundingClientRect();
      return (rect.width || 0) > 0 && (rect.height || 0) > 0;
    });
    if (input) break;
  }
  if (input) break;
}
if (!input) {
  input = Array.from(document.querySelectorAll('input')).find((node) => {
    const rect = node.getBoundingClientRect();
    return (rect.width || 0) > 120 && (rect.height || 0) > 0 && /\d{4}-\d{2}-\d{2}|\d{1,2}\/\d{1,2}\/\d{2,4}/.test(node.value || '');
  });
}
if (!input) return false;
input.focus();
input.value = target;
input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter' }));
input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter' }));
return true;
"""
    if driver.execute_script(script, _format_date_for_crm(target_date)) is not True:
        raise RuntimeError("CRM production date input was not found.")
    time.sleep(3.0)
    save = _find_clickable_by_text(driver, r"save\s+order")
    if save is None:
        raise RuntimeError("CRM save order button was not found after production date update.")
    _click_with_fallback(driver, save)
    _wait_for_text(driver, r"edit\s+order", timeout=25)
    driver.refresh()
    _wait_for_order_goods_page_ready(driver, order_id)
    refreshed = _extract_order_data(driver, order_id)
    if refreshed.get("production_date") != target_date:
        raise RuntimeError("CRM production date did not persist after save.")


def _fill_review_and_submit(driver, po, dry_run=False):
    text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
    if not re.search(r"\bNet\b", text, flags=re.I):
        raise RuntimeError("SanMar payment method NET was not visible on review page.")
    script = r"""
const po = arguments[0];
const labels = Array.from(document.querySelectorAll('body *')).filter((node) => /Customer PO/i.test(node.innerText || node.textContent || ''));
let input = null;
for (const label of labels) {
  for (let scope = label; scope && scope !== document.body; scope = scope.parentElement) {
    input = Array.from(scope.querySelectorAll('input:not([type="hidden"])')).find((node) => {
      const rect = node.getBoundingClientRect();
      return (rect.width || 0) > 0 && (rect.height || 0) > 0;
    });
    if (input) break;
  }
  if (input) break;
}
if (!input) {
  input = Array.from(document.querySelectorAll('input:not([type="hidden"])')).find((node) => {
    const rect = node.getBoundingClientRect();
    return (rect.width || 0) > 160 && (rect.height || 0) > 0;
  });
}
if (!input) return false;
input.focus();
input.value = po;
input.dispatchEvent(new Event('input', { bubbles: true }));
input.dispatchEvent(new Event('change', { bubbles: true }));
return true;
"""
    if driver.execute_script(script, po) is not True:
        raise RuntimeError("Customer PO input was not found on SanMar review page.")
    time.sleep(0.5)
    if dry_run:
        return "dry_run_review_ready"
    _click_sanmar_button(driver, r"Submit\s+Order")
    success_text = _wait_for_text(driver, r"Thank You For Your Order|Web Reference", timeout=30)
    if not success_text:
        raise RuntimeError("SanMar submit did not reach the thank-you page.")
    return "submitted"


def _capture_sanmar_confirmation(driver, order_id, po):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_order_id = re.sub(r"[^0-9A-Za-z_-]+", "_", str(order_id or "order"))
    screenshot_dir = os.path.join(SCRIPT_DIR, "screenshots")
    os.makedirs(screenshot_dir, exist_ok=True)
    screenshot_path = os.path.join(screenshot_dir, f"sanmar_shipping_bypass_{safe_order_id}_{stamp}.png")
    result = {
        "url": str(getattr(driver, "current_url", "") or ""),
        "screenshot": screenshot_path,
        "web_reference": "",
        "po": po,
    }
    try:
        text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        web_reference = re.search(r"Web Reference\s*#?\s*:?\s*([A-Za-z0-9-]+)", text, flags=re.I)
        if web_reference:
            result["web_reference"] = web_reference.group(1)
    except Exception:
        pass
    try:
        driver.save_screenshot(screenshot_path)
    except Exception as exc:
        result["screenshot_error"] = str(exc)
        result["screenshot"] = ""
    return result


def _record_crm_manual_order(driver, order_id, po, dry_run=False):
    if dry_run:
        return "dry_run_manual_order_ready"
    script = r"""
function normalize(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
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
const controls = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"],[ng-click],[onclick],.btn,[class*="btn"]')).filter(isVisible);
let best = null;
for (const control of controls) {
  const text = normalize(control.innerText || control.textContent || control.value || control.getAttribute('aria-label')).toLowerCase();
  if (!text.includes('order goods') && !text.includes('add box')) continue;
  let score = 999999;
  for (let scope = control; scope && scope !== document.body; scope = scope.parentElement) {
    const scopeText = normalize(scope.innerText || scope.textContent).toLowerCase();
    if (scopeText.includes('manual order')) {
      score = normalize(scope.innerText || scope.textContent).length;
      break;
    }
  }
  if (!best || score < best.score) best = { control, score };
}
return best ? best.control : null;
"""
    button = None
    last_text = ""
    for attempt in range(4):
        if attempt == 0:
            try:
                driver.refresh()
            except Exception:
                pass
        else:
            try:
                _open_target_order(driver, order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
            except Exception:
                safe_get_with_partial_load(driver, f"https://crm2.legacy.printfly.com/order/{order_id}", f"CRM order {order_id} manual order retry")
        _wait_for_order_goods_page_ready(driver, order_id, timeout=max(10, CRM_ACTION_TIMEOUT))
        time.sleep(0.8)
        try:
            button = driver.execute_script(script)
        except Exception:
            button = None
        if button is not None:
            break
        try:
            last_text = _normalize_text(driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        except Exception:
            last_text = ""
        time.sleep(1.0)
    if button is None:
        if re.search(re.escape(po), last_text, flags=re.I) and re.search(r"\bSanmar\b", last_text, flags=re.I):
            return "already_recorded"
        raise RuntimeError("CRM Manual Order order goods/add box button was not found after reopening the order.")
    _click_with_fallback(driver, button)
    time.sleep(1.0)
    script = r"""
const po = arguments[0];
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0;
}
function setNativeValue(node, value) {
  const proto = node.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  if (setter) setter.call(node, value);
  else node.value = value;
}
function emit(node, name) {
  node.dispatchEvent(new Event(name, { bubbles: true }));
  if (window.angular) {
    try { window.angular.element(node).triggerHandler(name); } catch (error) {}
  }
}
function digest(node) {
  if (!window.angular) return;
  try {
    const scope = window.angular.element(node).scope() || window.angular.element(node).isolateScope();
    if (scope && !scope.$$phase) scope.$applyAsync ? scope.$applyAsync() : scope.$apply();
  } catch (error) {}
}
const modal = Array.from(document.querySelectorAll('.modal.in .modal-content,.modal[style*="display: block"] .modal-content,.modal-content'))
  .filter(isVisible)
  .find((node) => /Manual Order/i.test(node.innerText || node.textContent || ''))
  || document;
const inputs = Array.from(modal.querySelectorAll('input:not([type="hidden"])')).filter(isVisible);
if (!inputs.length) return { success: false, message: 'PO input not found.' };
inputs[0].focus();
setNativeValue(inputs[0], po);
emit(inputs[0], 'input');
emit(inputs[0], 'change');
digest(inputs[0]);
const selects = Array.from(modal.querySelectorAll('select')).filter(isVisible);
if (selects.length) {
  const option = Array.from(selects[0].options || []).find((item) => /sanmar/i.test(item.textContent || item.value || ''));
  if (!option) return { success: false, message: 'Sanmar vendor option not found.' };
  setNativeValue(selects[0], option.value);
  option.selected = true;
  emit(selects[0], 'input');
  emit(selects[0], 'change');
  digest(selects[0]);
} else {
  const dropdown = Array.from(modal.querySelectorAll('[role="combobox"],[aria-haspopup="listbox"],button,div')).filter(isVisible).find((node) => {
    const text = String(node.innerText || node.textContent || '').toLowerCase();
    return text === '' || text.includes('vendor') || text.includes('select');
  });
  if (dropdown) dropdown.click();
  const option = Array.from(document.querySelectorAll('body *')).filter(isVisible).find((node) => /Sanmar/i.test(node.innerText || node.textContent || ''));
  if (!option) return { success: false, message: 'Sanmar dropdown option not found.' };
  option.click();
}
return { success: true };
"""
    result = driver.execute_script(script, po)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "CRM manual order popup could not be filled.")
    time.sleep(1.0)
    script = r"""
function isVisible(node) {
  if (!node) return false;
  const rect = node.getBoundingClientRect();
  return (rect.width || 0) > 0 && (rect.height || 0) > 0;
}
const modal = Array.from(document.querySelectorAll('.modal.in,.modal[style*="display: block"],.modal-content')).filter(isVisible)[0] || document;
let save = Array.from(modal.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]')).filter(isVisible).find((node) => {
  const text = String(node.innerText || node.textContent || node.value || '').replace(/\s+/g, ' ').trim();
  return /^save$/i.test(text);
});
if (!save) {
  save = Array.from(document.querySelectorAll('button,input[type="button"],input[type="submit"],a,[role="button"]')).filter(isVisible).find((node) => {
    const text = String(node.innerText || node.textContent || node.value || '').replace(/\s+/g, ' ').trim();
    return /^save$/i.test(text);
  });
}
if (!save) return { success: false, message: 'Manual order save button not found.' };
if (save.disabled || save.getAttribute('disabled') !== null || save.getAttribute('aria-disabled') === 'true') return { success: false, message: 'Manual order save button is disabled after filling PO/vendor.' };
save.click();
return { success: true };
"""
    result = driver.execute_script(script)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError((result or {}).get("message") or "CRM manual order popup could not be saved.")
    deadline = time.time() + 35
    while time.time() < deadline:
        if _crm_manual_order_row_exists(driver, po):
            return "recorded"
        time.sleep(0.8)
    _open_target_order(driver, order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
    if _crm_manual_order_row_exists(driver, po):
        return "recorded"
    raise RuntimeError("CRM Manual Order save did not produce a visible Sanmar manual-order row.")


def _crm_manual_order_row_exists(driver, po):
    script = r"""
const po = String(arguments[0] || '').toLowerCase();
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
return Array.from(document.querySelectorAll('tr')).filter(isVisible).some((row) => {
  if (row.closest('.modal')) return false;
  const text = normalize(row.innerText || row.textContent);
  return text.includes(po) && /\bsanmar\b/i.test(text);
});
"""
    try:
        return bool(driver.execute_script(script, po))
    except Exception:
        return False


def _process_open_order(crm_driver, sanmar_driver, order_id, dry_run=False):
    _wait_for_order_goods_page_ready(crm_driver, order_id)
    order = _extract_order_data(crm_driver, order_id)
    missing = [name for name in ("po", "product_id", "color", "production_date", "due_date") if not order.get(name)]
    if missing:
        return _result(order_id, False, "crm_data_missing", f"Missing CRM order data: {', '.join(missing)}.", order=order)
    if not order.get("quantities"):
        return _result(order_id, False, "crm_quantities_missing", "Could not map CRM size quantities from the order.", order=order)
    if order["order_type"] == "mach6" and "mach 6" not in order.get("subcontractor", "").lower():
        return _result(order_id, False, "subcontractor_mismatch", f"Unsupported subcontractor: {order.get('subcontractor')}.", order=order)

    safe_get_with_partial_load(sanmar_driver, SANMAR_URL, label="SanMar home")
    _ensure_sanmar_logged_in(sanmar_driver)
    cart = _sanmar_cart_has_items(sanmar_driver)
    if cart.get("hasItems"):
        safe_get_with_partial_load(sanmar_driver, SANMAR_CART_URL, label="SanMar cart")
        _open_visible_sanmar_cart()
        return _result(order_id, False, "sanmar_cart_not_empty", "SanMar shopping box already has items. Opened cart for review.", order=order, stop_run=True)

    products = order.get("products") if isinstance(order.get("products"), list) else []
    if not products:
        return _result(order_id, False, "crm_products_missing", "Could not map any CRM stock products from the order.", order=order)
    invalid_products = [
        product for product in products
        if not product.get("product_id") or not product.get("color") or not product.get("quantities")
    ]
    if invalid_products:
        labels = ", ".join(str(product.get("index") or "?") for product in invalid_products)
        return _result(order_id, False, "crm_product_data_missing", f"Missing product/color/size data for CRM product block(s): {labels}.", order=order)

    product_lines = []
    for product in products:
        search_id = _sanmar_search_id_for_product(product)
        try:
            _search_sanmar_product(sanmar_driver, search_id)
        except Exception as exc:
            return _result(order_id, False, "sanmar_product_not_found", f"SanMar product could not be found for {search_id}: {exc}", order=order, product=product)
        product_text = _normalize_text(sanmar_driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        if _upper_key(search_id) not in _upper_key(product_text):
            return _result(order_id, False, "sanmar_product_mismatch", f"SanMar product page did not match {search_id}.", order=order, product=product)
        try:
            _select_sanmar_color(sanmar_driver, product["color"])
        except Exception as exc:
            return _result(order_id, False, "sanmar_color_not_found", f"SanMar color could not be selected for {search_id} / {product['color']}: {exc}", order=order, product=product)
        inventory = _sanmar_inventory(sanmar_driver)
        if not inventory:
            return _result(order_id, False, "sanmar_inventory_missing", f"SanMar inventory table was not found for {search_id}.", order=order, product=product)
        product_lines.append(
            {
                "product": product,
                "search_id": search_id,
                "quantities": product["quantities"],
                "inventory": inventory,
            }
        )

    warehouse = _choose_common_warehouse(product_lines, order["order_type"])
    if not warehouse:
        return _result(order_id, False, "no_single_warehouse", "No single SanMar warehouse can fulfill every product/size/quantity on this order.", order=order, products=product_lines)

    for line in product_lines:
        product = line["product"]
        search_id = line["search_id"]
        _search_sanmar_product(sanmar_driver, search_id)
        product_text = _normalize_text(sanmar_driver.execute_script("return String(document.body && (document.body.innerText || document.body.textContent) || '');"))
        if _upper_key(search_id) not in _upper_key(product_text):
            return _result(order_id, False, "sanmar_product_mismatch", f"SanMar product page did not match {search_id} while filling cart.", order=order, product=product, warehouse=warehouse)
        _select_sanmar_color(sanmar_driver, product["color"])
        _fill_sanmar_quantities(sanmar_driver, warehouse, product["quantities"])
        _add_current_product_to_box(sanmar_driver)

    safe_get_with_partial_load(sanmar_driver, SANMAR_CART_URL, label="SanMar cart")
    cart_text = _wait_for_text(sanmar_driver, r"Continue\s+Checkout|Warehouse", timeout=20)
    if warehouse not in cart_text:
        return _result(order_id, False, "checkout_warehouse_mismatch", f"SanMar cart did not confirm warehouse {warehouse}.", order=order, warehouse=warehouse)
    _click_sanmar_button(sanmar_driver, r"Continue\s+Checkout")
    _wait_for_text(sanmar_driver, r"Shipping\s+Details|Shipping\s+Address", timeout=20)
    shipping = _select_shipping_destination(sanmar_driver, order["order_type"], warehouse)
    eta = None
    if shipping.get("ship_mode") == "ship":
        eta = _select_ups_and_eta(sanmar_driver, order["order_type"])
        if eta >= order["due_date"]:
            return _result(order_id, False, "eta_on_or_after_due_date", f"SanMar ETA {eta.isoformat()} is on/after due date {order['due_date'].isoformat()}.", order=order, warehouse=warehouse, eta=str(eta))
        if eta > order["production_date"]:
            _change_crm_production_date(crm_driver, order_id, eta)
            order["production_date"] = eta
    _click_sanmar_button(sanmar_driver, r"Proceed\s+To\s+Payment")
    _wait_for_text(sanmar_driver, r"Review\s+&\s+Submit|Review\s+and\s+Submit|Customer\s+PO", timeout=20)
    submit_state = _fill_review_and_submit(sanmar_driver, order["po"], dry_run=dry_run)
    sanmar_confirmation = None
    if submit_state == "submitted":
        sanmar_confirmation = _capture_sanmar_confirmation(sanmar_driver, order_id, order["po"])
    try:
        record_state = _record_crm_manual_order(crm_driver, order_id, order["po"], dry_run=dry_run)
    except Exception as exc:
        if submit_state == "submitted":
            return _result(
                order_id,
                False,
                "sanmar_submitted_crm_record_failed",
                f"SanMar order was submitted, but CRM Manual Order could not be recorded: {exc}",
                order=order,
                warehouse=warehouse,
                products=product_lines,
                eta=str(eta) if eta else None,
                shipping=shipping,
                sanmar_submit_state=submit_state,
                sanmar_confirmation=sanmar_confirmation,
                manual_review_required=True,
                retryable=False,
            )
        raise
    return _result(
        order_id,
        True,
        "shipping_bypass_ready" if dry_run else "shipping_bypass_ordered",
        "Shipping Bypasser dry run reached SanMar review and CRM manual-order readiness." if dry_run else "SanMar stock was ordered and recorded in CRM Manual Order.",
        manual_review_required=False,
        order=order,
        warehouse=warehouse,
        products=product_lines,
        eta=str(eta) if eta else None,
        shipping=shipping,
        sanmar_submit_state=submit_state,
        sanmar_confirmation=sanmar_confirmation,
        crm_record_state=record_state,
    )


def _run_order_with_drivers(crm_driver, sanmar_driver, order_id, dry_run=False):
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or CRM order URL.")
    _open_target_order(crm_driver, normalized_order_id, shipping_filter=RUSH_FILTER, list_url_override=None)
    _wait_for_order_goods_page_ready(crm_driver, normalized_order_id)
    return [_process_open_order(crm_driver, sanmar_driver, normalized_order_id, dry_run=dry_run)]


def _summary_message(report_items, refresh_passes=1, order_count=0):
    total = len(report_items)
    order_groups = {}
    for item in report_items:
        order_id = str(item.get("order_id") or "").strip()
        if order_id:
            order_groups.setdefault(order_id, []).append(item)
    successful_orders = sum(1 for items in order_groups.values() if all(bool(item.get("success")) for item in items))
    failed_orders = len(order_groups) - successful_orders
    if total == 0:
        return f"No {ALLOWED_SHIPPING_LIST_ROW_DESCRIPTION} Shipping Bypasser orders were detected in the CRM list."
    parts = [
        f"Shipping Bypasser processed {max(1, int(order_count or len(order_groups) or 0))} order(s) across {max(1, int(refresh_passes or 1))} CRM list refresh pass(es).",
        f"{successful_orders} order(s) succeeded.",
    ]
    if failed_orders:
        parts.append(f"{failed_orders} order(s) need attention.")
    return " ".join(parts)


def _run_single_with_mode(headless_mode, order_id, dry_run=False, profile_path=None, sanmar_visible=False):
    started_at = time.monotonic()
    normalized_order_id = _normalize_target_order_id(order_id)
    if not normalized_order_id:
        raise RuntimeError("Order ID must be a 7-digit value or CRM order URL.")
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    crm_driver = None
    sanmar_driver = None
    report_items = []
    try:
        crm_driver = _build_crm_session_driver(
            resolved_profile_path,
            headless_mode=headless_mode,
            profile_label=f"CRM shipping bypasser single {normalized_order_id}",
        )
        sanmar_driver = _build_sanmar_driver(visible=sanmar_visible or dry_run)
        report_items = _run_order_with_drivers(crm_driver, sanmar_driver, normalized_order_id, dry_run=dry_run)
    finally:
        safe_driver_quit(crm_driver, profile_path=resolved_profile_path)
        if not any(isinstance(item, dict) and item.get("outcome") == "sanmar_cart_not_empty" for item in report_items):
            safe_driver_quit(sanmar_driver, profile_path=os.path.abspath(SANMAR_PROFILE_PATH))
    success = all(bool(item.get("success")) for item in report_items) if report_items else True
    return {
        "action": "shipping_bypass_single",
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
            "action": "shipping_bypass_single",
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
                sanmar_visible=visible,
            )
            payload["headless"] = bool(headless_mode)
            return payload
        except Exception as exc:
            last_payload = {
                "action": "shipping_bypass_single",
                "success": False,
                "message": str(exc),
                "target_order_id": normalized_order_id,
                "order_count": 1,
                "order_ids": [normalized_order_id],
                "report": [_result(normalized_order_id, False, "worker_exception", str(exc), retryable=_is_retryable_exception(exc), error_type=type(exc).__name__)],
                "dry_run": bool(dry_run),
                "headless": bool(headless_mode),
                "shipping_filter": RUSH_FILTER,
                "duration_seconds": _elapsed_seconds(started_at),
                "manual_review_required": True,
                "resolution": "single",
            }
            if not headless_mode or index == len(modes) or not _is_retryable_exception(exc):
                break
            print("Headless Shipping Bypasser single run failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
    return last_payload


def _run_batch_with_mode(headless_mode, dry_run=False, batch_size=None, profile_path=None, list_url=None):
    batch_started_at = time.monotonic()
    target_url = _validate_runtime_config(list_url)
    requested_batch_size = _normalize_requested_batch_size(batch_size)
    resolved_profile_path = os.path.abspath(profile_path or PROFILE_PATH)
    crm_driver = _build_crm_session_driver(
        resolved_profile_path,
        headless_mode=headless_mode,
        profile_label="CRM shipping bypasser",
    )
    sanmar_driver = None
    report_items = []
    attempted_order_ids = []
    historical_order_id_set = _load_historical_shipping_bypass_order_ids()
    attempted_order_id_set = set(historical_order_id_set)
    refresh_passes = 0
    try:
        sanmar_driver = _build_sanmar_driver(visible=dry_run)
        while not _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
            refresh_passes += 1
            remaining = _batch_collection_limit(
                requested_batch_size,
                len(attempted_order_ids),
                worker_limit=CONTINUOUS_ORDER_FETCH_LIMIT,
            )
            order_ids = _collect_batch_order_ids_with_driver(
                crm_driver,
                RUSH_FILTER,
                remaining,
                list_url_override=target_url,
                exclude_order_ids=attempted_order_id_set,
            )
            if not order_ids:
                break
            for order_id in order_ids:
                if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                    break
                attempted_order_id_set.add(order_id)
                attempted_order_ids.append(order_id)
                print(f"Processing Shipping Bypasser order {len(attempted_order_ids)}: {order_id}...")
                order_started_at = time.monotonic()
                try:
                    order_report = _run_order_with_drivers(crm_driver, sanmar_driver, order_id, dry_run=dry_run)
                    order_duration = _elapsed_seconds(order_started_at)
                    for item in order_report:
                        if isinstance(item, dict):
                            item["duration_seconds"] = order_duration
                            item["session_duration_seconds"] = order_duration
                    report_items.extend(order_report)
                    if any(isinstance(item, dict) and item.get("stop_run") for item in order_report):
                        raise RuntimeError("SanMar cart already had items; stopped Shipping Bypasser.")
                except Exception as exc:
                    safe_take_screenshot(crm_driver, "crm_shipping_bypass_error")
                    order_duration = _elapsed_seconds(order_started_at)
                    report_items.append(_result(order_id, False, "worker_exception", str(exc), retryable=_is_retryable_exception(exc), error_type=type(exc).__name__, duration_seconds=order_duration))
                    if "cart already had items" in str(exc).lower():
                        break
            if _batch_limit_reached(len(attempted_order_ids), requested_batch_size):
                break
            print("Finished Shipping Bypasser list pass; reopening the list to look for more eligible orders...")
    finally:
        safe_driver_quit(crm_driver, profile_path=resolved_profile_path)
        if not any(isinstance(item, dict) and item.get("outcome") == "sanmar_cart_not_empty" for item in report_items):
            safe_driver_quit(sanmar_driver, profile_path=os.path.abspath(SANMAR_PROFILE_PATH))
    success = all(bool(item.get("success")) for item in report_items) if report_items else True
    skipped_historical_count = len(historical_order_id_set)
    message = _summary_message(report_items, refresh_passes=refresh_passes, order_count=len(attempted_order_ids))
    if skipped_historical_count:
        message = f"{message} Skipped {skipped_historical_count} previously logged Shipping Bypasser order(s); clear Stock Tools history to make them eligible again."
    return {
        "action": "shipping_bypass_batch",
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
        "parallel_workers": 1,
        "refresh_passes": refresh_passes,
        "duration_seconds": _elapsed_seconds(batch_started_at),
        "manual_review_required": any(bool(item.get("manual_review_required")) for item in report_items),
        "resolution": "batch" if attempted_order_ids else "no_orders",
        "skipped_historical_order_count": skipped_historical_count,
    }


def _run_batch(dry_run=False, batch_size=None, profile_path=None, list_url=None, visible=False):
    batch_started_at = time.monotonic()
    modes = [False] if visible else _crm_attempt_modes()
    last_payload = None
    for index, headless_mode in enumerate(modes, start=1):
        try:
            payload = _run_batch_with_mode(
                headless_mode,
                dry_run=dry_run,
                batch_size=batch_size,
                profile_path=profile_path,
                list_url=list_url,
            )
            payload["headless"] = bool(headless_mode)
            return payload
        except Exception as exc:
            last_payload = {
                "action": "shipping_bypass_batch",
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
            print("Headless Shipping Bypasser failed with a retryable error; retrying with visible Chrome...")
            time.sleep(1)
    return last_payload


def run(action="shipping_bypass_batch", dry_run=False, batch_size=None, profile_path=None, result_file=None, list_url=None, visible=False, order_id=None):
    if action not in {"shipping_bypass_batch", "shipping_bypass_single"}:
        raise RuntimeError("Unsupported CRM Shipping Bypasser action.")
    if action == "shipping_bypass_single" or order_id:
        payload = _run_single(order_id, dry_run=dry_run, profile_path=profile_path, visible=visible)
    else:
        payload = _run_batch(dry_run=dry_run, batch_size=batch_size, profile_path=profile_path, list_url=list_url, visible=visible)
    write_result_payload(
        AUTOMATION_NAME,
        "crm_shipping_bypasser.py",
        bool(payload.get("success")),
        payload.get("message") or "CRM Shipping Bypasser completed.",
        extra_fields=payload,
        result_file=result_file,
    )
    return 0 if payload.get("success") else 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Manually order SanMar stock for Shipping Cost Bypass CRM orders.")
    parser.add_argument("--action", choices=["shipping_bypass_batch", "shipping_bypass_single"], default="shipping_bypass_batch")
    parser.add_argument("--order-id", required=False, help="Optional single 7-digit CRM order ID or CRM order URL.")
    parser.add_argument("--batch-size", type=int, default=None, help="Process up to this many orders; 0/unset means run until no eligible orders remain.")
    parser.add_argument("--profile-path", required=False, help="Optional CRM Chrome user-data-dir override.")
    parser.add_argument("--result-file", required=False, help="Optional path for the JSON result payload.")
    parser.add_argument("--list-url", required=False, help="Optional Shipping Bypasser CRM report URL override.")
    parser.add_argument("--visible", action="store_true", help="Run Chrome visibly instead of headless for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Stop before SanMar submit and CRM Manual Order save.")
    return parser.parse_args(argv)


if __name__ == "__main__":
    options = parse_args(sys.argv[1:])
    sys.exit(
        run(
            action=options.action,
            dry_run=bool(options.dry_run),
            batch_size=options.batch_size,
            profile_path=options.profile_path,
            result_file=options.result_file,
            list_url=options.list_url,
            visible=bool(options.visible),
            order_id=options.order_id,
        )
    )
