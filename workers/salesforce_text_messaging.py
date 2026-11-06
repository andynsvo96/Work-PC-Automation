"""Send a receipted pair of texts through the active Salesforce Twilio panel.

A pending click is deliberately never repeated. A matching new outgoing Twilio
row confirms submission even when delivery fails, and can reconcile a receipt.
"""

import json
import os
from pathlib import Path
import tempfile
import time

from selenium.webdriver.common.keys import Keys


class TextMessageError(RuntimeError):
    pass


PANEL_SCRIPT = r"""
const [action, email, message, expectedRecord] = arguments;
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function parent(el) { return el.assignedSlot || el.parentElement || (el.getRootNode && el.getRootNode().host); }
function visible(el) {
  if (!el || !el.getClientRects().length) return false;
  for (let node = el; node && node.nodeType === 1; node = parent(node)) {
    const style = getComputedStyle(node);
    if (node.hidden || node.getAttribute('aria-hidden') === 'true'
        || style.display === 'none' || style.visibility === 'hidden') return false;
  }
  return true;
}
function all(root, selector) {
  const result = Array.from(root.querySelectorAll(selector));
  for (const node of root.querySelectorAll('*')) {
    if (node.shadowRoot) result.push(...all(node.shadowRoot, selector));
  }
  if (root.shadowRoot) result.push(...all(root.shadowRoot, selector));
  return Array.from(new Set(result));
}
function text(el) { return clean(el.innerText || el.textContent); }
const route = location.pathname.match(/\/lightning\/r\/(?:(Account|Contact)\/)?((?:001|003)[a-zA-Z0-9]{12}(?:[a-zA-Z0-9]{3})?)\/view\/?$/);
if (!route) return {error: 'The active Salesforce URL is not an Account or Contact record.'};
const record = `${route[1] || (route[2].startsWith('003') ? 'Contact' : 'Account')}/${route[2]}`;
if (expectedRecord && record !== expectedRecord) return {error: 'The Salesforce record changed. Review the text receipts before retrying.'};
let records = all(document, 'one-record-home-flexipage2').filter(visible);
if (!records.length) records = all(document, '.recordHomeFlexipage2').filter(visible);
if (records.length !== 1) return {error: 'The active Salesforce customer record is missing or ambiguous.'};
const root = records[0];
const emails = all(root, 'a[href^="mailto:"], lightning-formatted-email').filter(visible);
if (!emails.some(el => text(el).toLowerCase() === email.toLowerCase()
    || clean(el.getAttribute('href')).toLowerCase() === `mailto:${email.toLowerCase()}`)) {
  return {error: 'The active Salesforce record does not show the CRM customer email.'};
}
const tabs = all(root, '[role="tab"]').filter(el => visible(el) && text(el) === 'Text');
const panels = all(root, '.TwilioSFTwoWayMessages').filter(visible);
if (action === 'open' && tabs.length === 1 && tabs[0].getAttribute('aria-selected') !== 'true'
    && (record.startsWith('Contact/') || !panels.length)) {
  tabs[0].click();
  return {waiting: true};
}
if (record.startsWith('Contact/') && (tabs.length !== 1 || tabs[0].getAttribute('aria-selected') !== 'true')) {
  return {error: 'The customer Contact Text tab is missing or ambiguous.'};
}
if (panels.length > 1) return {error: 'More than one customer Twilio panel is visible.'};
if (!panels.length) return {waiting: true};
const panel = panels[0];
const problems = all(panel, '.empty-state, .add-message, [role="alert"]').filter(visible)
  .map(text).filter(value => /opt.in required|configuration needed|error authenticating|no phone|invalid phone|opted out|not opted|cannot send|unable to send/i.test(value));
if (problems.length) return {error: 'The customer Twilio panel requires review: ' + problems[0].slice(0, 250)};
const inputs = all(panel, 'textarea[placeholder="Enter Message..."]').filter(visible);
const buttons = all(panel, 'button').filter(el => visible(el) && text(el) === 'Send Message');
if (inputs.length > 1 || buttons.length > 1) return {error: 'The Twilio message input or Send Message button is ambiguous.'};
if (!inputs.length || !buttons.length) return {waiting: true};
const input = inputs[0], button = buttons[0];
const disabled = el => el.disabled || el.getAttribute('aria-disabled') === 'true';
if (action === 'fill') {
  if (disabled(input)) return {error: 'The customer Twilio message input is disabled.'};
  if (input.value && input.value !== message) return {error: 'The Twilio input contains another draft. Review it before retrying.'};
  // Keep the input above Salesforce's fixed utility bar before native entry.
  input.scrollIntoView({block: 'center', inline: 'nearest'});
  input.focus({preventScroll: true});
  return {input, record, value: input.value, enabled: !disabled(input) && !disabled(button)};
}
if (action === 'send') {
  if (input.value !== message || disabled(input) || disabled(button)) return {error: 'The exact text and enabled Send Message button were not retained.'};
  button.click();
  return {clicked: true, record};
}
const rows = all(panel, '.message-wrapper.outgoing, .message-wrapper.outgoing-error').map(row => {
  const body = all(row, '.message')[0];
  const statuses = all(row, '.message-time .uiOutputText');
  return {text: body ? text(body) : '', status: statuses.length ? text(statuses[statuses.length - 1]).toLowerCase() : ''};
});
return {record, value: input.value, enabled: !disabled(input) && !disabled(button), rows};
"""


def _write(path, receipt):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(receipt, stream)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_receipt(path, order_id, recipient):
    """Unreadable or inconsistent receipts must block sends, including retries."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        valid = (receipt["version"] == 1 and receipt["order_id"] == str(order_id)
                 and receipt["recipient"] == recipient.casefold()
                 and isinstance(receipt["email_confirmed"], bool)
                 and (receipt["record"] is None or isinstance(receipt["record"], str)))
        entries = receipt["messages"]
        if not valid or not isinstance(entries, list) or len(entries) != 2:
            raise ValueError("Invalid text receipt")
        for index, entry in enumerate(entries):
            if (not isinstance(entry["text"], str) or not entry["text"].strip()
                    or entry["state"] not in {"ready", "attempted", "confirmed"}):
                raise ValueError("Invalid message")
            if entry["state"] != "ready":
                if (not receipt["email_confirmed"] or not receipt["record"]
                        or type(entry["baseline_count"]) is not int or entry["baseline_count"] < 0):
                    raise ValueError("Invalid send checkpoint")
            if index and entry["state"] != "ready":
                if entries[0]["state"] != "confirmed":
                    raise ValueError("Invalid message order")
            if entry["state"] == "confirmed":
                if not isinstance(entry["status"], str):
                    raise ValueError("Invalid confirmation")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TextMessageError("The Salesforce text receipt cannot be verified. Review Twilio history before retrying; no text was sent.") from exc
    return receipt


def prepare_receipt(path, order_id, recipient, messages):
    path = Path(path)
    previous = load_receipt(path, order_id, recipient)
    if previous is not None:
        if [entry["text"] for entry in previous["messages"]] != messages:
            raise TextMessageError("The payment texts changed from their saved receipt. Review Twilio history before retrying.")
        return previous
    receipt = {"version": 1, "order_id": str(order_id), "recipient": recipient.casefold(),
               "email_confirmed": False, "record": None,
               "messages": [{"text": message, "state": "ready"} for message in messages]}
    path.parent.mkdir(parents=True, exist_ok=True)
    # A concurrent creator or interrupted partial write must never allow a send.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream)
    return receipt


def confirm_email(path, receipt):
    receipt["email_confirmed"] = True
    _write(Path(path), receipt)


def _snapshot(driver, recipient, action="inspect", message="", record=None):
    state = driver.execute_script(PANEL_SCRIPT, action, recipient, message, record) or {}
    if state.get("error"):
        raise TextMessageError(state["error"])
    return state


def _wait_for_panel(driver, recipient, *, record=None, message=None, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        state = _snapshot(driver, recipient, "open", record=record)
        if state.get("record") and (message is None or (state.get("value") == message and state.get("enabled"))):
            return state
        if time.monotonic() >= deadline:
            raise TextMessageError("The customer Twilio message input or enabled Send Message button did not become ready.")
        time.sleep(0.4)


def _fill_text(driver, recipient, message, record):
    state = _snapshot(driver, recipient, "fill", message, record)
    if state.get("input") is None:
        raise TextMessageError("The customer Twilio message input did not become ready for entry.")
    if state.get("value") == message and state.get("enabled"):
        return
    control = state["input"]
    # Lightning does not always bind synthetic input/change events to the Aura
    # message model. Native keys update that model and enable Send Message.
    if state.get("value") == message:
        control.send_keys(Keys.END, " ", Keys.BACKSPACE, Keys.TAB)
    else:
        control.send_keys(message, Keys.TAB)


def _wait_for_recorded_text(driver, recipient, record, entry, timeout):
    deadline = time.monotonic() + timeout
    expected = " ".join(entry["text"].split())
    while True:
        state = _snapshot(driver, recipient, record=record)
        rows = [row for row in state.get("rows", []) if " ".join(str(row.get("text") or "").split()) == expected]
        if len(rows) > entry["baseline_count"]:
            return rows[-1]
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def send_text_messages(driver, path, order_id, recipient, *, timeout=30):
    path = Path(path)
    receipt = load_receipt(path, order_id, recipient)
    if receipt is None or receipt["email_confirmed"] is not True:
        raise TextMessageError("Salesforce email must be confirmed before sending payment texts.")
    sent_count = 0
    recovered_count = 0
    for index, entry in enumerate(receipt["messages"]):
        if entry["state"] == "confirmed":
            continue
        panel = _wait_for_panel(driver, recipient, record=receipt["record"], timeout=timeout)
        receipt["record"] = panel["record"]
        if entry["state"] == "attempted":
            # Read-only recovery: an increased exact outgoing-row count on the
            # original record proves this text is logged. Never click it again.
            row = _wait_for_recorded_text(driver, recipient, receipt["record"], entry, timeout)
            if row is None:
                raise TextMessageError("A previous Salesforce text send is unconfirmed. Review Twilio history before retrying; no text was resent.")
            entry.update(state="confirmed", status=str(row.get("status") or ""), recovered_from_history=True)
            _write(path, receipt)
            recovered_count += 1
            continue
        _fill_text(driver, recipient, entry["text"], receipt["record"])
        panel = _wait_for_panel(driver, recipient, record=receipt["record"], message=entry["text"], timeout=timeout)
        exact = lambda row: " ".join(row.get("text", "").split()) == " ".join(entry["text"].split())
        baseline = sum(exact(row) for row in panel["rows"])
        entry.update(state="attempted", baseline_count=baseline)
        _write(path, receipt)
        # Any exception after this checkpoint leaves a blocked pending receipt.
        clicked = _snapshot(driver, recipient, "send", entry["text"], receipt["record"])
        if clicked.get("clicked") is not True:
            raise TextMessageError("The Salesforce text Send Message click is uncertain. Review Twilio history before retrying.")
        row = _wait_for_recorded_text(driver, recipient, receipt["record"], entry, timeout)
        if row is None:
            raise TextMessageError(f"Salesforce text {index + 1} was not confirmed in Twilio history. Review it before retrying; no automatic resend is allowed.")
        # Delivery status is recorded as information. Even an undelivered or
        # failed outgoing row proves submission and allows the invoice text.
        entry.update(state="confirmed", status=str(row.get("status") or ""))
        _write(path, receipt)
        sent_count += 1
    return {"sent": True, "confirmed_count": 2, "sent_count": sent_count, "recovered_count": recovered_count, "skipped": sent_count == 0,
            "messages": [{"text": entry["text"], "status": entry["status"]} for entry in receipt["messages"]]}
