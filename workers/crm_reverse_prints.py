"""CRM reverse-print clones, using the actual Style Sub and vendor pickers.

All source products are captured before editing. Size quantities are restored by
label, never by column position. Existing clones are matched conservatively and
verified on every run; a sales note alone is not evidence of a completed clone.
"""

import re
import sys
import time
import hashlib
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import StaleElementReferenceException

WORKERS_DIR = str(Path(__file__).resolve().parent)
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)

import crm_copyright_cancel as shared
from runtime_paths import STATE_DIR


class ReversePrintError(RuntimeError):
    pass


def email_receipt_path(order_id, customer_email, plan, mutation):
    """A durable receipt distinguishes a sent email from notes saved before send."""
    sources = []
    for job in mutation["jobs"]:
        source = job["source"]
        sources.append({"id": source["id"], "areas": source["areas"], "items": [
            {**{k: item[k] for k in ("style", "vendor", "color", "description")}, "quantities": _quantities(item)}
            for item in source["items"]]})
    payload = {"order_id": str(order_id), "email": customer_email, "sources": sources,
               "selections": [{k: selection.get(k) for k in ("tab_number", "quantity", "left", "right", "side_left", "side_right", "reverse")}
                              for selection in plan["selections"]], "ink_price": plan["ink_price"],
               "reverse_price": plan["reverse_price"], "embroidery_price": plan["embroidery_price"]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return Path(STATE_DIR) / "reverse_print_email_receipts" / f"{digest}.json"


def email_was_sent(path):
    if not path.exists():
        return False
    try:
        if json.loads(path.read_text(encoding="utf-8")) == {"sent": True}:
            return True
    except (OSError, ValueError):
        pass
    raise ReversePrintError("The reverse-print email receipt cannot be verified; review email history before retrying.")


def record_email_sent(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"sent": True}, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reverse_color(value):
    parts = [part.strip() for part in str(value or "").split("/")]
    if not all(parts) or len(parts) > 2:
        raise ReversePrintError(f"Cannot determine reverse color from {value!r}.")
    return "/ ".join(reversed(parts))


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _key(value):
    return re.sub(r"\s+", "", _clean(value)).casefold()


# These models and repeat containers are present in the supplied CRM snapshots.
# Read the form's catalog data, falling back to the persisted item where needed.
SNAPSHOT_JS = r"""
function clean(v) { return typeof v === 'string' || typeof v === 'number' ? String(v).replace(/\s+/g, ' ').trim() : ''; }
function active(xs) { return (xs || []).filter(x => x && x.crudAction !== 'd'); }
function label(z) { return clean(z.sizeCode || (z.size || {}).name || (z.size || {}).description || z.size || z.label || z.name || z.sizeName || z.sizeType); }
const blocks = Array.from(document.querySelectorAll('[ng-repeat="($itemIndex, item) in design.designItems"]'));
function itemScope(item) { return blocks.map(el => angular.element(el).scope()).find(sc => sc && sc.item === item); }
function vendor(item, catalog, scope, design) {
  const direct = clean(item.vendorName || item.vendor || item.supplierName || catalog.vendorName || catalog.vendor || catalog.supplierName);
  if (direct) return direct;
  const supplierId = item.supplierId || catalog.supplierId;
  const suppliers = (scope && scope.STATICS || s.STATICS || {}).suppliers || [];
  const supplier = supplierId && suppliers.find(v => String(v.id || v.key) === String(supplierId));
  if (supplier) return clean(supplier.value || supplier.name);
  // An already-ordered source can expose its vendor in its inventory table.
  const inventory = Array.from(document.querySelectorAll('[order-inventory]')).find(el => {
    const scopes = [angular.element(el).scope(), angular.element(el).isolateScope()];
    return scopes.some(sc => sc && (sc.design === design || sc.orderInventory === design));
  });
  if (inventory) {
    const names = Array.from(inventory.querySelectorAll('[ng-repeat="inventoryOrder in inventoryOrders"] th'))
      .map(el => clean(el.textContent).replace(/\s*\(Bulk\)$/i, ''));
    const unique = [...new Set(names.filter(Boolean))];
    if (unique.length === 1) return unique[0];
  }
  return '';
}
return (r.designs || []).map((d, index) => ({
  index, id: clean(d.id), design_id: clean(d.designId), name: clean(d.PO),
  areas: active(d.printAreas).map(a => ({
    description: clean(a.description || a.printAreaDescription || (a.printAreaTemplate || {}).description || (typeof s.setPrintAreaTemplateDescription === 'function' && s.setPrintAreaTemplateDescription(a))),
    method: clean(a.printMethodDescription || a.methodDescription || (a.printMethod || {}).description || (typeof s.setPrintMethodDescription === 'function' && s.setPrintMethodDescription(a))),
    artwork: clean(a.designId || a.artworkId || a.designStudioId),
  })),
  items: active(d.designItems).map(item => {
    const sc = itemScope(item), f = sc && sc.formItem || {}, cat = f.selectedCatalogItem || {};
    const sub = active(item.styleSubs)[0] || {};
    const isSub = Number(item.isStyleSub) === 1 || item.isStyleSub === true;
    return {
      id: clean(item.id), is_style_sub: isSub,
      catalog_style: clean(item.style),
      vendor: isSub ? clean(sub.vendor) : vendor(item, cat, sc, d),
      style: isSub ? clean(sub.style) : clean(item.style || f.selectedCatalogItemStyle),
      color: isSub ? clean(sub.color) : (clean(item.color) || clean(item.colorName) || clean((f.selectedColor || {}).name)),
      description: isSub ? clean(sub.description) : clean(item.ourLabel || item.label || cat.ourLabel || cat.label),
      split: Number(item.splitIntoSizes) !== 0,
      quantity: Number(item.quantity) || 0, price: clean(item.pricePerPiece),
      sizes: active(item.sizes).map(z => ({label: label(z), quantity: Number(z.quantity) || 0, price: clean(z.pricePerPiece)})),
    };
  }),
}));
"""


def read_designs(driver):
    return shared._order_scope(driver, SNAPSHOT_JS) or []


def _quantities(item):
    if not item["split"]:
        return {"unsized": item["quantity"]}
    result = {}
    for size in item["sizes"]:
        if size["quantity"] <= 0:
            continue
        label = _key(size["label"])
        if not label or label in result:
            raise ReversePrintError("Source has missing or duplicate size labels.")
        result[label] = size["quantity"]
    return result


def _validate_source(source):
    if _key(source["name"]) == "reverse-print":
        raise ReversePrintError("Select the original product tab, not an existing REVERSE-PRINT tab.")
    if not source["items"]:
        raise ReversePrintError("The selected source tab has no products.")
    for item in source["items"]:
        for field in ("vendor", "style", "color", "description"):
            if not item[field]:
                raise ReversePrintError(f"Cannot read source product {field} on tab {source['index'] + 1}.")
        reverse_color(item["color"])
        quantities = _quantities(item)
        if not quantities or any(q <= 0 or int(q) != q for q in quantities.values()):
            raise ReversePrintError("Source product must have whole-number quantities.")


def find_clone(source, designs, used=()):
    """Match completed or partial clones without relying on duplicate tab names.

    Style and copied design identity allow quantities, colors and prices to be
    repaired. Ambiguous clones are never guessed, and never cause a new duplicate.
    """
    candidates = []
    for design in designs:
        if design["index"] in used or _key(design["name"]) != "reverse-print":
            continue
        if len(design["items"]) != len(source["items"]):
            continue
        identity = source["design_id"] and source["design_id"] == design["design_id"]
        styles = all(_key(a["style"]) == _key(b["style"]) or
                     (b["is_style_sub"] and not b["style"])
                     for a, b in zip(source["items"], design["items"]))
        colors = all(not b["color"] or _key(b["color"]) in {_key(a["color"]), _key(reverse_color(a["color"]))}
                     for a, b in zip(source["items"], design["items"]))
        if styles and colors and (identity or not source["design_id"] or not design["design_id"]):
            candidates.append(design)
    if len(candidates) > 1:
        color_matches = [d for d in candidates if all(_key(b["color"]) in {_key(a["color"]), _key(reverse_color(a["color"]))}
                         for a, b in zip(source["items"], d["items"]))]
        if len(color_matches) == 1:
            candidates = color_matches
        else:
            raise ReversePrintError(f"Multiple REVERSE-PRINT clones match tab {source['index'] + 1}; manual review is needed.")
    if candidates:
        return candidates[0]
    # A damaged clone with the same design identity needs review, not another clone.
    if source["design_id"] and any(_key(d["name"]) == "reverse-print" and d["design_id"] == source["design_id"] and
                                  d["index"] not in used for d in designs):
        raise ReversePrintError("An existing REVERSE-PRINT clone has incompatible products; review it before retrying.")
    return None


def _select_tab(driver, index):
    shared._order_scope(driver, r"""
      const index = Number(arguments[0]);
      runInAngular(s, () => {
        if (!s.editMode) s.editModeOn();
        if (typeof s.onDesignTabSelect === 'function') s.onDesignTabSelect(index);
        else if (typeof s.changeDesignTab === 'function') s.changeDesignTab(index);
        else throw new Error('CRM design-tab selector is unavailable.');
      });
      return true;
    """, index)


def _wait(action, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = action()
            if result:
                return result
        except StaleElementReferenceException:
            pass
        time.sleep(0.2)
    raise ReversePrintError(message)


def _block(driver, index, item_index):
    return _wait(lambda: shared._order_scope(driver, r"""
      const design = r.designs[Number(arguments[0])];
      const item = (design.designItems || []).filter(i => i.crudAction !== 'd')[Number(arguments[1])];
      return Array.from(document.querySelectorAll('[ng-repeat="($itemIndex, item) in design.designItems"]')).find(el => {
        const sc = angular.element(el).scope();
        return sc && sc.item === item && el.getClientRects().length && getComputedStyle(el).display !== 'none';
      }) || null;
    """, index, item_index), "CRM product controls did not become available.")


def _fill(control, value):
    if not control.is_enabled():
        raise ReversePrintError("CRM disabled a required product field.")
    control.click()
    control.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
    control.send_keys(str(value))
    control.send_keys(Keys.TAB)


def _pick(control, value):
    """Typing is insufficient: click the exact suggestion in this input's popup."""
    control.click()
    control.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
    control.send_keys(str(value))

    def suggestion():
        for option in control.find_elements(By.XPATH, "following-sibling::ul[@typeahead-popup]//a"):
            if option.is_displayed() and _key(option.text) == _key(value):
                return option
        return None

    _wait(suggestion, f"CRM did not offer the exact {value!r} dropdown option.").click()


def _convert_product(driver, index, item_index, expected, unit_price):
    block = _block(driver, index, item_index)
    state = read_designs(driver)[index]["items"][item_index]
    if not state["is_style_sub"] or _key(state["catalog_style"]) not in {"stylesub", "style_sub"}:
        _pick(block.find_element(By.CSS_SELECTOR, '[ng-model="formItem.selectedCatalogItemStyle"]'), "Style Sub")
        _wait(lambda: next((b for b in _block(driver, index, item_index).find_elements(By.CSS_SELECTOR, 'button[ng-click]')
                           if b.is_displayed() and b.is_enabled() and b.text.strip().lower() == "apply"), None),
              "Style Sub Apply button did not appear.").click()
        _wait(lambda: read_designs(driver)[index]["items"][item_index]["is_style_sub"], "CRM did not apply Style Sub.")
    block = _block(driver, index, item_index)
    for field, placeholder in (("vendor", "Vendor Name"), ("style", "Style"), ("color", "Color"), ("description", "Description")):
        value = reverse_color(expected[field]) if field == "color" else expected[field]
        control = block.find_element(By.CSS_SELECTOR, f'[placeholder="{placeholder}"]')
        if _clean(control.get_attribute("value")) != _clean(value):
            (_pick if field == "vendor" else _fill)(control, value)
    _restore_sizes(driver, index, item_index, expected, unit_price)


def _restore_sizes(driver, index, item_index, expected, unit_price):
    shared._order_scope(driver, r"""
      const di = Number(arguments[0]), ordinal = Number(arguments[1]), expected = arguments[2], price = arguments[3];
      const d = r.designs[di], item = (d.designItems || []).filter(i => i.crudAction !== 'd')[ordinal];
      const itemIndex = d.designItems.indexOf(item);
      function key(v) { return String(v || '').replace(/\s+/g, '').toLowerCase(); }
      function label(z) { return key(z.sizeCode || (z.size || {}).name || (z.size || {}).description || z.size || z.label || z.name || z.sizeName || z.sizeType); }
      const sizes = (item.sizes || []).filter(z => z.crudAction !== 'd');
      const wanted = new Map(expected.sizes.filter(z => z.quantity > 0).map(z => [key(z.label), z.quantity]));
      if (expected.split) {
        for (const name of wanted.keys()) {
          if (sizes.filter(z => label(z) === name).length !== 1) throw new Error('Style Sub cannot preserve size ' + name);
        }
      }
      runInAngular(s, () => {
        if (expected.split !== (Number(item.splitIntoSizes) !== 0)) throw new Error('Style Sub changed the product sizing mode.');
        if (!expected.split) {
          if (Number(item.quantity) !== expected.quantity) {
            item.quantity = expected.quantity;
            s.watchItemQuantityChanges(item, {designKey:di,itemKey:itemIndex});
          }
          if (Number(item.pricePerPiece) !== Number(price)) {
            item.pricePerPiece = price;
            s.watchItemChanges(item);
          }
        } else {
          for (const z of sizes) {
            const quantity = wanted.get(label(z)) || 0;
            if (Number(z.quantity) !== quantity) {
              z.quantity = quantity;
              s.watchSizeQuantityChanges(item, z, {designKey:di,itemKey:itemIndex,sizeKey:item.sizes.indexOf(z)});
            }
            const nextPrice = quantity > 0 ? price : '0.00';
            if (Number(z.pricePerPiece) !== Number(nextPrice)) {
              z.pricePerPiece = nextPrice;
              s.watchSizeChanges(item, z);
            }
          }
        }
      });
      return true;
    """, index, item_index, expected, str(unit_price))


def _assert_clone(source, clone, unit_price):
    if clone["name"] != "REVERSE-PRINT" or len(clone["items"]) != len(source["items"]):
        raise ReversePrintError("Reverse-print clone name or product count is incorrect.")
    if clone["areas"] != source["areas"]:
        raise ReversePrintError("Reverse-print clone does not retain the source print areas and methods.")
    for original, actual in zip(source["items"], clone["items"]):
        if not actual["is_style_sub"] or _key(actual["catalog_style"]) not in {"stylesub", "style_sub"}:
            raise ReversePrintError("Reverse-print product is not Style Sub.")
        for field in ("vendor", "style", "color", "description"):
            value = reverse_color(original[field]) if field == "color" else original[field]
            if _key(value) != _key(actual[field]):
                raise ReversePrintError(f"Reverse-print {field} did not retain its expected value.")
        if _quantities(original) != _quantities(actual):
            raise ReversePrintError("Reverse-print size quantities differ from the original.")
        prices = [z["price"] for z in actual["sizes"] if z["quantity"] > 0] if actual["split"] else [actual["price"]]
        if any(Decimal(price or "-1") != Decimal(unit_price) for price in prices):
            raise ReversePrintError("Reverse-print unit prices did not retain the per-area charge.")


def apply_reverse_prints(driver, selections):
    # Load every selected tab's form before taking a source snapshot.
    for selection in selections:
        _select_tab(driver, selection["tab_number"] - 1)
        _block(driver, selection["tab_number"] - 1, 0)
    designs = read_designs(driver)
    jobs, used = [], set()
    for selection in selections:
        source = designs[selection["tab_number"] - 1]
        _validate_source(source)
        clone = find_clone(source, designs, used)
        if clone:
            used.add(clone["index"])
        jobs.append({"source": source, "clone_index": clone["index"] if clone else None,
                     "unit_price": str(selection["reverse_unit_price"]), "created": clone is None})
    # Identical originals cannot safely share an unmarked legacy clone.
    for job in jobs:
        if job["clone_index"] is None:
            potential = find_clone(job["source"], designs)
            if potential is not None and potential["index"] in used:
                raise ReversePrintError("An existing reverse-print clone matches more than one selected source tab.")
    for job in jobs:
        source = job["source"]
        if job["clone_index"] is None:
            job["clone_index"] = shared._order_scope(driver, r"""
              const source = Number(arguments[0]), count = r.designs.length;
              runInAngular(s, () => {
                s.duplicateDesign(source);
                if (r.designs.length !== count + 1) throw new Error('CRM did not create exactly one clone.');
                const clone = r.designs[count];
                clone.PO = 'REVERSE-PRINT';
                if (typeof s.watchDesignChanges === 'function') s.watchDesignChanges(clone, count);
              });
              return count;
            """, source["index"])
        index = job["clone_index"]
        _select_tab(driver, index)
        for ordinal, item in enumerate(source["items"]):
            _convert_product(driver, index, ordinal, item, job["unit_price"])
        _assert_clone(source, read_designs(driver)[index], job["unit_price"])
    return {"jobs": jobs}


def verify_reverse_prints(driver, mutation):
    designs = read_designs(driver)
    for job in mutation["jobs"]:
        source = job["source"]
        actual_source = designs[source["index"]]
        # Fields obtained from catalog controls may not be mounted after save.
        # Verify persisted source identity, sizes and original prices independently.
        if source["id"] != actual_source["id"] or source["name"] != actual_source["name"]:
            raise ReversePrintError("The original tab identity changed during reverse printing.")
        if len(source["items"]) != len(actual_source["items"]):
            raise ReversePrintError("The original product count changed during reverse printing.")
        for before, after in zip(source["items"], actual_source["items"]):
            if (before["catalog_style"], before["sizes"], before["quantity"], before["price"]) != (after["catalog_style"], after["sizes"], after["quantity"], after["price"]):
                raise ReversePrintError("The original product quantities or prices changed during reverse printing.")
        _assert_clone(source, designs[job["clone_index"]], job["unit_price"])
    return True
