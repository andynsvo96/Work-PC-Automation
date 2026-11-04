"""Send a single-order stock email with a required, user-entered replacement."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

WORKERS_DIR = str(Path(__file__).resolve().parent)
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)
from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from runtime_paths import STATE_DIR
from salesforce_templates import STOCK_ISSUE_ITEM_TEMPLATE_NAME, default_template_store
from workers import crm_stock_swap as stock_reader
from workers import crm_stock_issue_extension as extension
from workers import salesforce_activity_confirmation as confirmation
from workers import salesforce_template_composer as composer

shared = stock_reader.shared
AUTOMATION_KEY = "stock_issue_item"
AUTOMATION_NAME = "crm.stock_issue_item"
DISPLAY_NAME = "Suggest Different Item"
PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9_-]*\]")


class StockIssueItemError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


def _normalize_replacement(product):
    if not isinstance(product, str) or not product.strip():
        raise StockIssueItemError("Enter a suggested replacement product.")
    product = product.strip()
    if (len(product) > 500 or any(ord(char) < 32 or ord(char) == 127 for char in product)
            or "<" in product or ">" in product or PLACEHOLDER_RE.search(product)):
        raise StockIssueItemError(
            "The replacement product must be plain text, at most 500 characters, without template placeholders.")
    return re.sub(r"\s+", " ", product)


def normalize_request(product, products):
    try:
        selected = extension.normalize_selected_products(products)
    except extension.StockIssueExtensionError as exc:
        raise StockIssueItemError(str(exc).replace("Extension Required", DISPLAY_NAME)) from exc
    per_product = any("replacement_product" in row for row in products)
    if per_product and len(selected) != len(products):
        raise StockIssueItemError("Each scanned product/color must have one replacement; duplicate product rows need review.")
    replacements = [_normalize_replacement(row.get("replacement_product")) for row in products] if per_product else None
    # Preserve the original request format for queued tasks created before per-item inputs.
    legacy_product = None if per_product else _normalize_replacement(product)
    ids = []
    for index, item in enumerate(selected):
        if not item["design_item_ids"] or item["total_quantity"] is None:
            raise StockIssueItemError("Scan the CRM stock again; each selected item needs its saved ID and quantity.")
        ids.extend(item["design_item_ids"])
        # This workflow selects entire product/color rows, rather than individual sizes.
        item.pop("affected_sizes", None)
        item.pop("available_sizes", None)
        if per_product:
            item["replacement_product"] = replacements[index]
    if len(ids) != len(set(ids)):
        raise StockIssueItemError("The selected CRM stock contains duplicate product IDs; scan it again.")
    return {"products": selected} if per_product else {"product": legacy_product, "products": selected}


def verify_selected_stock(snapshot, products):
    """Use persisted CRM fields and stop if a queued selection has changed."""
    saved = {}
    for tab_number, design in enumerate(snapshot["designs"], 1):
        for item in design["items"]:
            item_id = f"design-item-{item['id']}"
            if item_id in saved:
                raise StockIssueItemError("CRM stock has ambiguous product IDs; review the order.")
            inventory = stock_reader._inventory({"items": [item]})
            if inventory:
                record = next(iter(inventory.values()))
                saved[item_id] = {**record, "tab_number": tab_number}
    verified = []
    for product in products:
        matches = [saved.get(item_id) for item_id in product["design_item_ids"]]
        if any(match is None for match in matches):
            raise StockIssueItemError("Selected CRM stock was removed or has no quantity; scan the order again.")
        for match in matches:
            if any(shared._clean_text(product[key]).casefold() != match[key].casefold()
                   for key in ("style", "description", "color")):
                raise StockIssueItemError("Selected CRM stock changed while queued; scan the order again.")
        quantity = sum(sum(match["quantities"].values()) for match in matches)
        if quantity != product["total_quantity"]:
            raise StockIssueItemError("Selected CRM stock quantity changed while queued; scan the order again.")
        verified.append({**product, **{key: matches[0][key] for key in ("style", "description", "color")},
                         "tab_numbers": sorted({match["tab_number"] for match in matches}),
                         "total_quantity": quantity})
    return verified


def placeholder_values(order_id, request):
    return {
        "[ORDER-NUMBER]": order_id,
        "[STOCK]": extension._natural_join([
            extension._format_stock_product(product) for product in request["products"]]),
        "[PRODUCT]": (extension._natural_join([row["replacement_product"] for row in request["products"]])
                      if "replacement_product" in request["products"][0] else request["product"]),
    }


def _prepare_email(driver, crm_handle, order_id, customer_email, request, *, login_wait_seconds=0):
    store = default_template_store()
    template = store.get_template(STOCK_ISSUE_ITEM_TEMPLATE_NAME)
    if "[ORDER-NUMBER]" not in template["subject"] or any(
            token not in template["body_html"] for token in ("[STOCK]", "[PRODUCT]")):
        raise StockIssueItemError(
            "Keep [ORDER-NUMBER] in the Suggest Different Item title and [STOCK] and [PRODUCT] in its body. "
            "Edit Settings > Salesforce Template before retrying.")
    sf_handle = shared._open_salesforce_account(
        driver, crm_handle, customer_email, order_id=order_id, login_wait_seconds=login_wait_seconds)
    shared._verify_salesforce_email(driver, customer_email)
    shared._click_salesforce_email(driver, customer_email)
    shared._wait_for_email_composer(driver)
    selected_from = shared._set_salesforce_from_orders(driver)
    values = placeholder_values(order_id, request)
    fill = composer.fill_template(driver, STOCK_ISSUE_ITEM_TEMPLATE_NAME, store=store, replacements=values)
    state = shared._read_salesforce_email_state(driver) or {}
    subject, body = (shared._clean_text(state.get(key)) for key in ("subject", "body"))
    if (subject != shared._clean_text(fill["state"]["subject"]) or order_id not in subject
            or any(shared._clean_text(values[token]) not in body for token in ("[STOCK]", "[PRODUCT]"))
            or PLACEHOLDER_RE.search(subject + " " + body)):
        raise StockIssueItemError("The Suggest Different Item draft could not be verified; no email was sent.")
    recipients = shared._verify_salesforce_email_recipients(driver, customer_email)
    return {"subject": subject, "body": body, "sent": False, "dry_run": True,
            "salesforce_handle": sf_handle, "from": selected_from, "recipients": recipients,
            "template_source": "local_json", "template_revision": fill["revision"]}


def _send_email(driver, crm_handle, order_id, customer_email, request, *, dry_run=False, login_wait_seconds=0):
    # Keep the request receipt independent of the editable title, including after an uncertain send.
    path = Path(STATE_DIR) / "stock_issue_item_emails" / f"{order_id}.json"
    identity = [{key: item[key] for key in ("style", "description", "color", "total_quantity")}
                | {"design_item_ids": sorted(item["design_item_ids"])}
                | ({"replacement_product": item["replacement_product"]} if "replacement_product" in item else {})
                for item in request["products"]]
    identity.sort(key=lambda item: item["design_item_ids"])
    def fingerprint_for(values):
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    per_product = "replacement_product" in identity[0]
    fingerprint = fingerprint_for([customer_email.casefold(), identity] if per_product else
                                  [customer_email.casefold(), request["product"], identity])
    compatible_fingerprints = {fingerprint}
    if per_product and len(identity) == 1:
        # A saved single-item email still has exactly the same stock/replacement pairing.
        legacy_identity = [{key: value for key, value in identity[0].items() if key != "replacement_product"}]
        compatible_fingerprints.add(fingerprint_for(
            [customer_email.casefold(), identity[0]["replacement_product"], legacy_identity]))
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(previous, dict) or previous.get("fingerprint") not in compatible_fingerprints:
                raise ValueError("The stock, replacement, or recipient changed.")
            if previous.get("confirmed") is True:
                salesforce = previous["salesforce"]
                if not isinstance(salesforce, dict) or salesforce.get("activity_verified") is not True:
                    raise ValueError("The confirmed email is missing its Activity verification.")
                return {**salesforce, "skipped": True}
        except (OSError, ValueError, KeyError) as exc:
            raise StockIssueItemError(
                "The previous Suggest Different Item receipt cannot be reused. Review Salesforce Activity before retrying.") from exc
    draft = _prepare_email(driver, crm_handle, order_id, customer_email, request,
                           login_wait_seconds=login_wait_seconds)
    if dry_run:
        return draft
    if previous and (draft["subject"], draft["body"]) != (previous.get("subject"), previous.get("body")):
        raise StockIssueItemError(
            "The Suggest Different Item draft changed after an earlier attempt. Review Salesforce Activity before retrying.")
    if previous is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        previous = {"fingerprint": fingerprint, "subject": draft["subject"], "body": draft["body"], "confirmed": False}
        with path.open("x", encoding="utf-8") as stream:
            json.dump(previous, stream)
    result = confirmation.send_and_confirm(
        driver, order_id, draft["subject"], customer_email, shared._click_salesforce_send_button,
        body=draft["body"], receipt_scope=AUTOMATION_KEY,
        prepare_retry=lambda: _prepare_email(driver, crm_handle, order_id, customer_email, request,
                                             login_wait_seconds=login_wait_seconds))
    if result.get("activity_verified") is not True:
        raise StockIssueItemError("Salesforce Activity has not confirmed the Suggest Different Item email.")
    salesforce = {**draft, **result, "dry_run": False, "sent": True}
    confirmation._write_receipt(path, {**previous, "confirmed": True, "salesforce": salesforce})
    return salesforce


def process_stock_issue_item_order(order_id, product, products, *, dry_run=False, visible=False,
                                   progress_callback=None, login_wait_seconds=0):
    order_id = shared._normalize_order_id(order_id)
    request = normalize_request(product, products)
    order_url = shared.PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
    result = {"order_id": order_id, "order_url": order_url, "automation": AUTOMATION_KEY,
              **request, "dry_run": bool(dry_run), "success": False, "stages": []}
    driver = None
    stage = "stock_verification"
    try:
        if callable(progress_callback):
            progress_callback(stage, "Verifying the selected stock on the saved CRM order.")
        driver = shared._open_driver(visible=visible)
        shared.safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        shared._login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        snapshot = stock_reader._read_snapshot(driver, order_id)
        request["products"] = verify_selected_stock(snapshot, request["products"])
        result.update({**request, "placeholders": placeholder_values(order_id, request)})
        result["stages"].append({"key": stage, "success": True})
        crm_handle = driver.current_window_handle
        contact = shared._wait_for_crm_contact_info(driver, order_id=order_id)
        stage = "salesforce_email"
        if callable(progress_callback):
            progress_callback(stage, "Preparing and verifying the Suggest Different Item email.")
        result["salesforce"] = _send_email(driver, crm_handle, order_id, contact["email"], request,
                                           dry_run=dry_run, login_wait_seconds=login_wait_seconds)
        result["stages"].append({"key": stage, "success": True, "details": result["salesforce"]})
        result["success"] = True
        return result
    except Exception as exc:
        if driver is not None:
            shared.safe_take_screenshot(driver, f"stock_issue_item_{order_id}_{stage}_error")
        message = str(exc).replace("Stock Swap", DISPLAY_NAME)
        result.update({"failed_stage": stage, "error": message, "error_type": type(exc).__name__})
        result["stages"].append({"key": stage, "success": False, "message": message})
        raise StockIssueItemError(f"{DISPLAY_NAME} stopped at {stage}: {message}", result=result) from exc
    finally:
        if driver is not None:
            shared.safe_driver_quit(driver, profile_path=shared._profile_path())


def run_stock_issue_item_order(order_id, product, products, **kwargs):
    try:
        result = process_stock_issue_item_order(order_id, product, products, **kwargs)
        action = "Dry run prepared" if result["dry_run"] else "Completed"
        return True, f"{action} {DISPLAY_NAME} for order {result['order_id']}.", result
    except (StockIssueItemError, shared.CopyrightCancelError) as exc:
        return False, str(exc), getattr(exc, "result", {})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--request-json", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--visible", action="store_true")
    args = parser.parse_args()
    try:
        request = json.loads(args.request_json)
        if not isinstance(request, dict):
            raise ValueError("The stock item request must be an object.")
        ok, message, payload = run_stock_issue_item_order(
            args.order_id, request.get("product"), request.get("products"), dry_run=not args.real,
            visible=args.visible, progress_callback=lambda stage, message: shared.write_status_payload(
                AUTOMATION_NAME, message, stage=stage, order_id=args.order_id))
    except ValueError as exc:
        ok, message, payload = False, str(exc), {"order_id": args.order_id}
    shared.write_result_payload(AUTOMATION_NAME, os.path.basename(__file__), ok, message, extra_fields=payload)
    sys.exit(0 if ok else 1)
