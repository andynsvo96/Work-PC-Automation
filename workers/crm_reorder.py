"""Explicit-order stock replacement. Never invoked by reports or normal ordering."""

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

import config
import crm_shipping_bypasser as bypass
import ssactivewear_ordering as sxs
from automation_runtime import (
    build_chrome_driver, configure_console_utf8, resolve_existing_automation_profile_path,
    safe_driver_quit, safe_take_screenshot, write_result_payload, write_status_payload,
)
from runtime_paths import state_file

AUTOMATION_NAME = "crm.reorder"
RECEIPT_DIR = state_file("stock_reorders")
PHILLY_SUBCONTRACTOR = re.compile(r"^M6 Print and Ship to Philly(?:\s*\(PA\))?$", re.I)

STOCK_ROWS_SCRIPT = sxs.DOM + r"""
const rows = [...document.querySelectorAll('table.goods-ordered tbody tr[ng-repeat*="inventoryOrder"]')].filter(visible);
const records = rows.map(row => {
 let model = null;
 try { model = window.angular?.element(row).scope()?.inventoryOrder; } catch (_) {}
 let assigned = typeof model?.assignedToCurrentSubcontractor === 'boolean' ? model.assignedToCurrentSubcontractor : null;
 // Archived pages and a few legacy views expose the evaluated inline style.
 // Never use the computed color: dark mode can override it.
 const rule = row.getAttribute('ng-style') || '';
 const inline = row.style.backgroundColor.replace(/\s/g,'');
 if(assigned===null && rule.includes('inventoryOrder.assignedToCurrentSubcontractor')) {
   if(inline==='rgb(234,152,153)') assigned=false;
   else if(inline==='rgb(253,253,149)') assigned=true;
 }
 const cells=[...row.children].map(c=>norm(c.innerText));
 return {id:String(model?.id || ''),vendor:cells[0]||'',po:cells[1]||'',vendor_order_number:cells[2]||'',shipped_to:cells[5]||'',assigned};
});
const active=document.querySelector('#order-design-details li.order-form-design-tab.active');
return {records,design_tab_id:active?.id||''};
"""


def vendor_key(vendor):
    value = re.sub(r"[^a-z0-9]", "", str(vendor).lower())
    if value in {"sanmar", "sanmarbulk"}:
        return "sanmar"
    if value in {"ss", "ssactivewear", "sandsactivewear"}:
        return "ssactivewear"
    raise RuntimeError(f"Unsupported stock supplier: {vendor}.")


def vendor_label(key):
    return "Sanmar" if key == "sanmar" else "S&S Activewear"


def _read_rows(driver):
    state = driver.execute_script(STOCK_ROWS_SCRIPT)
    if not isinstance(state, dict) or not isinstance(state.get("records"), list):
        raise RuntimeError("CRM stock records could not be inspected.")
    return state


def scan_order(driver, order_id):
    bypass._open_target_order(driver, order_id, shipping_filter="all", list_url_override=None)
    bypass._require_crm_order_ready_once_with_refresh(driver, order_id)
    tabs = bypass._find_stock_tabs(driver)
    if not tabs:
        raise RuntimeError("CRM design tabs could not be established; review this order.")
    scanned = []
    order = None
    for index, tab in enumerate(tabs, 1):
        if bypass._activate_stock_tab(driver, index - 1) is None:
            raise RuntimeError("A CRM design tab could not be selected.")
        data = bypass._extract_order_data(driver, order_id, tab_context=tab)
        if data.get("subcontractor_banner_present") and not PHILLY_SUBCONTRACTOR.fullmatch(data.get("subcontractor", "")):
            raise RuntimeError("Reorder requires in-house production or M6 Print and Ship to Philly (PA).")
        order = order or data
        state = _read_rows(driver)
        records = state["records"]
        if any(x.get("assigned") is None for x in records):
            raise RuntimeError("A stock record's destination is unclear; review before reordering.")
        red = [x for x in records if x.get("assigned") is False]
        if not red:
            continue
        suppliers = {vendor_key(x.get("vendor")) for x in red}
        if len(suppliers) != 1:
            raise RuntimeError("One design has red POs from multiple suppliers; review the quantities.")
        if any(not x.get("po") for x in red):
            raise RuntimeError("A red stock PO is missing.")
        products = data.get("products") or []
        if not products or any(not p.get("product_id") or not p.get("color") or not p.get("quantities") for p in products):
            raise RuntimeError("A red-stock design has incomplete product/color/size quantities.")
        scanned.append({
            "tab_index": index, "design_tab_id": state.get("design_tab_id"),
            "tab_label": bypass._stock_tab_summary_label(tab.get("label")),
            "vendor": next(iter(suppliers)), "red_records": red,
            "products": products, "valid_records": [x for x in records if x.get("assigned") is True],
        })
    return order or {}, scanned


def snapshot(tabs):
    return [{k: tab[k] for k in ("tab_index", "design_tab_id", "tab_label", "vendor", "red_records", "products")} for tab in tabs]


def signature(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load_receipt(order_id):
    path = Path(RECEIPT_DIR) / f"{order_id}.json"
    if not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("version") != 1 or receipt.get("order_id") != order_id or receipt.get("customer_po") != order_id or not isinstance(receipt.get("groups"), list):
            raise ValueError("invalid receipt")
        for group in receipt["groups"]:
            if group.get("state") not in {"planned", "submitting", "uncertain", "confirmed"} or group.get("vendor") not in {"sanmar", "ssactivewear"}:
                raise ValueError("invalid purchase state")
            if group["state"] == "confirmed" and (not group.get("confirmation", {}).get("po_confirmed") or group["confirmation"].get("po") != order_id):
                raise ValueError("missing confirmed purchase")
        return receipt
    except (ValueError, OSError, TypeError) as exc:
        raise RuntimeError("Reorder receipt cannot be read. Review existing purchases before retrying.") from exc


def save_receipt(receipt):
    path = Path(RECEIPT_DIR) / f"{receipt['order_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(bypass._json_safe(receipt), indent=2), encoding="utf-8")
    os.replace(temporary, path)


def aggregate_products(tabs, vendor):
    merged = {}
    for tab in tabs:
        if tab["vendor"] != vendor:
            continue
        for product in tab["products"]:
            key = (product["product_id"], product["product_name"], product["color"])
            if key not in merged:
                merged[key] = dict(product, quantities={})
            for size, quantity in product["quantities"].items():
                if not isinstance(quantity, int) or quantity <= 0:
                    raise RuntimeError("Invalid CRM stock quantity.")
                merged[key]["quantities"][size] = merged[key]["quantities"].get(size, 0) + quantity
    return list(merged.values())


def sanmar_groups(driver, products):
    lines = []
    for product in products:
        options = bypass._sanmar_search_options_for_product(product)
        bypass._search_sanmar_product(driver, options["search_id"], click_inventory_button=options.get("click_inventory_button"), expected_style_keys=options.get("expected_style_keys"))
        bypass._select_sanmar_color(driver, product["color"], product=product)
        inventory = bypass._wait_for_sanmar_inventory(driver, options["search_id"])
        inventory = bypass._exclude_closed_sanmar_warehouses(inventory, bypass._sanmar_closed_warehouses(driver))
        lines.append({"product": product, **options, "quantities": bypass._sanmar_quantities_for_product(product), "inventory": inventory})
    _, plan = bypass._choose_warehouse_plan(lines, "inhouse")
    if not plan:
        raise RuntimeError("SanMar cannot fill all required stock with the existing stock buffer.")
    plans = [plan["nj_plan"], plan["add_plan"]] if plan["mode"] == "nj_pickup_and_add" else [plan]
    return [{"vendor": "sanmar", "plan": p, "lines": p["expanded_lines"], "state": "planned"} for p in plans]


def sxs_group(driver, products):
    lines = []
    for product in products:
        selected = sxs.open_product(driver, product)
        quantities = {sxs.size_key(s): q for s, q in product["quantities"].items()}
        lines.append({"product": product, "selected": selected, "quantities": quantities, "inventory": sxs.inventory(driver)})
    return {"vendor": "ssactivewear", "lines": sxs.allocate(lines), "state": "planned"}


def date_changes(order, eta):
    due = order.get("due_date")
    if not due:
        raise RuntimeError("CRM due date is missing.")
    target = bypass._shipping_bypasser_production_target_for_eta(eta)
    free = order.get("shipping_class") == "free"
    if not free and target >= due:
        raise RuntimeError("Paid/rush stock would arrive too late for production. Review required; no further purchase was submitted.")
    production = max(target, order.get("production_date") or target)
    if free:
        due = max(due, bypass._next_business_day_after(production))
    return production, due


def _as_date(value):
    return date.fromisoformat(value) if isinstance(value, str) else value


def prepare_group(driver, group, order):
    po = order["order_id"]
    if group["vendor"] == "sanmar":
        # A saved allocation never silently changes warehouses after a partial purchase.
        for line in group["lines"]:
            bypass._search_sanmar_product(driver, line["search_id"], click_inventory_button=line.get("click_inventory_button", False), expected_style_keys=line.get("expected_style_keys"))
            bypass._select_sanmar_color(driver, line["product"]["color"], product=line["product"])
            inventory = bypass._exclude_closed_sanmar_warehouses(bypass._wait_for_sanmar_inventory(driver, line["search_id"]), bypass._sanmar_closed_warehouses(driver))
            available = bypass._inventory_by_warehouse(inventory).get(line["warehouse"], {}).get("stock", {})
            if not all(bypass._stock_qty_can_cover_order(available.get(size, 0), qty) for size, qty in line["quantities"].items()):
                raise RuntimeError("The saved SanMar warehouse allocation is no longer available.")
        state = bypass._prepare_sanmar_shipping_plan(driver, po, order, group["lines"], group["plan"], check_closed_warehouses=True, nj_pickup_order=True)
        if not state.get("success"):
            raise RuntimeError(state.get("message") or "SanMar checkout could not be prepared.")
        if state["shipping"]["ship_mode"] == "ship":
            if not state.get("eta"):
                raise RuntimeError("SanMar delivery date is unavailable; review required.")
            bypass._click_sanmar_button(driver, r"Proceed\s+To\s+Payment")
        bypass._wait_for_text(driver, r"Review\s+&\s+Submit|Review\s+and\s+Submit|Customer\s+PO", timeout=20)
        requirements = {"order_type": "inhouse", "shipping": state["shipping"]}
        bypass._fill_review_and_submit(driver, po, dry_run=True, shipping_requirements=requirements)
        return {"eta": state.get("eta") or date.today(), "shipping_requirements": requirements}
    by_product = defaultdict(list)
    for line in group["lines"]:
        by_product[(line["product"]["product_id"], line["product"]["color"])].append(line)
    for lines in by_product.values():
        selected = sxs.open_product(driver, lines[0]["product"])
        if selected["url"] != lines[0]["selected"]["url"]:
            raise RuntimeError("S&S product identity changed since the saved allocation.")
        sxs.fill_product(driver, lines)
    return sxs.checkout(driver, po, group["lines"])


def _empty(driver, vendor, owned=False):
    if vendor == "ssactivewear":
        if owned:
            # Remove only the fully verified cart created by this run, never a pre-existing cart.
            while sxs.cart_rows(driver):
                before = sxs.cart_rows(driver)
                sxs._click(driver, '#M_M_zCart .cR a[aria-label="remove item"]')
                sxs._wait(driver, lambda: sxs.cart_rows(driver) != before)
        else:
            sxs.require_empty_cart(driver)
    elif owned:
        result = bypass._clear_sanmar_cart(driver)
        if not result.get("success"):
            raise RuntimeError("SanMar Reorder cart could not be emptied; review before retrying.")
    else:
        bypass.safe_get_with_partial_load(driver, bypass.SANMAR_CART_URL, "SanMar cart")
        if bypass._sanmar_cart_has_items(driver):
            raise RuntimeError("SanMar cart already contains stock; review before Reorder.")


def verify_record(driver, tab, order_id):
    if bypass._activate_stock_tab(driver, tab["tab_index"] - 1) is None:
        raise RuntimeError("CRM replacement design tab could not be selected.")
    rows = _read_rows(driver)["records"]
    matches = [x for x in rows if x.get("assigned") is True and x.get("po") == order_id and vendor_key(x.get("vendor")) == tab["vendor"]]
    return len(matches) == 1


def finish_crm(driver, order_id, tabs, receipt):
    for tab in tabs:
        if not verify_record(driver, tab, order_id):
            bypass._record_crm_manual_order(driver, order_id, order_id, stock_tab_index=tab["tab_index"], vendor_name=vendor_label(tab["vendor"]), add_box=True)
        bypass._open_target_order(driver, order_id, shipping_filter="all", list_url_override=None)
        bypass._require_crm_order_ready_once_with_refresh(driver, order_id)
        if not verify_record(driver, tab, order_id):
            raise RuntimeError("The replacement CRM PO did not persist at the current production destination.")
    notes = []
    for group in receipt["groups"]:
        confirmation = group["confirmation"]
        reference = confirmation.get("web_reference") or confirmation.get("order_number") or "confirmed"
        details = "; ".join(f"{line['product']['product_id']} {line['product']['color']} " + ", ".join(f"{s} x{q}" for s, q in line["quantities"].items()) + f" ({line['warehouse']})" for line in group["lines"])
        notes.append(f"Reordered stock - PO {order_id} - {vendor_label(group['vendor'])} {reference}: {details}")
    for note in notes:
        bypass._save_and_verify_crm_production_note(driver, order_id, note)
    receipt["complete"] = True
    save_receipt(receipt)


def process_order(crm, vendors, order_id, dry_run=False):
    order, tabs = scan_order(crm, order_id)
    receipt = load_receipt(order_id)
    current_signature = signature(snapshot(tabs))
    if receipt and receipt.get("source_signature") != current_signature:
        raise RuntimeError("CRM products, design tabs, red source POs or destinations changed since this Reorder. Review saved purchases.")
    if receipt and any(g.get("state") in {"submitting", "uncertain"} for g in receipt["groups"]):
        raise RuntimeError("A previous Reorder submission is unconfirmed. Review the supplier order history; automatic resubmission is blocked.")
    if not tabs:
        return {"outcome": "no_red_stock", "message": "No red vendor POs require reordering.", "groups": []}
    if not receipt and any(t["valid_records"] for t in tabs):
        raise RuntimeError("Mixed red and current-destination stock records require quantity review; no replacement was ordered.")
    if receipt:
        confirmed = all(g.get("state") == "confirmed" for g in receipt["groups"])
        for tab in tabs:
            if any(not confirmed or r.get("po") != order_id or vendor_key(r.get("vendor")) != tab["vendor"] for r in tab["valid_records"]):
                raise RuntimeError("Mixed stock records are not accounted for by the confirmed Reorder receipt. Review required.")
    order.update(order_id=order_id, po=order_id, order_type="inhouse")
    order["shipping_class"] = bypass._order_shipping_class_from_text(bypass._normalize_text(crm.execute_script("return document.body.innerText;")))
    if order["shipping_class"] not in {"free", "rush"}:
        raise RuntimeError("CRM shipping charge could not be verified as free or paid.")
    required_vendors = sorted({t["vendor"] for t in tabs})
    if not receipt or not all(g.get("state") == "confirmed" for g in receipt["groups"]):
        for vendor in required_vendors:
            driver = vendors(vendor)
            (bypass._ensure_sanmar_logged_in(driver) if vendor == "sanmar" else sxs.ensure_login(driver))
            _empty(driver, vendor)
    if not receipt:
        groups = []
        for vendor in required_vendors:
            products = aggregate_products(tabs, vendor)
            if vendor == "sanmar":
                groups.extend(sanmar_groups(vendors(vendor), products))
            else:
                groups.append(sxs_group(vendors(vendor), products))
        receipt = {"version": 1, "order_id": order_id, "customer_po": order_id, "source_signature": current_signature, "source_tabs": snapshot(tabs), "groups": groups}
    if not receipt["groups"]:
        raise RuntimeError("No complete replacement purchase plan was created.")
    # Preflight every supplier/cart before buying any stock. Remove only carts that
    # exactly matched this run, so a later supplier failure cannot submit a partial order.
    for group in receipt["groups"]:
        if group["state"] == "confirmed":
            continue
        driver = vendors(group["vendor"])
        state = prepare_group(driver, group, order)
        group["review"] = bypass._json_safe(state)
        if group["vendor"] == "ssactivewear":
            bypass.safe_get_with_partial_load(driver, sxs.CART_URL, "S&S Reorder preflight cart")
            sxs.validate_cart(sxs.verified_cart_rows(driver, group["lines"]), group["lines"])
        _empty(driver, group["vendor"], owned=True)
    eta = max(_as_date(g["review"]["eta"]) for g in receipt["groups"])
    production, due = date_changes(order, eta)
    if dry_run:
        return {"outcome": "reorder_ready", "message": f"Reorder is ready using customer PO {order_id}; no purchase or CRM save was submitted.", "groups": receipt["groups"], "production_date": str(production), "due_date": str(due)}
    save_receipt(receipt)
    if order.get("due_date") != due:
        bypass._change_crm_due_date(crm, order_id, due)
    if order.get("production_date") != production:
        bypass._change_crm_production_date(crm, order_id, production)
    order.update(production_date=production, due_date=due)
    for group in receipt["groups"]:
        if group["state"] == "confirmed":
            continue
        driver = vendors(group["vendor"])
        _empty(driver, group["vendor"])
        state = prepare_group(driver, group, order)
        next_production, next_due = date_changes(order, state["eta"])
        if (next_production, next_due) != (production, due):
            raise RuntimeError("Supplier delivery dates changed after preflight. Review the saved allocation before purchasing.")
        fresh_order, fresh_tabs = scan_order(crm, order_id)
        if signature(snapshot(fresh_tabs)) != current_signature:
            raise RuntimeError("CRM stock changed during checkout. No further purchase was submitted.")
        if any(t["valid_records"] for t in fresh_tabs):
            raise RuntimeError("New current-destination stock appeared during checkout. Review required before purchasing.")
        if fresh_order.get("production_date") != production or fresh_order.get("due_date") != due:
            raise RuntimeError("CRM production or due date changed during checkout. Review required before purchasing.")
        current_shipping_class = bypass._order_shipping_class_from_text(bypass._normalize_text(crm.execute_script("return document.body.innerText;")))
        if current_shipping_class != order["shipping_class"]:
            raise RuntimeError("CRM free/paid shipping changed during checkout. Review required before purchasing.")
        group["state"] = "submitting"
        save_receipt(receipt)  # A crash from here can never automatically repeat the purchase.
        try:
            if group["vendor"] == "sanmar":
                bypass._fill_review_and_submit(driver, order_id, shipping_requirements=state["shipping_requirements"])
                confirmation = bypass._capture_sanmar_confirmation(driver, order_id, order_id)
            else:
                confirmation = sxs.submit(driver, order_id)
            if not confirmation or not confirmation.get("po_confirmed") or confirmation.get("po") != order_id:
                raise RuntimeError("The exact Reorder PO was not confirmed by the supplier.")
            group.update(state="confirmed", confirmation=confirmation)
            save_receipt(receipt)
        except Exception:
            group["state"] = "uncertain"
            save_receipt(receipt)
            raise
    finish_crm(crm, order_id, tabs, receipt)
    return {"outcome": "reordered", "message": f"Stock reordered and verified in CRM under customer PO {order_id}.", "groups": receipt["groups"]}


def run(order_id, dry_run=False, visible=False, result_file=None):
    if not re.fullmatch(r"\d{7}", str(order_id)):
        raise ValueError("Reorder requires one explicit seven-digit CRM order number.")
    drivers = {}
    configure_console_utf8()
    success = False
    payload = {}
    with ExitStack() as stack:
        try:
            stack.enter_context(bypass._worker_profile_lock(bypass.PROFILE_PATH))
            crm = bypass._build_crm_session_driver(bypass.PROFILE_PATH, headless_mode=not visible, profile_label=f"CRM Reorder {order_id}")
            drivers["crm"] = (crm, bypass.PROFILE_PATH)
            def vendor_driver(vendor):
                if vendor not in drivers:
                    profile = bypass.SANMAR_PROFILE_PATH if vendor == "sanmar" else resolve_existing_automation_profile_path(os.path.join(bypass.SCRIPT_DIR, getattr(config, "SSACTIVEWEAR_PROFILE_DIR", "chrome_profile_ssactivewear")))
                    stack.enter_context(bypass._worker_profile_lock(profile))
                    driver = bypass._build_sanmar_driver(visible=visible) if vendor == "sanmar" else build_chrome_driver(profile, headless_mode=not visible, page_load_timeout=config.CRM_PAGE_LOAD_TIMEOUT)
                    drivers[vendor] = (driver, profile)
                return drivers[vendor][0]
            write_status_payload(AUTOMATION_NAME, f"Reordering stock for order {order_id}.", stage="reading_red_stock", order_id=order_id, current=1, total=1)
            payload = process_order(crm, vendor_driver, order_id, dry_run=dry_run)
            success = True
            message = payload["message"]
        except Exception as exc:
            message = str(exc)
            payload.update(manual_review_required=True, outcome="review_required")
            for vendor, (driver, _profile) in drivers.items():
                safe_take_screenshot(driver, f"reorder_{order_id}_{vendor}_review")
        finally:
            for driver, profile in reversed(list(drivers.values())):
                safe_driver_quit(driver, profile_path=profile)
    payload.update(order_id=order_id, customer_po=order_id, dry_run=bool(dry_run), action="reorder_stock")
    write_result_payload(AUTOMATION_NAME, "crm_reorder.py", success, message, payload, result_file=result_file)
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--visible", action="store_true")
    parser.add_argument("--result-file")
    args = parser.parse_args()
    raise SystemExit(0 if run(args.order_id, args.dry_run, args.visible, args.result_file) else 1)
