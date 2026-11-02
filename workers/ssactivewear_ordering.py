"""S&S website ordering helpers used only by the explicit Reorder worker."""

import re
from collections import Counter
from datetime import date, timedelta
from urllib.parse import quote, urlsplit

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

import crm_shipping_bypasser as bypass
from automation_runtime import safe_get_with_partial_load, safe_take_screenshot
from credential_store import SSACTIVEWEAR_CREDENTIAL_TARGET, read_credential

BASE_URL = "https://www.ssactivewear.com"
CART_URL = BASE_URL + "/checkout/cart"
LOGIN_URL = BASE_URL + "/myaccount/login"

DOM = r"""
const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
function visible(el) {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return false;
  for (let p = el; p; p = p.parentElement) {
    const s = getComputedStyle(p);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
  }
  return true;
}
"""


def size_key(value):
    value = re.sub(r"\s+", "", str(value).upper())
    return {"XXL": "2XL", "XXXL": "3XL", "XXXXL": "4XL", "OSFA": "ONESIZE", "OS": "ONESIZE"}.get(value, value)


def color_key(value):
    return re.sub(r"[^A-Z0-9]", "", str(value).upper().replace("GREY", "GRAY"))


def style_id(product):
    raw = str(product["product_id"]).upper()
    if bypass._is_gildan_product(product):
        return bypass._gildan_sanmar_style_id(raw) or raw
    name = str(product.get("product_name", "")).lower()
    for prefix, brand in (("BC", "bella"), ("NL", "next level"), ("RS", "rabbit skins"), ("C", "comfort colors")):
        if brand in name and raw.startswith(prefix):
            return raw[len(prefix):]
    return raw


def _words(text):
    text = re.sub(r"t[ -]?shirts?|tees?\b", "shirt", str(text).lower())
    return set(re.findall(r"[a-z0-9]+", text)) - {"unisex", "a", "the", "alpha", "stock", "tm"}


def select_product(product, candidates):
    """An exact style alone is insufficient: require the CRM brand and description."""
    wanted_style = style_id(product)
    description = str(product.get("product_name") or product.get("stock_line") or "")
    brands = ("Gildan", "Bayside", "Bella", "Next Level", "Rabbit Skins", "Jerzees", "Hanes", "Comfort Colors", "A4", "Lane Seven", "Independent", "Port Authority", "Port and Company", "Team 365", "Sport Tek", "Fruit of the Loom", "Anvil", "Champion", "Adidas", "Columbia", "Harriton", "Devon", "Augusta")
    brand = next((b for b in brands if _words(b) <= _words(description)), None)
    if not brand:
        raise RuntimeError("S&S product brand could not be established from the CRM description; review the match.")
    wanted = _words(description) - _words(brand) - _words(product["product_id"]) - _words(wanted_style)
    wanted -= {"cotton100", "100", "mens", "men"}
    matches = {}
    for candidate in candidates:
        url = str(candidate.get("url", ""))
        parsed = urlsplit(url)
        parts = parsed.path.rstrip("/").split("/")
        title = candidate.get("title", "")
        if parsed.hostname != "www.ssactivewear.com" or len(parts) != 4 or parts[1] != "p" or parts[-1].upper() != wanted_style:
            continue
        words = _words(title)
        if not _words(brand) <= words:
            continue
        if any((token in _words(description)) != (token in words) for token in ("youth", "toddler", "infant", "women", "womens", "tall")):
            continue
        score = len(wanted & words) / max(1, len(wanted))
        if wanted and score >= 0.55:
            matches[url] = (score, candidate)
    ranked = sorted(matches.values(), key=lambda x: x[0], reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.2):
        raise RuntimeError(f"No unambiguous S&S brand/style/description match for {product['product_id']}.")
    return ranked[0][1]


def _wait(driver, fn, timeout=25):
    return WebDriverWait(driver, timeout, poll_frequency=0.25).until(lambda _driver: fn())


def _click(driver, selector):
    def find():
        return next((x for x in driver.find_elements(By.CSS_SELECTOR, selector) if x.is_displayed() and x.is_enabled()), False)
    bypass._click_with_fallback(driver, _wait(driver, find))


def _require_site(driver):
    parsed = urlsplit(driver.current_url)
    if parsed.scheme != "https" or parsed.hostname != "www.ssactivewear.com":
        raise RuntimeError("Unexpected S&S website origin; credentials and checkout were not submitted.")


def ensure_login(driver):
    safe_get_with_partial_load(driver, CART_URL, "S&S cart/login")
    def authenticated():
        return driver.find_elements(By.CSS_SELECTOR, 'a[href="https://www.ssactivewear.com/myaccount/signout"],a[href="/myaccount/signout"]')
    if authenticated():
        return
    safe_get_with_partial_load(driver, LOGIN_URL, "S&S login")
    _require_site(driver)
    from profile_setup_autofill import _first_visible, _USERNAME_SELECTORS, _PASSWORD_SELECTORS, _fill
    credential = read_credential(SSACTIVEWEAR_CREDENTIAL_TARGET)
    username = _wait(driver, lambda: _first_visible(driver, _USERNAME_SELECTORS))
    password = _wait(driver, lambda: _first_visible(driver, _PASSWORD_SELECTORS))
    if not _fill(username, credential.username) or not _fill(password, credential.secret):
        raise RuntimeError("S&S saved credentials could not be filled. Complete S&S profile setup.")
    buttons = driver.find_elements(By.CSS_SELECTOR, "input[type=submit],button[type=submit],a[role=button]")
    login = next((x for x in buttons if x.is_displayed() and re.fullmatch(r"log\s*in|sign\s*in", (x.get_attribute("value") or x.text or "").strip(), re.I)), None)
    if login is None:
        raise RuntimeError("S&S login submit control was not found. Complete S&S profile setup.")
    bypass._click_with_fallback(driver, login)
    _wait(driver, authenticated)
    safe_get_with_partial_load(driver, CART_URL, "S&S cart")
    _wait(driver, lambda: driver.find_elements(By.CSS_SELECTOR, "#M_M_zCart"))


def cart_rows(driver):
    return driver.execute_script(DOM + r"""
return [...document.querySelectorAll('#M_M_zCart .cR')].filter(visible).map(r => ({
  id: r.id,
  style: norm(r.querySelector('.cDes b')?.textContent),
  url: r.querySelector('.cDes a')?.href || '',
  color: norm(r.querySelector('.cCol')?.innerText),
  size: norm(r.querySelector('.cSiz')?.innerText).replace(/^Size:\s*/i, ''),
  method: norm(r.querySelector('.cMet')?.innerText),
  warehouse: norm(r.querySelector('.cLoc')?.innerText),
  quantity: Number(r.querySelector('.cPie input')?.value || 0)
}));
""") or []


def verified_cart_rows(driver, lines):
    _require_site(driver)
    rows = cart_rows(driver)
    original = [dict(row) for row in rows]
    for row in rows:
        parsed = urlsplit(row["url"])
        product_url = BASE_URL + "/" + "/".join(parsed.path.strip("/").split("/")[:3])
        expected = {x["warehouse"] for x in lines if x["selected"]["url"] == product_url and color_key(x["product"]["color"]) == color_key(row["color"]) and size_key(row["size"]) in x["quantities"]}
        if not expected:
            raise RuntimeError("S&S cart has an unexpected product/color/size.")
        visible_locations = [w for w in expected if w in row.get("warehouse", "")]
        single_quantity = sum(x["quantities"].get(size_key(row["size"]), 0) for x in lines if len(visible_locations) == 1 and x["warehouse"] == visible_locations[0] and x["selected"]["url"] == product_url and color_key(x["product"]["color"]) == color_key(row["color"]))
        if len(visible_locations) != 1 or (len(expected) > 1 and single_quantity != row["quantity"]):
            if not re.fullmatch(r"cartRow_[0-9_-]+", row.get("id", "")):
                raise RuntimeError("S&S grouped cart row identity could not be verified.")
            _click(driver, f"#{row['id']} .cMet a[aria-haspopup]")
            _wait(driver, lambda: driver.execute_script(DOM + "return visible(document.querySelector('#popupBody'));"))
            allocations = driver.execute_script(DOM + r"""
const root=document.querySelector('#popupBody'), expected=arguments[0], allocations={};
for(const name of expected){
 const candidates=[...root.querySelectorAll('tr,.row,.gR')].filter(r=>visible(r)&&norm(r.innerText).includes(name));
 const rows=candidates.filter(r=>!candidates.some(other=>other!==r&&r.contains(other)));
 if(rows.length!==1) throw Error('S&S warehouse breakdown row is ambiguous');
 const inputs=[...rows[0].querySelectorAll('input[type=number],input[type=text]')].filter(i=>!i.disabled&&!i.readOnly&&visible(i));
 if(inputs.length!==1 || !/^\d+$/.test(inputs[0].value)) throw Error('S&S warehouse quantity is ambiguous');
 allocations[name]=Number(inputs[0].value);
}
return allocations;
""", sorted(expected))
            if not isinstance(allocations, dict):
                raise RuntimeError("S&S warehouse breakdown was not readable.")
            row["allocations"] = allocations
            # Read only: cancel/close the breakdown without applying any changes.
            close = driver.execute_script(DOM + r"""
const body=document.querySelector('#popupBody');
const root=body?.closest('.modal,.popup,dialog') || document.querySelector('#modalPopupBody') || body?.parentElement;
return [...(root?.querySelectorAll('button,a,[role=button]') || [])].filter(visible).find(x=>
 /^(?:close|cancel)$/i.test(norm(x.innerText||x.getAttribute('aria-label'))) || x.classList.contains('popup-close')) || null;
""")
            if close is None:
                from selenium.webdriver.common.keys import Keys
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
            else:
                bypass._click_with_fallback(driver, close)
            _wait(driver, lambda: driver.execute_script(DOM + "return !visible(document.querySelector('#popupBody'));"))
    if cart_rows(driver) != original:
        raise RuntimeError("S&S cart changed while its warehouse breakdown was being verified.")
    return rows


def require_empty_cart(driver):
    safe_get_with_partial_load(driver, CART_URL, "S&S cart")
    _wait(driver, lambda: driver.find_elements(By.CSS_SELECTOR, "#M_M_zCart"))
    if cart_rows(driver):
        raise RuntimeError("S&S cart already contains stock. Review and empty it before Reorder; existing items were retained.")


def open_product(driver, product):
    safe_get_with_partial_load(driver, BASE_URL + "/ps/" + quote(style_id(product), safe=""), "S&S product search")
    def results():
        candidates = driver.execute_script(DOM + "return [...document.querySelectorAll('a[href*=\"/p/\"]')].filter(visible).map(a => ({url:a.href,title:norm(a.innerText)})).filter(x=>x.title);") or []
        parsed = urlsplit(driver.current_url)
        if re.fullmatch(r"/p/[^/]+/[^/]+/?", parsed.path):
            title = driver.execute_script("return document.querySelector('#productMainContainer')?.innerText || '';")
            if title:
                candidates.append({"url": BASE_URL + parsed.path.rstrip('/'), "title": title})
        return candidates
    candidates = _wait(driver, results)
    selected = select_product(product, candidates)
    safe_get_with_partial_load(driver, selected["url"], "S&S selected product")
    _wait(driver, lambda: driver.find_elements(By.CSS_SELECTOR, "#colorSwatch a"))
    options = driver.execute_script("return [...document.querySelectorAll('#colorSwatch a')].map(a=>({id:a.id,color:a.textContent.trim()}));")
    matches = [x for x in options if color_key(x["color"]) == color_key(product["color"])]
    if len(matches) != 1:
        raise RuntimeError(f"S&S color is missing or ambiguous: {product['color']}.")
    _click(driver, "#" + matches[0]["id"])
    def color_ready():
        return driver.execute_script("return document.querySelector('#zcolorname b')?.textContent || '';")
    _wait(driver, lambda: color_key(str(color_ready()).split(" - PMS")[0]) == color_key(product["color"]))
    _wait(driver, lambda: driver.find_elements(By.CSS_SELECTOR, '.gR[id^="wh_"] input[aria-label="quantity"]'))
    return selected


def inventory(driver):
    return driver.execute_script(DOM + r"""
return [...document.querySelectorAll('.gR[id^="wh_"]')].map(r => ({
 warehouse: norm(r.querySelector('.warehouse')?.textContent),
 eta_text: norm(r.querySelector('.shippingRowLabel')?.textContent),
 stock: Object.fromEntries([...r.querySelectorAll('.i')].filter(c=>c.querySelector('input[aria-label="quantity"]')).map(c=>[
   norm(c.querySelector('.sr-only')?.textContent),
   norm(c.querySelector('.quantity')?.textContent)
 ]))
})).filter(r=>r.warehouse);
""") or []


def parse_eta(text, today=None):
    today = today or date.today()
    match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b", str(text))
    if not match:
        raise RuntimeError("S&S warehouse delivery date could not be read.")
    month, day, year = match.groups()
    eta = date(int(year or today.year), int(month), int(day))
    if not year and eta < today - timedelta(days=30):
        eta = eta.replace(year=eta.year + 1)
    return eta


def allocate(lines):
    """Prefer Reading, then one complete warehouse, then a bounded split."""
    inventory_by_line = []
    all_warehouses = {}
    for line in lines:
        rows = {}
        for raw in line["inventory"]:
            try:
                eta = parse_eta(raw["eta_text"])
            except (ValueError, RuntimeError):
                continue
            stock = {size_key(s): int(re.sub(r"\D", "", str(q)) or 0) for s, q in raw["stock"].items()}
            rows[raw["warehouse"]] = stock
            all_warehouses[raw["warehouse"]] = max(eta, all_warehouses.get(raw["warehouse"], eta))
        inventory_by_line.append(rows)
    priority = sorted(all_warehouses, key=lambda w: (w != "Reading, PA", all_warehouses[w], w))
    common = next((w for w in priority if all(all(rows.get(w, {}).get(s, 0) >= q for s, q in line["quantities"].items()) for line, rows in zip(lines, inventory_by_line))), None)
    allocation = []
    for line, rows in zip(lines, inventory_by_line):
        remaining = dict(line["quantities"])
        for warehouse in ([common] if common else priority):
            quantities = {s: min(q, rows.get(warehouse, {}).get(s, 0)) for s, q in remaining.items() if q > 0}
            quantities = {s: q for s, q in quantities.items() if q > 0}
            if quantities:
                allocation.append(dict(line, warehouse=warehouse, quantities=quantities))
                for s, q in quantities.items():
                    remaining[s] -= q
        if any(remaining.values()):
            raise RuntimeError("S&S cannot fill every required size; no partial purchase is allowed.")
    return allocation


def fill_product(driver, lines):
    required = [{"warehouse": x["warehouse"], "quantities": x["quantities"]} for x in lines]
    result = driver.execute_script(DOM + r"""
const allocations = arguments[0];
const rows = [...document.querySelectorAll('.gR[id^="wh_"]')];
const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
const sizeKey = s => norm(s).toUpperCase().replace(/\s/g,'').replace(/^XXL$/, '2XL').replace(/^XXXL$/, '3XL');
const fills = [];
for (const a of allocations) {
 const matches = rows.filter(r=>norm(r.querySelector('.warehouse')?.textContent)===a.warehouse);
 if (matches.length!==1) throw Error('S&S warehouse row is ambiguous');
 for (const [size, qty] of Object.entries(a.quantities)) {
  const cells = [...matches[0].querySelectorAll('.i')].filter(c=>sizeKey(c.querySelector('.sr-only')?.textContent)===size);
  if(cells.length!==1) throw Error('S&S size cell is ambiguous');
  const input=cells[0].querySelector('input[aria-label="quantity"]');
  const stock=Number(norm(cells[0].querySelector('.quantity')?.textContent).replace(/\D/g,''));
  if(!input || input.disabled || stock<qty) throw Error('S&S stock is no longer available');
  fills.push([input,qty]);
 }
}
for(const input of document.querySelectorAll('.gR input[aria-label="quantity"]')) {
 setter.call(input,''); input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true}));
}
for(const [input,qty] of fills){setter.call(input,String(qty)); input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); input.dispatchEvent(new Event('blur',{bubbles:true}));}
return fills.every(([input,qty])=>Number(input.value)===qty);
""", required)
    if result is not True:
        raise RuntimeError("S&S quantities did not remain entered.")
    def cart_count():
        return driver.execute_script(r"""
const live=(document.querySelector('#qtyMiniCart')?.textContent || '').trim();
const count=live.match(/^(\d+)(?:\s+items?)?$/i);
return count ? `Cart: ${count[1]} items` : document.querySelector('a[aria-label^="Cart:"]')?.getAttribute('aria-label') || '';
""")
    before = re.search(r"Cart:\s*(\d+)", cart_count())
    if not before:
        raise RuntimeError("S&S cart count is unavailable before adding stock.")
    expected_count = int(before.group(1)) + sum(sum(x["quantities"].values()) for x in lines)
    _click(driver, "#aToCDesk, #aToCMob, #ninjaAddToCart")
    _wait(driver, lambda: bool(re.search(rf"Cart:\s*{expected_count}\s+items?\b", cart_count())))
    # The cart, rather than a toast or cleared input, confirms every addition.
    safe_get_with_partial_load(driver, CART_URL, "S&S cart after adding stock")
    _wait(driver, lambda: cart_rows(driver))


def validate_cart(rows, lines):
    expected = Counter()
    expected_allocation = Counter()
    actual = Counter()
    actual_allocation = Counter()
    for line in lines:
        for size, quantity in line["quantities"].items():
            expected[(line["selected"]["url"], color_key(line["product"]["color"]), size_key(size))] += quantity
            expected_allocation[(line["selected"]["url"], color_key(line["product"]["color"]), size_key(size), line["warehouse"])] += quantity
    for row in rows:
        if row.get("method") != "S&S Ship" or type(row.get("quantity")) is not int or row["quantity"] <= 0:
            raise RuntimeError("S&S cart has an unsupported fulfillment method or quantity.")
        parsed = urlsplit(row["url"])
        product_url = BASE_URL + "/" + "/".join(parsed.path.strip("/").split("/")[:3])
        if parsed.hostname != "www.ssactivewear.com":
            raise RuntimeError("S&S cart contains an unexpected product link.")
        actual[(product_url, color_key(row["color"]), size_key(row["size"]))] += int(row["quantity"])
        matching = [x for x in lines if x["selected"]["url"] == product_url and color_key(x["product"]["color"]) == color_key(row["color"]) and size_key(row["size"]) in x["quantities"]]
        expected_locations = {x["warehouse"] for x in matching}
        allocations = row.get("allocations")
        if allocations is None:
            locations = [w for w in expected_locations if w in row.get("warehouse", "")]
            if len(locations) != 1:
                raise RuntimeError("S&S cart warehouse allocation could not be verified.")
            allocations = {locations[0]: int(row["quantity"])}
        if not set(allocations) <= expected_locations or any(type(q) is not int or q <= 0 for q in allocations.values()) or sum(allocations.values()) != int(row["quantity"]):
            raise RuntimeError("S&S cart warehouse quantities do not match the saved allocation.")
        for warehouse, quantity in allocations.items():
            actual_allocation[(product_url, color_key(row["color"]), size_key(row["size"]), warehouse)] += quantity
    if actual != expected or actual_allocation != expected_allocation or not expected:
        raise RuntimeError("S&S cart does not exactly match the CRM brand/style/color/size quantities.")


def review_checkout(driver, po):
    _require_site(driver)
    if not re.fullmatch(r"\d{7}", str(po)):
        raise RuntimeError("Reorder customer PO must be the seven-digit CRM order number.")
    result = driver.execute_script(DOM + r"""
const po = arguments[0];
const container = document.querySelector('#M_M_zShippingContainer');
const address = norm(container?.innerText).split(/Shipping\s+(?:Options|Method)/i)[0];
const methods = [...document.querySelectorAll('select[id$="zFreightServiceCB"]')].map(s=>norm(s.selectedOptions[0]?.textContent));
const terms = document.querySelector('#M_M_zTermsCB');
const payment = norm(terms?.selectedOptions[0]?.textContent);
const input = document.querySelector('#M_M_zPONumberTB');
if (!visible(input) || !/2727\s+Commerce\s+Way/i.test(address) || !/Philadelphia,?\s+PA\s+19154/i.test(address)) throw Error('S&S shipping address is not Commerce Way');
if (!methods.length || methods.some(m=>! /\bUPS Ground\b/i.test(m))) throw Error('S&S shipping method is not UPS Ground for every warehouse');
if (payment !== 'Net 60') throw Error('S&S payment method is not Net 60');
Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input, po);
input.dispatchEvent(new Event('input',{bubbles:true})); input.dispatchEvent(new Event('change',{bubbles:true})); input.dispatchEvent(new Event('blur',{bubbles:true}));
return {po:input.value,address,payment,methods};
""", str(po))
    if not result or result.get("po") != str(po):
        raise RuntimeError("S&S PO was not filled exactly.")
    if not checkout_still_valid(driver, po):
        raise RuntimeError("S&S checkout changed after the PO was entered.")
    etas = [parse_eta(method) for method in result["methods"]]
    return {**result, "eta": max(etas)}


def checkout_still_valid(driver, po):
    _require_site(driver)
    return driver.execute_script(DOM + r"""
const address=norm(document.querySelector('#M_M_zShippingContainer')?.innerText).split(/Shipping\s+(?:Options|Method)/i)[0];
const methods=[...document.querySelectorAll('select[id$="zFreightServiceCB"]')];
return document.querySelector('#M_M_zPONumberTB')?.value===arguments[0]
 && norm(document.querySelector('#M_M_zTermsCB')?.selectedOptions[0]?.textContent)==='Net 60'
 && /2727\s+Commerce\s+Way/i.test(address) && /Philadelphia,?\s+PA\s+19154/i.test(address)
 && methods.length>0 && methods.every(s=>/\bUPS Ground\b/i.test(s.selectedOptions[0]?.textContent || ''));
""", str(po)) is True


def checkout(driver, po, lines):
    validate_cart(verified_cart_rows(driver, lines), lines)
    _click(driver, "#zCheckoutBTN")
    _wait(driver, lambda: driver.find_elements(By.ID, "M_M_zPONumberTB"))
    return review_checkout(driver, po)


def confirmation_from_text(text, po, url):
    parsed = urlsplit(url)
    if parsed.hostname != "www.ssactivewear.com" or not re.search(r"thank\s+you.*order|order\s+confirmation", text, re.I):
        return None
    references = {m.group(1) for m in re.finditer(r"\b(?:order|confirmation)\s*(?:number|no\.?|#)\s*:?\s*([A-Z0-9-]+)", text, re.I) if re.search(r"\d", m.group(1))}
    po_match = re.search(r"\b(?:customer\s+)?PO\s*(?:number|no\.?|#)?\s*:?\s*" + re.escape(str(po)) + r"(?![A-Za-z0-9_-])", text, re.I)
    if len(references) == 1 and po_match:
        return {"vendor": "S&S Activewear", "url": url, "order_number": next(iter(references)), "po": str(po), "po_confirmed": True}
    return None


def submit(driver, po):
    review_checkout(driver, po)
    _click(driver, "#M_M_zCheckoutBTN")
    # This is the one final confirmation shown by the supplied checkout archive.
    confirm = "#M_M_zConfirmOrderPopup_zCompleteBTN, #M_M_zConfirmOrderPopup_zCompleteBTN_I, #M_M_zConfirmOrderPopup_zConfirmOrderYesBTN"
    if not checkout_still_valid(driver, po):
        raise RuntimeError("S&S checkout changed before final confirmation; review the submission state.")
    _click(driver, confirm)
    def read():
        text = driver.execute_script("return document.body.innerText;")
        return confirmation_from_text(text, po, driver.current_url)
    result = _wait(driver, read, timeout=40)
    result["screenshot"] = safe_take_screenshot(driver, f"ssactivewear_reorder_{po}")
    return result
