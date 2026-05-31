"""
CRM Product Separator automation worker.

First implementation scope:
- Single CRM order only.
- Dry-run scan/report by default.
- Live mode is gated by --real.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from _bootstrap import ensure_project_root_on_path

PROJECT_ROOT = ensure_project_root_on_path()

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
    PROCESSOR_ACTION_TIMEOUT,
    PROCESSOR_DRY_RUN,
    PROCESSOR_HEADLESS,
    PROCESSOR_ORDER_URL_TEMPLATE,
    PROCESSOR_PAGE_LOAD_TIMEOUT,
    PROCESSOR_PROFILE_DIR,
    PRODUCT_SEPARATOR_DEFAULT_LIST_MODE,
    PRODUCT_SEPARATOR_LIST_URL,
    PRODUCT_SEPARATOR_LIST_URL_ALL,
    PRODUCT_SEPARATOR_LIST_URL_FREE,
    PRODUCT_SEPARATOR_LIST_URL_RUSH,
)

configure_console_utf8()

AUTOMATION_NAME = "crm.product_separator"
SOURCE = "product_separator_automation.py"

GROUP_LABELS = {
    "adult_general": "Adult/general",
    "youth": "Youth",
    "toddler": "Toddler",
    "infant": "Infant",
    "hat_cap": "Hat/cap",
    "bag": "Bag",
    "towel": "Towel",
}


class ProductSeparatorError(Exception):
    """Raised when Product Separator must stop before live changes."""


class ManualReviewRequired(ProductSeparatorError):
    """Raised when the order should be returned for manual review."""


def _profile_path(profile_dir=None):
    profile_dir = profile_dir or PROCESSOR_PROFILE_DIR
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


def _clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


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
    last_url = ""
    last_title = ""
    last_text = ""
    while time.monotonic() < deadline:
        try:
            _activate_crm_context(driver)
            last_url = str(driver.current_url or "")
            last_title = str(driver.title or "")
            last_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText || document.body.textContent || '' : '';"))
            if re.search(r"\bnot authenticated\b", last_text, flags=re.IGNORECASE):
                raise ProductSeparatorError("CRM authentication failed: Not authenticated.")
            ready = driver.execute_script("return !!(window.angular && document.body && document.body.innerText.length);")
            if ready:
                return True
        except Exception as err:
            last_error = err
        time.sleep(0.5)
    detail = f"url={last_url!r} title={last_title!r} text={last_text[:160]!r}"
    raise ProductSeparatorError(f"CRM app did not become ready. Last error: {last_error}. Last page: {detail}")


ANGULAR_APPLY_JS = """
function runInAngular(scope, fn) {
  const root = scope.$root || scope;
  if (root.$$phase) return fn();
  if (typeof scope.$apply === 'function') return scope.$apply(fn);
  const result = fn();
  if (typeof root.$digest === 'function') root.$digest();
  return result;
}
"""


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


def _order_scope(driver, script, *args):
    return driver.execute_script(ORDER_SCOPE_BOOTSTRAP + "\n" + ANGULAR_APPLY_JS + "\n" + script, *args)


def _is_login_page(driver):
    body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';")).lower()
    current_url = str(driver.current_url or "").lower()
    title = str(driver.title or "").lower()
    return "login" in body_text or "login" in title or "/login" in current_url


def _is_not_authenticated_page(driver):
    body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';")).lower()
    return bool(re.search(r"\bnot authenticated\b", body_text))


def _recover_not_authenticated_page(driver, target_url, login_wait_seconds=0):
    if not _is_not_authenticated_page(driver):
        return False
    safe_get_with_partial_load(driver, target_url, "CRM order after authentication error")
    _handle_login_if_needed(driver, target_url, login_wait_seconds=login_wait_seconds)
    _wait_for_crm_context(driver)
    if _is_not_authenticated_page(driver):
        raise ProductSeparatorError("CRM authentication failed: Not authenticated.")
    return True


def _maybe_click_saved_login(driver):
    clicked = bool(
        driver.execute_script(
            """
            const controls = Array.from(document.querySelectorAll('button,input[type=submit],a,[role=button],div,span'));
            const visible = controls.filter((el) => {
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
              return rect.width > 10 && rect.height > 10 && style.display !== 'none' && style.visibility !== 'hidden';
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
    return clicked


def _handle_login_if_needed(driver, target_url, login_wait_seconds=0):
    if not _is_login_page(driver):
        return False
    _maybe_click_saved_login(driver)
    time.sleep(3)
    if not _is_login_page(driver):
        safe_get_with_partial_load(driver, target_url, "CRM order after automatic login")
        return True
    if login_wait_seconds <= 0:
        raise ProductSeparatorError(
            "CRM login is required. Open the CRM profile browser, log in, or rerun with --login-wait-seconds."
        )
    print(f"Login is required. Complete login in the Chrome window within {login_wait_seconds} seconds.")
    deadline = time.monotonic() + login_wait_seconds
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            if not _is_login_page(driver):
                safe_get_with_partial_load(driver, target_url, "CRM order after manual login")
                return True
        except Exception:
            pass
    raise ProductSeparatorError("CRM login did not complete before the wait timeout.")


def _build_driver(
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    profile_dir=None,
    kill_existing=True,
):
    if attach_browser:
        options_driver = build_attached_chrome_driver(debugger_address=debugger_address)
        return options_driver, None
    profile = _profile_path(profile_dir=profile_dir)
    headless = not bool(visible)
    if kill_existing:
        kill_stale_chrome(profile, profile_label="Product Separator")
    driver = build_chrome_driver(
        profile,
        headless_mode=headless,
        page_load_strategy="eager",
        page_load_timeout=PROCESSOR_PAGE_LOAD_TIMEOUT,
        script_timeout=PROCESSOR_ACTION_TIMEOUT,
    )
    return driver, profile


VISIBLE_TABS_JS = r"""
const clickIndex = arguments.length ? Number(arguments[0]) : null;
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function visible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width <= 8 || rect.height <= 8) return false;
  const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
  return style.display !== 'none' && style.visibility !== 'hidden';
}
function scoreTab(el) {
  const text = clean(el.innerText || el.textContent);
  if (!/\b\d+\s*-\s*QTY\s*:\s*\d+/i.test(text) || !/Design Previews/i.test(text)) return -1;
  const rect = el.getBoundingClientRect();
  let score = 1000 - text.length;
  if (el.querySelector && el.querySelector('input')) score += 100;
  if (rect.top < 450) score += 100;
  return score;
}
const all = Array.from(document.querySelectorAll('div,a,button,li,span'));
const candidates = all
  .filter(visible)
  .map((el) => ({el, score: scoreTab(el), text: clean(el.innerText || el.textContent), rect: el.getBoundingClientRect()}))
  .filter((item) => item.score >= 0);
const bestByNumber = new Map();
for (const item of candidates) {
  const match = item.text.match(/\b(\d+)\s*-\s*QTY\s*:\s*(\d+)/i);
  if (!match) continue;
  const tabNumber = Number(match[1]);
  const previous = bestByNumber.get(tabNumber);
  if (!previous || item.score > previous.score) bestByNumber.set(tabNumber, item);
}
const tabs = Array.from(bestByNumber.entries()).map(([tabNumber, item]) => {
  const lines = String(item.el.innerText || item.el.textContent || '').split(/\n+/).map(clean).filter(Boolean);
  let name = '';
  for (const line of lines) {
    if (/Design Previews/i.test(line)) break;
    if (/\b\d+\s*-\s*QTY\s*:\s*\d+/i.test(line)) continue;
    if (line && !/clone/i.test(line) && line.toLowerCase() !== 'po number') {
      name = line;
      break;
    }
  }
  if (!name) {
    const input = item.el.querySelector && item.el.querySelector('input');
    name = input ? clean(input.value || input.placeholder) : '';
  }
  const qtyMatch = item.text.match(/\b\d+\s*-\s*QTY\s*:\s*(\d+)/i);
  return {
    element: item.el,
    tab_number: tabNumber,
    tab_name: name,
    quantity: qtyMatch ? Number(qtyMatch[1]) : null,
    text: item.text,
    x: item.rect.x,
    y: item.rect.y,
  };
}).sort((a, b) => a.tab_number - b.tab_number);
if (Number.isFinite(clickIndex)) {
  const tab = tabs.find((item) => item.tab_number === clickIndex);
  if (!tab) return null;
  tab.element.scrollIntoView({block: 'center', inline: 'center'});
  tab.element.click();
  return tab;
}
return tabs;
"""


PRODUCT_SCAN_JS = r"""
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function visible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
  return style.display !== 'none' && style.visibility !== 'hidden';
}
function nearestProductBlock(link) {
  let best = null;
  for (let el = link; el && el !== document.body; el = el.parentElement) {
    const text = clean(el.innerText || el.textContent);
    if (/Size\s*:/i.test(text) && /Quantity\s*:/i.test(text) && /Price\s*:/i.test(text)) {
      best = el;
      if (text.length > 80 && text.length < 2500) break;
    }
  }
  return best;
}
const links = Array.from(document.querySelectorAll('a'))
  .filter(visible)
  .filter((a) => /\bAlpha(?: Stock)?\b/i.test(a.innerText || a.textContent || ''));
const seen = new Set();
const products = [];
for (const link of links) {
  const block = nearestProductBlock(link);
  if (!block) continue;
  const rect = block.getBoundingClientRect();
  const key = `${Math.round(rect.top)}:${Math.round(rect.left)}:${clean(link.innerText || link.textContent)}`;
  if (seen.has(key)) continue;
  seen.add(key);
  const text = clean(block.innerText || block.textContent);
  const linkText = clean(link.innerText || link.textContent);
  const productMatch = text.match(/^(.+?)\s*-\s*Alpha Stock\b/i);
  const editProductMatch = text.match(/Check Stock:\s*\S+\s+(.+?)\s+[A-Z0-9][A-Z0-9-]{2,}\s+[A-Z][A-Z0-9 /-]*\s+Size\s*:/i);
  const productName = productMatch
    ? clean(productMatch[1])
    : editProductMatch
      ? clean(editProductMatch[1])
    : linkText.replace(/\s*-\s*Alpha Stock\s*$/i, '').trim();
  const color = (() => {
    const parts = text.split(productName);
    if (parts.length < 2) return '';
    const after = clean(parts[1]);
    const match = after.match(/Alpha Stock\s+([A-Z][A-Z0-9 /-]{1,40})\s+Total Quantity/i);
    return match ? clean(match[1]) : '';
  })();
  products.push({
    product_name: productName || linkText,
    link_text: linkText,
    text,
    color,
    y: rect.top,
    x: rect.left,
  });
}
products.sort((a, b) => (a.y - b.y) || (a.x - b.x));
return products;
"""


def _visible_design_tabs(driver):
    try:
        tabs = driver.execute_script(VISIBLE_TABS_JS)
    except Exception:
        return []
    return tabs if isinstance(tabs, list) else []


def _click_design_tab(driver, tab_number):
    tab = driver.execute_script(VISIBLE_TABS_JS, int(tab_number))
    time.sleep(0.7)
    return tab if isinstance(tab, dict) else None


def _scan_visible_products(driver):
    try:
        products = driver.execute_script(PRODUCT_SCAN_JS)
    except Exception:
        products = []
    return products if isinstance(products, list) else []


def _extract_sizes(product):
    text = _clean_text(product.get("text"))
    sizes = []
    known_patterns = [
        r"\bYXS\b", r"\bYS\b", r"\bYM\b", r"\bYL\b", r"\bYXL\b", r"\bYXXL\b",
        r"\b(?:2T|3T|4T|5T|6T|7T)\b", r"\b5/6\b",
        r"\bNB\b", r"\b0\s*-\s*3\s*MOS\b", r"\b3\s*-\s*6\s*MOS\b",
        r"\b6\s*-\s*12\s*MOS\b", r"\b12\s*-\s*18\s*MOS\b", r"\b18\s*-\s*24\s*MOS\b",
        r"\bONE\s+SIZE\b", r"\bOSFA\b",
        r"\bXS\b", r"\bS\b", r"\bM\b", r"\bL\b", r"\bXL\b", r"\b2XL\b", r"\b3XL\b", r"\b4XL\b", r"\b5XL\b",
        r"\bS/M\b", r"\bL/XL\b", r"\b2XL/3XL\b", r"\b4XL/5XL\b",
        r"\bLT\b", r"\bXLT\b", r"\b2XT\b", r"\b3XT\b", r"\b4XT\b",
    ]
    for pattern in known_patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = _clean_text(match.group(0)).upper().replace(" ", "")
            value = value.replace("MOS", "MOS")
            if value not in sizes:
                sizes.append(value)
    return sizes


def _classify_product(product):
    name = _clean_text(product.get("product_name")).lower()
    text = _clean_text(product.get("text")).lower()
    sizes = _extract_sizes(product)
    compact_sizes = {size.upper().replace(" ", "") for size in sizes}

    group = "adult_general"
    reason = "default adult/general"
    confidence = "normal"

    if re.search(r"\b(towel|rally towel|sport towel)\b", name):
        group, reason = "towel", "product name contains towel"
    elif re.search(r"\b(tote|bag|backpack|duffel|drawstring)\b", name):
        group, reason = "bag", "product name contains bag keyword"
    elif re.search(r"\b(hats?|caps?|beanie|beanies|snapbacks?|truckers?)\b", name):
        group, reason = "hat_cap", "product name contains hat/cap keyword"
    elif re.search(r"\b(toddler)\b", name) or compact_sizes.intersection({"2T", "3T", "4T", "5T", "6T", "7T", "5/6"}):
        group, reason = "toddler", "toddler name or toddler size"
    elif re.search(r"\b(infant|baby|onesie|romper)\b", name) or compact_sizes.intersection({"NB", "0-3MOS", "3-6MOS", "6-12MOS", "12-18MOS", "18-24MOS"}):
        group, reason = "infant", "infant/baby name or infant size"
    elif re.search(r"\b(youth|kids|kid's|girls|boys)\b", name) or compact_sizes.intersection({"YXS", "YS", "YM", "YL", "YXL", "YXXL"}):
        group, reason = "youth", "youth/kids/girls name or youth size"

    if not name:
        confidence = "uncertain"
        reason = "product name was not readable"
    elif group == "adult_general" and "one size" in text and re.search(r"\b(youth|kids|girls|boys|infant|baby|toddler|hat|cap|bag|tote|towel)\b", text):
        confidence = "normal"

    return {
        **product,
        "sizes": sizes,
        "group": group,
        "group_label": GROUP_LABELS.get(group, group),
        "classification_reason": reason,
        "classification_confidence": confidence,
    }


def _stock_state_from_text(text):
    normalized = " ".join(str(text or "").lower().split())
    stock_status_ordered = bool(re.search(r"\bstock\s+status\s*:\s*ordered\b", normalized))
    has_vendor_section = "order goods from vendor" in normalized
    has_yellow_po = bool(re.search(r"\bmanual order\b.*\blocal inventory\b.*\bvendor order\b", normalized))
    missing_order_goods = "order goods" not in normalized or "order goods from vendor" in normalized
    if stock_status_ordered and has_vendor_section and has_yellow_po:
        state = "ordered"
    elif stock_status_ordered:
        state = "ordered_header_only"
    elif has_vendor_section and has_yellow_po:
        state = "ordered_po_only"
    else:
        state = "not_ordered_or_unknown"
    return {
        "state": state,
        "stock_status_ordered": stock_status_ordered,
        "has_vendor_section": has_vendor_section,
        "has_po_row": has_yellow_po,
        "order_goods_button_missing_or_not_detected": missing_order_goods,
    }


def _extract_summary_products_from_text(text, expected_order_id=None):
    body_text = _clean_text(text)
    if not body_text:
        return []
    if expected_order_id:
        pattern = rf"\b{re.escape(str(expected_order_id))}\s+Summary:\s*(.+?)(?:\s+Quote\b|\s+Salesforce\b|\s+Edit Account\b|$)"
    else:
        pattern = r"\bSummary:\s*(.+?)(?:\s+Quote\b|\s+Salesforce\b|\s+Edit Account\b|$)"
    match = re.search(pattern, body_text, flags=re.IGNORECASE)
    if not match:
        return []
    summary = _clean_text(match.group(1))
    if not summary:
        return []
    products = []
    for raw_part in re.split(r"\s*/\s*", summary):
        part = _clean_text(raw_part)
        if not part:
            continue
        qty_match = re.search(r"\((\d+)\)\s*$", part)
        quantity = int(qty_match.group(1)) if qty_match else None
        name = re.sub(r"\s*\(\d+\)\s*$", "", part).strip()
        # CRM summary prefixes like "hF" and "hB" describe decoration sides, not product family.
        name = re.sub(r"^(?:h[FB]\s+)+", "", name, flags=re.IGNORECASE).strip()
        if not name:
            name = part
        products.append(
            {
                "product_name": name,
                "link_text": "",
                "text": part,
                "color": "",
                "quantity": quantity,
            }
        )
    return products


def _fallback_scan_from_order_summary(text, expected_order_id=None):
    products = [_classify_product(item) for item in _extract_summary_products_from_text(text, expected_order_id=expected_order_id)]
    if not products:
        return None
    groups = OrderedDict()
    for product in products:
        groups.setdefault(product["group"], []).append(product)
    if len(groups) > 1:
        return None
    summary_tab_name = products[0].get("product_name") or "Order summary"
    return {
        "order_id": expected_order_id or "",
        "tab_count": 1,
        "tabs": [
            {
                "tab_number": 1,
                "tab_name": summary_tab_name,
                "quantity": products[0].get("quantity"),
                "products": products,
                "groups": [
                    {
                        "group": group,
                        "group_label": GROUP_LABELS.get(group, group),
                        "product_count": len(items),
                        "product_names": [item.get("product_name") for item in items],
                    }
                    for group, items in groups.items()
                ],
                "needs_split": False,
                "stock": _stock_state_from_text(text),
                "warnings": ["Design tabs were not detected; used single-group order summary fallback."],
                "source": "order_summary_fallback",
            }
        ],
        "source": "order_summary_fallback",
    }


def _active_tab_stock_state(driver):
    text = driver.execute_script("return document.body ? document.body.innerText || document.body.textContent || '' : '';")
    return _stock_state_from_text(text)


def _scan_order(driver, expected_order_id=None):
    _wait_for_crm_context(driver)
    deadline = time.monotonic() + max(45, PROCESSOR_PAGE_LOAD_TIMEOUT)
    tabs = []
    while time.monotonic() < deadline:
        if _is_not_authenticated_page(driver):
            raise ProductSeparatorError("CRM authentication failed: Not authenticated.")
        tabs = _visible_design_tabs(driver)
        if tabs:
            break
        time.sleep(1)
    if not tabs:
        body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))
        if re.search(r"\bnot authenticated\b", body_text, flags=re.IGNORECASE):
            raise ProductSeparatorError("CRM authentication failed: Not authenticated.")
        fallback_scan = _fallback_scan_from_order_summary(body_text, expected_order_id=expected_order_id)
        if fallback_scan:
            return fallback_scan
        raise ProductSeparatorError(f"No design tabs were detected. Visible text starts: {body_text[:300]}")

    scanned_tabs = []
    for tab in tabs:
        tab_number = int(tab.get("tab_number") or 0)
        if not tab_number:
            continue
        clicked = _click_design_tab(driver, tab_number)
        products = [_classify_product(item) for item in _scan_visible_products(driver)]
        groups = OrderedDict()
        for product in products:
            groups.setdefault(product["group"], []).append(product)
        stock_state = _active_tab_stock_state(driver)
        scanned_tabs.append(
            {
                "tab_number": tab_number,
                "tab_name": _clean_text((clicked or tab).get("tab_name") or tab.get("tab_name")),
                "quantity": (clicked or tab).get("quantity") or tab.get("quantity"),
                "products": products,
                "groups": [
                    {
                        "group": group,
                        "group_label": GROUP_LABELS.get(group, group),
                        "product_count": len(items),
                        "product_names": [item.get("product_name") for item in items],
                    }
                    for group, items in groups.items()
                ],
                "needs_split": len(groups) > 1,
                "stock": stock_state,
                "warnings": [],
            }
        )

    return {
        "order_id": expected_order_id or "",
        "tab_count": len(scanned_tabs),
        "tabs": scanned_tabs,
    }


REPORT_ORDER_IDS_JS = r"""
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function visible(el) {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
}
function parseRgb(value) {
  const match = String(value || '').match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}
function closeRgb(actual, expected, tolerance) {
  if (!actual) return false;
  return Math.abs(actual[0] - expected[0]) <= tolerance
    && Math.abs(actual[1] - expected[1]) <= tolerance
    && Math.abs(actual[2] - expected[2]) <= tolerance;
}
function isTargetRowColor(row) {
  if (!row) return false;
  const rgb = parseRgb(window.getComputedStyle(row).backgroundColor);
  const purple = closeRgb(rgb, [134, 32, 159], 10);
  const tanNatural = closeRgb(rgb, [243, 196, 156], 12);
  return purple || tanNatural;
}
function reportRowFor(link, orderId) {
  for (let row = link; row && row !== document.body; row = row.parentElement) {
    const rect = row.getBoundingClientRect();
    const text = clean(row.innerText || row.textContent);
    if (text.includes(orderId) && rect.width > 500 && rect.height > 15 && visible(row)) {
      return row;
    }
  }
  return null;
}
const ids = new Set();
for (const link of Array.from(document.querySelectorAll('a[href]')).filter(visible)) {
  const href = link.href || '';
  const text = clean(link.innerText || link.textContent);
  let match = href.match(/\/order\/(\d{5,})/);
  const hrefOrderId = match ? match[1] : '';
  match = text.match(/^(\d{5,})\b/);
  const textOrderId = match ? match[1] : '';
  const orderId = hrefOrderId || textOrderId;
  if (!orderId) continue;
  const row = reportRowFor(link, orderId);
  if (isTargetRowColor(row)) ids.add(orderId);
}
return Array.from(ids);
"""


def _extract_report_order_ids(driver, list_url, login_wait_seconds=0):
    safe_get_with_partial_load(driver, list_url, "Product Separator report")
    _handle_login_if_needed(driver, list_url, login_wait_seconds=login_wait_seconds)
    deadline = time.monotonic() + max(45, PROCESSOR_PAGE_LOAD_TIMEOUT)
    order_ids = []
    while time.monotonic() < deadline:
        try:
            order_ids = driver.execute_script(REPORT_ORDER_IDS_JS) or []
        except Exception:
            order_ids = []
        order_ids = [str(order_id) for order_id in order_ids if re.fullmatch(r"\d{5,}", str(order_id or ""))]
        if order_ids:
            break
        time.sleep(1)
    if not order_ids:
        body_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))
        raise ProductSeparatorError(f"No order IDs were detected on the report page. Visible text starts: {body_text[:300]}")
    return list(OrderedDict((order_id, None) for order_id in order_ids).keys())


def _copy_profile_for_worker(source_profile, worker_profile):
    if os.path.exists(worker_profile):
        shutil.rmtree(worker_profile, ignore_errors=True)

    def ignore(_dir, names):
        ignored = set()
        exact_names = {
            "DevToolsActivePort",
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
            "lockfile",
            "Crashpad",
            "BrowserMetrics",
            "ShaderCache",
            "GrShaderCache",
            "GraphiteDawnCache",
            "GPUCache",
            "Code Cache",
            "Cache",
            "DawnCache",
            "Service Worker",
        }
        suffixes = ("-journal", ".tmp", ".log", ".ldb", ".lock")
        for name in names:
            if name in exact_names or name.endswith(suffixes):
                ignored.add(name)
        return ignored

    shutil.copytree(source_profile, worker_profile, ignore=ignore, symlinks=True)


def _prepare_worker_profiles(worker_count):
    source_profile = _profile_path()
    if not os.path.isdir(source_profile):
        raise ProductSeparatorError(f"Chrome profile does not exist: {source_profile}")
    run_dir = os.path.join(PROJECT_ROOT, "product_separator_worker_profiles", time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(run_dir, exist_ok=True)
    profiles = []
    for index in range(max(1, int(worker_count))):
        worker_profile = os.path.join(run_dir, f"worker_{index + 1}")
        _copy_profile_for_worker(source_profile, worker_profile)
        profiles.append(worker_profile)
    return run_dir, profiles


def _increment_trailing_number(name, offset=1):
    match = re.search(r"(\d+)(?!.*\d)", str(name or ""))
    if not match:
        return ""
    start, end = match.span(1)
    raw_number = match.group(1)
    number = int(raw_number) + int(offset)
    return f"{name[:start]}{str(number).zfill(len(raw_number))}{name[end:]}"


def _next_available_incremented_name(name, used_names, start_offset=1):
    offset = max(1, int(start_offset or 1))
    normalized_used = {_clean_text(value).lower() for value in used_names if _clean_text(value)}
    while offset < 1000:
        candidate = _increment_trailing_number(name, offset)
        if not candidate:
            return ""
        if _clean_text(candidate).lower() not in normalized_used:
            used_names.add(candidate)
            return candidate
        offset += 1
    return ""


def _format_tab_list(tab_numbers):
    numbers = [str(value) for value in tab_numbers]
    if not numbers:
        return ""
    if len(numbers) == 1:
        return f"tab {numbers[0]}"
    if len(numbers) == 2:
        return f"tab {numbers[0]} and {numbers[1]}"
    if _is_contiguous([int(value) for value in numbers]):
        return f"tab {numbers[0]}-{numbers[-1]}"
    return f"tab {', '.join(numbers[:-1])}, and {numbers[-1]}"


def _is_contiguous(values):
    if not values:
        return False
    sorted_values = sorted(values)
    return sorted_values == list(range(sorted_values[0], sorted_values[-1] + 1))


def _build_separator_plan(scan):
    tabs = scan.get("tabs") or []
    used_tab_names = {_clean_text(tab.get("tab_name")) for tab in tabs if _clean_text(tab.get("tab_name"))}
    max_tab_number = max([int(tab.get("tab_number") or 0) for tab in tabs] or [0])
    next_tab_number = max_tab_number + 1
    split_tabs = []
    manual_review = []
    production_notes = []

    for tab in tabs:
        if not tab.get("needs_split"):
            continue
        products = tab.get("products") or []
        groups = OrderedDict()
        for product in products:
            groups.setdefault(product["group"], []).append(product)
        if len(groups) <= 1:
            continue

        uncertain = [item for item in products if item.get("classification_confidence") == "uncertain"]
        if uncertain:
            manual_review.append(
                {
                    "tab_number": tab.get("tab_number"),
                    "reason": "One or more products could not be classified confidently.",
                    "products": [item.get("product_name") for item in uncertain],
                }
            )

        original_name = tab.get("tab_name") or ""
        if not _increment_trailing_number(original_name):
            manual_review.append(
                {
                    "tab_number": tab.get("tab_number"),
                    "reason": f"Tab name does not end in a number: {original_name}",
                }
            )

        ordered_groups = list(groups.keys())
        if "adult_general" in groups:
            keep_group = "adult_general"
        else:
            keep_group = ordered_groups[0]
        clone_groups = [group for group in ordered_groups if group != keep_group]

        assignments = [
            {
                "tab_number": tab.get("tab_number"),
                "tab_name": original_name,
                "source": "original",
                "keep_group": keep_group,
                "keep_group_label": GROUP_LABELS.get(keep_group, keep_group),
                "keep_product_names": [item.get("product_name") for item in groups[keep_group]],
            }
        ]
        clone_offset = 1
        created_tab_numbers = []
        for group in clone_groups:
            new_name = _next_available_incremented_name(original_name, used_tab_names, clone_offset)
            if not new_name:
                manual_review.append(
                    {
                        "tab_number": tab.get("tab_number"),
                        "reason": f"Could not create a unique incremented tab name from: {original_name}",
                    }
                )
                continue
            assignments.append(
                {
                    "tab_number": next_tab_number,
                    "tab_name": new_name,
                    "source": "clone",
                    "clone_from_tab_number": tab.get("tab_number"),
                    "keep_group": group,
                    "keep_group_label": GROUP_LABELS.get(group, group),
                    "keep_product_names": [item.get("product_name") for item in groups[group]],
                }
            )
            created_tab_numbers.append(next_tab_number)
            next_tab_number += 1
            clone_offset += 1

        all_related_tabs = [int(tab.get("tab_number"))] + created_tab_numbers
        note = f"{_format_tab_list(all_related_tabs)} in 1 box"
        production_notes.append(note)
        split_tabs.append(
            {
                "source_tab_number": tab.get("tab_number"),
                "source_tab_name": original_name,
                "source_stock_state": tab.get("stock"),
                "groups_detected": [
                    {
                        "group": group,
                        "group_label": GROUP_LABELS.get(group, group),
                        "product_names": [item.get("product_name") for item in items],
                    }
                    for group, items in groups.items()
                ],
                "assignments": assignments,
                "production_note_if_stock_ordered": note,
            }
        )

    affected_stock_states = []
    for split in split_tabs:
        state = ((split.get("source_stock_state") or {}).get("state") or "not_ordered_or_unknown")
        affected_stock_states.append(state)
    has_ordered = any(state in {"ordered", "ordered_header_only", "ordered_po_only"} for state in affected_stock_states)
    has_not_ordered = any(state == "not_ordered_or_unknown" for state in affected_stock_states)
    mixed_stock_state = bool(has_ordered and has_not_ordered)
    if mixed_stock_state:
        manual_review.append(
            {
                "reason": "Affected split tabs have mixed stock-ordered state. Do not apply stock ordered automatically.",
                "stock_states": affected_stock_states,
            }
        )

    return {
        "needs_split": bool(split_tabs),
        "split_tabs": split_tabs,
        "manual_review": manual_review,
        "manual_review_required": bool(manual_review),
        "stock_ordered_for_all_affected_tabs": bool(split_tabs and has_ordered and not has_not_ordered),
        "mixed_stock_state": mixed_stock_state,
        "production_notes": production_notes if bool(split_tabs and has_ordered and not has_not_ordered) else [],
    }


def _click_ng_button(driver, ng_click, text=None):
    return bool(
        driver.execute_script(
            """
            const ngClick = arguments[0];
            const expectedText = (arguments[1] || '').toLowerCase();
            const buttons = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
            const button = buttons.find((el) => {
              const ng = el.getAttribute('ng-click') || '';
              const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              return ng === ngClick && (!expectedText || text === expectedText) && rect.width >= 0 && rect.height >= 0;
            });
            if (!button) return false;
            button.scrollIntoView({block: 'center', inline: 'center'});
            button.click();
            return true;
            """,
            ng_click,
            text or "",
        )
    )


def _enter_edit_mode(driver):
    if not _click_ng_button(driver, "editModeOn();", "edit order"):
        clicked = bool(
            driver.execute_script(
                """
                const button = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]')).find((el) => {
                  const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                  return text === 'edit order';
                });
                if (!button) return false;
                button.scrollIntoView({block: 'center'});
                button.click();
                return true;
                """
            )
        )
        if not clicked:
            _order_scope(driver, "runInAngular(s, () => s.editModeOn()); return true;")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            state = _order_scope(driver, "return {editMode: !!s.editMode};")
            if state.get("editMode"):
                return True
        except Exception:
            pass
        text = driver.execute_script("return document.body ? document.body.innerText : '';").lower()
        if "save order" in text and "remove item" in text:
            return True
    raise ProductSeparatorError("Edit mode did not become available.")


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
        };
        """
    )


def _red_error_text(driver):
    return _clean_text(
        driver.execute_script(
            """
            const nodes = Array.from(document.querySelectorAll('body *'));
            const errors = [];
            for (const el of nodes) {
              const rect = el.getBoundingClientRect();
              if (rect.width <= 0 || rect.height <= 0) continue;
              const style = window.getComputedStyle(el);
              const bg = style.backgroundColor || '';
              const color = style.color || '';
              const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
              if (!text) continue;
              if ((/rgb\\(\\s*255\\s*,\\s*0\\s*,\\s*0\\s*\\)|rgb\\(\\s*220\\s*,|rgb\\(\\s*217\\s*,|#f/i.test(bg + color))
                  && /error|failed|cannot|someone|saved|invalid|required/i.test(text)) {
                errors.push(text);
              }
            }
            return errors.slice(0, 5).join(' | ');
            """
        )
    )


def _save_order_and_wait(driver):
    if not _click_ng_button(driver, "saveOrder();", "save order"):
        _order_scope(driver, "runInAngular(s, () => s.saveOrder()); return true;")
    deadline = time.monotonic() + 120
    stable_complete_checks = 0
    last = {}
    while time.monotonic() < deadline:
        time.sleep(1)
        error_text = _red_error_text(driver)
        if error_text:
            raise ManualReviewRequired(f"Save blocked by CRM error popup: {error_text}")
        try:
            summary = _order_scope(driver, "return {saving: !!s.saving, editMode: !!s.editMode, id: String(r.id || '')};")
        except Exception:
            summary = {}
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
            return last
    raise ProductSeparatorError(f"Order save did not complete. Last state: {last}")


def _append_production_notes(driver, notes):
    clean_notes = [_clean_text(note) for note in notes if _clean_text(note)]
    if not clean_notes:
        return {"notes_added": []}
    result = driver.execute_script(
        """
        const notes = arguments[0];
        function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
        const textareas = Array.from(document.querySelectorAll('textarea'));
        let target = null;
        for (const area of textareas) {
          let container = area;
          for (let hops = 0; container && hops < 5; container = container.parentElement, hops++) {
            if (/Production Notes/i.test(container.innerText || container.textContent || '')) {
              target = area;
              break;
            }
          }
          if (target) break;
        }
        if (!target && textareas.length === 1) target = textareas[0];
        if (!target) return {success: false, message: 'Production Notes textarea was not found.'};
        const existing = String(target.value || '').trim();
        const additions = notes.filter((note) => !existing.toLowerCase().includes(note.toLowerCase()));
        target.value = [existing].concat(additions).filter(Boolean).join('\\n');
        target.dispatchEvent(new Event('input', {bubbles: true}));
        target.dispatchEvent(new Event('change', {bubbles: true}));
        return {success: true, notes_added: additions, value: target.value};
        """,
        clean_notes,
    )
    if not isinstance(result, dict) or not result.get("success"):
        raise ProductSeparatorError((result or {}).get("message") or "Could not update Production Notes.")
    time.sleep(0.5)
    return result


def _apply_stock_ordered_status(driver):
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
        const bodyText = clean(document.body ? document.body.innerText : '');
        if (/Stock Status:\\s*(?:Stock\\s+)?Ordered/i.test(bodyText)) {
          return {already_applied: true, clicked_apply: false, confirmation: 'header'};
        }
        if (/\\bStock\\s*:\\s*Ordered\\b/i.test(bodyText) || /\\bOrdered Stock\\b/i.test(bodyText)) {
          return {already_applied: true, clicked_apply: false, confirmation: 'stock_history'};
        }
        const inputs = Array.from(document.querySelectorAll('input[type=text], input:not([type]), textarea')).filter(visible);
        const statusInput = inputs.find((input) => input.getAttribute('ng-model') === 'orderStatusName') || inputs.find((input) => {
          const rect = input.getBoundingClientRect();
          const nearby = Array.from(document.querySelectorAll('body *')).some((el) => {
            if (!visible(el)) return false;
            const text = clean(el.innerText || el.textContent);
            if (!/Stock Status:/i.test(text)) return false;
            const other = el.getBoundingClientRect();
            return Math.abs(other.top - rect.top) < 140 && Math.abs(other.left - rect.left) < 700;
          });
          return nearby || rect.top < 350;
        }) || inputs[0];
        if (!statusInput) return {success: false, message: 'Stock status input was not found.'};

        statusInput.scrollIntoView({block: 'center', inline: 'center'});
        statusInput.focus();
        statusInput.value = 'stock ordered';
        emit(statusInput, 'input');
        emit(statusInput, 'change');
        statusInput.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'd'}));
        statusInput.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'ArrowDown'}));
        statusInput.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'ArrowDown'}));
        return {success: true, typed: true};
        """
    )
    if isinstance(result, dict) and result.get("already_applied"):
        return {
            "status_applied": False,
            "already_applied": True,
            "confirmation": result.get("confirmation") or "header",
        }
    if not isinstance(result, dict) or not result.get("success"):
        raise ProductSeparatorError((result or {}).get("message") or "Could not type Stock Ordered status.")

    time.sleep(1)
    driver.execute_script(
        """
        function clean(value) { return String(value || '').replace(/\\s+/g, ' ').trim(); }
        function visible(el) {
          if (!el) return false;
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        const option = Array.from(document.querySelectorAll('li,a,button,div,span'))
          .filter(visible)
          .find((el) => clean(el.innerText || el.textContent).toLowerCase() === 'stock ordered');
        if (option) option.click();
        """
    )
    time.sleep(0.3)
    clicked = bool(
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
    if not clicked:
        raise ProductSeparatorError("Stock Ordered apply button was not found.")

    deadline = time.monotonic() + 30
    last_text = ""
    while time.monotonic() < deadline:
        time.sleep(0.5)
        last_text = _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))
        if re.search(r"Order's status updated to include\\s+Stock Ordered", last_text, flags=re.IGNORECASE):
            return {"status_applied": True, "confirmation": "green_popup"}
        if re.search(r"Stock Status:\\s*(?:Stock\\s+)?Ordered", last_text, flags=re.IGNORECASE):
            return {"status_applied": True, "confirmation": "header"}
    raise ProductSeparatorError(f"Stock Ordered status did not confirm. Visible text starts: {last_text[:300]}")


def _duplicate_design_from_index(driver, source_index, new_po):
    result = _order_scope(
        driver,
        r"""
        const sourceIndex = Number(arguments[0]);
        const newPo = String(arguments[1] || '');
        const designs = r.designs || [];
        if (!designs[sourceIndex]) {
          throw new Error('Source design index not found: ' + sourceIndex);
        }
        runInAngular(s, () => {
          s.duplicateDesign(sourceIndex);
          const newIndex = (r.designs || []).length - 1;
          const design = r.designs[newIndex];
          design.PO = newPo;
          design.crudAction = design.crudAction || 'c';
          if (typeof s.watchDesignChanges === 'function') s.watchDesignChanges(design, newIndex);
          if (typeof s.onDesignTabSelect === 'function') s.onDesignTabSelect(newIndex);
          else if (typeof s.changeDesignTab === 'function') s.changeDesignTab(newIndex);
        });
        const newIndex = (r.designs || []).length - 1;
        const design = r.designs[newIndex];
        return {
          new_index: newIndex,
          tab_number: newIndex + 1,
          tab_name: design.PO || '',
          item_count: (design.items || []).length
        };
        """,
        int(source_index),
        str(new_po),
    )
    time.sleep(0.8)
    return result


def _keep_only_group_on_design(driver, design_index, keep_group):
    result = _order_scope(
        driver,
        r"""
        const designIndex = Number(arguments[0]);
        const keepGroup = String(arguments[1] || '');
        const designs = r.designs || [];
        const design = designs[designIndex];
        if (!design) throw new Error('Design index not found: ' + designIndex);

        function clean(value) {
          return String(value || '').replace(/\s+/g, ' ').trim();
        }
        function classify(item) {
          const sizeText = (item.sizes || []).map((size) => `${size.sizeCode || ''} ${size.sizeType || ''}`).join(' ');
          const text = clean([
            item.style,
            item.ourLabel,
            item.label,
            item.note,
            item.internalNote,
            sizeText
          ].join(' ')).toLowerCase();
          const compact = text.replace(/\s+/g, '');
          if (/\b(towel|rally towel|sport towel)\b/.test(text)) return 'towel';
          if (/\b(tote|bag|backpack|duffel|drawstring)\b/.test(text)) return 'bag';
          if (/\b(hat|cap|beanie|snapback|trucker)\b/.test(text)) return 'hat_cap';
          if (/\b(toddler)\b/.test(text) || /\b(2t|3t|4t|5t|6t|7t)\b/.test(text) || text.includes('5/6')) return 'toddler';
          if (/\b(infant|baby|onesie|romper)\b/.test(text) || /\bnb\b/.test(text) || /(0-3mos|3-6mos|6-12mos|12-18mos|18-24mos)/.test(compact)) return 'infant';
          if (/\b(youth|kids|kid's|girls|boys)\b/.test(text) || /\b(yxs|ys|ym|yl|yxl|yxxl)\b/.test(text)) return 'youth';
          return 'adult_general';
        }
        function itemName(item) {
          return clean([item.style, item.ourLabel || item.label].filter(Boolean).join(' '));
        }

        const before = (design.items || []).map((item, index) => ({
          index,
          id: item.id || null,
          name: itemName(item),
          group: classify(item),
          crudAction: item.crudAction || ''
        }));

        runInAngular(s, () => {
          const removeMatchesFrom = (items, removedItem) => {
            (items || []).forEach((candidate) => {
              const sameId = removedItem.id && candidate.id && String(candidate.id) === String(removedItem.id);
              const sameStyle = !removedItem.id && clean(candidate.style) === clean(removedItem.style) && clean(candidate.ourLabel || candidate.label) === clean(removedItem.ourLabel || removedItem.label);
              if (candidate === removedItem || sameId || sameStyle) {
                candidate.crudAction = 'd';
              }
            });
          };
          (design.items || []).forEach((item, index) => {
            if (classify(item) !== keepGroup) {
              if (typeof s.removeDesignItem === 'function') {
                s.removeDesignItem(item, designIndex, index);
              } else {
                item.crudAction = 'd';
              }
              removeMatchesFrom(design.designItems, item);
            } else if (item.crudAction === 'd') {
              item.crudAction = item.id ? 'u' : 'c';
            }
          });
          (design.designItems || []).forEach((item) => {
            if (classify(item) !== keepGroup) item.crudAction = 'd';
          });
        });

        const after = (design.items || []).map((item, index) => ({
          index,
          id: item.id || null,
          name: itemName(item),
          group: classify(item),
          crudAction: item.crudAction || '',
          active: item.crudAction !== 'd'
        }));
        const active = after.filter((item) => item.active);
        const bad = active.filter((item) => item.group !== keepGroup);
        return {
          design_index: designIndex,
          tab_number: designIndex + 1,
          keep_group: keepGroup,
          before,
          after,
          removed_product_names: after.filter((item) => !item.active).map((item) => item.name),
          remaining_product_names: active.map((item) => item.name),
          bad_product_names: bad.map((item) => item.name)
        };
        """,
        int(design_index),
        str(keep_group),
    )
    if result.get("bad_product_names"):
        raise ProductSeparatorError(
            f"Tab cleanup failed. Products from other groups remain: {result.get('bad_product_names')}"
        )
    if not result.get("remaining_product_names"):
        raise ProductSeparatorError("Tab cleanup removed all products from a tab.")
    time.sleep(0.5)
    return result


def _apply_live_split(driver, plan):
    _enter_edit_mode(driver)
    actions = []
    for split in plan.get("split_tabs") or []:
        source_tab_number = int(split.get("source_tab_number"))
        source_design_index = source_tab_number - 1
        assignments = split.get("assignments") or []
        clone_assignments = [item for item in assignments if item.get("source") == "clone"]
        assignment_design_indexes = {source_tab_number: source_design_index}
        for clone in clone_assignments:
            clone_result = _duplicate_design_from_index(driver, source_design_index, clone.get("tab_name"))
            expected_number = int(clone_result.get("tab_number"))
            clone["tab_number"] = expected_number
            assignment_design_indexes[expected_number] = int(clone_result.get("new_index"))
            actions.append(
                {
                    "action": "clone_tab",
                    "source_tab_number": source_tab_number,
                    "new_tab_number": expected_number,
                    "new_tab_name": clone.get("tab_name"),
                    "result": clone_result,
                }
            )

        for assignment in assignments:
            tab_number = int(assignment.get("tab_number"))
            design_index = assignment_design_indexes.get(tab_number)
            if design_index is None:
                raise ProductSeparatorError(f"Internal tab assignment missing for tab {tab_number}.")
            cleanup = _keep_only_group_on_design(driver, design_index, assignment.get("keep_group"))
            actions.append(
                {
                    "action": "cleanup_tab",
                    "tab_number": tab_number,
                    "tab_name": assignment.get("tab_name"),
                    "keep_group": assignment.get("keep_group"),
                    "keep_group_label": assignment.get("keep_group_label"),
                    **cleanup,
                }
            )

    notes_result = {}
    if plan.get("production_notes"):
        notes_result = _append_production_notes(driver, plan.get("production_notes"))
        actions.append({"action": "production_notes", **notes_result})

    save_state = _save_order_and_wait(driver)
    status_state = {}
    if plan.get("stock_ordered_for_all_affected_tabs"):
        status_state = _apply_stock_ordered_status(driver)
        actions.append({"action": "apply_stock_ordered_status", **status_state})
    return {"actions": actions, "save_state": save_state, "status_state": status_state}


def _tabs_still_needing_split(scan):
    return [tab for tab in (scan.get("tabs") or []) if tab.get("needs_split")]


def _format_remaining_split_tabs(tabs):
    parts = []
    for tab in tabs:
        tab_number = tab.get("tab_number")
        tab_name = tab.get("tab_name") or ""
        groups = [
            group.get("group_label") or group.get("group")
            for group in (tab.get("groups") or [])
            if group.get("group_label") or group.get("group")
        ]
        suffix = f" ({', '.join(groups)})" if groups else ""
        parts.append(f"tab {tab_number} {tab_name}{suffix}".strip())
    return "; ".join(parts)


def run_product_separator_order(
    order_id=None,
    order_url=None,
    dry_run=True,
    visible=False,
    login_wait_seconds=0,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    profile_dir=None,
    kill_existing_profile=True,
    result_file=None,
):
    started = time.monotonic()
    resolved_order_id = _extract_order_id(order_id=order_id, order_url=order_url)
    target_url = _order_url(order_id=order_id, order_url=order_url)
    if not target_url:
        _write_result(False, "Order ID or CRM order URL is required.", result_file=result_file, action="product_separator_order")
        return 2

    driver = None
    profile = None
    try:
        driver, profile = _build_driver(
            visible=visible,
            attach_browser=attach_browser,
            debugger_address=debugger_address,
            profile_dir=profile_dir,
            kill_existing=kill_existing_profile,
        )
        safe_get_with_partial_load(driver, target_url, "Product Separator order")
        _handle_login_if_needed(driver, target_url, login_wait_seconds=login_wait_seconds)
        _wait_for_crm_context(driver)
        _recover_not_authenticated_page(driver, target_url, login_wait_seconds=login_wait_seconds)
        scan = _scan_order(driver, expected_order_id=resolved_order_id)
        plan = _build_separator_plan(scan)
        report = {"scan": scan, "plan": plan}

        if not plan.get("needs_split"):
            _write_result(
                True,
                f"Product Separator skipped order {resolved_order_id}: no mixed product tabs detected.",
                result_file=result_file,
                action="product_separator_order",
                dry_run=bool(dry_run),
                target_order_id=resolved_order_id,
                order_url=target_url,
                report=report,
                resolution="skipped_no_split_needed",
                duration_seconds=round(time.monotonic() - started, 2),
            )
            return 0

        if plan.get("manual_review_required"):
            _write_result(
                False,
                f"Product Separator requires manual review for order {resolved_order_id}.",
                result_file=result_file,
                action="product_separator_order",
                dry_run=bool(dry_run),
                target_order_id=resolved_order_id,
                order_url=target_url,
                report=report,
                manual_review_required=True,
                resolution="manual_review",
                duration_seconds=round(time.monotonic() - started, 2),
            )
            return 4

        if dry_run:
            _write_result(
                True,
                f"Product Separator dry run complete for order {resolved_order_id}. No CRM changes were made.",
                result_file=result_file,
                action="product_separator_order",
                dry_run=True,
                target_order_id=resolved_order_id,
                order_url=target_url,
                report=report,
                manual_review_required=False,
                resolution="dry_run_ready",
                duration_seconds=round(time.monotonic() - started, 2),
            )
            return 0

        live = _apply_live_split(driver, plan)
        safe_get_with_partial_load(driver, target_url, "Product Separator verification")
        _handle_login_if_needed(driver, target_url, login_wait_seconds=login_wait_seconds)
        _wait_for_crm_context(driver)
        _recover_not_authenticated_page(driver, target_url, login_wait_seconds=login_wait_seconds)
        scan_after = _scan_order(driver, expected_order_id=resolved_order_id)
        report["live"] = live
        report["scan_after"] = scan_after
        remaining_split_tabs = _tabs_still_needing_split(scan_after)
        if remaining_split_tabs:
            _write_result(
                False,
                (
                    f"Product Separator verification failed for order {resolved_order_id}: "
                    f"mixed product tabs still remain after save: {_format_remaining_split_tabs(remaining_split_tabs)}"
                ),
                result_file=result_file,
                action="product_separator_order",
                dry_run=False,
                target_order_id=resolved_order_id,
                order_url=target_url,
                report=report,
                manual_review_required=True,
                resolution="split_verification_failed",
                duration_seconds=round(time.monotonic() - started, 2),
            )
            return 4
        _write_result(
            True,
            f"Product Separator completed order {resolved_order_id}.",
            result_file=result_file,
            action="product_separator_order",
            dry_run=False,
            target_order_id=resolved_order_id,
            order_url=target_url,
            report=report,
            manual_review_required=False,
            resolution="split_complete",
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 0
    except ManualReviewRequired as err:
        if driver is not None:
            safe_take_screenshot(driver, "product_separator_manual_review")
        _write_result(
            False,
            str(err),
            result_file=result_file,
            action="product_separator_order",
            dry_run=bool(dry_run),
            target_order_id=resolved_order_id,
            order_url=target_url,
            error_type=type(err).__name__,
            manual_review_required=True,
            resolution="manual_review",
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 4
    except Exception as err:
        if driver is not None:
            safe_take_screenshot(driver, "product_separator_error")
        _write_result(
            False,
            f"Product Separator failed for order {resolved_order_id or target_url}: {err}",
            result_file=result_file,
            action="product_separator_order",
            dry_run=bool(dry_run),
            target_order_id=resolved_order_id,
            order_url=target_url,
            error_type=type(err).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 1
    finally:
        if not attach_browser:
            safe_driver_quit(driver, profile_path=profile)


def _read_worker_result(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as err:
        return {"success": False, "message": f"Could not read worker result: {err}", "result_file": path}


def _is_retryable_product_separator_payload(payload):
    if not isinstance(payload, dict) or payload.get("success"):
        return False
    text = " ".join(
        str(payload.get(key) or "")
        for key in ("message", "error_type", "resolution")
    ).lower()
    return any(
        phrase in text
        for phrase in (
            "crm app did not become ready",
            "not authenticated",
            "chrome not reachable",
            "disconnected",
            "timeout",
            "timed out",
        )
    )


def _run_order_dry_worker(order_id, profile_dir, result_dir, visible=False, login_wait_seconds=0):
    result_file = os.path.join(result_dir, f"{order_id}.json")
    exit_code = run_product_separator_order(
        order_id=order_id,
        dry_run=True,
        visible=visible,
        login_wait_seconds=login_wait_seconds,
        profile_dir=profile_dir,
        kill_existing_profile=False,
        result_file=result_file,
    )
    payload = _read_worker_result(result_file)
    payload["exit_code"] = exit_code
    payload["result_file"] = result_file
    return payload


def _run_order_chunk_worker(order_ids, profile_dir, result_dir, visible=False, login_wait_seconds=0):
    results = []
    for order_id in order_ids:
        payload = _run_order_dry_worker(order_id, profile_dir, result_dir, visible, login_wait_seconds)
        if _is_retryable_product_separator_payload(payload):
            retry_message = payload.get("message")
            print(f"Retrying Product Separator dry run for order {order_id} after transient CRM load error: {retry_message}")
            payload = _run_order_dry_worker(order_id, profile_dir, result_dir, visible, login_wait_seconds)
            payload["retried_after_transient_error"] = True
            payload["first_attempt_message"] = retry_message
        results.append(payload)
    return results


def _product_separator_list_url_for_mode(list_mode):
    mode = _clean_text(list_mode or PRODUCT_SEPARATOR_DEFAULT_LIST_MODE).lower() or "all"
    urls = {
        "free": PRODUCT_SEPARATOR_LIST_URL_FREE,
        "rush": PRODUCT_SEPARATOR_LIST_URL_RUSH,
        "all": PRODUCT_SEPARATOR_LIST_URL_ALL or PRODUCT_SEPARATOR_LIST_URL,
    }
    if mode not in urls:
        raise ProductSeparatorError(f"Unknown Product Separator list mode: {list_mode}")
    return mode, urls.get(mode) or ""


def run_product_separator_list(
    list_url=None,
    list_mode=None,
    dry_run=True,
    visible=False,
    login_wait_seconds=0,
    workers=4,
    max_orders=0,
    result_file=None,
):
    started = time.monotonic()
    resolved_list_mode, configured_list_url = _product_separator_list_url_for_mode(list_mode)
    list_url = list_url or configured_list_url
    if not list_url:
        _write_result(
            False,
            f"CRM report/list URL is required for Product Separator mode: {resolved_list_mode}.",
            result_file=result_file,
            action="product_separator_list",
            list_mode=resolved_list_mode,
        )
        return 2
    if not dry_run:
        _write_result(
            False,
            "Live list mode is disabled. Run dry-run first, then live selected order IDs intentionally.",
            result_file=result_file,
            action="product_separator_list",
            list_mode=resolved_list_mode,
            list_url=list_url,
        )
        return 3

    driver = None
    profile = None
    worker_profile_dir = None
    result_dir = os.path.join(PROJECT_ROOT, "product_separator_results", "list_scan_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(result_dir, exist_ok=True)
    try:
        driver, profile = _build_driver(visible=visible, profile_dir=None, kill_existing=True)
        order_ids = _extract_report_order_ids(driver, list_url, login_wait_seconds=login_wait_seconds)
    except Exception as err:
        if driver is not None:
            safe_take_screenshot(driver, "product_separator_list_error")
        _write_result(
            False,
            f"Product Separator list scan failed while reading report: {err}",
            result_file=result_file,
            action="product_separator_list",
            dry_run=True,
            list_mode=resolved_list_mode,
            list_url=list_url,
            error_type=type(err).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 1
    finally:
        safe_driver_quit(driver, profile_path=profile)

    if max_orders and int(max_orders) > 0:
        order_ids = order_ids[: int(max_orders)]

    worker_count = max(1, min(int(workers or 1), len(order_ids) or 1))
    try:
        worker_profile_dir, profile_dirs = _prepare_worker_profiles(worker_count)
    except Exception as err:
        _write_result(
            False,
            f"Product Separator list scan failed while preparing worker profiles: {err}",
            result_file=result_file,
            action="product_separator_list",
            dry_run=True,
            list_mode=resolved_list_mode,
            list_url=list_url,
            order_count=len(order_ids),
            order_ids=order_ids,
            error_type=type(err).__name__,
            duration_seconds=round(time.monotonic() - started, 2),
        )
        return 1

    order_results = []
    split_order_ids = []
    skipped_order_ids = []
    manual_review_order_ids = []
    failed_order_ids = []

    chunks = [[] for _ in range(worker_count)]
    for index, order_id in enumerate(order_ids):
        chunks[index % worker_count].append(order_id)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_run_order_chunk_worker, chunk, profile_dirs[index], result_dir, bool(visible), login_wait_seconds): chunk
            for index, chunk in enumerate(chunks)
            if chunk
        }
        for future in as_completed(futures):
            try:
                payloads = future.result()
            except Exception as err:
                payloads = [
                    {
                        "success": False,
                        "message": str(err),
                        "target_order_id": order_id,
                        "error_type": type(err).__name__,
                        "resolution": "worker_exception",
                    }
                    for order_id in futures[future]
                ]
            for payload in payloads:
                order_id = str(payload.get("target_order_id") or payload.get("order_id") or "")
                plan = ((payload.get("report") or {}).get("plan") or {})
                summary = {
                    "order_id": order_id,
                    "success": bool(payload.get("success")),
                    "exit_code": payload.get("exit_code"),
                    "resolution": payload.get("resolution"),
                    "message": payload.get("message"),
                    "result_file": payload.get("result_file"),
                    "needs_split": bool(plan.get("needs_split")),
                    "manual_review_required": bool(payload.get("manual_review_required") or plan.get("manual_review_required")),
                    "split_tabs": plan.get("split_tabs") or [],
                    "production_notes": plan.get("production_notes") or [],
                }
                order_results.append(summary)
                if summary["needs_split"] and summary["success"] and not summary["manual_review_required"]:
                    split_order_ids.append(order_id)
                elif summary["manual_review_required"]:
                    manual_review_order_ids.append(order_id)
                elif summary["success"]:
                    skipped_order_ids.append(order_id)
                else:
                    failed_order_ids.append(order_id)
                print(
                    f"[{len(order_results)}/{len(order_ids)}] {order_id}: "
                    f"{summary['resolution']} split={summary['needs_split']} manual={summary['manual_review_required']}"
                )

    order_results.sort(key=lambda item: order_ids.index(item["order_id"]) if item["order_id"] in order_ids else 999999)
    success = not failed_order_ids and not manual_review_order_ids
    _write_result(
        success,
        (
            f"Product Separator list dry run complete. "
            f"{len(split_order_ids)} order(s) need splitting, {len(skipped_order_ids)} already okay."
        ),
        result_file=result_file,
        action="product_separator_list",
        dry_run=True,
        list_mode=resolved_list_mode,
        list_url=list_url,
        workers=worker_count,
        order_count=len(order_ids),
        order_ids=order_ids,
        split_order_ids=split_order_ids,
        skipped_order_ids=skipped_order_ids,
        manual_review_order_ids=manual_review_order_ids,
        failed_order_ids=failed_order_ids,
        result_dir=result_dir,
        worker_profile_dir=worker_profile_dir,
        worker_profile_dir_cleaned=True,
        report=order_results,
        duration_seconds=round(time.monotonic() - started, 2),
    )
    if worker_profile_dir and os.path.isdir(worker_profile_dir):
        shutil.rmtree(worker_profile_dir, ignore_errors=True)
    return 0 if success else 4


def main(argv=None):
    parser = argparse.ArgumentParser(description="CRM Product Separator worker.")
    parser.add_argument("--action", choices=["product_separator_order", "product_separator_list"], default="product_separator_order")
    parser.add_argument("--order-id", default="")
    parser.add_argument("--order-url", default="")
    parser.add_argument("--list-url", default="")
    parser.add_argument("--list-mode", choices=["free", "rush", "all"], default=PRODUCT_SEPARATOR_DEFAULT_LIST_MODE)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-orders", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true", default=PROCESSOR_DRY_RUN)
    parser.add_argument("--real", action="store_true", help="Use live mode.")
    parser.add_argument("--visible", action="store_true", help="Force visible Chrome even if config enables headless mode.")
    parser.add_argument("--login-wait-seconds", type=int, default=0)
    parser.add_argument("--attach-browser", action="store_true", help="Attach to Chrome already opened by open_crm_profile.command.")
    parser.add_argument("--debugger-address", default="127.0.0.1:9222")
    parser.add_argument("--result-file", default=RESULT_FILE)
    args = parser.parse_args(argv)

    dry_run = bool(args.dry_run and not args.real)
    if args.action == "product_separator_list":
        return run_product_separator_list(
            list_url=args.list_url,
            list_mode=args.list_mode,
            dry_run=dry_run,
            visible=args.visible,
            login_wait_seconds=args.login_wait_seconds,
            workers=args.workers,
            max_orders=args.max_orders,
            result_file=args.result_file,
        )
    return run_product_separator_order(
        order_id=args.order_id,
        order_url=args.order_url,
        dry_run=dry_run,
        visible=args.visible,
        login_wait_seconds=args.login_wait_seconds,
        attach_browser=args.attach_browser,
        debugger_address=args.debugger_address,
        result_file=args.result_file,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
