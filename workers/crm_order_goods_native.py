"""Concurrent native CRM stock requests, with durable duplicate protection."""

import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from runtime_paths import STATE_DIR


RECEIPT_DIR = Path(STATE_DIR) / "order_goods_native"
REQUEST_TIMEOUT_SECONDS = 45
POLL_SECONDS = 0.25


@lru_cache(maxsize=1)
def _script():
    root = Path(__file__).resolve().parents[1]
    return (root / "crm-order-dark-mode-extension/order-all-batch.js").read_text(encoding="utf-8") + "\n" + (
        Path(__file__).with_suffix(".js").read_text(encoding="utf-8")
    )


def _path(order_id):
    if not re.fullmatch(r"[0-9]{7}", str(order_id)):
        raise ValueError("Order ID must be a 7-digit value.")
    return RECEIPT_DIR / f"{order_id}.json"


def receipt_exists(order_id):
    return _path(order_id).exists()


def _valid(record, order_id):
    if not isinstance(record, dict) or record.get("version") != 1 or record.get("orderId") != str(order_id):
        return False
    entries = record.get("entries")
    history = record.get("history", [])
    if not isinstance(entries, list) or not entries or not isinstance(history, list):
        return False
    if not all(isinstance(entry, dict) and isinstance(entry.get("id"), str) and re.fullmatch(r"[0-9]+", entry["id"]) and
               isinstance(entry.get("fingerprint"), str) for entry in entries + history):
        return False
    return len({entry["id"] for entry in entries}) == len(entries) and all(
        entry.get("state") in {"prepared", "submitted", "confirmed", "review", "not_sent"} and
        type(entry.get("tab")) is int and entry["tab"] > 0 for entry in entries
    )


def _load(order_id):
    path = _path(order_id)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        record = json.load(handle)
    if not _valid(record, order_id):
        raise ValueError("The native stock receipt is invalid; check CRM stock before retrying.")
    return record


def _save(record):
    path = _path(record["orderId"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def recovery_block(order_id, *, check_lock=True):
    """Check before unlocking, since CRM can itself buy stock during an unlock."""
    try:
        if check_lock and _path(order_id).with_suffix(".lock").exists():
            return "Another native stock run is active or was interrupted. Check the worker and CRM stock before retrying."
        record = _load(order_id)
        if record and any(entry["state"] in {"prepared", "submitted"} or
                          (entry["state"] == "confirmed" and entry.get("verified") is not True) or
                          (entry["state"] == "review" and entry.get("failureConfirmed") is not True)
                          for entry in record["entries"]):
            return f"An earlier native stock request is uncertain. Review CRM stock and {_path(order_id)} before retrying; no stock was resubmitted."
    except (OSError, ValueError, TypeError) as exc:
        return f"The native stock receipt could not be checked: {exc}"
    return None


@contextmanager
def _lock(order_id):
    path = _path(order_id).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.close(handle)
        yield
    finally:
        path.unlink()


def _result(order_id, tab, count, outcome, message, success=False, **details):
    return {
        "order_id": order_id, "success": success, "outcome": outcome, "message": message,
        "manual_review_required": not success, "stock_tab_index": tab["tab"], "stock_tab_count": count,
        "stock_tab_label": tab.get("label", ""), "stock_design_id": tab.get("id", ""), **details,
    }


def _errors(order_id, tabs, message):
    tabs = tabs or [{}]
    return [_result(order_id, {"tab": index + 1, "label": tab.get("label", "")}, len(tabs),
                    "native_stock_review_required", message) for index, tab in enumerate(tabs)]


def _inspect(driver, order_id, action="inspect"):
    deadline = time.monotonic() + 10
    while True:
        snapshot = driver.execute_script(_script(), action, str(order_id))
        if isinstance(snapshot, dict) and snapshot.get("ready") and isinstance(snapshot.get("designs"), list):
            return snapshot
        message = (snapshot or {}).get("error", "CRM native stock inventory could not be read.") if isinstance(snapshot, dict) else "CRM native stock inventory could not be read."
        if "not ready" not in message or time.monotonic() >= deadline:
            raise RuntimeError(message)
        time.sleep(POLL_SECONDS)


def _history(record):
    return (record or {}).get("history", []) + [
        {"id": entry["id"], "fingerprint": entry["fingerprint"]}
        for entry in (record or {}).get("entries", []) if entry["state"] == "confirmed"
    ]


def order_stock_tabs(driver, order_id, tabs, *, dry_run=False, wait_ready=None):
    try:
        if dry_run:
            return _run(driver, order_id, tabs, dry_run=True, wait_ready=wait_ready)
        with _lock(order_id):
            return _run(driver, order_id, tabs, wait_ready=wait_ready)
    except Exception as exc:
        return _errors(order_id, tabs, f"Native Order All stopped before further stock requests: {exc}")


def _run(driver, order_id, tabs, *, dry_run=False, wait_ready=None):
    blocked = recovery_block(order_id, check_lock=False)
    if blocked:
        return _errors(order_id, tabs, blocked)
    previous = _load(order_id)
    snapshot = _inspect(driver, order_id)
    designs = snapshot["designs"]
    if len(designs) != len(tabs):
        raise RuntimeError("The native design count does not match the loaded stock tabs.")
    for design, tab in zip(designs, tabs):
        design["label"] = tab.get("label", "")
    if any(design["kind"] == "busy" for design in designs):
        raise RuntimeError("Native stock ordering is already running on this order.")
    history = { (entry["id"], entry["fingerprint"]): entry for entry in _history(previous) + snapshot.get("history", []) }
    selected = [design for design in designs if design["eligible"] and (design["id"], design["fingerprint"]) not in history]
    results = {}
    for design in designs:
        if design in selected:
            results[design["id"]] = _result(order_id, design, len(tabs), "order_goods_ready", "Native Order Goods is available.", True)
        elif design["kind"] == "locked":
            results[design["id"]] = _result(order_id, design, len(tabs), "order_goods_locked", "This tab's automatic stock control is disabled or locked; check its remaining stock.")
        elif design["stockRecorded"] and not design["eligible"]:
            results[design["id"]] = _result(order_id, design, len(tabs), "already_stock_ordered", "Stock is already recorded for this tab; no request was sent.", True)
        elif (design["id"], design["fingerprint"]) in history:
            results[design["id"]] = _result(order_id, design, len(tabs), "native_stock_review_required", "CRM previously confirmed this tab's stock, but its saved stock is not verified. Check CRM; the request was not repeated.")
        elif design["kind"] == "manual":
            results[design["id"]] = _result(order_id, design, len(tabs), "stock_tab_not_vendor_orderable", "This tab requires manual ordering; no request was sent.", True)
        else:
            results[design["id"]] = _result(order_id, design, len(tabs), "native_stock_review_required", "This tab has no eligible automatic Order Goods control; check its stock.")
    if dry_run or not selected:
        return list(results.values())

    # Reserve every request on disk before invoking any native service. Browser
    # session receipts alone do not survive the workers' temporary profiles.
    record = {
        "version": 1, "orderId": str(order_id), "phase": "submitting", "automationId": uuid.uuid4().hex,
        "history": list(history.values()), "entries": [
            {"id": design["id"], "tab": design["tab"], "fingerprint": design["fingerprint"], "state": "submitted", "message": "Awaiting CRM confirmation."}
            for design in selected
        ],
    }
    _save(record)
    automation_id = record["automationId"]
    try:
        response = driver.execute_script(_script(), "start", str(order_id), {
            "entries": record["entries"], "designs": designs, "previous": record,
            "automationId": automation_id, "timeoutMs": int(REQUEST_TIMEOUT_SECONDS * 1000),
        })
        if not isinstance(response, dict) or not response.get("ready"):
            if isinstance(response, dict) and response.get("notSubmitted"):
                for entry in record["entries"]:
                    entry["state"] = "not_sent"
                    entry["message"] = response.get("error", "No stock request was sent.")
            raise RuntimeError((response or {}).get("error", "CRM did not return a native stock receipt."))
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS + 5
        while True:
            current = response.get("record")
            if not _valid(current, order_id) or current.get("automationId") != automation_id or [
                (entry["id"], entry["fingerprint"]) for entry in current["entries"]
            ] != [(entry["id"], entry["fingerprint"]) for entry in record["entries"]]:
                raise RuntimeError("CRM's native stock receipt changed or could not be read.")
            record = current
            _save(record)
            if record.get("phase") in {"confirmed", "review"}:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Timed out waiting for native stock confirmations.")
            time.sleep(POLL_SECONDS)
            response = driver.execute_script(_script(), "status", str(order_id), {"automationId": automation_id})
            if not isinstance(response, dict) or not response.get("ready"):
                raise RuntimeError("The page changed before all stock requests were confirmed.")
    except Exception as exc:
        record["phase"] = "review"
        record["message"] = str(exc)
        for entry in record["entries"]:
            if entry["state"] in {"prepared", "submitted"}:
                entry.update(state="review", failureConfirmed=False, message=str(exc))
        try:
            _save(record)
        except OSError:
            pass  # The pre-submission reservation still prevents a blind retry.

    persisted = {}
    verification_error = ""
    if any(entry["state"] == "confirmed" for entry in record["entries"]):
        try:
            driver.refresh()  # One reload after the entire batch has settled.
            if wait_ready:
                wait_ready()
            persisted = {design["id"]: design for design in _inspect(driver, order_id, "verify")["designs"]}
        except Exception as exc:
            verification_error = str(exc)
    for entry in record["entries"]:
        design = next(design for design in selected if design["id"] == entry["id"])
        saved = persisted.get(entry["id"], {})
        verified = (entry["state"] == "confirmed" and saved.get("fingerprint") == entry["fingerprint"]
                    and saved.get("stockRecorded") and saved.get("kind") == "ordered" and not saved.get("eligible"))
        if verified:
            outcome, message = "auto_order_succeeded", "CRM confirmed stock ordered; saved stock was verified after the batch refresh."
        elif entry["state"] == "confirmed":
            outcome, message = "native_stock_not_verified", f"CRM confirmed the request, but saved stock could not be verified. Do not reorder; check CRM. {verification_error}".strip()
        elif entry["state"] == "not_sent":
            outcome, message = "native_stock_not_sent", entry.get("message") or record.get("message") or "No stock request was sent."
        else:
            outcome, message = "native_stock_failed" if entry.get("failureConfirmed") is True else "native_stock_uncertain", entry.get("message") or record.get("message") or "CRM did not confirm stock ordering. Check CRM before retrying."
        entry["verified"] = bool(verified)
        results[entry["id"]] = _result(order_id, design, len(tabs), outcome, message, bool(verified), stock_order_receipt=str(_path(order_id)))
    try:
        _save(record)
    except OSError as exc:
        return _errors(order_id, tabs, f"Could not save the stock confirmations: {exc}. Check CRM before retrying.")
    return list(results.values())
