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
    refresh_if_crm_challenge_attempts_exceeded,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
from config import (
    CRM_SHIPPING_813_URL,
    PROCESSOR_ACTION_TIMEOUT,
    PROCESSOR_DRY_RUN,
    PROCESSOR_HEADLESS,
    PROCESSOR_ORDER_URL_TEMPLATE,
    PROCESSOR_PAGE_LOAD_TIMEOUT,
    PROCESSOR_PROFILE_DIR,
    PRODUCT_SEPARATOR_DEFAULT_LIST_MODE,
    PRODUCT_SEPARATOR_LIST_URL,
    PRODUCT_SEPARATOR_LIST_URL_813,
    PRODUCT_SEPARATOR_LIST_URL_ALL,
    PRODUCT_SEPARATOR_LIST_URL_FREE,
    PRODUCT_SEPARATOR_LIST_URL_RUSH,
)
import crm_shipping_bypasser as _shipping_bypasser
import crm_order_goods as _order_goods
from crm_shipping_bypasser import (
    _manual_order_vendor_label,
    _record_crm_manual_order as _record_crm_stock_manual_order,
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


MANUAL_ORDER_KNOWN_VENDOR_PATTERN = (
    r"S\s*&\s*S\s+Activewear"
    r"|S\s+and\s+S\s+Activewear"
    r"|SS\s*Activewear"
    r"|Sanmar"
    r"|Local\s+Inventory"
    r"|AS\s+COLOUR"
    r"|Atlantic\s+Coast\s+Cotton"
    r"|AUGUSTA\s+SPORTSWEAR"
)


def _looks_like_manual_order_po(value):
    text = _clean_text(value)
    if not text or len(text) > 90:
        return False
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", text):
        return False
    return bool(re.search(r"[A-Za-z]", text) and re.fullmatch(r"[A-Za-z0-9._/-]+", text))


def _manual_order_row_from_tokens(tokens):
    values = [_clean_text(token) for token in tokens if _clean_text(token)]
    if len(values) < 3:
        return None
    header_words = {"po", "vendor order #", "order date", "est. delivery", "shipped to"}
    for start in range(0, max(1, len(values) - 2)):
        vendor = values[start]
        po = values[start + 1] if start + 1 < len(values) else ""
        third = values[start + 2] if start + 2 < len(values) else ""
        fourth = values[start + 3] if start + 3 < len(values) else ""
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", third):
            vendor_order = ""
            order_date = third
        else:
            vendor_order = third
            order_date = fourth
        if vendor.lower() in header_words:
            continue
        if not _looks_like_manual_order_po(po):
            continue
        if vendor_order and not re.fullmatch(r"[A-Za-z0-9-]{2,40}", vendor_order):
            continue
        if not re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", order_date):
            continue
        return {
            "vendor": _manual_order_vendor_label(vendor),
            "po": po,
            "vendor_order_number": vendor_order,
            "order_date": order_date,
        }
    return None


def _manual_order_rows_from_text(text):
    raw_text = str(text or "").replace("\r", "\n")
    rows = []
    for line in raw_text.splitlines():
        if not re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", line):
            continue
        token_sets = []
        if "\t" in line:
            token_sets.append(re.split(r"\t+", line))
        token_sets.append(re.split(r"\s{2,}", line))
        for tokens in token_sets:
            row = _manual_order_row_from_tokens(tokens)
            if row:
                rows.append(row)
                break

    collapsed = _clean_text(raw_text)
    known_vendor = re.compile(
        rf"\b(?P<vendor>{MANUAL_ORDER_KNOWN_VENDOR_PATTERN})\b\s+"
        r"(?P<po>[A-Za-z0-9][A-Za-z0-9._/-]{1,89})\s+"
        r"(?:(?P<vendor_order>[A-Za-z0-9-]{2,40})\s+)?"
        r"(?P<order_date>\d{1,2}/\d{1,2}/\d{2,4})",
        flags=re.IGNORECASE,
    )
    for match in known_vendor.finditer(collapsed):
        po = _clean_text(match.group("po"))
        if not _looks_like_manual_order_po(po):
            continue
        rows.append(
            {
                "vendor": _manual_order_vendor_label(match.group("vendor")),
                "po": po,
                "vendor_order_number": _clean_text(match.group("vendor_order") or ""),
                "order_date": _clean_text(match.group("order_date")),
            }
        )

    unique_rows = []
    seen = set()
    for row in rows:
        key = (_clean_text(row.get("vendor")).lower(), _clean_text(row.get("po")).lower())
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


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
            if refresh_if_crm_challenge_attempts_exceeded(driver, "Product Separator CRM context"):
                last_error = "refreshed CRM challenge page"
                continue
            _activate_crm_context(driver)
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
  const totalQuantityMatch = text.match(/\bTotal Quantity\s*:?\s*(\d+)/i);
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
    total_quantity: totalQuantityMatch ? Number(totalQuantityMatch[1]) : null,
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
    if not isinstance(products, list):
        return []
    return [product for product in products if _product_has_positive_quantity(product)]


def _product_has_positive_quantity(product):
    if not isinstance(product, dict):
        return False
    if "total_quantity" not in product or product.get("total_quantity") in (None, ""):
        return True
    try:
        return int(product.get("total_quantity") or 0) > 0
    except (TypeError, ValueError):
        return True


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
    manual_order_rows = _manual_order_rows_from_text(text)
    primary_manual_order = manual_order_rows[0] if manual_order_rows else {}
    false_value = r"(?:false|no|not\s+ordered|unordered|0)"
    true_value = r"(?:ordered|true|yes|1)"
    stock_ordered_false = bool(re.search(rf"\bstock\s+ordered\s*[:=]\s*{false_value}\b", normalized))
    stock_status_ordered = (
        not stock_ordered_false
        and (
            bool(re.search(rf"\bstock\s+status\s*[:=]\s*{true_value}\b", normalized))
            or bool(re.search(rf"\bstock\s*[:=]\s*{true_value}\b", normalized))
            or bool(re.search(rf"\bstock\s+ordered\s*[:=]\s*{true_value}\b", normalized))
        )
    )
    has_vendor_section = "order goods from vendor" in normalized
    has_yellow_po = bool(manual_order_rows) or bool(re.search(r"\bmanual order\b.*\blocal inventory\b.*\bvendor order\b", normalized))
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
        "manual_order_vendor": primary_manual_order.get("vendor", ""),
        "manual_order_po": primary_manual_order.get("po", ""),
        "manual_order_rows": manual_order_rows,
        "order_goods_button_missing_or_not_detected": missing_order_goods,
    }


def _order_stock_status_from_text(text):
    body_text = _clean_text(text)
    match = re.search(
        r"\bstock\s+status\s*:\s*(?:stock\s+)?(?P<status>ordered|needs?\s+to\s+order|not\s+ordered|unordered)\b",
        body_text,
        flags=re.IGNORECASE,
    )
    if not match:
        return {
            "state": "unknown",
            "status_text": "",
            "stock_status_ordered": False,
            "stock_status_needs_order": False,
        }
    status_text = _clean_text(match.group("status"))
    normalized = status_text.lower()
    if normalized == "ordered":
        state = "ordered"
    elif re.fullmatch(r"needs?\s+to\s+order|not\s+ordered|unordered", normalized):
        state = "need_to_order"
    else:
        state = "unknown"
    return {
        "state": state,
        "status_text": status_text,
        "stock_status_ordered": state == "ordered",
        "stock_status_needs_order": state == "need_to_order",
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
        "order_stock_status": _order_stock_status_from_text(text),
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
    initial_body_text = ""
    while time.monotonic() < deadline:
        if _is_not_authenticated_page(driver):
            raise ProductSeparatorError("CRM authentication failed: Not authenticated.")
        initial_body_text = _read_clean_body_text(driver)
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
        "order_stock_status": _order_stock_status_from_text(initial_body_text),
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
  const limeGreen = closeRgb(rgb, [34, 236, 72], 18);
  return purple || tanNatural || limeGreen;
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


def _extract_report_order_ids(driver, list_url, login_wait_seconds=0, exclude_order_ids=None):
    safe_get_with_partial_load(driver, list_url, "Product Separator report")
    _handle_login_if_needed(driver, list_url, login_wait_seconds=login_wait_seconds)
    excluded = {str(order_id).strip() for order_id in (exclude_order_ids or []) if str(order_id).strip()}
    deadline = time.monotonic() + max(45, PROCESSOR_PAGE_LOAD_TIMEOUT)
    order_ids = []
    while time.monotonic() < deadline:
        try:
            order_ids = driver.execute_script(REPORT_ORDER_IDS_JS) or []
        except Exception:
            order_ids = []
        order_ids = [str(order_id) for order_id in order_ids if re.fullmatch(r"\d{5,}", str(order_id or ""))]
        if excluded:
            order_ids = [order_id for order_id in order_ids if order_id not in excluded]
        if order_ids:
            break
        time.sleep(1)
    if not order_ids:
        return []
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


def _source_manual_order_po(source_stock_state, source_tab_name):
    stock_po = _clean_text((source_stock_state or {}).get("manual_order_po"))
    if stock_po:
        return stock_po
    return _clean_text(source_tab_name)


def _is_contiguous(values):
    if not values:
        return False
    sorted_values = sorted(values)
    return sorted_values == list(range(sorted_values[0], sorted_values[-1] + 1))


def _product_signature_quantity(product):
    quantity = product.get("total_quantity")
    if quantity in (None, ""):
        quantity = product.get("quantity")
    try:
        return int(quantity)
    except (TypeError, ValueError):
        return None


def _product_group_signature(products):
    signature = []
    for product in products or []:
        quantity = _product_signature_quantity(product)
        signature.append(
            (
                _clean_text(product.get("product_name")).lower(),
                _clean_text(product.get("color")).lower(),
                "" if quantity is None else quantity,
            )
        )
    return tuple(sorted(signature))


def _find_existing_split_tab_for_group(tabs, source_tab, group, group_products, claimed_tab_numbers=None):
    source_tab_number = int(source_tab.get("tab_number") or 0)
    wanted_signature = _product_group_signature(group_products)
    if not wanted_signature:
        return None
    claimed_tab_numbers = set(claimed_tab_numbers or [])
    for candidate in tabs or []:
        candidate_tab_number = int(candidate.get("tab_number") or 0)
        if not candidate_tab_number or candidate_tab_number == source_tab_number:
            continue
        if candidate_tab_number in claimed_tab_numbers:
            continue
        if candidate.get("needs_split"):
            continue
        candidate_groups = [
            _clean_text(item.get("group"))
            for item in (candidate.get("groups") or [])
            if _clean_text(item.get("group"))
        ]
        if candidate_groups != [group]:
            continue
        candidate_products = [
            product
            for product in (candidate.get("products") or [])
            if _clean_text(product.get("group")) == group
        ]
        if _product_group_signature(candidate_products) == wanted_signature:
            return candidate
    return None


def _stock_state_is_ordered(stock_state):
    state = str((stock_state or {}).get("state") or "")
    return state in {"ordered", "ordered_header_only", "ordered_po_only"} or bool((stock_state or {}).get("stock_status_ordered"))


def _is_local_inventory_vendor(vendor):
    return _clean_text(vendor).lower().replace("-", " ") == "local inventory"


def _stock_state_has_local_inventory_row(stock_state):
    stock_state = stock_state or {}
    if _is_local_inventory_vendor(stock_state.get("manual_order_vendor")):
        return True
    return any(_is_local_inventory_vendor(row.get("vendor")) for row in stock_state.get("manual_order_rows") or [])


def _stock_state_should_auto_order_local_inventory(stock_state):
    return _stock_state_has_local_inventory_row(stock_state)


def _build_separator_plan(scan):
    tabs = scan.get("tabs") or []
    order_stock_status = scan.get("order_stock_status") if isinstance(scan.get("order_stock_status"), dict) else {}
    order_stock_status_state = str(order_stock_status.get("state") or "unknown")
    used_tab_names = {_clean_text(tab.get("tab_name")) for tab in tabs if _clean_text(tab.get("tab_name"))}
    max_tab_number = max([int(tab.get("tab_number") or 0) for tab in tabs] or [0])
    next_tab_number = max_tab_number + 1
    split_tabs = []
    manual_review = []
    production_notes = []
    manual_order_records = []
    local_inventory_auto_order_targets = []
    claimed_existing_tab_numbers = set()

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
        existing_tab_numbers = []
        for group in clone_groups:
            existing_tab = _find_existing_split_tab_for_group(
                tabs,
                tab,
                group,
                groups[group],
                claimed_tab_numbers=claimed_existing_tab_numbers,
            )
            if existing_tab:
                existing_tab_number = int(existing_tab.get("tab_number"))
                claimed_existing_tab_numbers.add(existing_tab_number)
                existing_tab_numbers.append(existing_tab_number)
                assignments.append(
                    {
                        "tab_number": existing_tab_number,
                        "tab_name": existing_tab.get("tab_name") or "",
                        "source": "existing",
                        "clone_from_tab_number": tab.get("tab_number"),
                        "keep_group": group,
                        "keep_group_label": GROUP_LABELS.get(group, group),
                        "keep_product_names": [item.get("product_name") for item in groups[group]],
                    }
                )
                continue
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

        source_stock_state = tab.get("stock") or {}
        source_stock_ordered = str(source_stock_state.get("state") or "") in {"ordered", "ordered_header_only", "ordered_po_only"}
        target_manual_order_assignments = [item for item in assignments if item.get("source") != "original"]
        source_manual_order_vendor = _clean_text(source_stock_state.get("manual_order_vendor"))
        source_manual_order_po = _source_manual_order_po(source_stock_state, original_name)
        split_manual_order_records = []
        split_local_inventory_targets = []
        if source_stock_ordered and target_manual_order_assignments:
            if _stock_state_should_auto_order_local_inventory(source_stock_state):
                for assignment in target_manual_order_assignments:
                    target = {
                        "source_tab_number": tab.get("tab_number"),
                        "source_tab_name": original_name,
                        "target_tab_number": assignment.get("tab_number"),
                        "target_tab_name": assignment.get("tab_name"),
                        "target_source": assignment.get("source"),
                        "reason": "Source tab is Local Inventory; auto-order the separated tab instead of copying a Manual Order row.",
                    }
                    split_local_inventory_targets.append(target)
                    local_inventory_auto_order_targets.append(target)
            elif not source_manual_order_po or not source_manual_order_vendor:
                manual_review.append(
                    {
                        "tab_number": tab.get("tab_number"),
                        "reason": "Source tab is stock ordered, but the ordered PO/vendor could not be detected for copied Manual Order records.",
                        "source_tab_name": original_name,
                        "detected_po": source_manual_order_po,
                        "detected_vendor": source_manual_order_vendor,
                        "source_stock_state": source_stock_state,
                    }
                )
            else:
                for assignment in target_manual_order_assignments:
                    record = {
                        "source_tab_number": tab.get("tab_number"),
                        "source_tab_name": original_name,
                        "target_tab_number": assignment.get("tab_number"),
                        "target_tab_name": assignment.get("tab_name"),
                        "target_source": assignment.get("source"),
                        "po": source_manual_order_po,
                        "vendor": source_manual_order_vendor,
                    }
                    split_manual_order_records.append(record)
                    manual_order_records.append(record)
        all_related_tabs = [int(tab.get("tab_number"))] + existing_tab_numbers + created_tab_numbers
        note = f"{_format_tab_list(all_related_tabs)} in 1 box"
        source_local_inventory = _stock_state_should_auto_order_local_inventory(source_stock_state)
        has_copied_manual_order_records = bool(split_manual_order_records)
        if created_tab_numbers and source_stock_ordered and not source_local_inventory and has_copied_manual_order_records:
            production_notes.append(note)
        split_tabs.append(
            {
                "source_tab_number": tab.get("tab_number"),
                "source_tab_name": original_name,
                "source_stock_state": source_stock_state,
                "source_stock_ordered": source_stock_ordered,
                "groups_detected": [
                    {
                        "group": group,
                        "group_label": GROUP_LABELS.get(group, group),
                        "product_names": [item.get("product_name") for item in items],
                    }
                    for group, items in groups.items()
                ],
                "assignments": assignments,
                "manual_order_records": split_manual_order_records,
                "local_inventory_auto_order_targets": split_local_inventory_targets,
                "production_note_if_stock_ordered": (
                    note
                    if created_tab_numbers
                    and source_stock_ordered
                    and not source_local_inventory
                    and has_copied_manual_order_records
                    else ""
                ),
            }
        )

    affected_stock_states = []
    for split in split_tabs:
        state = ((split.get("source_stock_state") or {}).get("state") or "not_ordered_or_unknown")
        affected_stock_states.append(state)
    has_ordered = any(state in {"ordered", "ordered_header_only", "ordered_po_only"} for state in affected_stock_states)
    has_not_ordered = any(state == "not_ordered_or_unknown" for state in affected_stock_states)
    mixed_stock_state = bool(has_ordered and has_not_ordered)
    stock_ordered_for_all_affected_tabs = bool(split_tabs and has_ordered and not has_not_ordered)
    apply_stock_ordered_after_split = False
    stock_ordered_apply_skip_reason = ""
    if mixed_stock_state:
        stock_ordered_apply_skip_reason = (
            "Affected split tabs have mixed stock-ordered state; "
            "do not apply Stock Ordered automatically."
        )
    elif order_stock_status_state == "need_to_order":
        stock_ordered_apply_skip_reason = (
            "Order Stock Status is Need To Order; do not apply Stock Ordered automatically."
        )
    elif local_inventory_auto_order_targets:
        stock_ordered_apply_skip_reason = (
            "Product Separator auto-orders Local Inventory separated tabs; "
            "do not apply Stock Ordered automatically."
        )
    elif stock_ordered_for_all_affected_tabs:
        stock_ordered_apply_skip_reason = (
            "Product Separator records copied Manual Order rows for separated stock-ordered tabs; "
            "do not apply Stock Ordered automatically."
        )

    return {
        "needs_split": bool(split_tabs),
        "split_tabs": split_tabs,
        "manual_review": manual_review,
        "manual_review_required": bool(manual_review),
        "stock_ordered_for_all_affected_tabs": stock_ordered_for_all_affected_tabs,
        "apply_stock_ordered_after_split": apply_stock_ordered_after_split,
        "stock_ordered_apply_skip_reason": stock_ordered_apply_skip_reason,
        "mixed_stock_state": mixed_stock_state,
        "order_stock_status_before_split": order_stock_status or _order_stock_status_from_text(""),
        "production_notes": production_notes,
        "manual_order_records": manual_order_records,
        "local_inventory_auto_order_targets": local_inventory_auto_order_targets,
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


def _body_text_confirms_stock_ordered(body_text):
    return bool(
        re.search(
            r"Stock Status:\s*(?:Stock\s+)?Ordered",
            _clean_text(body_text),
            flags=re.IGNORECASE,
        )
    )


def _read_clean_body_text(driver):
    return _clean_text(driver.execute_script("return document.body ? document.body.innerText : '';"))


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
        confirmation = result.get("confirmation") or "header"
        if confirmation != "header":
            raise ProductSeparatorError(
                f"Stock Ordered status was only confirmed by {confirmation}; current Stock Status was not confirmed."
            )
        return {
            "status_applied": False,
            "already_applied": True,
            "confirmation": confirmation,
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
        if re.search(r"Order's status updated to include\s+Stock Ordered", last_text, flags=re.IGNORECASE):
            return {"status_applied": True, "confirmation": "green_popup"}
        if _body_text_confirms_stock_ordered(last_text):
            return {"status_applied": True, "confirmation": "header"}
    try:
        driver.refresh()
        _wait_for_crm_context(driver)
        refresh_deadline = time.monotonic() + 15
        while time.monotonic() < refresh_deadline:
            time.sleep(0.5)
            last_text = _read_clean_body_text(driver)
            if _body_text_confirms_stock_ordered(last_text):
                return {"status_applied": True, "confirmation": "header_after_refresh"}
    except Exception:
        pass
    raise ProductSeparatorError(f"Stock Ordered status did not confirm after refresh. Visible text starts: {last_text[:300]}")


def _scan_confirms_stock_ordered_status(scan):
    tabs = scan.get("tabs") if isinstance(scan, dict) else []
    order_stock_status = scan.get("order_stock_status") if isinstance(scan, dict) else None
    if isinstance(order_stock_status, dict):
        state = str(order_stock_status.get("state") or "")
        if state in {"ordered", "need_to_order"}:
            return state == "ordered"
    if not tabs:
        return False
    return all(bool(((tab.get("stock") or {}).get("stock_status_ordered"))) for tab in tabs)


def _verify_stock_ordered_status_persisted(verification):
    for scan_key in ("scan_after_refresh", "scan_after"):
        scan = verification.get(scan_key) if isinstance(verification, dict) else None
        if scan and _scan_confirms_stock_ordered_status(scan):
            return {"stock_status_verified": True, "verification_scan": scan_key}
    return {"stock_status_verified": False, "verification_scan": ""}


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


def _zero_non_keep_group_quantity_inputs(driver, design_index, keep_group):
    _click_design_tab(driver, int(design_index) + 1)
    time.sleep(0.7)
    return driver.execute_script(
        r"""
        const keepGroup = String(arguments[0] || '');
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
        function visible(el) {
          if (!el) return false;
          const rect = el.getBoundingClientRect();
          const style = window.getComputedStyle ? window.getComputedStyle(el) : {};
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        function classifyText(text) {
          const normalized = clean(text).toLowerCase();
          const compact = normalized.replace(/\s+/g, '');
          if (/\b(towel|rally towel|sport towel)\b/.test(normalized)) return 'towel';
          if (/\b(tote|bag|backpack|duffel|drawstring)\b/.test(normalized)) return 'bag';
          if (/\b(hat|cap|beanie|snapback|trucker)\b/.test(normalized)) return 'hat_cap';
          if (/\b(toddler)\b/.test(normalized) || /\b(2t|3t|4t|5t|6t|7t)\b/.test(normalized) || normalized.includes('5/6')) return 'toddler';
          if (/\b(infant|baby|onesie|romper)\b/.test(normalized) || /\bnb\b/.test(normalized) || /(0-3mos|3-6mos|6-12mos|12-18mos|18-24mos)/.test(compact)) return 'infant';
          if (/\b(youth|kids|kid's|girls|boys)\b/.test(normalized) || /\b(yxs|ys|ym|yl|yxl|yxxl)\b/.test(normalized)) return 'youth';
          return 'adult_general';
        }
        function nearestProductBlock(link) {
          let best = null;
          for (let el = link; el && el !== document.body; el = el.parentElement) {
            const text = clean(el.innerText || el.textContent);
            if (/Size\s*:/i.test(text) && /Quantity\s*:/i.test(text) && /Price\s*:/i.test(text)) {
              best = el;
              if (text.length > 80 && text.length < 3500) break;
            }
          }
          return best;
        }
        function textNodes(block) {
          return Array.from(block.querySelectorAll('*'))
            .filter(visible)
            .map((el) => ({el, text: clean(el.innerText || el.textContent), rect: el.getBoundingClientRect()}));
        }
        function setInputValue(input, value) {
          const before = input.value;
          const proto = Object.getPrototypeOf(input);
          const descriptor = proto && Object.getOwnPropertyDescriptor(proto, 'value');
          input.scrollIntoView({block: 'center', inline: 'center'});
          input.focus();
          if (descriptor && descriptor.set) descriptor.set.call(input, value);
          else input.value = value;
          input.dispatchEvent(new Event('input', {bubbles: true}));
          input.dispatchEvent(new Event('change', {bubbles: true}));
          input.blur();
          return before;
        }
        const links = Array.from(document.querySelectorAll('a'))
          .filter(visible)
          .filter((a) => /\bAlpha(?: Stock)?\b/i.test(a.innerText || a.textContent || ''));
        const seen = new Set();
        const changed = [];
        const blocks = [];
        for (const link of links) {
          const block = nearestProductBlock(link);
          if (!block) continue;
          const rect = block.getBoundingClientRect();
          const linkText = clean(link.innerText || link.textContent);
          const key = `${Math.round(rect.top)}:${Math.round(rect.left)}:${linkText}`;
          if (seen.has(key)) continue;
          seen.add(key);
          const text = clean(block.innerText || block.textContent);
          const group = classifyText(text);
          blocks.push({group, text: text.slice(0, 160)});
          if (group === keepGroup) continue;
          const nodes = textNodes(block);
          const quantityLabel = nodes.find((item) => /^Quantity:?$/i.test(item.text));
          const priceLabel = nodes.find((item) => /^Price:?$/i.test(item.text));
          if (!quantityLabel || !priceLabel) continue;
          const qtyTop = quantityLabel.rect.top - 12;
          const qtyBottom = priceLabel.rect.top - 2;
          const inputs = Array.from(block.querySelectorAll('input'))
            .filter(visible)
            .filter((input) => {
              const type = String(input.type || '').toLowerCase();
              if (type === 'checkbox' || type === 'radio' || type === 'hidden') return false;
              const inputRect = input.getBoundingClientRect();
              return inputRect.top >= qtyTop && inputRect.top < qtyBottom;
            });
          for (const input of inputs) {
            const before = setInputValue(input, '0');
            changed.push({
              group,
              before,
              after: input.value,
              name: input.getAttribute('name') || '',
              ng_model: input.getAttribute('ng-model') || '',
              id: input.id || ''
            });
          }
        }
        return {changed_inputs: changed, product_blocks: blocks};
        """,
        str(keep_group),
    )


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
        const knownQuantityKeys = new Set([
          'quantity',
          'qty',
          'orderqty',
          'orderedqty',
          'quantityordered',
          'qtyordered',
          'stockorderquantity',
          'stockorderqty',
          'stockorderedquantity',
          'stockorderedqty',
          'totalquantity',
          'totalqty'
        ]);
        function numericValue(value) {
          if (typeof value === 'number') return Number.isFinite(value) ? value : null;
          const text = clean(value).replace(/,/g, '');
          if (!/^-?\d+(?:\.\d+)?$/.test(text)) return null;
          return Number(text);
        }
        function isQuantityKey(key) {
          const lower = String(key || '').toLowerCase();
          if (knownQuantityKeys.has(lower)) return true;
          if (/(price|cost|upcharge|id|code|type|name|size|sort|index|warehouse|available|inventory|sku|style|color)/.test(lower)) return false;
          return /(quantity|qty)/.test(lower);
        }
        function quantityKeys(target) {
          return Object.keys(target || {}).filter(isQuantityKey);
        }
        function sizeLabel(size) {
          return clean(size.sizeCode || size.size || size.label || size.name || size.sizeName || size.sizeType);
        }
        function quantityIndexObj(itemIndex, sizeIndex) {
          return {
            designKey: designIndex,
            designIndex: designIndex,
            itemKey: itemIndex,
            itemIndex: itemIndex,
            designItemKey: itemIndex,
            sizeKey: sizeIndex,
            sizeIndex: sizeIndex
          };
        }
        function notifyItemQuantityChanged(item, itemIndex) {
          const indexObj = quantityIndexObj(itemIndex, 0);
          if (typeof s.watchItemQuantityChanges === 'function') {
            s.watchItemQuantityChanges(item, indexObj);
          } else if (typeof s.watchItemChanges === 'function') {
            s.watchItemChanges(item);
          } else {
            markAsUpdated(item);
          }
        }
        function notifySizeQuantityChanged(item, size, itemIndex, sizeIndex) {
          const indexObj = quantityIndexObj(itemIndex, sizeIndex);
          if (typeof s.watchSizeQuantityChanges === 'function') {
            s.watchSizeQuantityChanges(item, size, indexObj);
          } else if (typeof s.watchSizeChanges === 'function') {
            s.watchSizeChanges(item, size);
          } else {
            markAsUpdated(item);
            markAsUpdated(size);
          }
        }
        function markDesignChanged() {
          markAsUpdated(design);
          design.$changed = true;
          const firstCost = (design.designCosts || [])[0];
          if (firstCost) markAsUpdated(firstCost);
        }
        function itemQuantity(item) {
          let found = false;
          let total = 0;
          for (const key of quantityKeys(item)) {
            const value = numericValue(item[key]);
            if (value === null) continue;
            found = true;
            total += value;
          }
          for (const size of (item.sizes || [])) {
            for (const key of quantityKeys(size)) {
              const value = numericValue(size[key]);
              if (value === null) continue;
              found = true;
              total += value;
            }
          }
          return found ? total : null;
        }
        function effectivelyActive(item) {
          if ((item.crudAction || '') === 'd') return false;
          const quantity = itemQuantity(item);
          return quantity === null || quantity > 0;
        }
        function markAsUpdated(item) {
          if ((item.crudAction || '') === 'd') {
            item.crudAction = item.id ? 'u' : 'c';
          } else if (!item.crudAction) {
            item.crudAction = item.id ? 'u' : 'c';
          }
        }
        function markRemoved(item, source, itemIndex) {
          item.crudAction = 'd';
          removedItems.push({
            source,
            itemIndex,
            product_name: itemName(item),
            id: item.id || null,
            quantity: itemQuantity(item)
          });
          markDesignChanged();
          return true;
        }

        const before = (design.items || []).map((item, index) => ({
          index,
          id: item.id || null,
          name: itemName(item),
          group: classify(item),
          crudAction: item.crudAction || '',
          quantity: itemQuantity(item)
        }));
        const removedItems = [];

        runInAngular(s, () => {
          const removeMatchesFrom = (items, removedItem) => {
            (items || []).forEach((candidate, candidateIndex) => {
              const sameId = removedItem.id && candidate.id && String(candidate.id) === String(removedItem.id);
              const sameStyle = !removedItem.id && clean(candidate.style) === clean(removedItem.style) && clean(candidate.ourLabel || candidate.label) === clean(removedItem.ourLabel || removedItem.label);
              if (candidate === removedItem || sameId || sameStyle) {
                markRemoved(candidate, 'design.designItems', candidateIndex);
              }
            });
          };
          (design.items || []).slice().forEach((item, index) => {
            if (classify(item) !== keepGroup) {
              if (typeof s.removeDesignItem === 'function') {
                s.removeDesignItem(item, designIndex, index);
                removedItems.push({
                  source: 'removeDesignItem',
                  itemIndex: index,
                  product_name: itemName(item),
                  id: item.id || null,
                  quantity: itemQuantity(item)
                });
              } else {
                markRemoved(item, 'design.items', index);
              }
              removeMatchesFrom(design.designItems, item);
            } else if (item.crudAction === 'd') {
              item.crudAction = item.id ? 'u' : 'c';
            }
          });
          (design.designItems || []).forEach((item, index) => {
            if (classify(item) !== keepGroup) {
              markRemoved(item, 'design.designItems', index);
            }
          });
          if (typeof s.watchDesignChanges === 'function') s.watchDesignChanges(design, designIndex);
        });

        const after = (design.items || []).map((item, index) => ({
          index,
          id: item.id || null,
          name: itemName(item),
          group: classify(item),
          crudAction: item.crudAction || '',
          active: effectivelyActive(item),
          quantity: itemQuantity(item)
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
          bad_product_names: bad.map((item) => item.name),
          removed_items: removedItems,
          zeroed_quantities: []
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


def _scan_tab_quantity_total(scan):
    tabs = scan.get("tabs") if isinstance(scan, dict) else []
    if not tabs:
        return None
    total = 0
    found = False
    for tab in tabs:
        try:
            quantity = int(tab.get("quantity"))
        except (TypeError, ValueError):
            continue
        total += quantity
        found = True
    return total if found else None


def _quantity_total_check(scan, expected_total):
    actual_total = _scan_tab_quantity_total(scan)
    return {
        "expected_total": expected_total,
        "actual_total": actual_total,
        "matches": expected_total is None or actual_total is None or int(actual_total) == int(expected_total),
    }


def _source_quantity_cleanup_targets(plan, scan):
    tabs_by_number = {}
    for tab in (scan.get("tabs") if isinstance(scan, dict) else []) or []:
        try:
            tabs_by_number[int(tab.get("tab_number"))] = tab
        except (TypeError, ValueError):
            continue

    targets = []
    for split in (plan.get("split_tabs") if isinstance(plan, dict) else []) or []:
        assignments = split.get("assignments") or []
        source_assignment = next((item for item in assignments if item.get("source") == "original"), None)
        if not source_assignment:
            continue
        try:
            tab_number = int(source_assignment.get("tab_number") or split.get("source_tab_number"))
        except (TypeError, ValueError):
            continue
        keep_group = _clean_text(source_assignment.get("keep_group"))
        if not keep_group:
            continue
        tab = tabs_by_number.get(tab_number)
        if not tab:
            continue
        stuck_products = [
            product
            for product in (tab.get("products") or [])
            if _clean_text(product.get("group")) != keep_group and _product_has_positive_quantity(product)
        ]
        if not stuck_products:
            continue
        targets.append(
            {
                "tab_number": tab_number,
                "design_index": tab_number - 1,
                "keep_group": keep_group,
                "keep_group_label": source_assignment.get("keep_group_label"),
                "stuck_product_names": [product.get("product_name") for product in stuck_products],
            }
        )
    return targets


def _apply_source_quantity_cleanup(driver, targets):
    if not targets:
        return {"attempted": False, "targets": []}
    _enter_edit_mode(driver)
    actions = []
    for target in targets:
        cleanup = _zero_non_keep_group_quantity_inputs(
            driver,
            int(target.get("design_index")),
            target.get("keep_group"),
        )
        actions.append(
            {
                "action": "zero_stuck_source_products",
                **target,
                "cleanup": cleanup,
            }
        )
    save_state = _save_order_and_wait(driver)
    return {"attempted": True, "targets": targets, "actions": actions, "save_state": save_state}


def _apply_live_split(driver, plan):
    _enter_edit_mode(driver)
    actions = []
    for split in plan.get("split_tabs") or []:
        source_tab_number = int(split.get("source_tab_number"))
        source_design_index = source_tab_number - 1
        assignments = split.get("assignments") or []
        clone_assignments = [item for item in assignments if item.get("source") == "clone"]
        assignment_design_indexes = {source_tab_number: source_design_index}
        for existing in [item for item in assignments if item.get("source") == "existing"]:
            existing_tab_number = int(existing.get("tab_number"))
            assignment_design_indexes[existing_tab_number] = existing_tab_number - 1
        for clone in clone_assignments:
            planned_tab_number = int(clone.get("tab_number") or 0)
            clone_result = _duplicate_design_from_index(driver, source_design_index, clone.get("tab_name"))
            expected_number = int(clone_result.get("tab_number"))
            clone["tab_number"] = expected_number
            if planned_tab_number and planned_tab_number != expected_number:
                for record in (plan.get("manual_order_records") or []) + (split.get("manual_order_records") or []):
                    same_planned_tab = int(record.get("target_tab_number") or 0) == planned_tab_number
                    same_tab_name = _clean_text(record.get("target_tab_name")) == _clean_text(clone.get("tab_name"))
                    if same_planned_tab and same_tab_name:
                        record["target_tab_number"] = expected_number
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
    return {"actions": actions, "save_state": save_state, "status_state": status_state}


def _record_separator_manual_orders(driver, order_id, order_url, plan, login_wait_seconds=0):
    records = plan.get("manual_order_records") or []
    actions = []
    original_reopen = _shipping_bypasser._reopen_crm_manual_order_target
    original_wait = _shipping_bypasser._wait_for_order_goods_page_ready

    def _separator_reopen(driver_arg, order_id_arg, order_url=None):
        try:
            driver_arg.switch_to.default_content()
        except Exception:
            pass
        target_url = order_url or PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id_arg)
        safe_get_with_partial_load(driver_arg, target_url, f"Product Separator CRM order {order_id_arg} manual order retry")
        _handle_login_if_needed(driver_arg, target_url, login_wait_seconds=login_wait_seconds)
        _wait_for_crm_context(driver_arg)

    def _separator_wait(driver_arg, order_id_arg, timeout=None):
        try:
            _activate_crm_context(driver_arg)
        except Exception:
            pass
        return original_wait(driver_arg, order_id_arg, timeout=timeout)

    try:
        _shipping_bypasser._reopen_crm_manual_order_target = _separator_reopen
        _shipping_bypasser._wait_for_order_goods_page_ready = _separator_wait
        for record in records:
            target_tab_number = int(record.get("target_tab_number") or 0)
            po = _clean_text(record.get("po"))
            vendor = _clean_text(record.get("vendor"))
            if not target_tab_number or not po or not vendor:
                raise ProductSeparatorError(f"Manual Order record is incomplete: {record}")
            try:
                _separator_reopen(driver, order_id, order_url=order_url)
            except Exception:
                pass
            state = _record_crm_stock_manual_order(
                driver,
                order_id,
                po,
                dry_run=False,
                stock_tab_index=target_tab_number,
                vendor_name=vendor,
                order_url=order_url,
            )
            actions.append(
                {
                    "action": "manual_order_record",
                    "state": state,
                    **record,
                }
            )
    finally:
        _shipping_bypasser._reopen_crm_manual_order_target = original_reopen
        _shipping_bypasser._wait_for_order_goods_page_ready = original_wait
    return {"attempted": bool(records), "records": actions}


def _auto_order_local_inventory_tabs(driver, order_id, order_url, plan, login_wait_seconds=0):
    targets = plan.get("local_inventory_auto_order_targets") or []
    actions = []
    for target in targets:
        target_tab_number = int(target.get("target_tab_number") or 0)
        if not target_tab_number:
            raise ProductSeparatorError(f"Local Inventory auto-order target is incomplete: {target}")
        try:
            _order_goods._require_order_goods_page_ready(driver, order_id)
        except Exception:
            safe_get_with_partial_load(driver, order_url, f"Product Separator CRM order {order_id} local inventory auto-order")
            _handle_login_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
            _order_goods._require_order_goods_page_ready(driver, order_id)
        tab = _order_goods._activate_stock_tab(driver, target_tab_number - 1)
        if tab is None:
            raise ProductSeparatorError(f"Local Inventory target tab {target_tab_number} could not be activated for auto-order.")
        stock_state = _active_tab_stock_state(driver)
        if _stock_state_has_local_inventory_row(stock_state):
            result = {
                "success": True,
                "outcome": "already_local_inventory_ordered",
                "message": "Target tab already has a Local Inventory row.",
                "manual_review_required": False,
            }
        else:
            result = _order_goods._order_goods_for_open_order(
                driver,
                order_id,
                dry_run=False,
                allow_unlock_retry=True,
                stock_tab_index=target_tab_number - 1,
                ignore_already_ordered=True,
            )
        action = {
            "action": "local_inventory_auto_order",
            **target,
            "stock_tab_index": target_tab_number,
            "result": result,
        }
        actions.append(action)
        if not isinstance(result, dict) or not result.get("success"):
            raise ProductSeparatorError((result or {}).get("message") or f"Local Inventory auto-order failed for tab {target_tab_number}.")
        if str(result.get("outcome") or "") == "order_goods_clicked":
            safe_get_with_partial_load(driver, order_url, f"Product Separator CRM order {order_id} after local inventory auto-order")
            _handle_login_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
            _wait_for_crm_context(driver)
    return {"attempted": bool(targets), "targets": actions}


def _manual_order_row_matches(row, po, vendor):
    row_po = _clean_text((row or {}).get("po")).lower()
    wanted_po = _clean_text(po).lower()
    row_vendor = _manual_order_vendor_label((row or {}).get("vendor"))
    wanted_vendor = _manual_order_vendor_label(vendor)
    return bool(row_po and wanted_po and row_po == wanted_po and row_vendor == wanted_vendor)


def _manual_order_record_missing_after_scan(scan, record):
    target_tab_number = int(record.get("target_tab_number") or 0)
    target_tab_name = _clean_text(record.get("target_tab_name"))
    target_tab = None
    for tab in scan.get("tabs") or []:
        tab_number = int(tab.get("tab_number") or 0)
        tab_name = _clean_text(tab.get("tab_name"))
        if target_tab_number and tab_number == target_tab_number:
            target_tab = tab
            break
        if target_tab_name and tab_name.lower() == target_tab_name.lower():
            target_tab = tab
            break
    if not target_tab:
        return {**record, "missing_reason": "target tab was not found after recording"}
    stock = target_tab.get("stock") or {}
    rows = stock.get("manual_order_rows") or []
    if any(_manual_order_row_matches(row, record.get("po"), record.get("vendor")) for row in rows):
        return None
    if _manual_order_row_matches(stock, record.get("po"), record.get("vendor")):
        return None
    return {**record, "missing_reason": "target tab still has no matching Manual Order row after recording"}


def _verify_manual_order_records_persisted(scan, records):
    missing = []
    for record in records or []:
        missing_record = _manual_order_record_missing_after_scan(scan, record)
        if missing_record:
            missing.append(missing_record)
    return {
        "verified": not missing,
        "missing_records": missing,
    }


def _local_inventory_auto_order_missing_after_scan(scan, target):
    target_tab_number = int(target.get("target_tab_number") or 0)
    target_tab_name = _clean_text(target.get("target_tab_name"))
    target_tab = None
    for tab in scan.get("tabs") or []:
        tab_number = int(tab.get("tab_number") or 0)
        tab_name = _clean_text(tab.get("tab_name"))
        if target_tab_number and tab_number == target_tab_number:
            target_tab = tab
            break
        if target_tab_name and tab_name.lower() == target_tab_name.lower():
            target_tab = tab
            break
    if not target_tab:
        return {**target, "missing_reason": "target tab was not found after Local Inventory auto-order"}
    stock = target_tab.get("stock") or {}
    if _stock_state_has_local_inventory_row(stock):
        return None
    return {**target, "missing_reason": "target tab still has no Local Inventory row after auto-order"}


def _verify_local_inventory_auto_orders(scan, targets):
    missing = []
    for target in targets or []:
        missing_target = _local_inventory_auto_order_missing_after_scan(scan, target)
        if missing_target:
            missing.append(missing_target)
    return {
        "verified": not missing,
        "missing_targets": missing,
    }


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


def _scan_split_signature(scan):
    def signature_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    signature = []
    for tab in scan.get("tabs") or []:
        products = []
        for product in tab.get("products") or []:
            products.append(
                (
                    _clean_text(product.get("product_name")),
                    _clean_text(product.get("group")),
                    _clean_text(product.get("color")),
                    tuple(_clean_text(size) for size in (product.get("sizes") or [])),
                )
            )
        groups = tuple(
            (_clean_text(group.get("group")), _clean_text(group.get("group_label")))
            for group in (tab.get("groups") or [])
        )
        signature.append(
            (
                signature_int(tab.get("tab_number")),
                _clean_text(tab.get("tab_name")),
                signature_int(tab.get("quantity")),
                tuple(products),
                groups,
                bool(tab.get("needs_split")),
            )
        )
    return tuple(signature)


def _scan_looks_unchanged_after_split(before_scan, after_scan):
    return bool(before_scan and after_scan) and _scan_split_signature(before_scan) == _scan_split_signature(after_scan)


def _refresh_order_before_final_split_verification(driver, target_url, login_wait_seconds=0):
    try:
        driver.refresh()
    except Exception:
        safe_get_with_partial_load(driver, target_url, "Product Separator verification refresh")
    _handle_login_if_needed(driver, target_url, login_wait_seconds=login_wait_seconds)
    _wait_for_crm_context(driver)
    _recover_not_authenticated_page(driver, target_url, login_wait_seconds=login_wait_seconds)


def _verify_split_persisted_after_save(driver, target_url, resolved_order_id, login_wait_seconds=0):
    safe_get_with_partial_load(driver, target_url, "Product Separator verification")
    _handle_login_if_needed(driver, target_url, login_wait_seconds=login_wait_seconds)
    _wait_for_crm_context(driver)
    _recover_not_authenticated_page(driver, target_url, login_wait_seconds=login_wait_seconds)
    scan_after = _scan_order(driver, expected_order_id=resolved_order_id)
    verification = {
        "scan_after": scan_after,
        "verification_refresh_attempted": False,
    }
    remaining_split_tabs = _tabs_still_needing_split(scan_after)
    if not remaining_split_tabs:
        return verification, remaining_split_tabs

    verification["verification_refresh_attempted"] = True
    verification["remaining_before_refresh"] = _format_remaining_split_tabs(remaining_split_tabs)
    _refresh_order_before_final_split_verification(
        driver,
        target_url,
        login_wait_seconds=login_wait_seconds,
    )
    scan_after_refresh = _scan_order(driver, expected_order_id=resolved_order_id)
    verification["scan_after_refresh"] = scan_after_refresh
    remaining_split_tabs = _tabs_still_needing_split(scan_after_refresh)
    verification["remaining_after_refresh"] = _format_remaining_split_tabs(remaining_split_tabs)
    return verification, remaining_split_tabs


def _last_verification_scan(verification):
    return verification.get("scan_after_refresh") or verification.get("scan_after") or {}


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
        expected_quantity_total = _scan_tab_quantity_total(scan)

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
                manual_review_required=False,
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
        report["live"] = live
        verification, remaining_split_tabs = _verify_split_persisted_after_save(
            driver,
            target_url,
            resolved_order_id,
            login_wait_seconds=login_wait_seconds,
        )
        report.update(verification)
        quantity_total_check = _quantity_total_check(_last_verification_scan(verification), expected_quantity_total)
        report["quantity_total_check"] = quantity_total_check
        if not quantity_total_check.get("matches"):
            cleanup_targets = _source_quantity_cleanup_targets(plan, _last_verification_scan(verification))
            cleanup_result = {"attempted": False, "targets": cleanup_targets}
            if cleanup_targets:
                cleanup_result = _apply_source_quantity_cleanup(driver, cleanup_targets)
                cleanup_verification, remaining_split_tabs = _verify_split_persisted_after_save(
                    driver,
                    target_url,
                    resolved_order_id,
                    login_wait_seconds=login_wait_seconds,
                )
                cleanup_result["verification"] = cleanup_verification
                cleanup_result["quantity_total_check_after"] = _quantity_total_check(
                    _last_verification_scan(cleanup_verification),
                    expected_quantity_total,
                )
                report["source_quantity_cleanup"] = cleanup_result
                report["quantity_total_check"] = cleanup_result["quantity_total_check_after"]
                verification = cleanup_verification
                report.update(cleanup_verification)
            else:
                report["source_quantity_cleanup"] = cleanup_result
        if remaining_split_tabs and _scan_looks_unchanged_after_split(scan, _last_verification_scan(verification)):
            retry_scan = _last_verification_scan(verification)
            retry_plan = _build_separator_plan(retry_scan)
            retry = {
                "attempted": True,
                "reason": "first live save verification still matched the original mixed order after refresh",
                "scan_before_retry": retry_scan,
                "plan": retry_plan,
            }
            report["live_retry"] = retry
            if retry_plan.get("needs_split") and not retry_plan.get("manual_review_required"):
                retry["live"] = _apply_live_split(driver, retry_plan)
                retry_verification, remaining_split_tabs = _verify_split_persisted_after_save(
                    driver,
                    target_url,
                    resolved_order_id,
                    login_wait_seconds=login_wait_seconds,
                )
                retry.update(retry_verification)
                retry_quantity_check = _quantity_total_check(
                    _last_verification_scan(retry_verification),
                    expected_quantity_total,
                )
                retry["quantity_total_check"] = retry_quantity_check
                if not retry_quantity_check.get("matches"):
                    retry_cleanup_targets = _source_quantity_cleanup_targets(
                        retry_plan,
                        _last_verification_scan(retry_verification),
                    )
                    retry_cleanup_result = {"attempted": False, "targets": retry_cleanup_targets}
                    if retry_cleanup_targets:
                        retry_cleanup_result = _apply_source_quantity_cleanup(driver, retry_cleanup_targets)
                        retry_cleanup_verification, remaining_split_tabs = _verify_split_persisted_after_save(
                            driver,
                            target_url,
                            resolved_order_id,
                            login_wait_seconds=login_wait_seconds,
                        )
                        retry_cleanup_result["verification"] = retry_cleanup_verification
                        retry_cleanup_result["quantity_total_check_after"] = _quantity_total_check(
                            _last_verification_scan(retry_cleanup_verification),
                            expected_quantity_total,
                        )
                        retry["source_quantity_cleanup"] = retry_cleanup_result
                        retry["quantity_total_check"] = retry_cleanup_result["quantity_total_check_after"]
                        retry.update(retry_cleanup_verification)
                    else:
                        retry["source_quantity_cleanup"] = retry_cleanup_result
            else:
                retry["skipped"] = True
                retry["skip_reason"] = "refreshed order no longer had a retryable split plan"
        if remaining_split_tabs:
            retry_suffix = " after retrying save/split once" if report.get("live_retry", {}).get("attempted") else ""
            _write_result(
                False,
                (
                    f"Product Separator verification failed for order {resolved_order_id}: "
                    f"mixed product tabs still remain after save{retry_suffix}: "
                    f"{_format_remaining_split_tabs(remaining_split_tabs)}"
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
        final_verification = report.get("live_retry") if report.get("live_retry", {}).get("scan_after") else report
        final_quantity_check = _quantity_total_check(_last_verification_scan(final_verification), expected_quantity_total)
        report["final_quantity_total_check"] = final_quantity_check
        if not final_quantity_check.get("matches"):
            _write_result(
                False,
                (
                    f"Product Separator verification failed for order {resolved_order_id}: "
                    f"quantity total changed from {final_quantity_check.get('expected_total')} "
                    f"to {final_quantity_check.get('actual_total')} after split."
                ),
                result_file=result_file,
                action="product_separator_order",
                dry_run=False,
                target_order_id=resolved_order_id,
                order_url=target_url,
                report=report,
                manual_review_required=True,
                resolution="quantity_total_mismatch",
                duration_seconds=round(time.monotonic() - started, 2),
            )
            return 4
        if plan.get("manual_order_records"):
            try:
                report["manual_order_recording"] = _record_separator_manual_orders(
                    driver,
                    resolved_order_id,
                    target_url,
                    plan,
                    login_wait_seconds=login_wait_seconds,
                )
            except Exception as exc:
                report["manual_order_recording"] = {
                    "attempted": True,
                    "records": plan.get("manual_order_records") or [],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
                _write_result(
                    False,
                    (
                        f"Product Separator split persisted for order {resolved_order_id}, "
                        f"but copied Manual Order records could not be saved: {exc}"
                    ),
                    result_file=result_file,
                    action="product_separator_order",
                    dry_run=False,
                    target_order_id=resolved_order_id,
                    order_url=target_url,
                    report=report,
                    manual_review_required=True,
                    resolution="manual_order_record_failed",
                    duration_seconds=round(time.monotonic() - started, 2),
                )
                return 4
            report["manual_order_recording"]["sequence"] = "after_split_save_verification"
            _refresh_order_before_final_split_verification(
                driver,
                target_url,
                login_wait_seconds=login_wait_seconds,
            )
            scan_after_manual_order = _scan_order(driver, expected_order_id=resolved_order_id)
            manual_order_verification = _verify_manual_order_records_persisted(
                scan_after_manual_order,
                plan.get("manual_order_records") or [],
            )
            report["manual_order_recording"]["scan_after"] = scan_after_manual_order
            report["manual_order_recording"]["verification"] = manual_order_verification
            if not manual_order_verification.get("verified"):
                _write_result(
                    False,
                    (
                        f"Product Separator split persisted for order {resolved_order_id}, "
                        "but copied Manual Order records were not visible on their target tabs after refresh."
                    ),
                    result_file=result_file,
                    action="product_separator_order",
                    dry_run=False,
                    target_order_id=resolved_order_id,
                    order_url=target_url,
                    report=report,
                    manual_review_required=True,
                    resolution="manual_order_record_not_persisted",
                    duration_seconds=round(time.monotonic() - started, 2),
                )
                return 4
        if plan.get("local_inventory_auto_order_targets"):
            try:
                report["local_inventory_auto_ordering"] = _auto_order_local_inventory_tabs(
                    driver,
                    resolved_order_id,
                    target_url,
                    plan,
                    login_wait_seconds=login_wait_seconds,
                )
            except Exception as exc:
                report["local_inventory_auto_ordering"] = {
                    "attempted": True,
                    "targets": plan.get("local_inventory_auto_order_targets") or [],
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
                _write_result(
                    False,
                    (
                        f"Product Separator split persisted for order {resolved_order_id}, "
                        f"but Local Inventory auto-order failed: {exc}"
                    ),
                    result_file=result_file,
                    action="product_separator_order",
                    dry_run=False,
                    target_order_id=resolved_order_id,
                    order_url=target_url,
                    report=report,
                    manual_review_required=True,
                    resolution="local_inventory_auto_order_failed",
                    duration_seconds=round(time.monotonic() - started, 2),
                )
                return 4
            _refresh_order_before_final_split_verification(
                driver,
                target_url,
                login_wait_seconds=login_wait_seconds,
            )
            scan_after_local_inventory = _scan_order(driver, expected_order_id=resolved_order_id)
            local_inventory_verification = _verify_local_inventory_auto_orders(
                scan_after_local_inventory,
                plan.get("local_inventory_auto_order_targets") or [],
            )
            report["local_inventory_auto_ordering"]["scan_after"] = scan_after_local_inventory
            report["local_inventory_auto_ordering"]["verification"] = local_inventory_verification
            remaining_after_local_inventory = _tabs_still_needing_split(scan_after_local_inventory)
            if remaining_after_local_inventory:
                _write_result(
                    False,
                    (
                        f"Product Separator verification failed for order {resolved_order_id}: "
                        "split changed after Local Inventory auto-order."
                    ),
                    result_file=result_file,
                    action="product_separator_order",
                    dry_run=False,
                    target_order_id=resolved_order_id,
                    order_url=target_url,
                    report=report,
                    manual_review_required=True,
                    resolution="local_inventory_split_verification_failed",
                    duration_seconds=round(time.monotonic() - started, 2),
                )
                return 4
            if not local_inventory_verification.get("verified"):
                _write_result(
                    False,
                    (
                        f"Product Separator split persisted for order {resolved_order_id}, "
                        "but Local Inventory auto-order was not visible on the target tab after refresh."
                    ),
                    result_file=result_file,
                    action="product_separator_order",
                    dry_run=False,
                    target_order_id=resolved_order_id,
                    order_url=target_url,
                    report=report,
                    manual_review_required=True,
                    resolution="local_inventory_auto_order_not_persisted",
                    duration_seconds=round(time.monotonic() - started, 2),
                )
                return 4
        if plan.get("apply_stock_ordered_after_split"):
            status_state = _apply_stock_ordered_status(driver)
            report["stock_status_apply"] = status_state
            status_verification, remaining_after_status = _verify_split_persisted_after_save(
                driver,
                target_url,
                resolved_order_id,
                login_wait_seconds=login_wait_seconds,
            )
            report["stock_status_verification_scan"] = status_verification
            if remaining_after_status:
                _write_result(
                    False,
                    (
                        f"Product Separator verification failed for order {resolved_order_id}: "
                        "split changed after applying Stock Ordered status."
                    ),
                    result_file=result_file,
                    action="product_separator_order",
                    dry_run=False,
                    target_order_id=resolved_order_id,
                    order_url=target_url,
                    report=report,
                    manual_review_required=True,
                    resolution="stock_ordered_split_verification_failed",
                    duration_seconds=round(time.monotonic() - started, 2),
                )
                return 4
            stock_status_verification = _verify_stock_ordered_status_persisted(final_verification)
            if not stock_status_verification.get("stock_status_verified"):
                stock_status_verification = _verify_stock_ordered_status_persisted(status_verification)
            report["stock_status_verification"] = stock_status_verification
            if not stock_status_verification.get("stock_status_verified"):
                _write_result(
                    False,
                    (
                        f"Product Separator verification failed for order {resolved_order_id}: "
                        "split persisted, but Stock Ordered status was not confirmed after refresh."
                    ),
                    result_file=result_file,
                    action="product_separator_order",
                    dry_run=False,
                    target_order_id=resolved_order_id,
                    order_url=target_url,
                    report=report,
                    manual_review_required=True,
                    resolution="stock_ordered_status_not_persisted",
                    duration_seconds=round(time.monotonic() - started, 2),
                )
                return 4
        elif plan.get("stock_ordered_apply_skip_reason"):
            report["stock_status_apply"] = {
                "status_applied": False,
                "skipped": True,
                "reason": plan.get("stock_ordered_apply_skip_reason"),
                "order_stock_status_before_split": plan.get("order_stock_status_before_split"),
            }
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


def _run_product_separator_order_id_batch(order_ids, profile_dirs, result_dir, visible=False, login_wait_seconds=0):
    worker_count = max(1, min(len(profile_dirs or []), len(order_ids) or 1))
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
    return {
        "worker_count": worker_count,
        "order_results": order_results,
        "split_order_ids": split_order_ids,
        "skipped_order_ids": skipped_order_ids,
        "manual_review_order_ids": manual_review_order_ids,
        "failed_order_ids": failed_order_ids,
    }


def _product_separator_list_url_for_mode(list_mode):
    mode = _clean_text(list_mode or PRODUCT_SEPARATOR_DEFAULT_LIST_MODE).lower() or "all"
    urls = {
        "free": PRODUCT_SEPARATOR_LIST_URL_FREE,
        "rush": PRODUCT_SEPARATOR_LIST_URL_RUSH,
        "all": PRODUCT_SEPARATOR_LIST_URL_ALL or PRODUCT_SEPARATOR_LIST_URL,
        "813": PRODUCT_SEPARATOR_LIST_URL_813 or CRM_SHIPPING_813_URL,
    }
    if mode not in urls:
        raise ProductSeparatorError(f"Unknown Product Separator list mode: {list_mode}")
    return mode, urls.get(mode) or ""


def _product_separator_list_summary_message(split_count, skipped_count, manual_review_count=0, failed_count=0):
    parts = [
        f"{int(split_count or 0)} order(s) need splitting",
        f"{int(skipped_count or 0)} already okay",
    ]
    if manual_review_count:
        parts.append(f"{int(manual_review_count)} require manual review")
    if failed_count:
        parts.append(f"{int(failed_count)} failed")
    return "Product Separator list dry run complete. " + ", ".join(parts) + "."


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

    worker_profile_dir = None
    result_dir = os.path.join(PROJECT_ROOT, "product_separator_results", "list_scan_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(result_dir, exist_ok=True)
    max_order_count = int(max_orders or 0) if max_orders else 0
    requested_worker_count = max(1, int(workers or 1))
    profile_dirs = []
    refresh_passes = 0
    attempted_order_ids = set()
    order_ids = []
    order_results = []
    split_order_ids = []
    skipped_order_ids = []
    manual_review_order_ids = []
    failed_order_ids = []
    worker_count = 0
    try:
        while True:
            driver = None
            profile = None
            try:
                driver, profile = _build_driver(visible=visible, profile_dir=None, kill_existing=True)
                remaining = max_order_count - len(order_ids) if max_order_count > 0 else 0
                pass_order_ids = _extract_report_order_ids(
                    driver,
                    list_url,
                    login_wait_seconds=login_wait_seconds,
                    exclude_order_ids=set(attempted_order_ids),
                )
                if remaining > 0:
                    pass_order_ids = pass_order_ids[:remaining]
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
                    refresh_passes=refresh_passes,
                )
                return 1
            finally:
                safe_driver_quit(driver, profile_path=profile)

            if not pass_order_ids:
                break

            refresh_passes += 1
            for order_id in pass_order_ids:
                attempted_order_ids.add(order_id)
                order_ids.append(order_id)

            if not profile_dirs:
                worker_count = max(1, min(requested_worker_count, len(pass_order_ids) or 1))
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
                        refresh_passes=refresh_passes,
                    )
                    return 1

            batch = _run_product_separator_order_id_batch(
                pass_order_ids,
                profile_dirs,
                result_dir,
                visible=visible,
                login_wait_seconds=login_wait_seconds,
            )
            worker_count = max(worker_count, int(batch.get("worker_count") or 0))
            order_results.extend(batch["order_results"])
            split_order_ids.extend(batch["split_order_ids"])
            skipped_order_ids.extend(batch["skipped_order_ids"])
            manual_review_order_ids.extend(batch["manual_review_order_ids"])
            failed_order_ids.extend(batch["failed_order_ids"])

            if max_order_count > 0 and len(order_ids) >= max_order_count:
                break
            print(f"Finished Product Separator list refresh pass {refresh_passes}; reopening the list to look for additional orders...")
    finally:
        if worker_profile_dir and os.path.isdir(worker_profile_dir):
            shutil.rmtree(worker_profile_dir, ignore_errors=True)

    if not order_ids:
        _write_result(
            True,
            "No orders detected",
            result_file=result_file,
            action="product_separator_list",
            dry_run=True,
            list_mode=resolved_list_mode,
            list_url=list_url,
            workers=0,
            order_count=0,
            order_ids=[],
            split_order_ids=[],
            skipped_order_ids=[],
            manual_review_order_ids=[],
            failed_order_ids=[],
            result_dir=result_dir,
            report=[],
            duration_seconds=round(time.monotonic() - started, 2),
            refresh_passes=refresh_passes,
        )
        return 0

    order_results.sort(key=lambda item: order_ids.index(item["order_id"]) if item["order_id"] in order_ids else 999999)
    success = not failed_order_ids and not manual_review_order_ids
    _write_result(
        success,
        _product_separator_list_summary_message(
            len(split_order_ids),
            len(skipped_order_ids),
            manual_review_count=len(manual_review_order_ids),
            failed_count=len(failed_order_ids),
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
        refresh_passes=refresh_passes,
    )
    return 0 if success else 4


def main(argv=None):
    parser = argparse.ArgumentParser(description="CRM Product Separator worker.")
    parser.add_argument("--action", choices=["product_separator_order", "product_separator_list"], default="product_separator_order")
    parser.add_argument("--order-id", default="")
    parser.add_argument("--order-url", default="")
    parser.add_argument("--list-url", default="")
    parser.add_argument("--list-mode", choices=["free", "rush", "all", "813"], default=PRODUCT_SEPARATOR_DEFAULT_LIST_MODE)
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
