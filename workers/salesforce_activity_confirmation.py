"""Confirm a new email timeline row without treating a Send click as delivery."""

import hashlib
import json
from pathlib import Path
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


class UnconfirmedEmailError(RuntimeError):
    pass


def send_and_confirm(driver, order_id, subject, recipient, click_send, *, timeout=30):
    """Persist an attempt before clicking; ambiguity must never trigger a resend."""
    subject = ' '.join(str(subject).split())
    if str(order_id) not in subject:
        raise UnconfirmedEmailError('Email subject does not contain the order number.')
    key = hashlib.sha256(json.dumps([str(order_id), subject, recipient.lower()]).encode()).hexdigest()
    path = Path(STATE_DIR) / 'extra_print_email_confirmations' / f'{key}.json'
    if path.exists():
        previous = json.loads(path.read_text(encoding='utf-8'))
        if previous.get('activity_verified') is True:
            return {**previous, 'skipped': True}
        raise UnconfirmedEmailError(
            'A previous email send attempt is unconfirmed. Review Salesforce Activity before retrying; no email was resent.'
        )
    check_started = time.perf_counter()
    baseline = driver.execute_script(ACTIVITY_SCRIPT, subject) or []
    baseline_seconds = time.perf_counter() - check_started
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents concurrent/repeated sends; a partial file also blocks retry.
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'order_id': str(order_id), 'subject': subject, 'activity_verified': False}, stream)
    click_started = time.perf_counter()
    if not click_send(driver):
        raise UnconfirmedEmailError('Salesforce Send was not confirmed. Review Activity before retrying.')
    click_finished = time.perf_counter()
    polls = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = driver.execute_script(ACTIVITY_SCRIPT, subject) or []
        polls += 1
        # Count identical rows as well: the same subject can legitimately be used twice.
        remaining = list(baseline)
        for row in rows:
            if row in remaining:
                remaining.remove(row)
            else:
                finished = time.perf_counter()
                result = {'sent': True, 'send_clicked': True, 'activity_verified': True,
                          'order_id': str(order_id), 'subject': subject, 'activity': row,
                          'timing': {'baseline_seconds': round(baseline_seconds, 4),
                                     'click_seconds': round(click_finished - click_started, 4),
                                     'after_click_seconds': round(finished - click_finished, 4),
                                     'total_seconds': round(finished - check_started, 4),
                                     'polls': polls}}
                path.write_text(json.dumps(result), encoding='utf-8')
                return result
        time.sleep(0.5)
    raise UnconfirmedEmailError(
        f'Send was clicked for order {order_id}, but no new matching Salesforce email Activity appeared. '
        'Send status is unconfirmed; review Activity before retrying. Do not resend automatically.'
    )
