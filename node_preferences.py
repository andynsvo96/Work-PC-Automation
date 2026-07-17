"""Machine-local preferences that must not be synchronized through Git."""

from __future__ import annotations

import json
import os
import tempfile
import threading

from runtime_paths import state_file


NODE_PREFERENCES_FILE = state_file("node_preferences.json")
_lock = threading.RLock()


def default_node_preferences():
    return {
        # Preserve the established single-worker behavior until Auto is
        # explicitly selected in Settings on this computer.
        "worker_mode": "manual",
        "manual_workers": 1,
    }


def _normalize_worker_mode(value):
    return "manual" if str(value or "").strip().lower() == "manual" else "auto"


def _normalize_worker_count(value):
    try:
        return max(1, min(8, int(value)))
    except (TypeError, ValueError):
        return 1


def normalize_node_preferences(values):
    values = values if isinstance(values, dict) else {}
    return {
        "worker_mode": _normalize_worker_mode(values.get("worker_mode")),
        "manual_workers": _normalize_worker_count(values.get("manual_workers")),
    }


def load_node_preferences(path=NODE_PREFERENCES_FILE):
    with _lock:
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = default_node_preferences()
        return normalize_node_preferences(payload)


def save_node_preferences(values, path=NODE_PREFERENCES_FILE):
    normalized = normalize_node_preferences(values)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with _lock:
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                suffix=".tmp",
                delete=False,
                encoding="utf-8",
                dir=parent,
            ) as handle:
                temp_path = handle.name
                json.dump(normalized, handle, indent=2)
            os.replace(temp_path, path)
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
    return normalized


def update_node_preferences(updates, path=NODE_PREFERENCES_FILE):
    if not isinstance(updates, dict):
        raise ValueError("Node preference updates must be an object.")
    with _lock:
        current = load_node_preferences(path)
        if "worker_mode" in updates:
            current["worker_mode"] = _normalize_worker_mode(updates.get("worker_mode"))
        if "manual_workers" in updates:
            current["manual_workers"] = _normalize_worker_count(updates.get("manual_workers"))
        return save_node_preferences(current, path)
