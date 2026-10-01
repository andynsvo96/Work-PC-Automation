"""Confirm a new email timeline row without treating a Send click as delivery."""

import hashlib
import json
from pathlib import Path
import time

from runtime_paths import STATE_DIR


ACTIVITY_SCRIPT = r"""
const subject = arguments[0];
function clean(value) { return String(value || '').replace(/\s+/g, ' ').trim(); }
function all(root, selector) {
  const result = Array.from(root.querySelectorAll(selector));
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) result.push(...all(el.shadowRoot, selector));
  }
  return result;
}
return all(document, 'li.row').filter(row => {
  if (!row.getClientRects().length || row.closest('[aria-hidden="true"]')) return false;
  if (!all(row, '[icon-name="standard:email"]').length) return false;
  return all(row, '.timelineSubject').some(el => clean(el.textContent) === subject)
    && all(row, '.fromAddress a[href*="/005"]').length
    && all(row, '.toAddress').some(el => clean(el.textContent));
}).map(row => ({
  subject,
  sender: clean(all(row, '.fromAddress')[0].textContent),
  recipient: clean(all(row, '.toAddress')[0].textContent)
}));
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
    baseline = driver.execute_script(ACTIVITY_SCRIPT, subject) or []
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents concurrent/repeated sends; a partial file also blocks retry.
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'order_id': str(order_id), 'subject': subject, 'activity_verified': False}, stream)
    if not click_send(driver):
        raise UnconfirmedEmailError('Salesforce Send was not confirmed. Review Activity before retrying.')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = driver.execute_script(ACTIVITY_SCRIPT, subject) or []
        # Count identical rows as well: the same subject can legitimately be used twice.
        remaining = list(baseline)
        for row in rows:
            if row in remaining:
                remaining.remove(row)
            else:
                result = {'sent': True, 'send_clicked': True, 'activity_verified': True,
                          'order_id': str(order_id), 'subject': subject, 'activity': row}
                path.write_text(json.dumps(result), encoding='utf-8')
                return result
        time.sleep(0.5)
    raise UnconfirmedEmailError(
        f'Send was clicked for order {order_id}, but no new matching Salesforce email Activity appeared. '
        'Send status is unconfirmed; review Activity before retrying. Do not resend automatically.'
    )
