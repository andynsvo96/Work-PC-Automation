"""Single-order Stock Issue -> Suggest Different Color automation."""

from __future__ import annotations

import os
import re
import sys
import time

import config as config_module

WORKERS_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)

import crm_stock_issue_extension as extension
from rush_order_notifications import RUSH_ORDER_SLACK_CHANNEL_URL


shared = extension.shared
AUTOMATION_KEY = "stock_issue_color"
AUTOMATION_NAME = "crm.stock_issue_color"
DISPLAY_NAME = "Stock Issue - Suggest Different Color"
SALESFORCE_TEMPLATE = str(
    getattr(
        config_module,
        "SALESFORCE_STOCK_ISSUE_COLOR_TEMPLATE",
        "[AUTO] STOCK - Color",
    )
    or "[AUTO] STOCK - Color"
).strip()
CRM_STATUS = str(
    getattr(config_module, "STOCK_ISSUE_EXTENSION_CRM_STATUS", "Issue - Stock") or "Issue - Stock"
).strip()
ORDER_NUMBER_PLACEHOLDER = "[ORDER-NUMBER]"
STOCK_PLACEHOLDER = "[STOCK]"
COLOR_PLACEHOLDER = "[COLOR]"
MAX_COLORS = 20
SUGGESTION_LABEL = "color"


class StockIssueColorError(RuntimeError):
    """Raised when Suggest Different Color must stop before its next stage."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = dict(result or {})


STOCK_COLOR_PROCESS = shared.CancelProcess(
    key=AUTOMATION_KEY,
    issue_type=DISPLAY_NAME,
    salesforce_template=SALESFORCE_TEMPLATE,
    template_search=SALESFORCE_TEMPLATE,
    sales_note_reason_label="",
    sales_note_email_line="",
    subject_markers=("urgent stock issue",),
    body_markers=(
        "[stock]",
        "currently out of stock",
        "available color",
        "such as [color]",
        "approve the color change",
        "rushordertees.com team",
    ),
    display_name=DISPLAY_NAME,
    requires_reason=False,
    cancel_and_refund=False,
)


def _clean_color(value):
    color = re.sub(r"\s+", " ", str(value or "")).strip()
    if not color:
        raise StockIssueColorError("Enter at least one suggested color.")
    if len(color) > 80:
        raise StockIssueColorError("Each suggested color must be 80 characters or fewer.")
    if any(ord(char) < 32 for char in color) or "<" in color or ">" in color:
        raise StockIssueColorError("Suggested colors must contain plain text only.")
    return color


def normalize_suggested_colors(colors):
    """Validate, trim, and case-insensitively deduplicate user-entered colors."""
    if isinstance(colors, str):
        raw_colors = colors.split(",")
    elif isinstance(colors, list):
        raw_colors = []
        for value in colors:
            if not isinstance(value, str):
                raise StockIssueColorError("Suggested colors must be plain text separated by commas.")
            raw_colors.extend(value.split(","))
    else:
        raise StockIssueColorError("Enter suggested colors separated by commas.")
    if not raw_colors or len(raw_colors) > MAX_COLORS:
        raise StockIssueColorError(f"Enter between 1 and {MAX_COLORS} suggested colors.")

    normalized = []
    seen = set()
    for raw_color in raw_colors:
        color = _clean_color(raw_color)
        key = color.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(color)
    return normalized


def normalize_request(colors, products):
    try:
        selected_products = extension.normalize_selected_products(products)
    except extension.StockIssueExtensionError as exc:
        message = str(exc).replace("Extension Required", "Suggest Different Color")
        raise StockIssueColorError(message) from exc
    return {
        "colors": normalize_suggested_colors(colors),
        "products": selected_products,
    }


def format_suggested_colors(colors):
    """Format one color, two colors with 'or', or an Oxford-comma list."""
    values = normalize_suggested_colors(colors)
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} or {values[1]}"
    return f"{', '.join(values[:-1])}, or {values[-1]}"


def format_email_stock_text(products):
    try:
        return extension.format_email_stock_text(products)
    except extension.StockIssueExtensionError as exc:
        raise StockIssueColorError(str(exc)) from exc


def format_sales_note(colors, products):
    groups = extension._group_products(products)
    product_text = extension._natural_join(
        [f"{group['style']} in {extension.format_color_list(group['colors'])}" for group in groups],
        final_word="and",
    )
    return f"No stock for {product_text} - suggested {format_suggested_colors(colors)}\nEmailed Txted"


def format_slack_message(order_id):
    order_id = shared._normalize_order_id(order_id)
    return f"{shared.PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)} - Rush Order needs {SUGGESTION_LABEL} change"


def _stock_color_template_signature_error(state):
    state = state if isinstance(state, dict) else {}
    subject = shared._clean_text(state.get("subject"))
    body = shared._clean_text(state.get("body"))
    expected_subject = re.compile(
        r"^RushOrderTees Order #\[ORDER-NUMBER\]\s*-\s*URGENT Stock Issue$",
        flags=re.IGNORECASE,
    )
    if not expected_subject.fullmatch(subject):
        return f"subject did not match the approved Suggest Different Color template (found: {subject or 'blank'})"
    missing = shared._missing_body_markers(body, STOCK_COLOR_PROCESS)
    if missing:
        return f"body was missing: {', '.join(missing)}"
    for placeholder in (STOCK_PLACEHOLDER, COLOR_PLACEHOLDER):
        if body.casefold().count(placeholder.casefold()) != 1:
            return f"body must contain exactly one {placeholder} placeholder"
    return ""


def _click_exact_stock_color_template(driver, template_name=None):
    target = str(template_name or SALESFORCE_TEMPLATE).strip().casefold()
    option = driver.execute_script(
        r"""
        const target = String(arguments[0] || '').replace(/\s+/g, ' ').trim().toLowerCase();
        function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
        function visible(el) {
          if (!el || !el.getBoundingClientRect) return false;
          const rect = el.getBoundingClientRect();
          const view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
          const style = view.getComputedStyle(el);
          return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden'
            && rect.bottom > 0 && rect.top < window.innerHeight && rect.right > 0 && rect.left < window.innerWidth;
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
          // Never treat Salesforce's template-search input as a matching result.
          .filter((el) => !/^(input|textarea|select|option)$/i.test(el.tagName || ''))
          .filter((el) => visible(el) && clean(el.innerText || el.textContent || '') === target)
          .filter((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width >= 40 && rect.height >= 10;
          })
          .sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const aAction = /^(a|button|td|li)$/i.test(a.tagName) || /button|option|menuitem|row/i.test(a.getAttribute('role') || '') ? 0 : 1;
            const bAction = /^(a|button|td|li)$/i.test(b.tagName) || /button|option|menuitem|row/i.test(b.getAttribute('role') || '') ? 0 : 1;
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


def _insert_exact_stock_color_template(driver):
    shared._focus_salesforce_body_editor(driver)
    shared._click_template_button(driver)
    time.sleep(0.5)
    if not shared._open_full_template_picker_from_menu(driver):
        raise StockIssueColorError("Salesforce full email-template picker could not be opened.")
    shared._ensure_private_email_templates_folder(driver)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        shared._search_full_template_modal(driver, SALESFORCE_TEMPLATE)
        time.sleep(1)
        if _click_exact_stock_color_template(driver):
            shared._confirm_salesforce_template_insert(driver)
            if shared._wait_for_salesforce_template_markers(driver, STOCK_COLOR_PROCESS, timeout=20):
                state = shared._read_salesforce_email_state(driver) or {}
                signature_error = _stock_color_template_signature_error(state)
                if not signature_error:
                    return True
                raise StockIssueColorError(
                    f"Salesforce inserted {SALESFORCE_TEMPLATE}, but its content was not the approved version: "
                    f"{signature_error}."
                )
            state = shared._read_salesforce_email_state(driver) or {}
            signature_error = _stock_color_template_signature_error(state) or "approved subject/body markers were not detected before timeout"
            raise StockIssueColorError(
                f"Salesforce inserted {SALESFORCE_TEMPLATE}, but its content could not be verified: {signature_error}."
            )
        shared._scroll_full_template_modal(driver)
        time.sleep(0.5)
    raise StockIssueColorError(
        f"Salesforce template {SALESFORCE_TEMPLATE} was not found after searching its full name."
    )


def _replace_stock_color_placeholders(driver, stock_text, color_text):
    result = driver.execute_script(
        r"""
        const replacements = {
          String(arguments[2]): String(arguments[0]),
          String(arguments[3]): String(arguments[1])
        };
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
            if (!Object.keys(replacements).some((placeholder) => {
              const pattern = new RegExp(placeholder.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
              return pattern.test(original);
            })) continue;
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
        const counts = {[String(arguments[2])]: 0, [String(arguments[3])]: 0, unwrapped_links: 0};
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
        color_text,
        STOCK_PLACEHOLDER,
        COLOR_PLACEHOLDER,
    ) or {}
    if int(result.get(STOCK_PLACEHOLDER) or 0) < 1:
        raise StockIssueColorError(f"Salesforce template body does not contain {STOCK_PLACEHOLDER}.")
    if int(result.get(COLOR_PLACEHOLDER) or 0) < 1:
        raise StockIssueColorError(f"Salesforce template body does not contain {COLOR_PLACEHOLDER}.")
    return result


def _verify_email_content(driver, order_id, stock_text, color_text):
    state = shared._read_salesforce_email_state(driver) or {}
    subject = shared._clean_text(state.get("subject"))
    body = shared._clean_text(state.get("body"))
    unresolved = [
        placeholder
        for placeholder in (ORDER_NUMBER_PLACEHOLDER, STOCK_PLACEHOLDER, COLOR_PLACEHOLDER)
        if placeholder.casefold() in f"{subject}\n{body}".casefold()
    ]
    if unresolved:
        raise StockIssueColorError(f"Salesforce email still contains unresolved placeholders: {', '.join(unresolved)}.")
    if str(order_id) not in subject:
        raise StockIssueColorError(f"Salesforce subject does not contain order {order_id}.")
    if stock_text.casefold() not in body.casefold():
        raise StockIssueColorError("Salesforce body does not contain the complete selected product text.")
    if color_text.casefold() not in body.casefold():
        raise StockIssueColorError(f"Salesforce body does not contain the complete suggested {SUGGESTION_LABEL} list.")
    return {"subject": subject, "body": body, "state": state}


def _selected_product_text_is_linked(driver, products):
    styles = [group["style"].casefold() for group in extension._group_products(products)]
    return bool(driver.execute_script(
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
    ))


def _prepare_and_send_salesforce_email(
    driver,
    crm_handle,
    order_id,
    customer_email,
    stock_text,
    color_text,
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
    _insert_exact_stock_color_template(driver)

    deadline = time.monotonic() + 20
    state = {}
    while time.monotonic() < deadline:
        state = shared._read_salesforce_email_state(driver) or {}
        subject = shared._clean_text(state.get("subject"))
        body = shared._clean_text(state.get("body"))
        if (
            subject
            and body
            and STOCK_PLACEHOLDER.casefold() in body.casefold()
            and COLOR_PLACEHOLDER.casefold() in body.casefold()
        ):
            break
        time.sleep(0.5)
    else:
        raise StockIssueColorError(
            f"Salesforce template {SALESFORCE_TEMPLATE} loaded without the required {STOCK_PLACEHOLDER} and "
            f"{COLOR_PLACEHOLDER} placeholders. Update the live Salesforce template before retrying."
        )
    if not shared._subject_has_order_placeholder(subject) and str(order_id) not in subject:
        raise StockIssueColorError(f"Salesforce template subject does not contain {ORDER_NUMBER_PLACEHOLDER}.")
    if str(order_id) not in subject:
        shared._replace_subject_order_number(driver, order_id)
    replacement = _replace_stock_color_placeholders(driver, stock_text, color_text)
    time.sleep(0.8)
    content = _verify_email_content(driver, order_id, stock_text, color_text)
    if _selected_product_text_is_linked(driver, products):
        raise StockIssueColorError("Selected Salesforce product text is still hyperlinked; refusing to send.")
    recipients = extension._verify_final_recipients(driver, customer_email)
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
        raise StockIssueColorError("Salesforce Send button was not found.")
    confirmation = extension._wait_for_send_confirmation(driver)
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
        raise StockIssueColorError(f"Required Suggest Different Color Slack notification failed: {response}")
    activity["slack_sent"] = True
    result.update({"sent": True, "result": response})
    return result


def inspect_stock_color_template_options(
    order_id,
    search_text="Color",
    *,
    visible=False,
    login_wait_seconds=0,
    select_configured_template=False,
    template_name=None,
):
    """Read matching Salesforce template-picker text without selecting or sending."""
    order_id = shared._normalize_order_id(order_id)
    order_url = shared.PROCESSOR_ORDER_URL_TEMPLATE.format(order_id=order_id)
    driver = None
    try:
        print(f"Opening CRM order {order_id} for template inspection...", flush=True)
        driver = shared._open_driver(visible=visible)
        shared.safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        shared._login_to_crm_if_needed(driver, order_url, login_wait_seconds=login_wait_seconds)
        shared._switch_to_crm_app_frame(driver)
        shared._wait_for_order_scope(driver, order_id=order_id)
        print("CRM order verified; reading its saved Sales Note and customer email...", flush=True)
        try:
            import crm_product_separator

            product_scan = crm_product_separator._scan_order(
                driver,
                expected_order_id=order_id,
                refresh_on_missing_tabs=False,
            )
            shared._activate_crm_context(driver)
            shared._wait_for_order_scope(driver, order_id=order_id)
        except Exception as exc:
            product_scan = {"error": f"{type(exc).__name__}: {exc}"}
        crm_handle = driver.current_window_handle
        sales_notes = shared._order_scope(
            driver,
            "return String(r.addSalesNotes || r.salesNotes || r.filteredSalesNotes || '');",
        )
        contact = shared._wait_for_crm_contact_info(driver, order_id=order_id)
        print("Opening the verified Salesforce customer account...", flush=True)
        shared._open_salesforce_account(
            driver,
            crm_handle,
            contact["email"],
            login_wait_seconds=login_wait_seconds,
            order_id=order_id,
        )
        shared._verify_salesforce_email(driver, contact["email"])
        print("Salesforce customer email verified; opening the email template picker...", flush=True)
        shared._click_salesforce_email(driver, contact["email"])
        shared._wait_for_email_composer(driver)
        shared._focus_salesforce_body_editor(driver)
        shared._click_template_button(driver)
        time.sleep(0.5)
        if not shared._open_full_template_picker_from_menu(driver):
            raise StockIssueColorError("Salesforce full email-template picker could not be opened.")
        shared._ensure_private_email_templates_folder(driver)
        shared._search_full_template_modal(driver, str(search_text or "Color"))
        time.sleep(3)
        print(f"Reading template results for {search_text!r}...", flush=True)
        picker = driver.execute_script(
            r"""
            function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
            function visible(el) {
              if (!el || !el.getBoundingClientRect) return false;
              const rect = el.getBoundingClientRect();
              const style = window.getComputedStyle(el);
              return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
            }
            const dialogs = Array.from(document.querySelectorAll('[role=dialog],section,.modal-container'))
              .filter((el) => visible(el) && /Insert Email Template/i.test(el.innerText || el.textContent || ''))
              .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
            const dialog = dialogs[0] || document;
            const candidates = Array.from(dialog.querySelectorAll('a,button,td,tr,[role=row],[role=option],[role=menuitem],li'))
              .filter(visible)
              .map((el) => clean(el.innerText || el.textContent || ''))
              .filter((text) => text && text.length <= 240)
              .filter((text) => !/^(?:Cancel|Search|Templates|Template Folders)$/i.test(text));
            return {
              text: clean(dialog.innerText || dialog.textContent || ''),
              candidates: Array.from(new Set(candidates)),
            };
            """
        ) or {}
        template_state = None
        signature_error = None
        if select_configured_template:
            inspected_template = str(template_name or SALESFORCE_TEMPLATE).strip()
            print(f"Selecting and reading {inspected_template!r} without sending...", flush=True)
            if not _click_exact_stock_color_template(driver, inspected_template):
                raise StockIssueColorError(
                    f"Salesforce template {inspected_template} was not present in the inspected results."
                )
            shared._confirm_salesforce_template_insert(driver)
            time.sleep(2)
            template_state = shared._read_salesforce_email_state(driver) or {}
            signature_error = _stock_color_template_signature_error(template_state)
        shared.safe_take_screenshot(driver, f"stock_issue_color_{order_id}_template_search_diagnostic")
        return {
            "order_id": order_id,
            "customer_email": contact["email"],
            "sales_notes": str(sales_notes or ""),
            "product_scan": product_scan,
            "search_text": str(search_text or "Color"),
            "picker": picker,
            "configured_template": SALESFORCE_TEMPLATE,
            "inspected_template": str(template_name or SALESFORCE_TEMPLATE).strip(),
            "template_state": template_state,
            "signature_error": signature_error,
        }
    finally:
        if driver is not None:
            shared.safe_driver_quit(driver, profile_path=shared._profile_path())


def process_stock_issue_color_order(
    order_id,
    colors,
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
    request_data = normalize_request(colors, products)
    normalized_products = request_data["products"]
    normalized_colors = request_data["colors"]
    stock_text = format_email_stock_text(normalized_products)
    color_text = format_suggested_colors(normalized_colors)
    sales_note = format_sales_note(normalized_colors, normalized_products)
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
        "colors": normalized_colors,
        "color_text": color_text,
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
        begin("browser_start", f"Opening CRM order {order_id} for Suggest Different Color.")
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

        begin("sales_note", "Saving and confirming the Suggest Different Color Sales Note.")
        note_result = extension._append_sales_note(driver, sales_note, dry_run=dry_run)
        activity["sales_note_saved"] = bool(
            note_result.get("updated") or note_result.get("already_present")
        ) and not dry_run
        result["sales_note"] = note_result
        complete("sales_note", note_result)

        begin("salesforce_email", "Preparing and verifying the Suggest Different Color Salesforce email.")
        salesforce = _prepare_and_send_salesforce_email(
            driver,
            crm_handle,
            order_id,
            contact["email"],
            stock_text,
            color_text,
            normalized_products,
            activity,
            dry_run=dry_run,
            login_wait_seconds=login_wait_seconds,
        )
        result["salesforce"] = salesforce
        complete("salesforce_email", salesforce)

        begin("slack", "Posting the required Suggest Different Color Slack notification.")
        slack = _send_required_slack(order_id, activity, dry_run=dry_run)
        result["slack"] = slack
        complete("slack", slack)

        begin("crm_status", f"Applying and confirming {CRM_STATUS}.")
        driver.switch_to.window(crm_handle)
        shared._activate_crm_context(driver)
        shared._wait_for_order_scope(driver, order_id=order_id)
        status = shared._apply_order_status(driver, CRM_STATUS, dry_run=dry_run)
        activity["status_applied"] = bool(
            status.get("status_applied") or status.get("already_applied")
        ) and not dry_run
        result["crm_status"] = status
        complete("crm_status", status)

        result["success"] = True
        result["failed_stage"] = None
        return result
    except Exception as exc:
        if driver is not None:
            shared.safe_take_screenshot(driver, f"stock_issue_color_{order_id}_{stage}_error")
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
        raise StockIssueColorError(
            f"Suggest Different Color stopped at {stage}: {exc}. Recovery state: {recovery}.",
            result=result,
        ) from exc
    finally:
        if driver is not None and not attach_browser:
            shared.safe_driver_quit(driver, profile_path=shared._profile_path())


def run_stock_issue_color_order(order_id, colors, products, **kwargs):
    """Queue-friendly result tuple with structured failure diagnostics."""
    try:
        result = process_stock_issue_color_order(order_id, colors, products, **kwargs)
        return True, f"Suggest Different Color completed for order {result['order_id']}.", result
    except StockIssueColorError as exc:
        return False, str(exc), exc.result
