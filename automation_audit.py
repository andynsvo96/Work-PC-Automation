"""
Shared automation audit logging helpers.
"""

import os
import threading
from datetime import datetime

from runtime_paths import log_file

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AUDIT_LOG_FILE = log_file("automation_record_log.txt")
_AUDIT_LOCK = threading.Lock()


def _clean_field(value):
    text = "" if value is None else str(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def _timestamp_12h():
    return datetime.now().strftime("%Y-%m-%d %I:%M:%S %p")


def log_automation_event(automation_name, status, message="", source=""):
    name = _clean_field(automation_name) or "unknown_automation"
    state = (_clean_field(status) or "UNKNOWN").upper()
    detail = _clean_field(message)
    origin = _clean_field(source)

    parts = [f"[{_timestamp_12h()}]", f"[{state}]", f"[{name}]"]
    if origin:
        parts.append(f"[source:{origin}]")

    line = " ".join(parts)
    if detail:
        line = f"{line} {detail}"

    with _AUDIT_LOCK:
        with open(AUDIT_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def log_automation_result(automation_name, success, message="", source=""):
    status = "SUCCEEDED" if bool(success) else "FAILED"
    log_automation_event(automation_name, status, message=message, source=source)
