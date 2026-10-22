"""Verified design selections and resumable partial copyright refunds.

This module deliberately has no order-cancellation action. A selection keeps its
original scope even after CRM removes or renumbers tabs.
"""
from collections import Counter
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

from runtime_paths import STATE_DIR


ROOT = Path(__file__).resolve().parents[1]
READER = (ROOT / "crm-order-dark-mode-extension/copyright-design-reader.js").read_text(encoding="utf-8")


class CopyrightSelectionError(ValueError):
    pass


def money(value, label):
    if value is None or isinstance(value, bool):
        raise CopyrightSelectionError(f"Could not read {label}; stopped before refund.")
    text = str(value).strip().replace("$", "").replace(",", "")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        raise CopyrightSelectionError(f"Could not read {label}; stopped before refund.")
    try:
        amount = Decimal(text)
        if not amount.is_finite() or abs(amount - amount.quantize(Decimal("0.01"))) > Decimal("0.000001"):
            raise InvalidOperation
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise CopyrightSelectionError(f"Invalid {label}; stopped before refund.") from exc


def selection_id(design):
    """Identify a persisted order tab, retaining artwork IDs for legacy requests."""
    if "order_design_id" in design:
        return f"order-design:{design['order_design_id']}"
    return design["design_id"]


def normalize_selection(selection):
    if not isinstance(selection, dict) or not re.fullmatch(r"\d{7}", str(selection.get("order_id", ""))):
        raise CopyrightSelectionError("Scan and select the copyright design tabs in the extension first.")
    rows = selection.get("all_designs")
    selected = selection.get("selected_design_ids")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000 or not isinstance(selected, list) or not selected:
        raise CopyrightSelectionError("Select at least one affected copyright design tab.")
    result = []
    ids, numbers = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise CopyrightSelectionError("Invalid copyright design scan.")
        identifier = row.get("design_id")
        order_identifier = row.get("order_design_id")
        number, quantity = row.get("tab_number"), row.get("quantity")
        name = " ".join(str(row.get("design_name") or "").split())
        if (not isinstance(identifier, str) or not re.fullmatch(r"[1-9]\d{0,19}", identifier)
                or ("order_design_id" in row and (not isinstance(order_identifier, str)
                    or not re.fullmatch(r"[1-9]\d{0,19}", order_identifier)))
                or type(number) is not int or not 1 <= number <= 1000
                or type(quantity) is not int or not 0 <= quantity <= 1_000_000
                or not name or len(name) > 500
                or re.search(r"\[\s*(?:DESIGN|REASON|ORDER[ _-]*NUMBER)\s*\]|XXXXXX", name, re.I)
                or selection_id(row) in ids or number in numbers):
            raise CopyrightSelectionError("Each copyright tab needs a distinct ID, tab number, Design Name, and readable quantity.")
        subtotal = money(row.get("subtotal"), "design subtotal")
        if subtotal < 0 or (quantity == 0 and subtotal != 0):
            raise CopyrightSelectionError("A copyright design has inconsistent quantity and price; review the order.")
        ids.add(selection_id(row))
        numbers.add(number)
        result.append({"tab_number": number, "design_id": identifier, "design_name": name,
                       **({"order_design_id": order_identifier} if "order_design_id" in row else {}),
                       "quantity": quantity, "subtotal": f"{subtotal:.2f}"})
    if len({"order_design_id" in row for row in result}) != 1:
        raise CopyrightSelectionError("Every copyright tab must use the same kind of saved tab identity. Scan again.")
    if (any(not isinstance(value, str) for value in selected) or len(set(selected)) != len(selected)
            or not set(selected).issubset(ids)):
        raise CopyrightSelectionError("The affected copyright designs must come from the complete scan, without duplicates.")
    return {"order_id": str(selection["order_id"]), "all_designs": sorted(result, key=lambda row: row["tab_number"]),
            "selected_design_ids": sorted(selected)}


def selected_designs(selection):
    return [row for row in selection["all_designs"] if selection_id(row) in selection["selected_design_ids"]]


def design_text(designs):
    names = list(dict.fromkeys(" ".join(str(row.get("design_name") or "").split()) for row in designs))
    if not names or any(not name for name in names):
        raise CopyrightSelectionError("The copyright email requires the selected Design Names.")
    return " and ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + ", and " + names[-1]


def is_partial(selection):
    return len(selection["selected_design_ids"]) < len(selection["all_designs"])


def click_design_tab(driver, number):
    return driver.execute_script(r"""
      const matches = Array.from(document.querySelectorAll('a,button,[role=tab],[ng-click]'))
        .map(element => ({element, text: (element.innerText || '').replace(/\s+/g, ' ').trim(),
                         rect: element.getBoundingClientRect()}))
        .filter(row => row.rect.width > 8 && row.rect.height > 8 &&
          /Design Previews|View Proofs/i.test(row.text) &&
          Number(row.text.match(/\b(\d+)\s*-\s*QTY\s*:\s*\d+/i)?.[1]) === arguments[0])
        .sort((left, right) => left.text.length - right.text.length ||
          left.rect.width * left.rect.height - right.rect.width * right.rect.height);
      if (!matches.length) return false;
      matches[0].element.scrollIntoView({block: 'center'});
      matches[0].element.click();
      return true;
    """, number)


def design_totals(text):
    return {int(match[1]): {"quantity": int(match[2]), "subtotal": f"{money(match[3], 'design total'):.2f}"}
            for match in re.finditer(r"\bDesign\s+(\d{1,4})\s+Total\s*:\s*(\d+)\s+pieces?\s*\$?(-?[0-9,]+\.\d{2})", text, re.I)}


def scan_designs(driver, order_id):
    from workers import crm_auto_splitter as splitter
    metadata = driver.execute_script(READER + "\nreturn readCopyrightDesignIds();")
    identities = (metadata or {}).get("designs") or []
    if str((metadata or {}).get("order_id")) != str(order_id) or not identities:
        raise CopyrightSelectionError("Could not verify the copyright order and every design ID.")
    designs = []
    for identity in identities:
        number = identity["tab_number"]
        if not click_design_tab(driver, number):
            raise CopyrightSelectionError(f"Could not open copyright design tab {number}.")
        previous, detail = None, {}
        for _ in range(30):
            time.sleep(0.2)
            detail = splitter._scan_current_design_detail(driver, number)
            current = (detail.get("design_name"), detail.get("design_id"))
            if current[0] and current[1] == identity["design_id"] and current == previous:
                break
            previous = current
        else:
            raise CopyrightSelectionError(f"Could not verify Design Name and ID for copyright tab {number}.")
        totals = design_totals(driver.execute_script("return document.body.innerText;"))
        if number not in totals:
            raise CopyrightSelectionError(f"Could not read the quantity and total for copyright tab {number}.")
        designs.append({"tab_number": number, "design_id": identity["design_id"],
                        **({"order_design_id": identity["order_design_id"]} if "order_design_id" in identity else {}),
                        "design_name": detail["design_name"], **totals[number]})
    after = driver.execute_script(READER + "\nreturn readCopyrightDesignIds();")
    if after != metadata:
        raise CopyrightSelectionError("The order design IDs changed while scanning. Select the copyright designs again.")
    return normalize_selection({"order_id": str(order_id), "all_designs": designs,
                                "selected_design_ids": [selection_id(row) for row in designs]})["all_designs"]


def _live_selection_rows(selection, live):
    if "order_design_id" in selection["all_designs"][0]:
        if any("order_design_id" not in row for row in live):
            raise CopyrightSelectionError("Could not verify every saved copyright tab identity; manual review required.")
        return live
    # Old queue tasks and receipts used artwork IDs. Keep their exact payloads
    # and receipt keys, but never reinterpret them when artwork IDs repeat.
    if len({row["design_id"] for row in live}) != len(live):
        raise CopyrightSelectionError("This legacy copyright selection has ambiguous design IDs. Review the order before continuing.")
    return [{key: value for key, value in row.items() if key != "order_design_id"} for row in live]


def verify_original(selection, live):
    if selection["all_designs"] != _live_selection_rows(selection, live):
        raise CopyrightSelectionError("The order designs, names, quantities, or prices changed. Reopen Copyright - Cancel and scan again.")


def verify_removed(selection, live, *, allow_remaining=False):
    """Compare by stable ID; surviving tabs may have new numbers."""
    live = _live_selection_rows(selection, live)
    expected = {selection_id(row): row for row in selection["all_designs"]}
    actual = {selection_id(row): row for row in live}
    selected = set(selection["selected_design_ids"])
    if len(actual) != len(live) or set(actual) - set(expected) or set(expected) - selected - set(actual):
        raise CopyrightSelectionError("The remaining copyright order designs changed; manual review required.")
    removed, zeroed, remaining = [], [], []
    for identifier, original in expected.items():
        current = actual.get(identifier)
        if identifier in selected and current is None:
            removed.append(identifier)
            continue
        if current is None or any(current[key] != original[key] for key in ("design_id", "design_name")):
            raise CopyrightSelectionError("A copyright Design Name or identity changed; manual review required.")
        if identifier in selected and current["quantity"] == 0 and money(current["subtotal"], "removed design total") == 0:
            zeroed.append(identifier)
        elif all(current[key] == original[key] for key in ("quantity", "subtotal")):
            if identifier in selected:
                remaining.append(identifier)
        else:
            raise CopyrightSelectionError("A retained design or partially removed design changed; manual review required.")
    if remaining and not allow_remaining:
        raise CopyrightSelectionError("The selected copyright designs were not removed or zeroed after saving.")
    return {"verified": True, "removed_design_ids": removed, "zeroed_design_ids": zeroed,
            "remaining_design_ids": remaining}


def operation_key(selection, reason):
    encoded = json.dumps([selection, " ".join(str(reason).split())], sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def receipt_path(selection, reason):
    return Path(STATE_DIR) / "copyright_partial_cancellations" / selection["order_id"] / f"{operation_key(selection, reason)}.json"


def load_receipt(path):
    if not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("stage") not in {
                "removing", "removed", "refund_pending", "refunded", "email_sent", "case_pending", "complete"}:
            raise ValueError("Invalid partial copyright receipt")
        return receipt
    except (OSError, ValueError) as exc:
        raise CopyrightSelectionError("The partial copyright receipt cannot be read. Review CRM payments and Salesforce before retrying.") from exc


def save_receipt(path, receipt, *, create=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".copyright-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def assert_no_other_pending(selection, reason):
    target = receipt_path(selection, reason)
    for path in target.parent.glob("*.json"):
        if path != target and load_receipt(path)["stage"] != "complete":
            raise CopyrightSelectionError("This order has an unfinished partial copyright cancellation. Retry its original queue task before starting another cancellation.")


def read_financials(shared, driver):
    state = shared._get_order_live_state(driver)
    totals = {name: state.get(key) for name, key in
              (("grand_total", "grand_total"), ("paid", "amount_paid"), ("balance_due", "amount_due"))}
    values = {name: f"{money(totals.get(name), name):.2f}" for name in ("grand_total", "paid", "balance_due")}
    if money(values["grand_total"], "grand total") - money(values["paid"], "paid") != money(values["balance_due"], "balance due"):
        raise CopyrightSelectionError("CRM payment totals do not reconcile; stopped before refund.")
    return {**values, "transactions": state.get("transactions") or [], "order_fees": state.get("order_fees") or []}


def validate_partial_refund(totals, context, modal):
    grand, paid, balance = (money(totals.get(key), key) for key in ("grand_total", "paid", "balance_due"))
    amount = money(context.get("refund_amount"), "partial refund amount")
    if (grand < 0 or paid <= 0 or amount <= 0 or amount > paid or grand != money(context.get("grand_total"), "saved grand total")
            or paid != money(context.get("paid"), "saved paid amount") or balance != grand - paid
            or balance != -amount or money(modal.get("input_amount"), "refund input") != amount
            or money(modal.get("max_refund"), "maximum available refund") < amount):
        raise CopyrightSelectionError("Partial refund blocked: saved total, payment, credit balance, refund input, and available refund do not agree.")
    return {"matched": True, "partial": True, "paid": f"{paid:.2f}", "grand_total": f"{grand:.2f}",
            "balance_due": f"{balance:.2f}", "payment_amount": f"{amount:.2f}"}


def set_partial_refund_amount(driver, amount):
    changed = driver.execute_script(r"""
      const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
      const dialogs = Array.from(document.querySelectorAll('.modal,.modal-dialog,.modal-content,[role=dialog]'))
        .filter(el => visible(el) && /refund/i.test(el.innerText || ''))
        .filter(el => Array.from(el.querySelectorAll('input')).some(visible))
        .sort((a, b) => a.getBoundingClientRect().width * a.getBoundingClientRect().height -
                        b.getBoundingClientRect().width * b.getBoundingClientRect().height);
      const root = dialogs[0];
      if (!root) return false;
      const inputs = Array.from(root.querySelectorAll('input')).filter(el => visible(el) &&
        !['hidden','button','submit','checkbox','radio'].includes(el.type));
      const named = inputs.filter(el => /amount|refund/i.test([
        el.name, el.id, el.getAttribute('ng-model'), el.getAttribute('aria-label')].join(' ')));
      const candidates = named.length ? named : inputs;
      if (candidates.length !== 1) return false;
      const input = candidates[0];
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, arguments[0]);
      input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
      return true;
    """, f"{money(amount, 'partial refund amount'):.2f}")
    if not changed:
        raise CopyrightSelectionError("Could not identify the partial refund amount field unambiguously.")


def _active_status(shared, driver):
    status = driver.execute_script("""
      const headings = Array.from(document.querySelectorAll('#order-info-panel h3'))
        .filter(element => element.getClientRects().length && getComputedStyle(element).visibility !== 'hidden');
      return headings.length === 1 ? headings[0].innerText.trim() : '';
    """)
    if not isinstance(status, str) or not status.strip():
        raise CopyrightSelectionError("Could not verify the current order status; stopped before continuing the partial cancellation.")
    if shared._is_cancel_order_status(status):
        raise CopyrightSelectionError("This order is cancelled. Partial copyright cancellation requires an active order.")
    return status.strip()


def _reload(shared, driver, crm_handle, order_id):
    driver.switch_to.window(crm_handle)
    driver.refresh()
    shared._activate_crm_context(driver)
    shared._wait_for_order_scope(driver, order_id=order_id, timeout=30)
    return _active_status(shared, driver)


_FIND_DESIGN_JS = r"""
  const byOrderId = arguments[0].startsWith('order-design:');
  const wanted = byOrderId ? arguments[0].slice('order-design:'.length) : arguments[0];
  const matches = (r.designs || []).map((design, index) => ({design, index}))
    .filter(({design}) => String(design.crudAction || '').toLowerCase() !== 'd' &&
      String(byOrderId ? design.id : design.designId) === wanted);
  if (matches.length > 1) throw new Error('The copyright tab identity is ambiguous; manual review required.');
  const index = matches.length ? matches[0].index : -1;
"""


def remove_design(shared, driver, identifier):
    from workers import crm_auto_splitter as splitter
    started = shared._order_scope(driver, _FIND_DESIGN_JS + """
      if (index < 0) return false;
      if (typeof s.removeDesign !== 'function') throw new Error('CRM design removal is unavailable.');
      runInAngular(s, () => { s.editModeOn(); s.removeDesign(r.designs[index], index, r); });
      return true;
    """, identifier)
    if not started:
        return
    confirmed = False
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        error = splitter._visible_crm_error_message(driver)
        if error:
            raise CopyrightSelectionError(f"CRM could not remove the copyright design: {error}")
        text = splitter._find_modal_text(driver).lower()
        if not confirmed and ("delete this design" in text or "are you sure" in text):
            if not splitter._click_modal_choice(driver, "yes"):
                raise CopyrightSelectionError("Could not confirm the selected copyright design's red-X removal.")
            confirmed = True
        state = shared._order_scope(driver, _FIND_DESIGN_JS + """
          return {number: index + 1, text: document.body.innerText};
        """, identifier)
        if not state["number"]:
            return
        total = design_totals(state["text"]).get(state["number"])
        if total and total["quantity"] == 0 and money(total["subtotal"], "zeroed design total") == 0:
            return
        time.sleep(0.25)
    raise CopyrightSelectionError("The selected copyright design was not removed or reduced to QTY 0 and $0.00.")


def _refund_total(financials):
    return sum((money(row.get("amount"), "refund transaction").copy_abs()
                for row in financials["transactions"]
                if "refund" in f"{row.get('tag', '')} {row.get('type', '')}".lower()
                and money(row.get("amount"), "refund transaction") < 0), Decimal("0.00"))


def verify_refunded(before, after, amount):
    amount = money(amount, "partial refund amount")
    positive = lambda state: Counter(json.dumps(row, sort_keys=True) for row in state["transactions"]
                                     if money(row.get("amount"), "payment transaction") > 0)
    return (after["grand_total"] == before["grand_total"] and after["order_fees"] == before["order_fees"]
            and money(after["paid"], "paid") == money(before["paid"], "paid") - amount
            and money(after["balance_due"], "balance due") == 0
            and _refund_total(after) - _refund_total(before) == amount and positive(after) == positive(before))


def _confirm_refund(shared, driver, crm_handle, selection, receipt, *, wait=False):
    deadline = time.monotonic() + (30 if wait else 0)
    while True:
        _reload(shared, driver, crm_handle, selection["order_id"])
        verify_removed(selection, scan_designs(driver, selection["order_id"]))
        after = read_financials(shared, driver)
        if verify_refunded(receipt["after_removal"], after, receipt["refund_amount"]):
            return after
        if time.monotonic() >= deadline:
            raise CopyrightSelectionError("The partial refund submission is unconfirmed. Review CRM/Stripe before retrying; no second refund will be submitted automatically.")
        time.sleep(1)


def process_partial(shared, driver, crm_handle, selection, reason, contact, *, dry_run, process,
                    login_wait_seconds=0, click_refund_button=True):
    selection = normalize_selection(selection)
    if not is_partial(selection):
        raise CopyrightSelectionError("The partial copyright workflow requires some, but not all, design tabs.")
    if " ".join(str(contact.get("salesforce_label", "")).split()).casefold() == "salesforce contact":
        raise CopyrightSelectionError("Salesforce Contact accounts cannot send this email; review the partial cancellation manually.")
    if not str(reason).strip():
        raise CopyrightSelectionError("Copyright cancellation requires a reason.")
    path = receipt_path(selection, reason)
    receipt = load_receipt(path)
    assert_no_other_pending(selection, reason)
    if receipt and (receipt.get("selection") != selection or receipt.get("reason") != reason):
        raise CopyrightSelectionError("The saved partial cancellation does not match this request.")
    if receipt and receipt.get("customer_email") != contact["email"].strip().casefold():
        raise CopyrightSelectionError("The customer email changed after partial cancellation started. Review Salesforce before retrying.")
    if receipt and receipt["stage"] == "complete":
        return {**receipt["result"], "already_completed": True, "dry_run": bool(dry_run)}
    _active_status(shared, driver)
    preflight = None
    if dry_run and receipt:
        verify_removed(selection, scan_designs(driver, selection["order_id"]), allow_remaining=receipt["stage"] == "removing")
        preflight = shared._prepare_and_maybe_send_salesforce_email(
            driver, crm_handle, selection["order_id"], contact["email"], True, process=process,
            reason=reason, contact=contact, designs=selected_designs(selection), login_wait_seconds=login_wait_seconds,
        )
    if not receipt:
        verify_original(selection, scan_designs(driver, selection["order_id"]))
        before = read_financials(shared, driver)
        if money(before["balance_due"], "balance due") < 0:
            raise CopyrightSelectionError("The order already has a credit balance. Review its payments before partial copyright cancellation.")
        if not any(row["quantity"] > 0 for row in selected_designs(selection)):
            raise CopyrightSelectionError("The selected copyright tabs are already empty. Select the affected active designs.")
        # Verify the exact template and all placeholders before changing CRM.
        preflight = shared._prepare_and_maybe_send_salesforce_email(
            driver, crm_handle, selection["order_id"], contact["email"], True, process=process,
            reason=reason, contact=contact, designs=selected_designs(selection),
            login_wait_seconds=login_wait_seconds,
        )
        driver.switch_to.window(crm_handle)
        shared._activate_crm_context(driver)
        if not dry_run:
            receipt = {"version": 1, "stage": "removing", "selection": selection, "reason": reason,
                       "before": before, "payment": shared._read_payment_summary(driver),
                       "customer_email": contact["email"].strip().casefold()}
            save_receipt(path, receipt, create=True)
    if dry_run:
        return {"cancel": {"cancelled": False, "dry_run": True},
                "removal": {"dry_run": True, "designs": selected_designs(selection)},
                "refund_fee": {"added": False, "skipped": True},
                "refund": {"refunded": False, "dry_run": True,
                           "message": "Refund only the verified credit balance after saving the selected design removals."},
                "salesforce": preflight}
    if receipt["stage"] == "removing":
        _reload(shared, driver, crm_handle, selection["order_id"])
        removal = verify_removed(selection, scan_designs(driver, selection["order_id"]), allow_remaining=True)
        current_financials = read_financials(shared, driver)
        if any(current_financials[key] != receipt["before"][key] for key in ("paid", "transactions", "order_fees")):
            raise CopyrightSelectionError("The order's payments or fees changed before design removal; review CRM.")
        for identifier in removal["remaining_design_ids"]:
            remove_design(shared, driver, identifier)
        shared._save_order_and_wait(driver)
        _reload(shared, driver, crm_handle, selection["order_id"])
        receipt["removal"] = verify_removed(selection, scan_designs(driver, selection["order_id"]))
        after = read_financials(shared, driver)
        if (after["paid"] != receipt["before"]["paid"] or after["transactions"] != receipt["before"]["transactions"]
                or after["order_fees"] != receipt["before"]["order_fees"]
                or not 0 <= money(after["grand_total"], "grand total") < money(receipt["before"]["grand_total"], "original total")):
            raise CopyrightSelectionError("The saved partial cancellation has unexpected prices or payments. Review CRM before refunding.")
        receipt.update(stage="removed", after_removal=after, refund_amount=f"{-money(after['balance_due'], 'balance due'):.2f}")
        save_receipt(path, receipt)
    if receipt["stage"] == "removed":
        _reload(shared, driver, crm_handle, selection["order_id"])
        verify_removed(selection, scan_designs(driver, selection["order_id"]))
        if read_financials(shared, driver) != receipt["after_removal"]:
            raise CopyrightSelectionError("The saved partial cancellation's prices or payments changed before refunding.")
        if money(receipt["refund_amount"], "partial refund amount") <= 0:
            raise CopyrightSelectionError("Removing these designs did not create a refundable credit balance. Review CRM before sending a refund email.")
        names = "; ".join(f"Tab {row['tab_number']}: {row['design_name']}" for row in selected_designs(selection))
        note = f"Partial copyright cancellation - {names}\n{reason} copyright\nRefund ${receipt['refund_amount']}\nemailed copyright partial cancellation"
        shared._append_copyright_cancel_sales_note(driver, reason, process=replace(process, fixed_sales_note=note))
        _reload(shared, driver, crm_handle, selection["order_id"])
        state = shared._get_order_live_state(driver)
        if " ".join(note.split()).casefold() not in " ".join(str(state.get("sales_notes", "")).split()).casefold():
            raise CopyrightSelectionError("The partial copyright Sales Note did not persist after reload.")
        verify_removed(selection, scan_designs(driver, selection["order_id"]))
        if read_financials(shared, driver) != receipt["after_removal"]:
            raise CopyrightSelectionError("CRM prices or payments changed while saving the partial cancellation note.")
        if not click_refund_button:
            return {"cancel": {"cancelled": False}, "removal": receipt["removal"],
                    "refund_fee": {"added": False, "skipped": True},
                    "refund": {"refunded": False, "skipped": True, "amount": receipt["refund_amount"]},
                    "email_skipped": True}
        if shared._requires_salesforce_refund_case(receipt["payment"]):
            receipt.update(stage="refunded", refund={"refunded": False, "case_required": True,
                                                     "method": "salesforce_case", "amount": receipt["refund_amount"]})
            save_receipt(path, receipt)
        else:
            shared._validate_refundable_stripe_payment(receipt["payment"])
            context = {**receipt["after_removal"], "refund_amount": receipt["refund_amount"]}

            def before_submit():
                receipt["stage"] = "refund_pending"
                save_receipt(path, receipt)

            shared._refund_via_stripe_payment_modal(driver, False, payment_amount=receipt["refund_amount"],
                                                   partial_context=context, before_submit=before_submit)
    if receipt["stage"] == "refund_pending":
        after = _confirm_refund(shared, driver, crm_handle, selection, receipt, wait=True)
        receipt.update(stage="refunded", refund={"refunded": True, "amount": receipt["refund_amount"],
                                                 "verified": True, "totals": after})
        save_receipt(path, receipt)
    if receipt["stage"] == "refunded":
        _reload(shared, driver, crm_handle, selection["order_id"])
        verify_removed(selection, scan_designs(driver, selection["order_id"]))
        if receipt["refund"].get("refunded"):
            if not verify_refunded(receipt["after_removal"], read_financials(shared, driver), receipt["refund_amount"]):
                raise CopyrightSelectionError("The completed partial refund no longer matches CRM. Review payments before emailing.")
        elif read_financials(shared, driver) != receipt["after_removal"]:
            raise CopyrightSelectionError("The partial refund case amount no longer matches CRM. Review payments before emailing.")
        receipt["salesforce"] = shared._prepare_and_maybe_send_salesforce_email(
            driver, crm_handle, selection["order_id"], contact["email"], False, process=process,
            reason=reason, contact=contact, designs=selected_designs(selection),
            login_wait_seconds=login_wait_seconds, receipt_scope=f"copyright_cancel_partial:{operation_key(selection, reason)}",
        )
        if not receipt["salesforce"].get("sent"):
            raise CopyrightSelectionError("The partial copyright refund email was not confirmed.")
        receipt["stage"] = "email_sent"
        save_receipt(path, receipt)
    if receipt["stage"] == "case_pending":
        raise CopyrightSelectionError("A partial refund Case submission is unconfirmed. Review Salesforce Cases before retrying; no duplicate Case will be created.")
    if (receipt["stage"] == "email_sent" and receipt["refund"].get("case_required")
            and not (receipt.get("salesforce_refund_case") or {}).get("created")):
        _reload(shared, driver, crm_handle, selection["order_id"])
        verify_removed(selection, scan_designs(driver, selection["order_id"]))
        if read_financials(shared, driver) != receipt["after_removal"]:
            raise CopyrightSelectionError("The partial credit changed before Case creation. Review CRM payments.")
        # A resumed worker starts in CRM; the prior Salesforce handle may belong
        # to a browser that has already exited. Reopen the verified account.
        shared._open_salesforce_account(driver, crm_handle, contact["email"],
                                        order_id=selection["order_id"], login_wait_seconds=login_wait_seconds)
        shared._verify_salesforce_email(driver, contact["email"])
        # Persist uncertainty before the external Case Save, as for Stripe Save.
        receipt["stage"] = "case_pending"
        save_receipt(path, receipt)
        case_process = replace(process, refund_case_subject=f"Copyright - Partial - ${receipt['refund_amount']}")
        receipt["salesforce_refund_case"] = shared._create_salesforce_refund_pending_case(
            driver, selection["order_id"], process=case_process, dry_run=False)
        if not receipt["salesforce_refund_case"].get("created"):
            raise CopyrightSelectionError("The partial refund Case was not confirmed saved.")
        receipt["stage"] = "email_sent"
        save_receipt(path, receipt)
    status = _reload(shared, driver, crm_handle, selection["order_id"])
    verify_removed(selection, scan_designs(driver, selection["order_id"]))
    result = {"cancel": {"cancelled": False, "verified_active": True, "status": status}, "removal": receipt["removal"],
              "refund_fee": {"added": False, "skipped": True, "reason": "selected_designs_already_reduce_price"},
              "refund": receipt["refund"], "salesforce": receipt["salesforce"],
              "salesforce_refund_case": receipt.get("salesforce_refund_case"), "receipt": str(path)}
    receipt.update(stage="complete", result=result)
    save_receipt(path, receipt)
    return result
