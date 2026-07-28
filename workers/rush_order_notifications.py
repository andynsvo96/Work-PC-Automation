"""Post-success Slack notifications for paid rush orders."""

from decimal import Decimal, InvalidOperation

import config as config_module

from slack_team import run as _run_slack_team


# The channel requested for rush-order reachout notifications.  A local
# config.py may override this when the workspace/channel changes.
DEFAULT_RUSH_ORDER_SLACK_CHANNEL_URL = "https://app.slack.com/client/T03DK2TN7/C04KPACK6VC"
RUSH_ORDER_SLACK_CHANNEL_URL = str(
    getattr(config_module, "RUSH_ORDER_SLACK_CHANNEL_URL", DEFAULT_RUSH_ORDER_SLACK_CHANNEL_URL)
    or DEFAULT_RUSH_ORDER_SLACK_CHANNEL_URL
).strip()
INTERNATIONAL_STANDARD_SHIPPING_RATE = Decimal("25.00")


class RushOrderSlackNotificationError(RuntimeError):
    """Raised when a required paid-rush notification could not be sent."""


def _shipping_amount(value):
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() == "free":
        return Decimal("0.00")
    cleaned = "".join(char for char in text if char.isdigit() or char in ".-")
    if not cleaned or cleaned in {"-", ".", "-."}:
        return None
    try:
        return Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def rush_notification_eligibility(shipping_charge):
    """Return whether a shipping charge requires the paid-rush Slack notice.

    CRM uses $25.00 as its standard international/military rate.  That rate is
    not rush; amounts above it, as well as other positive paid domestic rates,
    are rush.  Unknown shipping charges are deliberately kept quiet.
    """
    amount = _shipping_amount(shipping_charge)
    result = {
        "eligible": False,
        "shipping_charge": str(shipping_charge or ""),
        "shipping_amount": f"{amount:.2f}" if amount is not None else "",
    }
    if amount is None:
        result["reason"] = "Shipping charge could not be determined; Slack notification skipped."
    elif amount <= 0:
        result["reason"] = "Order has no paid shipping; Slack notification skipped."
    elif amount == INTERNATIONAL_STANDARD_SHIPPING_RATE:
        result["reason"] = "Order uses the $25.00 standard international/military rate; Slack notification skipped."
    else:
        result["eligible"] = True
        result["reason"] = "Order has paid rush shipping."
    return result


def send_paid_rush_notification(order_url, notification_type, shipping_charge, dry_run=False):
    """Send the requested notification after its CRM/Salesforce work succeeds."""
    result = rush_notification_eligibility(shipping_charge)
    result.update(
        {
            "sent": False,
            "dry_run": bool(dry_run),
            "channel_url": RUSH_ORDER_SLACK_CHANNEL_URL,
            "message": "",
        }
    )
    if not result["eligible"]:
        return result

    message = f"{str(order_url or '').strip()} Rush Order - {str(notification_type or '').strip()}"
    result["message"] = message
    if dry_run:
        result["planned"] = True
        return result

    ok, response = _run_slack_team(
        "custom",
        custom_message=message,
        channel_url=RUSH_ORDER_SLACK_CHANNEL_URL,
    )
    if not ok:
        raise RushOrderSlackNotificationError(f"Rush-order Slack notification failed: {response}")
    result.update({"sent": True, "result": response})
    return result
