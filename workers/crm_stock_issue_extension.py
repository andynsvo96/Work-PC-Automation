"""Single-order Stock Issue -> Extension Required automation.

This worker is intentionally separate from the other CRM issue automations.
It receives a validated product selection from the Chrome extension, writes an
idempotent CRM Sales Note, sends the dedicated Salesforce template, posts the
required Slack notification, and applies Issue - Stock only after every prior
step succeeds.
"""

from __future__ import annotations

import re
import os
import sys
import time
from collections import OrderedDict

import config as config_module

WORKERS_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)

import crm_copyright_cancel as shared
from rush_order_notifications import RUSH_ORDER_SLACK_CHANNEL_URL


AUTOMATION_KEY = "stock_issue_extension"
AUTOMATION_NAME = "crm.stock_issue_extension"
DISPLAY_NAME = "Stock Issue - Extension Required"
SALESFORCE_TEMPLATE = str(
    getattr(config_module, "SALESFORCE_STOCK_ISSUE_EXTENSION_TEMPLATE", "[AUTO] STOCK - Extension")
    or "[AUTO] STOCK - Extension"
).strip()
CRM_STATUS = str(getattr(config_module, "STOCK_ISSUE_EXTENSION_CRM_STATUS", "Issue - Stock") or "Issue - Stock").strip()
ORDER_NUMBER_PLACEHOLDER = "[ORDER-NUMBER]"
STOCK_PLACEHOLDER = "[STOCK]"
DAYS_PLACEHOLDER = "[DAYS]"
MAX_PRODUCTS = 100


class StockIssueExtensionError(RuntimeError):
    """Raised when Stock Issue Extension must stop before its next stage."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = dict(result or {})


STOCK_EXTENSION_PROCESS = shared.CancelProcess(
    key=AUTOMATION_KEY,
    issue_type="Stock Issue - Extension Required",
    salesforce_template=SALESFORCE_TEMPLATE,
    template_search="[AUTO]",
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=("urgent", "extension required"),
    body_markers=(
        "unable to receive the required",
        "[stock]",
        "[days]-business day(s) extension",
        "not including holidays",
        "additional stock to arrive and complete your order",
        "available options",
    ),
    display_name=DISPLAY_NAME,
    requires_reason=False,
    cancel_and_refund=False,
)


def _stock_extension_template_signature_error(state):
    state = state if isinstance(state, dict) else {}
    subject = shared._clean_text(state.get("subject"))
    body = shared._clean_text(state.get("body"))
    expected_subject = re.compile(
        r"^RushOrderTees Order #\[ORDER-NUMBER\]\s*-\s*URGENT\s*-\s*Extension Required$",
        flags=re.IGNORECASE,
    )
    if not expected_subject.fullmatch(subject):
        return f"subject did not match the new Stock Extension template (found: {subject or 'blank'})"
    missing = shared._missing_body_markers(body, STOCK_EXTENSION_PROCESS)
    if missing:
        return f"body was missing: {', '.join(missing)}"
    if body.casefold().count(STOCK_PLACEHOLDER.casefold()) != 1:
        return f"body must contain exactly one {STOCK_PLACEHOLDER} placeholder"
    if body.casefold().count(DAYS_PLACEHOLDER.casefold()) != 1:
        return f"body must contain exactly one {DAYS_PLACEHOLDER} placeholder"
    return ""


def _click_exact_stock_extension_template(driver):
    """Select only the exact template-name result, never a containing menu or row."""
    target = SALESFORCE_TEMPLATE.casefold()
    option = driver.execute_script(
        r"""
        const target = String(arguments[0] || '').replace(/\s+/g, ' ').trim().toLowerCase();
        function clean(value) {
          return String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
        }
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const style = view.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0
            && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight
            && rect.right > 0 && rect.left < window.innerWidth;
        }
        function walk(root, out = []) {
          if (!root || !root.querySelectorAll) return out;
          for (const el of Array.from(root.querySelectorAll('*'))) {
            out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot, out);
          }
          return out;
        }
        const exact = walk(document)
          .filter((el) => visible(el) && clean(el.innerText || el.textContent || el.value || '') === target)
          .filter((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width >= 40 && rect.height >= 10;
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const aAction = /^(a|button|td|li)$/i.test(a.tagName)
              || /button|option|menuitem|row/i.test(a.getAttribute('role') || '') ? 0 : 1;
            const bAction = /^(a|button|td|li)$/i.test(b.tagName)
              || /button|option|menuitem|row/i.test(b.getAttribute('role') || '') ? 0 : 1;
            if (aAction !== bAction) return aAction - bAction;
            return (ar.width * ar.height) - (br.width * br.height);
          });
        if (!exact.length) return null;
        const node = exact[0];
        const clickable = node.closest('a,button,[role=button],[role=option],[role=menuitem],tr,[role=row],li') || node;
        try { clickable.scrollIntoView({block: 'center', inline: 'center'}); } catch (err) {}
        return clickable;
        """,
        target,
    )
    if option is None:
        return False
    return shared._click_element_center(driver, option)


def _insert_exact_stock_extension_template(driver):
    """Open the full picker, search [AUTO], and require the exact Stock Extension result."""
    shared._focus_salesforce_body_editor(driver)
    shared._click_template_button(driver)
    time.sleep(0.5)
    if not shared._open_full_template_picker_from_menu(driver):
        raise StockIssueExtensionError("Salesforce full email-template picker could not be opened.")
    shared._ensure_private_email_templates_folder(driver)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        shared._search_full_template_modal(driver, "[AUTO]")
        time.sleep(1)
        if _click_exact_stock_extension_template(driver):
            shared._confirm_salesforce_template_insert(driver)
            if shared._wait_for_salesforce_template_markers(driver, STOCK_EXTENSION_PROCESS, timeout=10):
                state = shared._read_salesforce_email_state(driver) or {}
                signature_error = _stock_extension_template_signature_error(state)
                if not signature_error:
                    return True
                raise StockIssueExtensionError(
                    f"Salesforce inserted {SALESFORCE_TEMPLATE}, but its content was not the new approved version: "
                    f"{signature_error}."
                )
            raise StockIssueExtensionError(
                f"Salesforce inserted an email, but it was not the exact {SALESFORCE_TEMPLATE} content."
            )
        shared._scroll_full_template_modal(driver)
        time.sleep(0.5)
    raise StockIssueExtensionError(
        f"Salesforce template {SALESFORCE_TEMPLATE} was not found after searching [AUTO]."
    )


def _clean_text(value, *, field, maximum, required=True):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise StockIssueExtensionError(f"Each selected product requires {field}.")
    if len(text) > maximum:
        raise StockIssueExtensionError(f"Selected product {field} is too long (maximum {maximum} characters).")
    if any(ord(char) < 32 for char in text) or "<" in text or ">" in text:
        raise StockIssueExtensionError(f"Selected product {field} contains unsupported characters.")
    return text


def _positive_integer(value, *, label, maximum=None):
    if isinstance(value, bool):
        raise StockIssueExtensionError(f"{label} must be a positive whole number.")
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9]\d*", text):
        raise StockIssueExtensionError(f"{label} must be a positive whole number.")
    number = int(text)
    if maximum is not None and number > maximum:
        raise StockIssueExtensionError(f"{label} cannot exceed {maximum}.")
    return number


def normalize_selected_products(products):
    """Validate and deduplicate selected style/description/color occurrences."""
    if not isinstance(products, list) or not products:
        raise StockIssueExtensionError("Select at least one product before queueing Extension Required.")
    if len(products) > MAX_PRODUCTS:
        raise StockIssueExtensionError(f"A maximum of {MAX_PRODUCTS} selected products is allowed.")

    normalized = OrderedDict()
    for product in products:
        if not isinstance(product, dict):
            raise StockIssueExtensionError("Each selected product must be a structured record.")
        style = _clean_text(product.get("style"), field="style", maximum=40)
        description = _clean_text(product.get("description"), field="description", maximum=180)
        color = _clean_text(product.get("color"), field="color", maximum=80)
        design_item_id = _clean_text(
            product.get("design_item_id"), field="design item ID", maximum=60, required=False
        )
        if design_item_id and not re.fullmatch(r"design-item-\d+", design_item_id, flags=re.IGNORECASE):
            raise StockIssueExtensionError("Selected product design item ID is malformed.")
        raw_tab_numbers = product.get("tab_numbers") if isinstance(product.get("tab_numbers"), list) else [product.get("tab_number")]
        tab_numbers = []
        for tab_number in raw_tab_numbers:
            if tab_number in (None, ""):
                continue
            clean_tab_number = _positive_integer(tab_number, label="Product tab number", maximum=999)
            if clean_tab_number not in tab_numbers:
                tab_numbers.append(clean_tab_number)
        raw_design_item_ids = (
            product.get("design_item_ids")
            if isinstance(product.get("design_item_ids"), list)
            else [design_item_id]
        )
        design_item_ids = []
        for raw_design_item_id in raw_design_item_ids:
            clean_design_item_id = _clean_text(
                raw_design_item_id, field="design item ID", maximum=60, required=False
            )
            if clean_design_item_id and not re.fullmatch(
                r"design-item-\d+", clean_design_item_id, flags=re.IGNORECASE
            ):
                raise StockIssueExtensionError("Selected product design item ID is malformed.")
            if clean_design_item_id and clean_design_item_id not in design_item_ids:
                design_item_ids.append(clean_design_item_id)
        total_quantity = product.get("total_quantity")
        if total_quantity in (None, ""):
            total_quantity = None
        else:
            total_quantity = _positive_integer(total_quantity, label="Product total quantity", maximum=1_000_000)

        key = (style.casefold(), description.casefold(), color.casefold())
        if key not in normalized:
            normalized[key] = {
                "style": style,
                "description": description,
                "color": color,
                "tab_numbers": [],
                "design_item_ids": [],
                "total_quantity": 0 if total_quantity is not None else None,
            }
        row = normalized[key]
        for tab_number in tab_numbers:
            if tab_number not in row["tab_numbers"]:
                row["tab_numbers"].append(tab_number)
        for design_item_id in design_item_ids:
            if design_item_id not in row["design_item_ids"]:
                row["design_item_ids"].append(design_item_id)
        if total_quantity is not None:
            row["total_quantity"] = int(row.get("total_quantity") or 0) + total_quantity

    return list(normalized.values())


def normalize_request(days, products):
    return {
        "days": _positive_integer(days, label="Extension days", maximum=365),
        "products": normalize_selected_products(products),
    }


def _natural_join(values, *, final_word="and"):
    values = [str(value) for value in values if str(value)]
    if len(values) < 2:
        return values[0] if values else ""
    if len(values) == 2:
        return f"{values[0]} {final_word} {values[1]}"
    return f"{', '.join(values[:-1])}, {final_word} {values[-1]}"


def _group_products(products):
    groups = OrderedDict()
    for product in normalize_selected_products(products):
        key = (product["style"].casefold(), product["description"].casefold())
        group = groups.setdefault(
            key,
            {"style": product["style"], "description": product["description"], "colors": []},
        )
        if product["color"].casefold() not in {color.casefold() for color in group["colors"]}:
            group["colors"].append(product["color"])
    return list(groups.values())


def format_color_list(colors):
    return _natural_join(colors, final_word="or" if len(colors) >= 3 else "and")


def format_email_stock_text(products):
    groups = _group_products(products)
    phrases = [
        f"{group['style']} {group['description']} in the color {format_color_list(group['colors'])}"
        for group in groups
    ]
    return _natural_join(phrases, final_word="and")


def format_sales_note(days, products):
    day_count = _positive_integer(days, label="Extension days", maximum=365)
    groups = _group_products(products)
    product_text = _natural_join(
        [f"{group['style']} in {format_color_list(group['colors'])}" for group in groups],
        final_word="and",
    )
    verb = "needs" if len(groups) == 1 else "need"
    return f"{product_text} {verb} {day_count}-day(s) extension\nEmailed Txted"


def format_slack_message(order_id):
    order_id = shared._normalize_order_id(order_id)
    return f"{shared.PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)} - Rush Order needs extension"


def _append_sales_note(driver, note, dry_run=False):
    existing = shared._order_scope(
        driver,
        "return String(r.addSalesNotes || r.salesNotes || r.filteredSalesNotes || '');",
    )
    if note.casefold() in str(existing or "").casefold():
        return {"updated": False, "already_present": True, "dry_run": bool(dry_run), "note": note}
    if dry_run:
        return {
            "updated": False,
            "already_present": False,
            "dry_run": True,
            "note": note,
            "message": "Skipped updating CRM Sales Notes in dry-run mode.",
        }
    update = shared._order_scope(
        driver,
        """
        const note = arguments[0];
        const existingDraft = String(r.addSalesNotes || '').trim();
        const alreadyPresent = existingDraft.toLowerCase().includes(note.toLowerCase());
        const after = alreadyPresent ? existingDraft : note;
        runInAngular(s, () => {
          s.editModeOn();
          r.addSalesNotes = after;
          if (s.order.setAddSalesNotes) s.order.setAddSalesNotes(r.addSalesNotes);
        });
        return {updated: after !== existingDraft, already_present: alreadyPresent};
        """,
        note,
    ) or {}
    save = shared._save_order_and_wait(driver)
    persisted = shared._order_scope(
        driver,
        "return String(r.addSalesNotes || r.salesNotes || r.filteredSalesNotes || '');",
    )
    if note.casefold() not in str(persisted or "").casefold():
        raise StockIssueExtensionError("CRM Sales Note save completed, but the exact note was not confirmed afterward.")
    return {
        "updated": bool(update.get("updated")),
        "already_present": bool(update.get("already_present")),
        "dry_run": False,
        "note": note,
        "save": save,
        "confirmed": True,
    }


def _replace_stock_body_placeholders(driver, stock_text, days):
    result = driver.execute_script(
        r"""
        const replacements = {'[STOCK]': String(arguments[0]), '[DAYS]': String(arguments[1])};
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const style = view.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        function walk(root, out = []) {
          if (!root || !root.querySelectorAll) return out;
          for (const el of Array.from(root.querySelectorAll('*'))) {
            out.push(el);
            if (el.shadowRoot) walk(el.shadowRoot, out);
          }
          return out;
        }
        function replaceText(value, counts) {
          let output = String(value || '');
          for (const [placeholder, replacement] of Object.entries(replacements)) {
            const pattern = new RegExp(placeholder.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi');
            const matches = output.match(pattern) || [];
            counts[placeholder] += matches.length;
            output = output.replace(pattern, replacement);
          }
          return output;
        }
        function replaceRoot(root, counts) {
          if (!root) return;
          const doc = root.ownerDocument || document;
          const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          const nodes = [];
          while (walker.nextNode()) nodes.push(walker.currentNode);
          for (const node of nodes) {
            const original = node.nodeValue || '';
            if (!/\[\s*(?:STOCK|DAYS)\s*\]/i.test(original)) continue;
            const replaced = replaceText(original, counts);
            const anchor = node.parentElement && node.parentElement.closest('a');
            node.nodeValue = replaced;
            if (anchor && root.contains(anchor)) {
              const text = doc.createTextNode(anchor.textContent || replaced);
              anchor.replaceWith(text);
              counts.unwrapped_links += 1;
            }
          }
          try {
            root.dispatchEvent(new Event('input', {bubbles: true}));
            root.dispatchEvent(new Event('change', {bubbles: true}));
          } catch (err) {}
        }
        const counts = {'[STOCK]': 0, '[DAYS]': 0, unwrapped_links: 0};
        const seen = new Set();
        function inspectDocument(doc) {
          if (!doc || seen.has(doc)) return;
          seen.add(doc);
          try {
            const editors = Object.values((doc.defaultView.CKEDITOR && doc.defaultView.CKEDITOR.instances) || {});
            for (const editor of editors) {
              try {
                const editable = editor.editable && editor.editable();
                const element = editable && editable.$;
                if (element) {
                  replaceRoot(element, counts);
                  if (editor.updateElement) editor.updateElement();
                  if (editor.fire) editor.fire('change');
                }
              } catch (err) {}
            }
          } catch (err) {}
          if (doc.body) replaceRoot(doc.body, counts);
          for (const frame of walk(doc).filter((el) => (el.tagName || '').toLowerCase() === 'iframe' && visible(el))) {
            try { inspectDocument(frame.contentDocument || (frame.contentWindow && frame.contentWindow.document)); } catch (err) {}
          }
        }
        inspectDocument(document);
        return counts;
        """,
        stock_text,
        str(days),
    ) or {}
    if int(result.get(STOCK_PLACEHOLDER) or 0) < 1:
        raise StockIssueExtensionError(f"Salesforce template body does not contain {STOCK_PLACEHOLDER}.")
    if int(result.get(DAYS_PLACEHOLDER) or 0) < 1:
        raise StockIssueExtensionError(f"Salesforce template body does not contain {DAYS_PLACEHOLDER}.")
    return result


def _read_recipient_state(driver):
    return driver.execute_script(
        r"""
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const style = view.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        function emails(value) {
          return Array.from(new Set((String(value || '').match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || []).map((v) => v.toLowerCase())));
        }
        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => visible(el) && /\bfrom\b/i.test(el.innerText || '') && /\bsubject\b/i.test(el.innerText || '') && /\bsend\b/i.test(el.innerText || ''))
          .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
        const composer = composers[0] || document;
        function read(label) {
          const lower = label.toLowerCase();
          const found = [];
          for (const field of Array.from(composer.querySelectorAll('input,textarea,[role=combobox],[data-email]')).filter(visible)) {
            const hint = clean(`${field.getAttribute('aria-label') || ''} ${field.getAttribute('name') || ''} ${field.getAttribute('placeholder') || ''}`).toLowerCase();
            if (new RegExp(`(^|\\b)${lower}(\\b|$)`).test(hint)) {
              found.push(...emails(`${field.value || ''} ${field.innerText || ''} ${field.getAttribute('data-email') || ''}`));
            }
          }
          const labels = Array.from(composer.querySelectorAll('label,span,div,td,th')).filter((el) => visible(el) && clean(el.innerText || el.textContent).replace(/:$/, '').toLowerCase() === lower);
          for (const marker of labels) {
            let row = marker.parentElement;
            for (let depth = 0; row && row !== composer && depth < 5; depth += 1, row = row.parentElement) {
              const text = clean(`${row.innerText || ''} ${Array.from(row.querySelectorAll('[title],[data-email]')).map((el) => `${el.title || ''} ${el.getAttribute('data-email') || ''}`).join(' ')}`);
              const rowEmails = emails(text);
              if (rowEmails.length && text.length < 800) {
                found.push(...rowEmails);
                break;
              }
            }
          }
          return Array.from(new Set(found));
        }
        return {to: read('To'), cc: read('Cc'), bcc: read('Bcc')};
        """
    ) or {"to": [], "cc": [], "bcc": []}


def _verify_final_recipients(driver, expected_email):
    expected = str(expected_email or "").strip().casefold()
    state = _read_recipient_state(driver)
    to = [str(value).strip().casefold() for value in state.get("to") or [] if str(value).strip()]
    cc = [str(value).strip().casefold() for value in state.get("cc") or [] if str(value).strip()]
    bcc = [str(value).strip().casefold() for value in state.get("bcc") or [] if str(value).strip()]
    if to != [expected] or cc or bcc:
        raise StockIssueExtensionError(
            "Salesforce recipient verification failed immediately before Send. "
            f"Expected To {expected}; found To {to or ['blank']}, Cc {cc or []}, Bcc {bcc or []}."
        )
    composer_state = shared._read_salesforce_email_state(driver) or {}
    from_text = shared._clean_text(composer_state.get("from"))
    expected_from = str(shared.SALESFORCE_COPYRIGHT_CANCEL_FROM_EMAIL or "").strip().casefold()
    if expected_from and expected_from not in from_text.casefold():
        raise StockIssueExtensionError(
            f"Salesforce From is not Orders immediately before Send. Current From: {from_text or 'blank'}."
        )
    return {"to": to, "cc": cc, "bcc": bcc, "from": from_text}


def _wait_for_send_confirmation(driver, timeout=20):
    deadline = time.monotonic() + timeout
    last_text = ""
    while time.monotonic() < deadline:
        result = driver.execute_script(
            r"""
            function visible(el) {
              if (!el || !el.getBoundingClientRect) return false;
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            }
            const nodes = Array.from(document.querySelectorAll('[role=alert],[role=status],.toastMessage,.slds-notify,.forceToastMessage'))
              .filter(visible);
            const text = nodes.map((el) => el.innerText || el.textContent || '').join(' ').replace(/\s+/g, ' ').trim();
            return {text, confirmed: /(?:email|message)\s+(?:was\s+)?sent|sent\s+successfully/i.test(text)};
            """
        ) or {}
        last_text = str(result.get("text") or "")
        if result.get("confirmed"):
            return {"confirmed": True, "indicator": last_text}
        time.sleep(0.25)
    raise StockIssueExtensionError(
        "Salesforce Send was clicked, but its sent-success indicator was not confirmed. "
        f"Last notification text: {last_text or 'none'}"
    )


def _verify_email_content(driver, order_id, stock_text, days):
    state = shared._read_salesforce_email_state(driver) or {}
    subject = shared._clean_text(state.get("subject"))
    body = shared._clean_text(state.get("body"))
    unresolved = []
    for placeholder in (ORDER_NUMBER_PLACEHOLDER, STOCK_PLACEHOLDER, DAYS_PLACEHOLDER):
        if placeholder.casefold() in f"{subject}\n{body}".casefold():
            unresolved.append(placeholder)
    if unresolved:
        raise StockIssueExtensionError(f"Salesforce email still contains unresolved placeholders: {', '.join(unresolved)}.")
    if str(order_id) not in subject:
        raise StockIssueExtensionError(f"Salesforce subject does not contain order {order_id}.")
    if stock_text.casefold() not in body.casefold():
        raise StockIssueExtensionError("Salesforce body does not contain the complete selected product text.")
    if not re.search(rf"\b{re.escape(str(days))}\b", body):
        raise StockIssueExtensionError(f"Salesforce body does not contain the requested {days}-day value.")
    return {"subject": subject, "body": body, "state": state}


def _prepare_and_send_salesforce_email(
    driver,
    crm_handle,
    order_id,
    customer_email,
    stock_text,
    days,
    products,
    activity,
    *,
    dry_run=False,
    login_wait_seconds=0,
):
    sf_handle = shared._open_salesforce_account(
        driver,
        crm_handle,
        customer_email,
        login_wait_seconds=login_wait_seconds,
        order_id=order_id,
    )
    shared._verify_salesforce_email(driver, customer_email)
    shared._click_salesforce_email(driver, customer_email)
    shared._wait_for_email_composer(driver)
    selected_from = shared._set_salesforce_from_orders(driver)
    time.sleep(0.5)
    _insert_exact_stock_extension_template(driver)

    deadline = time.monotonic() + 20
    state = {}
    while time.monotonic() < deadline:
        state = shared._read_salesforce_email_state(driver) or {}
        subject = shared._clean_text(state.get("subject"))
        body = shared._clean_text(state.get("body"))
        if subject and body and STOCK_PLACEHOLDER.casefold() in body.casefold() and DAYS_PLACEHOLDER.casefold() in body.casefold():
            break
        time.sleep(0.5)
    else:
        raise StockIssueExtensionError(
            f"Salesforce template {SALESFORCE_TEMPLATE} loaded without the required {STOCK_PLACEHOLDER} and "
            f"{DAYS_PLACEHOLDER} placeholders. Update the live Salesforce template before retrying."
        )
    if not shared._subject_has_order_placeholder(subject) and str(order_id) not in subject:
        raise StockIssueExtensionError(f"Salesforce template subject does not contain {ORDER_NUMBER_PLACEHOLDER}.")
    if str(order_id) not in subject:
        shared._replace_subject_order_number(driver, order_id)
    replacement = _replace_stock_body_placeholders(driver, stock_text, days)
    time.sleep(0.8)
    content = _verify_email_content(driver, order_id, stock_text, days)

    styles = [group["style"].casefold() for group in _group_products(products)]
    linked_product_text = driver.execute_script(
        r"""
        const styles = arguments[0];
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const style = view.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        }
        function walk(root, out = []) {
          if (!root || !root.querySelectorAll) return out;
          for (const element of Array.from(root.querySelectorAll('*'))) {
            out.push(element);
            if (element.shadowRoot) walk(element.shadowRoot, out);
          }
          return out;
        }
        const composers = Array.from(document.querySelectorAll('[role=dialog], .modal-container, .uiPanel, section, div'))
          .filter((el) => visible(el) && /\bsubject\b/i.test(el.innerText || '') && /\bsend\b/i.test(el.innerText || ''))
          .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
        const composer = composers[0] || document;
        const seen = new Set();
        function linkedIn(root) {
          if (!root || seen.has(root)) return false;
          seen.add(root);
          if (walk(root).filter((el) => (el.tagName || '').toLowerCase() === 'a').filter(visible).some((anchor) => {
            const text = String(anchor.innerText || anchor.textContent || '').toLowerCase();
            return styles.some((style) => style && text.includes(style));
          })) return true;
          for (const frame of walk(root).filter((el) => (el.tagName || '').toLowerCase() === 'iframe' && visible(el))) {
            try {
              const doc = frame.contentDocument || (frame.contentWindow && frame.contentWindow.document);
              if (linkedIn(doc)) return true;
            } catch (err) {}
          }
          return false;
        }
        return linkedIn(composer);
        """,
        styles,
    )
    if linked_product_text:
        raise StockIssueExtensionError("Selected Salesforce product text is still hyperlinked; refusing to send.")
    recipients = _verify_final_recipients(driver, customer_email)
    if dry_run:
        return {
            "sent": False,
            "dry_run": True,
            "salesforce_handle": sf_handle,
            "from": selected_from,
            "recipients": recipients,
            "replacement": replacement,
            **content,
        }
    activity["email_send_attempted"] = True
    if not shared._click_salesforce_send_button(driver):
        raise StockIssueExtensionError("Salesforce Send button was not found.")
    confirmation = _wait_for_send_confirmation(driver)
    activity["email_sent"] = True
    return {
        "sent": True,
        "dry_run": False,
        "salesforce_handle": sf_handle,
        "from": selected_from,
        "recipients": recipients,
        "replacement": replacement,
        "confirmation": confirmation,
        **content,
    }


def _send_required_slack(order_id, activity, dry_run=False):
    message = format_slack_message(order_id)
    result = {
        "sent": False,
        "dry_run": bool(dry_run),
        "message": message,
        "channel_url": RUSH_ORDER_SLACK_CHANNEL_URL,
    }
    if dry_run:
        return result
    activity["slack_send_attempted"] = True
    ok, response = shared._run_slack_team(
        "custom",
        custom_message=message,
        channel_url=RUSH_ORDER_SLACK_CHANNEL_URL,
    )
    if not ok:
        raise StockIssueExtensionError(f"Required Stock Extension Slack notification failed: {response}")
    activity["slack_sent"] = True
    result.update({"sent": True, "result": response})
    return result


def process_stock_issue_extension_order(
    order_id,
    days,
    products,
    *,
    dry_run=False,
    visible=False,
    attach_browser=False,
    debugger_address="127.0.0.1:9222",
    login_wait_seconds=0,
    progress_callback=None,
):
    order_id = shared._normalize_order_id(order_id)
    request_data = normalize_request(days, products)
    normalized_products = request_data["products"]
    day_count = request_data["days"]
    stock_text = format_email_stock_text(normalized_products)
    sales_note = format_sales_note(day_count, normalized_products)
    order_url = shared.PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
    activity = {
        "sales_note_saved": False,
        "email_send_attempted": False,
        "email_sent": False,
        "slack_send_attempted": False,
        "slack_sent": False,
        "status_applied": False,
    }
    result = {
        "success": False,
        "order_id": order_id,
        "order_url": order_url,
        "automation": AUTOMATION_KEY,
        "days": day_count,
        "products": normalized_products,
        "stock_text": stock_text,
        "sales_note_text": sales_note,
        "dry_run": bool(dry_run),
        "stages": [],
        "activity": activity,
    }
    driver = None
    stage = "browser_start"

    def begin(key, message):
        nonlocal stage
        stage = key
        if callable(progress_callback):
            progress_callback(key, message)

    def complete(key, details):
        result["stages"].append({"key": key, "success": True, "details": details})

    try:
        begin("browser_start", f"Opening CRM order {order_id} for Stock Extension.")
        driver = shared._open_driver(
            visible=visible,
            attach_browser=attach_browser,
            debugger_address=debugger_address,
        )
        shared.safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        shared._login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        shared._switch_to_crm_app_frame(driver)
        shared._wait_for_order_scope(driver, order_id=order_id)
        crm_handle = driver.current_window_handle
        contact = shared._wait_for_crm_contact_info(driver, order_id=order_id)
        complete("crm_order_verification", {"customer_email": contact["email"]})

        begin("sales_note", "Saving and confirming the Stock Extension Sales Note.")
        note_result = _append_sales_note(driver, sales_note, dry_run=dry_run)
        activity["sales_note_saved"] = bool(note_result.get("updated") or note_result.get("already_present")) and not dry_run
        result["sales_note"] = note_result
        complete("sales_note", note_result)

        begin("salesforce_email", "Preparing and verifying the Stock Extension Salesforce email.")
        salesforce = _prepare_and_send_salesforce_email(
            driver,
            crm_handle,
            order_id,
            contact["email"],
            stock_text,
            day_count,
            normalized_products,
            activity,
            dry_run=dry_run,
            login_wait_seconds=login_wait_seconds,
        )
        result["salesforce"] = salesforce
        complete("salesforce_email", salesforce)

        begin("slack", "Posting the required Stock Extension Slack notification.")
        slack = _send_required_slack(order_id, activity, dry_run=dry_run)
        result["slack"] = slack
        complete("slack", slack)

        begin("crm_status", f"Applying and confirming {CRM_STATUS}.")
        driver.switch_to.window(crm_handle)
        shared._activate_crm_context(driver)
        shared._wait_for_order_scope(driver, order_id=order_id)
        status = shared._apply_order_status(driver, CRM_STATUS, dry_run=dry_run)
        activity["status_applied"] = bool(status.get("status_applied") or status.get("already_applied")) and not dry_run
        result["crm_status"] = status
        complete("crm_status", status)

        result["success"] = True
        result["failed_stage"] = None
        return result
    except Exception as exc:
        if driver is not None:
            shared.safe_take_screenshot(driver, f"stock_issue_extension_{order_id}_{stage}_error")
        result["failed_stage"] = stage
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["stages"].append({"key": stage, "success": False, "message": str(exc)})
        recovery = (
            f"sales_note_saved={activity['sales_note_saved']}, "
            f"email_sent={activity['email_sent']}, email_send_attempted={activity['email_send_attempted']}, "
            f"slack_sent={activity['slack_sent']}, slack_send_attempted={activity['slack_send_attempted']}, "
            f"status_applied={activity['status_applied']}"
        )
        raise StockIssueExtensionError(
            f"Stock Extension stopped at {stage}: {exc}. Recovery state: {recovery}.",
            result=result,
        ) from exc
    finally:
        if driver is not None and not attach_browser:
            shared.safe_driver_quit(driver, profile_path=shared._profile_path())


def run_stock_issue_extension_order(order_id, days, products, **kwargs):
    """Queue-friendly result tuple with structured failure diagnostics."""
    try:
        result = process_stock_issue_extension_order(order_id, days, products, **kwargs)
        return True, f"Stock Extension completed for order {result['order_id']}.", result
    except StockIssueExtensionError as exc:
        return False, str(exc), exc.result
