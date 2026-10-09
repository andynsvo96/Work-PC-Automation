"""Confirm a new email timeline row without treating a Send click as delivery."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from runtime_paths import STATE_DIR


ACTIVITY_SCRIPT = r"""
const subject = arguments[0];
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function label(el) {
  if (!el) return '';
  const value = clean(el.innerText || el.textContent);
  if (value) return value;
  return el.shadowRoot ? clean(Array.from(el.shadowRoot.childNodes).map(label).join(' ')) : '';
}
function all(root, selector) {
  const result = Array.from(root.querySelectorAll(selector));
  if (root.shadowRoot) result.push(...all(root.shadowRoot, selector));
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) result.push(...all(el.shadowRoot, selector));
  }
  return result;
}
const matches = [];
for (const row of new Set(all(document, 'li.row'))) {
  if (!row.getClientRects().length || row.closest('[aria-hidden="true"]')) continue;
  if (!all(row, '.timelineSubject, .subjectLink, .subjectText')
        .some(el => clean(el.textContent) === subject)) continue;
  const from = all(row, '.fromAddress')[0];
  const to = all(row, '.toAddress')[0];
  // Lightning component boundaries cannot be crossed by a compound CSS selector.
  if (from && to && all(row, '[icon-name="standard:email"]').length
      && all(from, 'a[href*="/005"]').length && label(to)) {
    matches.push({subject, sender: label(from), recipient: label(to)});
    continue;
  }
  // Salesforce also renders persisted EmailMessage records using the Aura timeline.
  const summary = all(row, '.summary').find(el => /sent an email to/i.test(clean(el.textContent)));
  if (summary) {
    const sender = all(summary, 'a[href^="mailto:"]')[0];
    const recipient = all(summary, '.outputLookupLink')[0];
    if (sender && recipient && clean(recipient.textContent)) {
      matches.push({subject, sender: clean(sender.textContent), recipient: clean(recipient.textContent)});
    }
  }
}
return matches;
"""


FAILURE_MARKER = 'Salesforce email confirmation stopped for order'


class UnconfirmedEmailError(RuntimeError):
    pass


def _write_receipt(path, receipt):
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(receipt, stream)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def send_and_confirm(driver, order_id, subject, recipient, click_send, *, timeout=30,
                     prepare_retry=None, body=None, receipt_scope=None):
    """Confirm Activity, refresh, then allow one validated resend within this run.

    Persist every attempt before clicking. A later run must never restart the
    two-attempt budget, including when an older unconfirmed receipt exists.
    """
    subject = ' '.join(str(subject).split())
    if str(order_id) not in subject:
        raise UnconfirmedEmailError('Email subject does not contain the order number.')
    identity = [str(order_id), subject, recipient.lower()]
    if receipt_scope is not None:
        identity.insert(0, receipt_scope)
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    path = Path(STATE_DIR) / 'extra_print_email_confirmations' / f'{key}.json'
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(previous, dict):
                raise ValueError('Invalid receipt')
        except (OSError, ValueError) as exc:
            raise UnconfirmedEmailError(
                f'{FAILURE_MARKER} {order_id}: the previous email receipt cannot be read. '
                'Review Salesforce Activity before retrying; no email was resent.'
            ) from exc
        if previous.get('activity_verified') is True:
            return {**previous, 'skipped': True}
        raise UnconfirmedEmailError(
            f'{FAILURE_MARKER} {order_id}: a previous email send attempt is unconfirmed. '
            'Review Salesforce Activity before retrying; no email was resent.'
        )
    check_started = time.perf_counter()
    baseline = driver.execute_script(ACTIVITY_SCRIPT, subject) or []
    baseline_seconds = time.perf_counter() - check_started
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'order_id': str(order_id), 'subject': subject, 'activity_verified': False,
               'send_attempts': 0, 'refreshes': 0, 'baseline_count': len(baseline)}
    # Keep the existing receipt folder/identity for Extra Print Areas and Extension Required.
    # Exclusive creation prevents concurrent sends; a partial file also blocks retry.
    with path.open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream)
    polls = 0
    click_seconds = 0
    click_finished = check_started

    def stop(reason):
        message = (f'{FAILURE_MARKER} {order_id} after {receipt["send_attempts"]} send attempt(s): '
                   f'{reason} No further email will be sent automatically; review Salesforce Activity.')
        receipt.update({'manual_review_required': True, 'retryable': False, 'message': message})
        try:
            _write_receipt(path, receipt)
        except OSError:
            # The earlier unconfirmed receipt still blocks any later run.
            pass
        raise UnconfirmedEmailError(message)

    def new_activity():
        nonlocal polls
        rows = driver.execute_script(ACTIVITY_SCRIPT, subject) or []
        polls += 1
        # Refresh can change sender/recipient labels without adding an email. Only
        # an increased count of outgoing rows with this exact subject confirms Send.
        return rows[-1] if len(rows) > len(baseline) else None

    def wait_for_activity():
        deadline = time.monotonic() + timeout
        while True:
            row = new_activity()
            if row is not None:
                return row
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    def confirmed(row, phase):
        finished = time.perf_counter()
        result = {**receipt, 'sent': True, 'send_clicked': True, 'activity_verified': True,
                  'activity': row, 'confirmation_phase': phase,
                  'timing': {'baseline_seconds': round(baseline_seconds, 4),
                             'click_seconds': round(click_seconds, 4),
                             'after_click_seconds': round(finished - click_finished, 4),
                             'total_seconds': round(finished - check_started, 4), 'polls': polls}}
        _write_receipt(path, result)
        return result

    try:
        for attempt in (1, 2):
            if attempt == 2:
                if prepare_retry is None:
                    stop('No new matching Salesforce email Activity appeared, and the draft cannot be rebuilt.')
                draft = prepare_retry()
                state = draft.get('email_state') or draft
                if (draft.get('dry_run') is not True or draft.get('sent')
                        or ' '.join(str(state.get('subject') or '').split()) != subject
                        or (body is not None and ' '.join(str(state.get('body') or '').split())
                            != ' '.join(str(body).split()))):
                    stop('The rebuilt email did not retain the original subject and body; the second email was not sent.')
                # A delayed first send may appear while its replacement is prepared.
                row = new_activity()
                if row is not None:
                    return confirmed(row, 'before_resend')
            receipt['send_attempts'] = attempt
            _write_receipt(path, receipt)
            click_started = time.perf_counter()
            if not click_send(driver):
                stop('Salesforce Send button was not confirmed.')
            click_finished = time.perf_counter()
            click_seconds += click_finished - click_started
            row = wait_for_activity()
            if row is not None:
                return confirmed(row, 'after_send')
            driver.refresh()
            receipt['refreshes'] += 1
            _write_receipt(path, receipt)
            row = wait_for_activity()
            if row is not None:
                return confirmed(row, 'after_refresh')
        stop('No new matching Salesforce email Activity appeared after the second email and refresh check.')
    except UnconfirmedEmailError:
        raise
    except Exception as exc:
        stop(f'The send or confirmation check could not be completed ({exc}).')
