"""Small append-only history for successful Slack automation posts."""

import json
from datetime import datetime
from urllib.parse import urlparse

from runtime_paths import log_file


SLACK_POST_HISTORY_FILE = log_file("slack_post_history.jsonl")


def _clean(value):
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def channel_id_from_url(channel_url):
    try:
        parts = [part for part in urlparse(str(channel_url or "")).path.split("/") if part]
    except Exception:
        return ""
    if len(parts) >= 3 and parts[0].lower() == "client":
        return parts[2]
    return parts[-1] if parts else ""


def record_slack_post(*, channel_url, message, action, channel_name="", posted_at=None):
    """Record a confirmed Slack post without retaining entries in application state."""
    timestamp = posted_at if isinstance(posted_at, datetime) else datetime.now()
    channel_id = channel_id_from_url(channel_url)
    name = _clean(channel_name)
    if name.startswith("#"):
        name = name[1:].strip()
    entry = {
        "posted_at": timestamp.isoformat(),
        "channel_name": name or channel_id or "Slack channel",
        "channel_id": channel_id,
        "message": _clean(message),
        "action": _clean(action),
    }
    if not entry["message"]:
        return None
    with open(SLACK_POST_HISTORY_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def get_todays_slack_posts(now=None, limit=100):
    """Return only posts whose local timestamp falls on ``now``'s date."""
    current = now if isinstance(now, datetime) else datetime.now()
    try:
        max_rows = min(250, max(1, int(limit)))
    except (TypeError, ValueError):
        max_rows = 100
    rows = []
    try:
        with open(SLACK_POST_HISTORY_FILE, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-1000:]
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
            posted_at = datetime.fromisoformat(str(entry.get("posted_at") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if posted_at.date() != current.date():
            continue
        rows.append(
            {
                "posted_at": posted_at.isoformat(),
                "channel_name": _clean(entry.get("channel_name")) or _clean(entry.get("channel_id")) or "Slack channel",
                "channel_id": _clean(entry.get("channel_id")),
                "message": _clean(entry.get("message")),
                "action": _clean(entry.get("action")),
            }
        )
    rows.sort(key=lambda row: row["posted_at"], reverse=True)
    return rows[:max_rows]
