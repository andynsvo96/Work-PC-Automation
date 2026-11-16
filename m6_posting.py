"""Machine-local M6 posting preference shared by the dashboard and workers."""

import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlparse

from runtime_paths import STATE_DIR


SETTINGS_FILE = Path(STATE_DIR) / "m6_posting.json"
M6_CHANNEL_URL = "https://app.slack.com/client/T03DK2TN7/C04KPACK6VC"
SKIPPED_MESSAGE = "Post to M6 is off; Slack notification skipped."


class M6PostingError(RuntimeError):
    pass


def get_m6_posting_enabled():
    try:
        payload = json.loads(Path(SETTINGS_FILE).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            raise ValueError("Invalid M6 posting preference")
        return payload["enabled"]
    except FileNotFoundError:
        return True
    except (OSError, ValueError) as exc:
        raise M6PostingError("Could not read the Post to M6 setting. Save it again in the dashboard before retrying.") from exc


def set_m6_posting_enabled(enabled):
    if type(enabled) is not bool:
        raise ValueError("Post to M6 needs a boolean enabled value.")
    path = Path(SETTINGS_FILE)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".m6-posting-", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"enabled": enabled}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise M6PostingError("Could not save the Post to M6 setting.") from exc
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return enabled


def skipped_post_result(channel_url, response=None):
    """Describe an intentional skip without creating a delivery receipt."""
    if response is not None:
        skipped = response == SKIPPED_MESSAGE
    else:
        url = urlparse(str(channel_url or ""))
        skipped = (
            url.hostname == "app.slack.com"
            and url.path.rstrip("/") == "/client/T03DK2TN7/C04KPACK6VC"
            and not get_m6_posting_enabled()
        )
    if skipped:
        return {"sent": False, "confirmed": False, "skipped": True,
                "disabled_by_setting": True, "reason": SKIPPED_MESSAGE}
    return None
