"""
Slack day-message rotation helpers.

Provides optional alternating day messages that flip between
primary/alternate text each time that day/action is successfully used.
"""

import json
import os
import threading
from datetime import datetime

DAY_NAMES = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]
VALID_ACTIONS = {"in", "out"}
STATE_FILENAME = "slack_message_rotation_state.json"
STATE_VERSION = 1

_state_lock = threading.Lock()


def _clean_text(value):
    return str(value or "").strip()


def _default_state():
    return {"version": STATE_VERSION, "entries": {}}


def _default_state_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILENAME)


def _normalize_entry(raw):
    if not isinstance(raw, dict):
        return {"use_alt_next": False, "last_used_date": "", "last_used_variant": ""}
    return {
        "use_alt_next": bool(raw.get("use_alt_next")),
        "last_used_date": str(raw.get("last_used_date") or ""),
        "last_used_variant": str(raw.get("last_used_variant") or ""),
    }


def _normalize_state(raw):
    if not isinstance(raw, dict):
        return _default_state()
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    cleaned_entries = {}
    for key, value in entries.items():
        cleaned_entries[str(key)] = _normalize_entry(value)
    return {"version": STATE_VERSION, "entries": cleaned_entries}


def _load_state(path):
    if not os.path.exists(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return _default_state()
    return _normalize_state(raw)


def _save_state(path, state):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(temp_path, path)


def _state_key(action, day_name):
    action_key = str(action or "").strip().lower()
    day_key = str(day_name or "").strip().upper()
    if action_key not in VALID_ACTIONS:
        raise ValueError(f"Unsupported action '{action}'.")
    if day_key not in DAY_NAMES:
        raise ValueError(f"Unsupported day name '{day_name}'.")
    return f"{action_key}:{day_key}"


def _day_name_for_datetime(now=None):
    if now is None:
        now = datetime.now()
    idx = (now.weekday() + 1) % 7
    return DAY_NAMES[idx]


def _resolve_config_keys(action, day_name):
    action_key = str(action or "").strip().lower()
    if action_key not in VALID_ACTIONS:
        raise ValueError(f"Unsupported action '{action}'.")
    day_key = str(day_name or "").strip().upper()
    if day_key not in DAY_NAMES:
        raise ValueError(f"Unsupported day name '{day_name}'.")
    base = f"SLACK_MESSAGE_{'IN' if action_key == 'in' else 'OUT'}_{day_key}"
    return base, f"{base}_ALTERNATE", f"{base}_ALTERNATE_ENABLED"


def _pick_variant(entry, date_iso, alternating_active):
    last_date = str(entry.get("last_used_date") or "")
    last_variant = str(entry.get("last_used_variant") or "")
    if last_date == date_iso and last_variant in ("primary", "alternate"):
        if alternating_active:
            return last_variant
        return "primary"
    if alternating_active and bool(entry.get("use_alt_next")):
        return "alternate"
    return "primary"


def select_slack_day_message(config_obj, action, now=None, state_file_path=None):
    """
    Resolve the effective Slack day message for an action ("in"/"out").
    Does not mutate rotation state.
    """
    if now is None:
        now = datetime.now()
    state_path = state_file_path or _default_state_path()

    action_key = str(action or "").strip().lower()
    day_name = _day_name_for_datetime(now)
    base_key, alternate_key, enabled_key = _resolve_config_keys(action_key, day_name)

    primary_message = _clean_text(getattr(config_obj, base_key, ""))
    alternate_message = _clean_text(getattr(config_obj, alternate_key, ""))
    alternating_enabled = bool(getattr(config_obj, enabled_key, False))
    alternating_active = bool(alternating_enabled and alternate_message)
    date_iso = now.date().isoformat()
    key = _state_key(action_key, day_name)

    with _state_lock:
        state = _load_state(state_path)
        entry = _normalize_entry(state.get("entries", {}).get(key))
    variant = _pick_variant(entry, date_iso, alternating_active)

    message = alternate_message if variant == "alternate" else primary_message
    if not message and variant == "alternate":
        # If alternate was selected but now empty, safely fall back.
        variant = "primary"
        message = primary_message

    return {
        "action": action_key,
        "day_name": day_name,
        "display_day_name": day_name.title(),
        "date_iso": date_iso,
        "variant": variant,
        "message": message,
        "alternating_enabled": alternating_enabled,
        "alternating_active": alternating_active,
        "primary_message": primary_message,
        "alternate_message": alternate_message,
        "state_file_path": state_path,
        "state_key": key,
    }


def record_slack_day_message_use(selection, now=None, state_file_path=None):
    """
    Persist a successful use of a previously selected day message.
    """
    if not isinstance(selection, dict):
        raise ValueError("selection must be a dict returned by select_slack_day_message().")
    if now is None:
        now = datetime.now()

    action = str(selection.get("action") or "").strip().lower()
    day_name = str(selection.get("day_name") or "").strip().upper()
    variant = str(selection.get("variant") or "").strip().lower()
    if variant not in ("primary", "alternate"):
        variant = "primary"

    key = str(selection.get("state_key") or _state_key(action, day_name))
    used_on = str(selection.get("date_iso") or now.date().isoformat())
    alternating_active = bool(selection.get("alternating_active"))
    path = state_file_path or selection.get("state_file_path") or _default_state_path()

    with _state_lock:
        state = _load_state(path)
        entries = state.setdefault("entries", {})
        entry = _normalize_entry(entries.get(key))

        changed = False
        if entry.get("last_used_date") != used_on:
            entry["last_used_date"] = used_on
            changed = True
        if entry.get("last_used_variant") != variant:
            entry["last_used_variant"] = variant
            changed = True

        next_use_alt = bool(alternating_active and variant == "primary")
        if bool(entry.get("use_alt_next")) != next_use_alt:
            entry["use_alt_next"] = next_use_alt
            changed = True

        if changed:
            entries[key] = entry
            _save_state(path, state)

    return True
