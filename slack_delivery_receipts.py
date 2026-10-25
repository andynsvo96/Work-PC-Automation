"""Durable duplicate protection for one-time order notifications in Slack."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlparse

from runtime_paths import STATE_DIR


RECEIPT_DIR = Path(STATE_DIR) / "slack_order_deliveries"


class SlackReceiptError(RuntimeError):
    pass


def _identity(channel_url, delivery_key, message):
    url = urlparse(str(channel_url))
    parts = url.path.strip("/").split("/")
    if url.hostname != "app.slack.com" or len(parts) < 3 or parts[0] != "client" or not delivery_key:
        raise SlackReceiptError("Slack duplicate prevention requires an explicit workspace, conversation, and delivery key.")
    return {
        "conversation": "/".join(parts[:3]),
        "delivery_key": str(delivery_key),
        "message": " ".join(str(message).split()),
    }


def _path(identity):
    encoded = json.dumps([identity["conversation"], identity["delivery_key"]]).encode("utf-8")
    return Path(RECEIPT_DIR) / (hashlib.sha256(encoded).hexdigest() + ".json")


def load(channel_url, delivery_key, message):
    identity = _identity(channel_url, delivery_key, message)
    path = _path(identity)
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or receipt.get("version") != 1 or
                receipt.get("status") not in {"submitting", "confirmed"} or
                any(receipt.get(key) != value for key, value in identity.items())):
            raise ValueError("Invalid or mismatched Slack receipt")
        if receipt["status"] == "confirmed" and not re.fullmatch(r"\d{10}\.\d{1,6}", str(receipt.get("message_id") or "")):
            raise ValueError("Missing Slack message timestamp")
        return receipt
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SlackReceiptError("Slack delivery receipt cannot be verified. Automatic sending stopped; review the conversation before resolving the receipt.") from exc


def _write(path, payload, *, create):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if create:
            # Exclusive creation prevents two attempts from both submitting.
            with path.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".slack-", suffix=".tmp", dir=path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    except OSError as exc:
        raise SlackReceiptError("Slack delivery receipt could not be saved. Automatic sending stopped to avoid duplicates; review the conversation.") from exc
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def reserve(channel_url, delivery_key, message):
    """Record uncertainty before Send; an interrupted attempt stays reserved."""
    identity = _identity(channel_url, delivery_key, message)
    _write(_path(identity), {
        "version": 1, **identity, "status": "submitting",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }, create=True)


def confirm(channel_url, delivery_key, message, message_id):
    if not re.fullmatch(r"\d{10}\.\d{1,6}", str(message_id or "")):
        raise SlackReceiptError("Slack confirmation is missing its message timestamp; automatic sending stopped.")
    identity = _identity(channel_url, delivery_key, message)
    previous = load(channel_url, delivery_key, message)
    _write(_path(identity), {
        "version": 1, **(previous or identity), "status": "confirmed",
        "message_id": message_id, "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }, create=previous is None)
