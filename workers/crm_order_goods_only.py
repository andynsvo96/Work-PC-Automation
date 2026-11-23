"""Order Goods: preload one window of orders and submit native stock requests.

No unlocks, stock-error investigation, page recovery, or purchase verification.
Response polling only keeps owned tabs alive and retains duplicate protection.
"""

import os
import time
import uuid
from contextlib import ExitStack

from selenium.webdriver.common.by import By

import crm_order_goods_native as native


TAB_LIMIT = 25
POLL_SECONDS = 0.15


def _result(order_id, outcome, message, success=True, **details):
    return {"order_id": order_id, "success": success, "outcome": outcome, "message": message,
            "submission_only": True, **details}


def _read(driver, order_id, action="inspect", frame=None, **options):
    """Find the loaded CRM document, without scanning its messages or notes."""
    driver.switch_to.default_content()
    options["submissionOnly"] = True
    if frame is not None:
        driver.switch_to.frame(frame)
    result = driver.execute_script(native._script(), action, order_id, options)
    if action != "inspect":
        return result  # Never replay a write while discovering another frame.
    if isinstance(result, dict) and result.get("ready"):
        result["_frame"] = None
        return result
    matches = []
    for frame in driver.find_elements(By.CSS_SELECTOR, "iframe"):
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            read = driver.execute_script(native._script(), action, order_id, options)
            if isinstance(read, dict) and read.get("ready"):
                read["_frame"] = frame
                matches.append(read)
        finally:
            driver.switch_to.default_content()
    if len(matches) > 1:
        raise RuntimeError("The CRM order document is ambiguous; no further requests were submitted.")
    return matches[0] if matches else result


def _save_status(job, response):
    record = response.get("record") if isinstance(response, dict) else None
    expected = job["record"]
    if not native._valid(record, job["id"]) or record.get("automationId") != expected["automationId"] or [
        (entry["id"], entry["fingerprint"]) for entry in record["entries"]
    ] != [(entry["id"], entry["fingerprint"]) for entry in expected["entries"]]:
        raise RuntimeError("The submission receipt could not be retained. Check CRM before another attempt.")
    record["submissionOnly"] = True
    native._save(record)
    job["record"] = record


def _finish(job, uncertain=False):
    record = job["record"]
    results = []
    for entry in record["entries"]:
        if uncertain and entry["state"] in {"prepared", "submitted"}:
            entry.update(state="review", failureConfirmed=False)
        submitted = entry["state"] != "not_sent" and job.get("accepted", False)
        unknown = entry["state"] != "not_sent" and not submitted
        results.append(_result(job["id"], "order_goods_submitted" if submitted else ("native_stock_uncertain" if unknown else "native_stock_not_sent"),
            "Stock request submitted; purchase outcome was not checked." if submitted else
            ("Submission outcome unknown; its receipt prevents another attempt." if unknown else "Stock request was not submitted."),
            success=submitted, stock_tab_index=entry["tab"], stock_tab_count=job["count"],
            request_submitted=submitted, manual_review_required=bool(uncertain), stock_order_receipt=str(native._path(job["id"]))))
    if uncertain:
        record["phase"] = "review"
    native._save(record)
    job["results"].extend(results)
    job["done"] = True


def _start(driver, job, snapshot, dry_run):
    designs = snapshot["designs"]
    job["count"] = len(designs)
    job["frame"] = snapshot.get("_frame")
    previous = native._load(job["id"])
    history = {(entry["id"], entry["fingerprint"]): entry for entry in native._history(previous) + snapshot.get("history", [])}
    selected = []
    for design in designs:
        if design.get("eligible") and (design["id"], design["fingerprint"]) not in history:
            selected.append(design)
        else:
            job["results"].append(_result(job["id"], "order_goods_skipped", "No new enabled automatic stock request; skipped.",
                stock_tab_index=design["tab"], stock_tab_count=len(designs)))
    if not selected or dry_run:
        job["results"].extend(_result(job["id"], "order_goods_ready", "Automatic stock request available; dry run submitted nothing.",
            stock_tab_index=design["tab"], stock_tab_count=len(designs)) for design in selected)
        if not designs:
            job["results"].append(_result(job["id"], "order_goods_skipped", "Order is editing or has no automatic stock controls; skipped."))
        job["done"] = True
        return
    record = {"version": 1, "orderId": job["id"], "phase": "submitting", "submissionOnly": True,
        "automationId": uuid.uuid4().hex, "history": list(history.values()), "entries": [
            {"id": design["id"], "tab": design["tab"], "fingerprint": design["fingerprint"], "state": "submitted"} for design in selected]}
    native._save(record)  # Reserve all requests on disk before any native call.
    job["record"] = record
    job["until"] = time.monotonic() + native.REQUEST_TIMEOUT_SECONDS + 5
    response = _read(driver, job["id"], "start", frame=job["frame"], entries=record["entries"], designs=designs, previous=record,
        automationId=record["automationId"], timeoutMs=int(native.REQUEST_TIMEOUT_SECONDS * 1000))
    if not isinstance(response, dict) or not response.get("ready"):
        if isinstance(response, dict) and response.get("notSubmitted"):
            for entry in record["entries"]:
                entry["state"] = "not_sent"
            _finish(job)
            return
        raise RuntimeError("Could not confirm submission. Its saved reservation prevents a duplicate attempt.")
    job["accepted"] = True
    _save_status(job, response)


def _open_tab(driver, url):
    # Creating targets starts every navigation without waiting for another page
    # to load. Only these owned tabs are later closed.
    target = driver.execute_cdp_cmd("Target.createTarget", {"url": url, "background": True})["targetId"]
    handles = driver.window_handles
    found = [handle for handle in handles if handle == target or handle.endswith("-" + target)]
    if len(found) != 1:
        raise RuntimeError("Chrome did not identify the new order tab.")
    return found[0]


def order_window(core, driver, order_ids, *, dry_run=False, order_url=None, existing_handle=None):
    """Open the whole wave first; start ready orders before awaiting responses."""
    source = driver.current_window_handle
    jobs = []
    with ExitStack() as locks:
        for order_id in dict.fromkeys(order_ids):
            job = {"id": order_id, "results": [], "done": False, "until": 0}
            jobs.append(job)
            try:
                blocked = native.recovery_block(order_id, submission_only=True)
                if blocked:
                    raise RuntimeError(blocked)
                if not dry_run:
                    locks.enter_context(native._lock(order_id))
                    blocked = native.recovery_block(order_id, check_lock=False, submission_only=True)
                    if blocked:
                        raise RuntimeError(blocked)
                job["handle"] = existing_handle or _open_tab(driver, (order_url or (lambda value: f"https://crm2.legacy.printfly.com/order/{value}"))(order_id))
                job["until"] = time.monotonic() + max(core.CRM_ACTION_TIMEOUT, 18)
            except Exception as exc:
                job["done"] = True
                job["results"].append(_result(order_id, "native_stock_review_required", str(exc), False, manual_review_required=True))
        try:
            while any(not job["done"] for job in jobs):
                for job in jobs:
                    if job["done"]:
                        continue
                    try:
                        driver.switch_to.window(job["handle"])
                        if "record" not in job:
                            snapshot = _read(driver, job["id"])
                            if isinstance(snapshot, dict) and snapshot.get("ready") and isinstance(snapshot.get("designs"), list):
                                _start(driver, job, snapshot, dry_run)
                        else:
                            response = _read(driver, job["id"], "status", frame=job.get("frame"), automationId=job["record"]["automationId"])
                            _save_status(job, response)
                            if job["record"].get("phase") in {"confirmed", "review"}:
                                _finish(job)
                        if not job["done"] and time.monotonic() >= job["until"]:
                            raise TimeoutError("The order or submission receipt did not become ready. No retry was made.")
                    except Exception as exc:
                        if "record" in job:
                            # The pre-submission disk reservation remains even if
                            # the browser or storage connection disappears.
                            try:
                                _finish(job, uncertain=True)
                            except Exception:
                                pass
                        job["done"] = True
                        job["results"].append(_result(job["id"], "native_stock_review_required", str(exc), False, manual_review_required=True))
                if any(not job["done"] for job in jobs):
                    time.sleep(POLL_SECONDS)
        finally:
            for job in jobs:
                if job.get("handle") and not existing_handle:
                    try:
                        driver.switch_to.window(job["handle"])
                        driver.close()
                    except Exception:
                        pass
            driver.switch_to.window(source)
            driver.switch_to.default_content()
    return [result for job in jobs for result in job["results"]]


def run_with_mode(core, headless_mode, *, dry_run=False, batch_size=None, parallel_workers=1, profile_path=None, list_url=None, order_id=None):
    started = time.monotonic()
    profile = os.path.abspath(profile_path or core.PROFILE_PATH)
    target_url = None if order_id else core._validate_runtime_config(list_url)
    limit = core._normalize_requested_batch_size(batch_size)
    order_ids, report, timings = [], [], []
    passes = 0
    driver = None
    try:
        core._publish_status("Opening one CRM browser for stock submissions.", stage="loading_crm")
        driver = core._build_crm_session_driver(profile, headless_mode=headless_mode, profile_label="CRM Order Goods")
        source = driver.current_window_handle
        if order_id:
            normalized = core._normalize_target_order_id(order_id)
            if not normalized:
                raise ValueError("Order ID must be a seven-digit value or CRM order URL.")
            order_ids.append(normalized)
            blocked = native.recovery_block(normalized, submission_only=True)
            if blocked:
                raise RuntimeError(blocked)
            core._open_target_order(driver, normalized, shipping_filter=core.RUSH_FILTER)
            report.extend(order_window(core, driver, [normalized], dry_run=dry_run, existing_handle=driver.current_window_handle))
        else:
            seen = set()
            while not core._batch_limit_reached(len(order_ids), limit):
                driver.switch_to.window(source)
                remaining = min(TAB_LIMIT, max(0, limit - len(order_ids))) if limit else TAB_LIMIT
                scan_started = time.monotonic()
                collected = core._collect_batch_order_ids_with_driver(driver, core.RUSH_FILTER, remaining,
                    list_url_override=target_url, exclude_order_ids=seen, **core._order_goods_allowed_row_options(target_url))
                ids = list(dict.fromkeys(core._normalize_target_order_id(value) for value in collected))
                if any(not value for value in ids):
                    raise ValueError("The order list returned an invalid order ID.")
                ids = [value for value in ids if value not in seen][:remaining]
                passes += 1
                core._record_stage_timing(timings, "list_scan", scan_started, refresh_pass=passes, order_count=len(ids))
                if not ids:
                    break
                seen.update(ids)
                order_ids.extend(ids)
                core._publish_status(f"Submitting available stock for {len(ids)} orders in one Chrome window.", stage="ordering_stock",
                    current=len(order_ids) - len(ids), total=len(order_ids))
                wave_started = time.monotonic()
                report.extend(order_window(core, driver, ids, dry_run=dry_run))
                core._record_stage_timing(timings, "window_stock_submissions", wave_started, order_count=len(ids), browser_count=1)
    except Exception as exc:
        report.append(_result(order_ids[-1] if order_ids else (order_id or ""), "native_stock_review_required", str(exc), False, manual_review_required=True))
    finally:
        core.safe_driver_quit(driver, profile_path=profile)
    submitted = sum(bool(item.get("request_submitted")) for item in report)
    skipped = sum(item["outcome"] == "order_goods_skipped" for item in report)
    ready = sum(item["outcome"] == "order_goods_ready" for item in report)
    success = all(item["success"] for item in report)
    return {"action": "order_goods_single" if order_id else "order_goods_batch", "success": success,
        "message": (f"Dry run: {ready} stock request(s) available; nothing submitted." if dry_run else
            f"Submitted {submitted} stock request(s) across {len(order_ids)} order(s); {skipped} stock tab(s) skipped. Purchase outcomes were not checked.") +
            (" Some submissions could not be completed; review the report." if not success else ""),
        "submission_only": True, "submitted_request_count": submitted, "skipped_stock_tab_count": skipped,
        "order_count": len(order_ids), "order_ids": order_ids, "report": report, "dry_run": bool(dry_run),
        "headless": bool(headless_mode), "shipping_filter": core.RUSH_FILTER, "list_url": target_url, "batch_size": limit,
        "parallel_workers": max(1, int(parallel_workers or 1)), "browser_count": 1, "tab_batch_size": TAB_LIMIT,
        "refresh_passes": passes, "stage_timings": timings, "duration_seconds": core._elapsed_seconds(started),
        "manual_review_required": any(item.get("manual_review_required") for item in report),
        "resolution": "single" if order_id else ("batch" if order_ids else "no_orders"), "skipped_historical_order_count": 0,
        **({"target_order_id": order_ids[0]} if order_id and order_ids else {})}
