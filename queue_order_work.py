"""Explicit single-order work allowed in the local queue's shared worker pool."""

import copy
import json
import re


MANUAL_WORKERS = {
    "address_validator": "crm_validate_address.py",
    "product_separator": "crm_product_separator.py",
    "order_goods": "crm_order_goods.py",
    "push_back": "crm_push_back.py",
}
SCANNER_WORKFLOWS = frozenset({
    "copyright_cancel", "content_violation_cancel", "existing_designs_cancel",
    "outside_limit_cancel", "unresponsive_cancel", "copyright_removal",
    "copyright_reachout", "complicated_emb_to_hdd", "complicated_emb_feedback",
    "oversize_emb_to_hdd",
})
PARALLEL_ORDER_WORKFLOWS = frozenset(MANUAL_WORKERS) | SCANNER_WORKFLOWS


def parallel_order_request(task):
    """Fail closed on batch/report work, repeats, and unrecognized descriptors."""
    if task.get("restore_error") or task.get("queue_mode", "normal") != "normal":
        return None
    descriptor = task.get("execution_descriptor")
    if isinstance(descriptor, dict):
        if descriptor.get("kind") != "crm.extension_manual":
            return None
        if not isinstance(descriptor.get("arguments"), dict):
            return None
        request = copy.deepcopy(descriptor["arguments"])
    else:
        arguments = task.get("task_arguments") or {}
        if not isinstance(arguments, dict):
            return None
        if arguments.get("list_url") or arguments.get("delete_sheet_row") or arguments.get("dry_run"):
            return None
        task_type = str(task.get("task_type") or "")
        if task_type not in {"crm." + key for key in MANUAL_WORKERS} | {"crm.sheet_scanner_order"}:
            return None
        automation = task_type.removeprefix("crm.")
        if task_type == "crm.sheet_scanner_order":
            automation = arguments.get("process")
        elif automation not in MANUAL_WORKERS:
            return None
        if automation == "address_validator" and arguments.get("action", "validate_order") != "validate_order":
            return None
        request = {
            "order_id": arguments.get("order_id"), "automation_key": automation,
            "reason": arguments.get("reason", ""),
            "request_data": {"designs": copy.deepcopy(arguments["designs"])} if arguments.get("designs") is not None else None,
        }
        request["dry_run"] = bool(arguments.get("dry_run", False))
        request["shipping_filter"] = arguments.get("shipping_filter") or "free"
        request["processing_filter"] = arguments.get("processing_filter", "rush")
    if (request.get("automation_key") not in PARALLEL_ORDER_WORKFLOWS
            or not re.fullmatch(r"\d{7}", str(request.get("order_id") or ""))):
        return None
    request["order_id"] = str(request["order_id"])
    return request


def worker_invocation(request, profile_path, result_path):
    """Build only a targeted invocation of an existing worker; never a report."""
    order_id = str(request.get("order_id") or "")
    automation = request.get("automation_key")
    if automation not in PARALLEL_ORDER_WORKFLOWS or not re.fullmatch(r"\d{7}", order_id):
        raise ValueError("A supported workflow and exact seven-digit order ID are required.")
    args = ["--order-id", order_id, "--result-file", result_path]
    dry_run = bool(request.get("dry_run"))
    if automation in SCANNER_WORKFLOWS:
        args += ["--action", "process_order", "--process", automation, "--profile-dir", profile_path,
                 "--dry-run" if dry_run else "--real"]
        if request.get("reason"):
            args += ["--reason", str(request["reason"])]
        designs = (request.get("request_data") or {}).get("designs")
        if designs is not None:
            args += ["--copyright-designs-json" if automation == "copyright_cancel" else "--emb-designs-json",
                     json.dumps(designs)]
        return "crm_copyright_cancel.py", args
    if automation == "product_separator":
        args += ["--action", "product_separator_order", "--dry-run" if dry_run else "--real"]
    else:
        action = {"address_validator": "validate_order", "order_goods": "order_goods_single", "push_back": "push_back_single"}[automation]
        args += ["--action", action, "--profile-path", profile_path]
        if automation == "address_validator":
            args += ["--shipping-filter", request.get("shipping_filter") or "free"]
        if automation == "push_back":
            args += ["--processing-filter", request.get("processing_filter") or "rush"]
        if dry_run:
            args.append("--dry-run")
    return MANUAL_WORKERS[automation], args
