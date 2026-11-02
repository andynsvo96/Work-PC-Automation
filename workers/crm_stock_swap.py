"""Email one order's saved stock swaps and record its confirmed reachout in CRM."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

WORKERS_DIR = str(Path(__file__).resolve().parent)
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)
from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

import crm_copyright_cancel as shared
from runtime_paths import STATE_DIR
from salesforce_templates import STOCK_SWAP_TEMPLATE_NAME, default_template_store
from workers.crm_auto_splitter import _quote_scope
from workers import salesforce_activity_confirmation as confirmation
from workers import salesforce_template_composer as composer


AUTOMATION_KEY = "stock_swap"
AUTOMATION_NAME = "crm.stock_swap"
QUOTE_URL = "https://crm2.legacy.printfly.com/app#/quotes/{quote_id}"


class StockSwapError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


# Orders expose designItems; original quotes expose items and lowercase po.
# Style Sub overrides the catalog product on either page.
SNAPSHOT_JS = r"""
function clean(v) { return typeof v === 'string' || typeof v === 'number' ? String(v).replace(/\s+/g, ' ').trim() : ''; }
function active(xs) { return (xs || []).filter(x => x && x.crudAction !== 'd'); }
const blocks = Array.from(document.querySelectorAll('[ng-repeat="($itemIndex, item) in design.designItems"]'));
return {
  id: clean(r.id), order_id: clean(r.orderId), quote_id: clean(r.quoteId), account_id: clean(r.accountId),
  editing: !!s.editMode,
  designs: active(r.designs).map(d => ({
    id: clean(d.id), artwork_id: clean(d.designId), name: clean(d.PO) || clean(d.po),
    items_loaded: Array.isArray(d.designItems) || Array.isArray(d.items),
    items: active(Array.isArray(d.designItems) ? d.designItems : d.items).map(item => {
      const sc = blocks.map(el => angular.element(el).scope()).find(sc => sc && sc.item === item);
      const form = sc && sc.formItem || {}, cat = form.selectedCatalogItem || {};
      const subs = active(item.styleSubs), sub = subs[0] || {};
      const substituted = Number(item.isStyleSub) === 1 || item.isStyleSub === true;
      if (substituted && subs.length !== 1) throw new Error('Ambiguous or missing Style Sub product.');
      return {
        id: clean(item.id),
        style: substituted ? clean(sub.style) : clean(item.style || form.selectedCatalogItemStyle),
        color: substituted ? clean(sub.color) : (clean(item.color) || clean(item.colorName) || clean((form.selectedColor || {}).name)),
        description: substituted ? clean(sub.description) : clean(item.ourLabel || item.label || cat.ourLabel || cat.label),
        quantity: item.quantity, split: Number(item.splitIntoSizes) !== 0,
        sizes: active(item.sizes).map(z => ({
          label: clean(z.sizeCode || (z.size || {}).name || (z.size || {}).description || z.size || z.label || z.name || z.sizeName || z.sizeType),
          quantity: z.quantity,
        })),
      };
    }),
  })),
};
"""


def _read_snapshot(driver, order_id, *, quote=False, quote_id=None, account_id=None, timeout=45):
    deadline = time.monotonic() + timeout
    last_error = "The page has not loaded."
    while True:
        try:
            shared._activate_crm_context(driver)
            if quote:
                snapshot = _quote_scope(driver, "const r = op;\n" + SNAPSHOT_JS.replace(
                    "id: clean(r.id), order_id: clean(r.orderId), quote_id: clean(r.quoteId), account_id: clean(r.accountId)",
                    "id: clean(q.id), order_id: clean(q.orderId), quote_id: clean(q.id), account_id: clean(q.accountId)"))
                linked_order = snapshot.get("order_id")
                if ((linked_order and linked_order != order_id)
                        or (snapshot.get("id") and snapshot["id"] != str(quote_id or order_id))
                        or (account_id and snapshot.get("account_id") and snapshot["account_id"] != str(account_id))):
                    raise StockSwapError("The original quote does not belong to this order; review it before sending.")
                if _quote_scope(driver, "return (q.options || []).length;") != 1:
                    raise StockSwapError("The original quote has multiple options; review which products were ordered.")
            else:
                snapshot = shared._order_scope(driver, SNAPSHOT_JS)
                if snapshot.get("id") and snapshot["id"] != order_id:
                    raise StockSwapError("CRM loaded a different order; no Stock Swap email was sent.")
            if not quote and snapshot.get("editing"):
                raise StockSwapError("Save the CRM changes before queueing Stock Swap.")
            ready = (bool(snapshot.get("id")) and bool(snapshot.get("designs"))
                     and all(design.get("items_loaded", True) for design in snapshot["designs"]))
            if quote and account_id and not snapshot.get("account_id"):
                ready = False
            if ready:
                return snapshot
            last_error = "CRM is still loading the order/quote identity, designs, or products."
        except StockSwapError:
            raise
        except Exception as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            raise StockSwapError(f"Could not read the {'original quote' if quote else 'saved order'}: {last_error}")
        time.sleep(0.5)


def _quantity(value):
    try:
        number = float(value)
    except (ValueError, TypeError):
        number = -1.0
    if isinstance(value, bool) or not number.is_integer() or number < 0:
        raise StockSwapError("A product quantity is unreadable; review the quote and order before sending.")
    return int(number)


def _inventory(design):
    inventory = {}
    for item in design.get("items", []):
        quantities = Counter()
        if item.get("split"):
            for size in item.get("sizes", []):
                count = _quantity(size.get("quantity"))
                if count:
                    label = shared._clean_text(size.get("label")).upper()
                    if not label:
                        raise StockSwapError("A product size is unreadable; review the quote and order.")
                    quantities[label] += count
            if not quantities and _quantity(item.get("quantity")):
                raise StockSwapError("A product's ordered sizes could not be read.")
        else:
            quantities[""] = _quantity(item.get("quantity"))
        quantities = +quantities
        if not quantities:
            continue
        fields = {key: shared._clean_text(item.get(key)) for key in ("style", "color", "description")}
        if not all(fields.values()):
            raise StockSwapError("A product style, color, or description is missing; review the quote and order.")
        key = (fields["style"].casefold(), fields["color"].casefold())
        record = inventory.setdefault(key, {**fields, "quantities": Counter()})
        record["quantities"].update(quantities)
    return inventory


def _match_designs(original, current):
    before, after = original.get("designs", []), current.get("designs", [])
    if not before or len(before) != len(after):
        raise StockSwapError("The quote and order have different design tabs; review the stock swaps manually.")
    key_functions = (
        lambda d: d.get("id"),
        lambda d: (d.get("artwork_id"), d.get("name")) if d.get("artwork_id") and d.get("name") else None,
        lambda d: d.get("artwork_id"),
        lambda d: d.get("name"),
    )
    matches, used = [], set()
    for design in before:
        match = None
        for key in key_functions:
            value = key(design)
            if not value or sum(key(d) == value for d in before) != 1:
                continue
            candidates = [index for index, d in enumerate(after) if key(d) == value]
            if len(candidates) == 1 and candidates[0] not in used:
                match = candidates[0]
                break
        if match is None:
            raise StockSwapError("Could not uniquely match quote and order design tabs; review before sending.")
        used.add(match)
        matches.append((design, after[match]))
    return matches


def find_swaps(original, current):
    """Subtract unchanged quantities, then pair only unambiguous replacements."""
    swaps = []
    for before, after in _match_designs(original, current):
        old, new = _inventory(before), _inventory(after)
        if old.keys() == new.keys() and all(old[key]["quantities"] == new[key]["quantities"] for key in old):
            continue
        identified = _swaps_by_item_id(before, after)
        if identified is not None:
            swaps.extend(identified)
            continue
        for key in old.keys() & new.keys():
            unchanged = old[key]["quantities"] & new[key]["quantities"]
            old[key]["quantities"].subtract(unchanged)
            new[key]["quantities"].subtract(unchanged)
        removed = [p for p in old.values() if +p["quantities"]]
        added = [p for p in new.values() if +p["quantities"]]
        if len(removed) == len(added) == 1 and sum(removed[0]["quantities"].values()) == sum(added[0]["quantities"].values()):
            # A single replacement is unambiguous even when the saved size labels
            # differ. Keep each side's sizes instead of assigning the old size to it.
            swaps.append({"design": before.get("name") or before.get("artwork_id"),
                          "stock": {**removed[0], "quantities": dict(+removed[0]["quantities"])},
                          "product": {**added[0], "quantities": dict(+added[0]["quantities"])}})
            continue
        sizes = dict.fromkeys(size for product in [*removed, *added] for size in +product["quantities"])
        pairs = {}
        for size in sizes:
            sources = [p for p in removed if p["quantities"][size] > 0]
            targets = [p for p in added if p["quantities"][size] > 0]
            if sum(p["quantities"][size] for p in sources) != sum(p["quantities"][size] for p in targets):
                raise StockSwapError("Stock swap quantities changed; review the quote and order before sending.")
            if len(sources) == 1:
                allocations = [(sources[0], target, target["quantities"][size]) for target in targets]
            elif len(targets) == 1:
                allocations = [(source, targets[0], source["quantities"][size]) for source in sources]
            else:
                raise StockSwapError("Multiple products match a stock replacement; review before sending.")
            for source, target, count in allocations:
                key = (source["style"], source["color"], target["style"], target["color"])
                pair = pairs.setdefault(key, {"design": before.get("name") or before.get("artwork_id"),
                    "stock": {**source, "quantities": {}}, "product": {**target, "quantities": {}}})
                pair["stock"]["quantities"][size] = count
                pair["product"]["quantities"][size] = count
        swaps.extend(pairs.values())
    if not swaps:
        raise StockSwapError("No saved stock swaps were found between the original quote and current order.")
    return swaps


def _swaps_by_item_id(before, after):
    """Saved product IDs disambiguate equal-quantity swaps on the same design."""
    old_items, new_items = before.get("items", []), after.get("items", [])
    old = {p.get("id"): p for p in old_items if p.get("id")}
    new = {p.get("id"): p for p in new_items if p.get("id")}
    if len(old) != len(old_items) or len(new) != len(new_items) or old.keys() != new.keys():
        return None
    swaps = []
    for item_id, old_item in old.items():
        old_product, new_product = _inventory({"items": [old_item]}), _inventory({"items": [new[item_id]]})
        if not old_product and not new_product:
            continue
        if len(old_product) != 1 or len(new_product) != 1:
            return None
        original, replacement = next(iter(old_product.values())), next(iter(new_product.values()))
        if original["quantities"] != replacement["quantities"]:
            return None
        if old_product.keys() != new_product.keys():
            swaps.append({"design": before.get("name") or before.get("artwork_id"),
                          "stock": {**original, "quantities": dict(original["quantities"])},
                          "product": {**replacement, "quantities": dict(replacement["quantities"])}})
    return swaps


def placeholder_values(order_id, swaps):
    def label(product):
        sizes = ", ".join(f"{size}: {count}" for size, count in product["quantities"].items() if size)
        text = f"{product['style']} {product['color']} {product['description']}"
        return f"{text} ({sizes})" if sizes else text

    def listing(key):
        return "; ".join((f"{index}. " if len(swaps) > 1 else "") + label(swap[key])
                         for index, swap in enumerate(swaps, 1))

    return {"[ORDER-NUMBER]": order_id, "[STOCK]": listing("stock"), "[PRODUCT]": listing("product")}


def format_sales_note(order_id, swaps):
    values = placeholder_values(order_id, swaps)
    return f"Swapped {values['[STOCK]']} to {values['[PRODUCT]']}\nEmailed"


def _append_sales_note(driver, order_id, note, *, dry_run=False):
    """Append once, preserving drafts, and require the saved note after reload."""
    shared._activate_crm_context(driver)
    shared._wait_for_order_scope(driver, order_id=order_id)

    def saved_note_present():
        saved = shared._order_scope(driver, "return String(r.salesNotes || r.filteredSalesNotes || '');")
        normalize = lambda value: str(value or "").replace("\r\n", "\n").replace("\r", "\n").casefold()
        return normalize(note) in normalize(saved)

    if saved_note_present():
        return {"note": note, "updated": False, "already_present": True,
                "confirmed": True, "dry_run": bool(dry_run)}
    if dry_run:
        return {"note": note, "updated": False, "already_present": False, "dry_run": True}
    # Opening Edit Order can replace the resource and initialize its note field.
    previous_draft = shared._order_scope(driver, "return String(r.addSalesNotes || '').trim();")
    shared._product_separator._enter_edit_mode(driver)
    shared._order_scope(driver, r"""
        const note = arguments[0];
        const draft = String(r.addSalesNotes || '').trim();
        const normalize = text => String(text).replace(/\r\n?/g, '\n').toLowerCase();
        const previous = String(arguments[1] || '').trim();
        const merged = normalize(draft).includes(normalize(previous)) ? draft
          : normalize(previous).includes(normalize(draft)) ? previous : [previous, draft].filter(Boolean).join('\n');
        const after = normalize(merged).includes(normalize(note)) ? merged : [merged, note].filter(Boolean).join('\n');
        runInAngular(s, () => {
          r.addSalesNotes = after;
          if (s.order.setAddSalesNotes) s.order.setAddSalesNotes(after);
        });
        return true;
    """, note, previous_draft)
    save = shared._save_order_and_wait(driver)
    driver.refresh()
    shared._activate_crm_context(driver)
    shared._wait_for_order_scope(driver, order_id=order_id)
    if not saved_note_present():
        raise StockSwapError("The Stock Swap Sales Note did not persist after reload. "
                             "Retry the original task to recover the note without resending the confirmed email.")
    return {"note": note, "updated": True, "already_present": False,
            "confirmed": True, "dry_run": False, "save": save}


def _prepare_email(driver, crm_handle, order_id, customer_email, swaps, *, login_wait_seconds=0):
    store = default_template_store()
    template = store.get_template(STOCK_SWAP_TEMPLATE_NAME)
    if "[ORDER-NUMBER]" not in template["subject"] or any(
        token not in template["body_html"] for token in ("[STOCK]", "[PRODUCT]")
    ):
        raise StockSwapError("Keep [ORDER-NUMBER] in the Stock Swap title and [STOCK] and [PRODUCT] in its body. "
                             "Edit Settings > Salesforce Template before retrying.")
    sf_handle = shared._open_salesforce_account(
        driver, crm_handle, customer_email, order_id=order_id, login_wait_seconds=login_wait_seconds)
    shared._verify_salesforce_email(driver, customer_email)
    shared._click_salesforce_email(driver, customer_email)
    shared._wait_for_email_composer(driver)
    selected_from = shared._set_salesforce_from_orders(driver)
    values = placeholder_values(order_id, swaps)
    fill = composer.fill_template(driver, STOCK_SWAP_TEMPLATE_NAME, store=store, replacements=values)
    state = shared._read_salesforce_email_state(driver) or {}
    subject, body = (shared._clean_text(state.get(key)) for key in ("subject", "body"))
    if (subject != shared._clean_text(fill["state"]["subject"]) or order_id not in subject
            or any(shared._clean_text(values[token]) not in body for token in ("[STOCK]", "[PRODUCT]"))
            or re.search(r"\[[A-Z][A-Z0-9_-]*\]", subject + " " + body)):
        raise StockSwapError("The completed Stock Swap draft could not be verified; no email was sent.")
    recipients = shared._verify_salesforce_email_recipients(driver, customer_email)
    return {"subject": subject, "body": body, "sent": False, "dry_run": True,
            "salesforce_handle": sf_handle, "from": selected_from, "recipients": recipients,
            "template_source": "local_json", "template_revision": fill["revision"]}


def _send_email(driver, crm_handle, order_id, customer_email, swaps, *, dry_run=False, login_wait_seconds=0):
    # A per-order receipt survives subject edits. Never repeat a completed Stock
    # Swap email, or start a fresh resend budget after an uncertain submission.
    path = Path(STATE_DIR) / "stock_swap_emails" / f"{order_id}.json"
    fingerprint = hashlib.sha256(json.dumps([customer_email.casefold(), swaps], sort_keys=True).encode()).hexdigest()
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or previous.get("fingerprint") != fingerprint:
                raise ValueError("The stock swap or recipient changed.")
        except (OSError, ValueError) as exc:
            raise StockSwapError("The previous Stock Swap receipt cannot be reused. Review Salesforce Activity before retrying.") from exc
        if previous.get("confirmed") is True:
            return {**previous["salesforce"], "skipped": True}
    draft = _prepare_email(driver, crm_handle, order_id, customer_email, swaps, login_wait_seconds=login_wait_seconds)
    if dry_run:
        return draft
    if previous and (draft["subject"], draft["body"]) != (previous.get("subject"), previous.get("body")):
        raise StockSwapError("The Stock Swap draft changed after an earlier attempt. Review Salesforce Activity before retrying.")
    if previous is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = {"fingerprint": fingerprint, "subject": draft["subject"], "body": draft["body"], "confirmed": False}
        with path.open("x", encoding="utf-8") as stream:
            json.dump(previous, stream)
    result = confirmation.send_and_confirm(
        driver, order_id, draft["subject"], customer_email, shared._click_salesforce_send_button,
        body=draft["body"], receipt_scope=AUTOMATION_KEY,
        prepare_retry=lambda: _prepare_email(driver, crm_handle, order_id, customer_email, swaps,
                                             login_wait_seconds=login_wait_seconds),
    )
    salesforce = {**draft, **result, "dry_run": False, "sent": True}
    confirmation._write_receipt(path, {**previous, "confirmed": True, "salesforce": salesforce})
    return salesforce


def process_stock_swap_order(order_id, *, dry_run=False, visible=False, progress_callback=None, login_wait_seconds=0):
    order_id = shared._normalize_order_id(order_id)
    order_url = shared.PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
    result = {"order_id": order_id, "order_url": order_url,
              "automation": AUTOMATION_KEY, "dry_run": bool(dry_run), "stages": [], "success": False}
    driver = None
    stage = "browser_start"

    def begin(key, message):
        nonlocal stage
        stage = key
        if callable(progress_callback):
            progress_callback(key, message)

    try:
        begin("browser_start", f"Reading the saved products for order {order_id}.")
        driver = shared._open_driver(visible=visible)
        shared.safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        shared._login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        current = _read_snapshot(driver, order_id)
        quote_id = str(current.get("quote_id") or "").strip()
        if not re.fullmatch(r"[1-9]\d{0,11}", quote_id):
            raise StockSwapError("The saved order has no readable original quote ID; review before sending.")
        result.update({"quote_id": quote_id, "quote_url": QUOTE_URL.format(quote_id=quote_id)})
        crm_handle = driver.current_window_handle
        contact = shared._wait_for_crm_contact_info(driver, order_id=order_id)
        begin("original_quote", f"Reading the original quote for order {order_id}.")
        shared.safe_get_with_partial_load(driver, result["quote_url"], f"Original quote for {order_id}")
        shared._login_to_crm_if_needed(driver, result["quote_url"], login_wait_seconds=login_wait_seconds)
        original = _read_snapshot(driver, order_id, quote=True, quote_id=quote_id, account_id=current.get("account_id"))
        begin("stock_comparison", "Matching every saved stock swap with its original product.")
        swaps = find_swaps(original, current)
        # Re-read the persisted order before drafting to catch changes during the quote scan.
        shared.safe_get_with_partial_load(driver, order_url, f"CRM order {order_id} verification")
        verified = _read_snapshot(driver, order_id)
        if verified != current:
            raise StockSwapError("The saved order changed during the quote scan; review it and queue Stock Swap again.")
        note = format_sales_note(order_id, swaps)
        result.update({"swaps": swaps, "placeholders": placeholder_values(order_id, swaps), "sales_note_text": note})
        result["stages"].append({"key": "stock_comparison", "success": True, "details": {"swaps": swaps}})
        begin("salesforce_email", "Preparing one Stock Swap email covering every swap.")
        result["salesforce"] = _send_email(driver, crm_handle, order_id, contact["email"], swaps,
                                           dry_run=dry_run, login_wait_seconds=login_wait_seconds)
        result["stages"].append({"key": "salesforce_email", "success": True, "details": result["salesforce"]})
        if not dry_run and result["salesforce"].get("activity_verified") is not True:
            raise StockSwapError("Salesforce Activity has not confirmed the Stock Swap email; the Emailed note was not saved.")
        begin("sales_note", "Saving and verifying the Stock Swap Sales Note.")
        driver.switch_to.window(crm_handle)
        shared.safe_get_with_partial_load(driver, order_url, f"CRM order {order_id} Sales Note")
        result["sales_note"] = _append_sales_note(driver, order_id, note, dry_run=dry_run)
        result["stages"].append({"key": "sales_note", "success": True, "details": result["sales_note"]})
        result["success"] = True
        return result
    except Exception as exc:
        result.update({"failed_stage": stage, "error": str(exc), "error_type": type(exc).__name__})
        if driver is not None:
            shared.safe_take_screenshot(driver, f"stock_swap_{order_id}_{stage}_error")
        raise StockSwapError(f"Stock Swap stopped at {stage}: {exc}", result) from exc
    finally:
        if driver is not None:
            shared.safe_driver_quit(driver, profile_path=shared._profile_path())


def run_stock_swap_order(order_id, **kwargs):
    try:
        result = process_stock_swap_order(order_id, **kwargs)
        action = "Dry run prepared" if result["dry_run"] else "Completed"
        return True, f"{action} Stock Swap for order {result['order_id']} ({len(result['swaps'])} swap(s)).", result
    except (StockSwapError, shared.CopyrightCancelError) as exc:
        return False, str(exc), getattr(exc, "result", {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="Send the verified email; otherwise prepare a dry run.")
    mode.add_argument("--dry-run", action="store_true", help="Prepare and verify the draft without sending.")
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    ok, message, payload = run_stock_swap_order(
        args.order_id, dry_run=not args.real, visible=args.visible,
        progress_callback=lambda stage, message: shared.write_status_payload(
            AUTOMATION_NAME, message, stage=stage, order_id=args.order_id))
    shared.write_result_payload(AUTOMATION_NAME, os.path.basename(__file__), ok, message, extra_fields=payload)
    sys.exit(0 if ok else 1)
