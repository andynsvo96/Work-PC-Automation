"""File-backed handoff for interactive Salesforce verification codes.

Salesforce workers run in child processes, while the control panel runs in the
server process.  One small JSON file per challenge lets either side restart or
poll independently without putting verification codes in logs or result files.
"""

from datetime import datetime, timezone
import json
import os
import re
import secrets
import threading
import time

from runtime_paths import STATE_DIR


REQUEST_DIR = os.path.join(STATE_DIR, "salesforce_verification_requests")
TERMINAL_RETENTION_SECONDS = 60 * 60
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _request_path(request_id):
    clean = str(request_id or "").strip()
    if not REQUEST_ID_RE.fullmatch(clean):
        raise ValueError("Invalid Salesforce verification request ID.")
    return os.path.join(REQUEST_DIR, f"{clean}.json")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _write_json_atomic(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def create_request(*, worker_slot=None, order_id=None, timeout_seconds=300, previous_error=""):
    now = time.time()
    timeout = max(60, min(600, int(timeout_seconds or 300)))
    request_id = secrets.token_urlsafe(24)
    payload = {
        "request_id": request_id,
        "status": "pending",
        "worker_slot": int(worker_slot) if str(worker_slot or "").isdigit() else None,
        "order_id": str(order_id or "").strip(),
        "process_id": os.getpid(),
        "created_at": _now_iso(),
        "created_at_epoch": now,
        "expires_at_epoch": now + timeout,
        "previous_error": str(previous_error or "").strip(),
    }
    _write_json_atomic(_request_path(request_id), payload)
    return dict(payload)


def get_request(request_id):
    return _read_json(_request_path(request_id))


def _request_is_expired(payload, now=None):
    try:
        return float(payload.get("expires_at_epoch") or 0) <= float(now or time.time())
    except (TypeError, ValueError):
        return True


def _worker_is_alive(payload):
    try:
        pid = int(payload.get("process_id") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _public_payload(payload):
    return {
        key: value
        for key, value in payload.items()
        if key != "verification_code"
    }


def list_pending_requests():
    os.makedirs(REQUEST_DIR, exist_ok=True)
    now = time.time()
    pending = []
    for name in os.listdir(REQUEST_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(REQUEST_DIR, name)
        payload = _read_json(path)
        if not payload:
            continue
        status = str(payload.get("status") or "")
        active_status = status in {"pending", "submitted", "processing"}
        if active_status and (_request_is_expired(payload, now) or not _worker_is_alive(payload)):
            payload["status"] = "expired"
            payload["completed_at"] = _now_iso()
            payload.pop("verification_code", None)
            _write_json_atomic(path, payload)
            status = "expired"
        if status == "pending":
            pending.append(_public_payload(payload))
            continue
        try:
            age = now - float(payload.get("created_at_epoch") or now)
        except (TypeError, ValueError):
            age = 0
        if age > TERMINAL_RETENTION_SECONDS:
            try:
                os.remove(path)
            except OSError:
                pass
    pending.sort(key=lambda row: float(row.get("created_at_epoch") or 0))
    return pending


def submit_code(request_id, verification_code):
    code = re.sub(r"\D", "", str(verification_code or ""))
    if len(code) != 6:
        raise ValueError("Enter the 6-digit Salesforce verification code.")
    path = _request_path(request_id)
    payload = _read_json(path)
    if not payload:
        raise ValueError("This Salesforce verification request no longer exists.")
    if str(payload.get("status") or "") != "pending":
        raise ValueError("This Salesforce verification request is no longer waiting for a code.")
    if _request_is_expired(payload):
        raise ValueError("This Salesforce verification request expired. Wait for the worker to request a new code.")
    payload["status"] = "submitted"
    payload["submitted_at"] = _now_iso()
    payload["verification_code"] = code
    _write_json_atomic(path, payload)
    return _public_payload(payload)


def consume_submitted_code(request_id):
    path = _request_path(request_id)
    payload = _read_json(path)
    if not payload:
        return None, "missing"
    status = str(payload.get("status") or "")
    if status == "canceled":
        return None, "canceled"
    if status != "submitted":
        return None, status
    code = re.sub(r"\D", "", str(payload.pop("verification_code", "")))
    if len(code) != 6:
        return None, "invalid"
    payload["status"] = "processing"
    payload["consumed_at"] = _now_iso()
    _write_json_atomic(path, payload)
    return code, "processing"


def cancel_request(request_id):
    path = _request_path(request_id)
    payload = _read_json(path)
    if not payload:
        raise ValueError("This Salesforce verification request no longer exists.")
    if str(payload.get("status") or "") not in {"pending", "submitted"}:
        raise ValueError("This Salesforce verification request is no longer active.")
    payload["status"] = "canceled"
    payload["completed_at"] = _now_iso()
    payload.pop("verification_code", None)
    _write_json_atomic(path, payload)
    return _public_payload(payload)


def finish_request(request_id, *, success, message=""):
    path = _request_path(request_id)
    payload = _read_json(path) or {"request_id": str(request_id or "")}
    payload["status"] = "completed" if success else "failed"
    payload["completed_at"] = _now_iso()
    payload["message"] = str(message or "").strip()
    payload.pop("verification_code", None)
    _write_json_atomic(path, payload)
    return _public_payload(payload)
