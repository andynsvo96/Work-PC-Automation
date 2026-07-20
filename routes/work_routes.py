"""
Work-domain route registration for the automation server.
"""

import re
from datetime import datetime

from flask import g, jsonify, request


def register_work_routes(
    app,
    *,
    enqueue_automation,
    run_clock,
    automation_test_catalog,
    run_automation_test_suite,
    run_slack,
    is_trueish,
    start_slack_lunch_break,
    get_slack_lunch_payload,
    cancel_slack_lunch_break,
    run_work,
    run_work_sync,
    schedule_auto_clock_out_from_active_shift,
    update_manual_auto_clock_out_schedule,
    clear_auto_clock_out_schedule,
    get_work_status_payload,
    start_crm_run,
    run_crm_run_queued,
    get_crm_status_payload,
    get_crm_state_payload,
    clear_crm_history,
    start_crm_address_run,
    run_crm_address_run_queued,
    get_crm_address_status_payload,
    get_crm_address_state_payload,
    clear_crm_address_history,
    set_crm_address_filter,
    update_crm_address_preferences,
    start_crm_order_goods_run,
    run_crm_order_goods_run_queued,
    get_crm_order_goods_status_payload,
    update_crm_order_goods_preferences,
    start_crm_shipping_bypasser_run,
    run_crm_shipping_bypasser_run_queued,
    get_crm_shipping_bypasser_status_payload,
    start_crm_push_back_run,
    run_crm_push_back_run_queued,
    get_crm_push_back_status_payload,
    open_sanmar_cart_browser,
    start_crm_product_separator_run,
    run_crm_product_separator_run_queued,
    get_crm_product_separator_status_payload,
    start_crm_auto_splitter_run,
    run_crm_auto_splitter_run_queued,
    get_crm_auto_splitter_status_payload,
    clear_crm_auto_splitter_history,
    start_crm_mass_emailer_run,
    run_crm_mass_emailer_run_queued,
    get_crm_mass_emailer_status_payload,
    clear_crm_mass_emailer_history,
    start_crm_processing_run,
    run_crm_processing_run_queued,
    get_crm_processing_status_payload,
    get_crm_processing_state_payload,
    update_crm_processing_preferences,
):
    def _first_present(*values):
        for value in values:
            if value is not None:
                return value
        return None

    def _queue_response(
        label,
        category,
        fn,
        status_payload_fn=None,
        queue_details=None,
        queue_options=None,
        task_type=None,
        task_arguments=None,
        required_capability=None,
        target_node=None,
    ):
        queue_options = queue_options if isinstance(queue_options, dict) else {}
        shared_defaults = {
            "Paycom Clock In": ("communications.paycom_clock", {"action": "in", "dry_run": False}),
            "Paycom Clock Out": ("communications.paycom_clock", {"action": "out", "dry_run": False}),
            "Paycom Clock In Dry Run": ("communications.paycom_clock", {"action": "in", "dry_run": True}),
            "Paycom Clock Out Dry Run": ("communications.paycom_clock", {"action": "out", "dry_run": True}),
            "Slack In": ("communications.slack", {"action": "in"}),
            "Slack Out": ("communications.slack", {"action": "out"}),
            "Slack Lunch Start": ("communications.slack_lunch", {"force_test_url": False}),
            "Slack Lunch Test Start": ("communications.slack_lunch", {"force_test_url": True}),
            "Work In": ("communications.work", {"action": "in", "automatic": False}),
            "Work Out": ("communications.work", {"action": "out", "automatic": False}),
            "Sync Paycom Hours": ("communications.work_sync", {}),
            "Stock Unlocker": ("crm.stock_unlocker", {"dry_run": False}),
            "Stock Unlocker Dry Run": ("crm.stock_unlocker", {"dry_run": True}),
        }
        default_descriptor = shared_defaults.get(label)
        if default_descriptor and not task_type:
            task_type, task_arguments = default_descriptor
        if required_capability is None and str(task_type or "").startswith("crm."):
            required_capability = "crm"
        if target_node is None:
            automatic_target = str(getattr(g, "automation_target_node", "") or "").strip()
            requested_target = automatic_target or str(request.headers.get("X-Automation-Target-Node") or "").strip()
            target_node = requested_target if requested_target and requested_target.lower() != "any" else None
        ok, msg, task = enqueue_automation(
            label,
            category,
            fn,
            details=queue_details,
            status_fn=status_payload_fn if callable(status_payload_fn) else None,
            task_type=task_type,
            task_arguments=task_arguments if isinstance(task_arguments, dict) else {},
            required_capability=required_capability,
            target_node=target_node,
            **queue_options,
        )
        payload = status_payload_fn() if callable(status_payload_fn) else {"success": True}
        payload.update({"success": ok, "message": msg, "queued": ok, "queue_task": task})
        if getattr(g, "home_automation_request", False):
            payload["home_assistant_failure"] = not ok
            payload["target_node"] = target_node
        failure_status = 503 if getattr(g, "home_automation_request", False) else 500
        return jsonify(payload), (202 if ok else failure_status)

    @app.route("/clock/in", methods=["POST", "GET"])
    def clock_in():
        return _queue_response("Paycom Clock In", "Communications", lambda: run_clock("in", dry_run=False))

    @app.route("/clock/out", methods=["POST", "GET"])
    def clock_out():
        return _queue_response("Paycom Clock Out", "Communications", lambda: run_clock("out", dry_run=False))

    @app.route("/clock/test/in", methods=["POST", "GET"])
    def clock_test_in():
        return _queue_response("Paycom Clock In Dry Run", "Development", lambda: run_clock("in", dry_run=True))

    @app.route("/clock/test/out", methods=["POST", "GET"])
    def clock_test_out():
        return _queue_response("Paycom Clock Out Dry Run", "Development", lambda: run_clock("out", dry_run=True))

    @app.route("/automation/test-options", methods=["GET"])
    def automation_test_options():
        return jsonify(
            {
                "success": True,
                "tests": automation_test_catalog,
            }
        )

    @app.route("/automation/test-suite", methods=["POST"])
    def automation_test_suite():
        data = request.get_json(silent=True) or {}
        selected = data.get("tests")
        return _queue_response(
            "Automation Test Suite",
            "Development",
            lambda: run_automation_test_suite(selected)[:2],
            task_type="development.test_suite",
            task_arguments={"selected_tests": selected},
        )

    @app.route("/slack/in", methods=["POST", "GET"])
    def slack_in():
        return _queue_response("Slack In", "Communications", lambda: run_slack("in"))

    @app.route("/slack/out", methods=["POST", "GET"])
    def slack_out():
        return _queue_response("Slack Out", "Communications", lambda: run_slack("out"))

    @app.route("/slack/lunch", methods=["POST", "GET"])
    def slack_lunch():
        use_test_url = False
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            use_test_url = is_trueish(data.get("test_url"))
        else:
            use_test_url = is_trueish(request.args.get("test_url"))
        label = "Slack Lunch Test Start" if use_test_url else "Slack Lunch Start"
        return _queue_response(
            label,
            "Development" if use_test_url else "Communications",
            lambda: start_slack_lunch_break(force_test_url=use_test_url),
            lambda: {"success": True, "lunch": get_slack_lunch_payload()},
        )

    @app.route("/slack/lunch/status", methods=["GET"])
    def slack_lunch_status():
        return jsonify(get_slack_lunch_payload()), 200

    @app.route("/slack/lunch/cancel", methods=["POST", "GET"])
    def slack_lunch_cancel():
        ok, msg = cancel_slack_lunch_break(audit=True)
        payload = get_slack_lunch_payload()
        return jsonify({"success": ok, "message": msg, "lunch": payload}), 200

    @app.route("/work/in", methods=["POST", "GET"])
    def work_in():
        return _queue_response("Work In", "Communications", lambda: run_work("in", automatic=False))

    @app.route("/work/out", methods=["POST", "GET"])
    def work_out():
        return _queue_response("Work Out", "Communications", lambda: run_work("out", automatic=False))

    @app.route("/work/sync", methods=["POST", "GET"])
    def work_sync():
        return _queue_response("Sync Paycom Hours", "Communications", run_work_sync)

    @app.route("/work/schedule", methods=["POST", "GET"])
    def work_schedule():
        ok, msg = schedule_auto_clock_out_from_active_shift()
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/work/update-schedule", methods=["POST", "GET"])
    def work_update_schedule():
        raw_time = None
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            raw_time = data.get("time")
            if raw_time is None:
                raw_time = data.get("auto_clock_out_at")
        else:
            raw_time = request.args.get("time") or request.args.get("auto_clock_out_at")
        ok, msg = update_manual_auto_clock_out_schedule(raw_time)
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/work/cancel-schedule", methods=["POST", "GET"])
    def work_cancel_schedule():
        ok, msg = clear_auto_clock_out_schedule()
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/work/status", methods=["GET"])
    def work_status():
        return jsonify(get_work_status_payload())

    @app.route("/crm/unlock", methods=["POST", "GET"])
    def crm_unlock():
        return _queue_response("Stock Unlocker", "Processing", lambda: run_crm_run_queued(dry_run=False), get_crm_status_payload)

    def _crm_processing_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "stock_unlocker_enabled": _first_present(
                data.get("stock_unlocker_enabled"),
                data.get("stockUnlockerEnabled"),
                request.args.get("stock_unlocker_enabled"),
                request.args.get("stockUnlockerEnabled"),
            ),
            "address_validator_enabled": _first_present(
                data.get("address_validator_enabled"),
                data.get("addressValidatorEnabled"),
                request.args.get("address_validator_enabled"),
                request.args.get("addressValidatorEnabled"),
            ),
            "order_goods_enabled": _first_present(
                data.get("order_goods_enabled"),
                data.get("orderGoodsEnabled"),
                request.args.get("order_goods_enabled"),
                request.args.get("orderGoodsEnabled"),
            ),
            "shipping_bypasser_enabled": _first_present(
                data.get("shipping_bypasser_enabled"),
                data.get("shippingBypasserEnabled"),
                request.args.get("shipping_bypasser_enabled"),
                request.args.get("shippingBypasserEnabled"),
            ),
            "push_back_enabled": _first_present(
                data.get("push_back_enabled"),
                data.get("pushBackEnabled"),
                request.args.get("push_back_enabled"),
                request.args.get("pushBackEnabled"),
            ),
            "product_separator_enabled": _first_present(
                data.get("product_separator_enabled"),
                data.get("productSeparatorEnabled"),
                request.args.get("product_separator_enabled"),
                request.args.get("productSeparatorEnabled"),
            ),
            "processing_filter": _first_present(
                data.get("processing_filter"),
                data.get("processingFilter"),
                data.get("filter"),
                request.args.get("processing_filter"),
                request.args.get("processingFilter"),
                request.args.get("filter"),
            ),
            "advanced_mode": _first_present(
                data.get("advanced_mode"),
                data.get("advancedMode"),
                data.get("queue_mode"),
                data.get("queueMode"),
                request.args.get("advanced_mode"),
                request.args.get("advancedMode"),
                request.args.get("queue_mode"),
                request.args.get("queueMode"),
            ),
            "repeat_interval_minutes": _first_present(
                data.get("repeat_interval_minutes"),
                data.get("repeatIntervalMinutes"),
                data.get("repeat_minutes"),
                data.get("repeatMinutes"),
                request.args.get("repeat_interval_minutes"),
                request.args.get("repeatIntervalMinutes"),
                request.args.get("repeat_minutes"),
                request.args.get("repeatMinutes"),
            ),
            "scheduled_time": _first_present(
                data.get("scheduled_time"),
                data.get("scheduledTime"),
                data.get("scheduled_for"),
                data.get("scheduledFor"),
                request.args.get("scheduled_time"),
                request.args.get("scheduledTime"),
                request.args.get("scheduled_for"),
                request.args.get("scheduledFor"),
            ),
        }

    def _crm_processing_filter_label(processing_filter):
        key = str(processing_filter or "").strip().lower()
        key = key.replace("-", "_").replace(" ", "_")
        if key == "813":
            return "813"
        if key == "high_value":
            return "High Value"
        if key == "all":
            return "All"
        if key == "free":
            return "Free Ship"
        return "Rush"

    def _crm_processing_bool(value, default=False):
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        return is_trueish(value)

    def _crm_processing_effective_options(options):
        options = dict(options or {})
        payload = get_crm_processing_state_payload()
        state = payload.get("state") if isinstance(payload, dict) else {}
        state = state if isinstance(state, dict) else {}
        raw_filter = options.get("processing_filter")
        processing_filter = str(raw_filter if raw_filter is not None else state.get("processing_filter") or "rush").strip().lower()
        processing_filter = processing_filter.replace("-", "_").replace(" ", "_")
        if processing_filter not in {"rush", "free", "all", "813", "high_value"}:
            processing_filter = "rush"
        rush_like = processing_filter in {"rush", "high_value"}
        unlocker_capable = rush_like or processing_filter in {"free", "all"}
        mode_preferences = state.get("mode_preferences") if isinstance(state.get("mode_preferences"), dict) else {}
        mode_state = mode_preferences.get(processing_filter) if isinstance(mode_preferences.get(processing_filter), dict) else {}
        fallback_state = {**state, **mode_state}

        effective = {
            "stock_unlocker_enabled": _crm_processing_bool(
                options.get("stock_unlocker_enabled"),
                fallback_state.get("stock_unlocker_enabled", True),
            ),
            "address_validator_enabled": _crm_processing_bool(
                options.get("address_validator_enabled"),
                fallback_state.get("address_validator_enabled", True),
            ),
            "product_separator_enabled": _crm_processing_bool(
                options.get("product_separator_enabled"),
                fallback_state.get("product_separator_enabled", True),
            ),
            "order_goods_enabled": _crm_processing_bool(
                options.get("order_goods_enabled"),
                fallback_state.get("order_goods_enabled", True),
            ),
            "shipping_bypasser_enabled": _crm_processing_bool(
                options.get("shipping_bypasser_enabled"),
                fallback_state.get("shipping_bypasser_enabled", False),
            ),
            "push_back_enabled": _crm_processing_bool(
                options.get("push_back_enabled"),
                fallback_state.get("push_back_enabled", False),
            ),
            "processing_filter": processing_filter,
        }
        if processing_filter == "all":
            effective["shipping_bypasser_enabled"] = False
            effective["push_back_enabled"] = False
        elif processing_filter == "813":
            effective["stock_unlocker_enabled"] = False
            effective["product_separator_enabled"] = False
        elif processing_filter == "free":
            effective["shipping_bypasser_enabled"] = False
            effective["push_back_enabled"] = False
        elif not unlocker_capable:
            effective["stock_unlocker_enabled"] = False
            effective["order_goods_enabled"] = False
            effective["shipping_bypasser_enabled"] = False
            effective["push_back_enabled"] = False
        return effective

    def _crm_processing_advanced_mode(options):
        key = str((options or {}).get("advanced_mode") or "").strip().lower()
        if key in {"repeat", "scheduled"}:
            return key
        return "normal"

    def _crm_processing_schedule_label(value):
        text = str(value or "").strip()
        if not text:
            return "selected time"
        try:
            return datetime.fromisoformat(text).strftime("%b %d, %I:%M %p").replace(" 0", " ")
        except ValueError:
            return text

    def _crm_processing_step_signature(effective):
        steps = []
        processing_filter = effective.get("processing_filter")
        rush_like = processing_filter in {"rush", "high_value"}
        if effective.get("address_validator_enabled"):
            steps.append("validator")
        if effective.get("product_separator_enabled") and processing_filter != "813":
            steps.append("separator")
        if effective.get("stock_unlocker_enabled") and (rush_like or processing_filter in {"free", "all"}):
            steps.append("unlocker")
        if (
            effective.get("order_goods_enabled")
            and (rush_like or processing_filter in {"free", "all", "813"})
        ):
            steps.append("order_goods")
        if effective.get("shipping_bypasser_enabled") and (rush_like or processing_filter == "813"):
            steps.append("shipping_bypasser")
        if effective.get("push_back_enabled") and (rush_like or processing_filter == "813"):
            steps.append("push_back")
        return steps

    def _crm_processing_queue_options(options):
        effective = _crm_processing_effective_options(options)
        mode = _crm_processing_advanced_mode(options)
        steps = _crm_processing_step_signature(effective)
        signature = {
            "type": "crm_processing",
            "mode": effective.get("processing_filter"),
            "steps": steps,
            "advanced_mode": mode,
        }
        queue_options = {"automation_signature": signature}
        if mode == "repeat":
            interval = options.get("repeat_interval_minutes")
            interval_label = 5 if interval in (None, "") else interval
            repeat_label = (
                "Repeat immediately"
                if str(interval_label).strip() == "0"
                else f"Repeat every {interval_label} minutes"
            )
            queue_options.update(
                {
                    "queue_mode": "repeat",
                    "repeat_interval_minutes": interval,
                    "advanced_summary": f"{repeat_label} | {effective.get('processing_filter')} | {', '.join(steps)}",
                }
            )
            signature["repeat_interval_minutes"] = interval
        elif mode == "scheduled":
            scheduled = options.get("scheduled_time")
            scheduled_label = _crm_processing_schedule_label(scheduled)
            queue_options.update(
                {
                    "queue_mode": "scheduled",
                    "scheduled_for": scheduled,
                    "advanced_summary": f"Scheduled for {scheduled_label} | {effective.get('processing_filter')} | {', '.join(steps)}",
                }
            )
            signature["scheduled_time"] = scheduled
        return queue_options

    def _crm_processing_queue_label(options):
        effective = _crm_processing_effective_options(options)
        advanced_mode = _crm_processing_advanced_mode(options)
        steps = []
        processing_filter = effective.get("processing_filter")
        rush_like = processing_filter in {"rush", "high_value"}
        if effective.get("address_validator_enabled"):
            steps.append("Validator")
        if effective.get("product_separator_enabled") and processing_filter != "813":
            steps.append("Separator")
        if effective.get("stock_unlocker_enabled") and (rush_like or processing_filter in {"free", "all"}):
            steps.append("Unlocker")
        if (
            effective.get("order_goods_enabled")
            and (rush_like or processing_filter in {"free", "all", "813"})
        ):
            steps.append("Order Goods")
        if effective.get("shipping_bypasser_enabled") and (rush_like or processing_filter == "813"):
            steps.append("Shipping Bypasser")
        if effective.get("push_back_enabled") and (rush_like or processing_filter == "813"):
            steps.append("Push Back")
        step_text = ", ".join(steps) if steps else "none selected"
        prefix = "Processing"
        if advanced_mode == "repeat":
            prefix = "Repeat Processing"
        elif advanced_mode == "scheduled":
            prefix = "Scheduled Processing"
        return f"{prefix} - {_crm_processing_filter_label(effective.get('processing_filter'))}: {step_text}"

    @app.route("/crm/process", methods=["POST", "GET"])
    def crm_process():
        raw_options = _crm_processing_request_options()
        options = _crm_processing_effective_options(raw_options)
        queue_source = {**raw_options, **options}
        return _queue_response(
            _crm_processing_queue_label(queue_source),
            "Processing",
            lambda: run_crm_processing_run_queued(**options),
            get_crm_processing_status_payload,
            queue_options=_crm_processing_queue_options(queue_source),
            task_type="crm.processing",
            task_arguments=options,
        )

    def _crm_processing_mode_options(processing_filter):
        options = _crm_processing_request_options()
        options["processing_filter"] = processing_filter
        return options

    def _start_crm_processing_mode(processing_filter):
        raw_options = _crm_processing_mode_options(processing_filter)
        options = _crm_processing_effective_options(raw_options)
        queue_source = {**raw_options, **options}
        return _queue_response(
            _crm_processing_queue_label(queue_source),
            "Processing",
            lambda: run_crm_processing_run_queued(**options),
            get_crm_processing_status_payload,
            queue_options=_crm_processing_queue_options(queue_source),
            task_type="crm.processing",
            task_arguments=options,
        )

    @app.route("/crm/process/rush", methods=["POST", "GET"])
    @app.route("/crm/process/rushes", methods=["POST", "GET"])
    def crm_process_rush():
        return _start_crm_processing_mode("rush")

    @app.route("/crm/process/free", methods=["POST", "GET"])
    @app.route("/crm/process/free-ship", methods=["POST", "GET"])
    @app.route("/crm/process/free_ship", methods=["POST", "GET"])
    def crm_process_free_ship():
        return _start_crm_processing_mode("free")

    @app.route("/crm/process/all", methods=["POST", "GET"])
    def crm_process_all():
        return _start_crm_processing_mode("all")

    @app.route("/crm/process/813", methods=["POST", "GET"])
    def crm_process_813():
        return _start_crm_processing_mode("813")

    @app.route("/crm/process/high-value", methods=["POST", "GET"])
    @app.route("/crm/process/high_value", methods=["POST", "GET"])
    @app.route("/crm/process/highvalue", methods=["POST", "GET"])
    def crm_process_high_value():
        return _start_crm_processing_mode("high_value")

    @app.route("/crm/process/status", methods=["GET"])
    def crm_process_status():
        return jsonify(get_crm_processing_status_payload()), 200

    @app.route("/crm/process/state", methods=["GET"])
    def crm_process_state():
        return jsonify(get_crm_processing_state_payload()), 200

    @app.route("/crm/process/preferences", methods=["POST"])
    def crm_process_preferences():
        options = _crm_processing_request_options()
        preference_options = {
            key: options.get(key)
            for key in (
                "stock_unlocker_enabled",
                "address_validator_enabled",
                "product_separator_enabled",
                "order_goods_enabled",
                "shipping_bypasser_enabled",
                "push_back_enabled",
                "processing_filter",
            )
        }
        ok, msg, _state = update_crm_processing_preferences(**preference_options)
        payload = get_crm_processing_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/unlock/dry-run", methods=["POST", "GET"])
    def crm_unlock_dry_run():
        return _queue_response("Stock Unlocker Dry Run", "Processing", lambda: run_crm_run_queued(dry_run=True), get_crm_status_payload)

    @app.route("/crm/status", methods=["GET"])
    def crm_status():
        return jsonify(get_crm_status_payload()), 200

    @app.route("/crm/state", methods=["GET"])
    def crm_state():
        return jsonify(get_crm_state_payload()), 200

    @app.route("/crm/history/clear", methods=["POST"])
    def crm_history_clear():
        ok, msg = clear_crm_history()
        payload = get_crm_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200


    def _crm_address_request_order_id():
        raw = None
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            raw = data.get("order_id")
            if raw is None:
                raw = data.get("orderId")
            if raw is None:
                raw = data.get("target_order_id")
        if raw is None:
            raw = request.args.get("order_id") or request.args.get("orderId") or request.args.get("target_order_id")
        return raw

    def _crm_address_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "order_id": _crm_address_request_order_id(),
            "action": _first_present(data.get("action"), data.get("mode"), request.args.get("action"), request.args.get("mode")),
            "batch_size": _first_present(data.get("batch_size"), data.get("batchSize"), request.args.get("batch_size"), request.args.get("batchSize")),
            "parallel_workers": _first_present(data.get("parallel_workers"), data.get("parallelWorkers"), request.args.get("parallel_workers"), request.args.get("parallelWorkers")),
            "list_url": _first_present(data.get("list_url"), data.get("listUrl"), request.args.get("list_url"), request.args.get("listUrl")),
        }

    def _crm_order_goods_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "order_id": _first_present(data.get("order_id"), data.get("orderId"), data.get("target_order_id"), request.args.get("order_id"), request.args.get("orderId"), request.args.get("target_order_id")),
            "batch_size": _first_present(data.get("batch_size"), data.get("batchSize"), request.args.get("batch_size"), request.args.get("batchSize")),
            "parallel_workers": _first_present(data.get("parallel_workers"), data.get("parallelWorkers"), request.args.get("parallel_workers"), request.args.get("parallelWorkers")),
            "list_url": _first_present(data.get("list_url"), data.get("listUrl"), request.args.get("list_url"), request.args.get("listUrl")),
        }

    def _crm_shipping_bypasser_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "order_id": _first_present(data.get("order_id"), data.get("orderId"), data.get("target_order_id"), request.args.get("order_id"), request.args.get("orderId"), request.args.get("target_order_id")),
            "batch_size": _first_present(data.get("batch_size"), data.get("batchSize"), request.args.get("batch_size"), request.args.get("batchSize")),
            "list_url": _first_present(data.get("list_url"), data.get("listUrl"), request.args.get("list_url"), request.args.get("listUrl")),
        }

    def _crm_push_back_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "order_id": _first_present(data.get("order_id"), data.get("orderId"), data.get("target_order_id"), request.args.get("order_id"), request.args.get("orderId"), request.args.get("target_order_id")),
            "processing_filter": _first_present(data.get("processing_filter"), data.get("processingFilter"), data.get("filter"), request.args.get("processing_filter"), request.args.get("processingFilter"), request.args.get("filter")),
            "batch_size": _first_present(data.get("batch_size"), data.get("batchSize"), request.args.get("batch_size"), request.args.get("batchSize")),
            "parallel_workers": _first_present(data.get("parallel_workers"), data.get("parallelWorkers"), request.args.get("parallel_workers"), request.args.get("parallelWorkers")),
            "list_url": _first_present(data.get("list_url"), data.get("listUrl"), request.args.get("list_url"), request.args.get("listUrl")),
        }

    def _crm_product_separator_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "order_id": _first_present(data.get("order_id"), data.get("orderId"), data.get("target_order_id"), request.args.get("order_id"), request.args.get("orderId"), request.args.get("target_order_id")),
            "list_mode": _first_present(data.get("list_mode"), data.get("listMode"), data.get("mode"), data.get("filter"), request.args.get("list_mode"), request.args.get("listMode"), request.args.get("mode"), request.args.get("filter")),
            "parallel_workers": _first_present(data.get("parallel_workers"), data.get("parallelWorkers"), request.args.get("parallel_workers"), request.args.get("parallelWorkers")),
            "list_url": _first_present(data.get("list_url"), data.get("listUrl"), request.args.get("list_url"), request.args.get("listUrl")),
        }

    def _crm_auto_splitter_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "order_target": _first_present(
                data.get("order_target"),
                data.get("orderTarget"),
                data.get("order_id"),
                data.get("orderId"),
                data.get("order_url"),
                data.get("orderUrl"),
                request.args.get("order_target"),
                request.args.get("orderTarget"),
                request.args.get("order_id"),
                request.args.get("orderId"),
                request.args.get("order_url"),
                request.args.get("orderUrl"),
            ),
            "tab_count": _first_present(data.get("tab_count"), data.get("tabCount"), request.args.get("tab_count"), request.args.get("tabCount")),
            "divisions": _first_present(data.get("divisions"), data.get("division_count"), data.get("divisionCount"), request.args.get("divisions"), request.args.get("division_count"), request.args.get("divisionCount")),
            "minimum_tabs": _first_present(data.get("minimum_tabs"), data.get("minimumTabs"), request.args.get("minimum_tabs"), request.args.get("minimumTabs")),
            "parallel_workers": _first_present(data.get("parallel_workers"), data.get("parallelWorkers"), request.args.get("parallel_workers"), request.args.get("parallelWorkers")),
        }

    def _crm_auto_splitter_order_label(order_target):
        text = str(order_target or "").strip()
        match = re.search(r"(?:^|/order/|[?&]order_id=)(\d{5,})", text, flags=re.I) or re.search(r"\b(\d{5,})\b", text)
        return match.group(1) if match else text

    def _crm_stock_order_label(order_id):
        text = str(order_id or "").strip()
        match = re.search(r"(?:^|/order/|[?&]order_id=)(\d{5,})", text, flags=re.I) or re.search(r"\b(\d{5,})\b", text)
        return match.group(1) if match else ""

    def _crm_stock_queue_label(base_label, options):
        order = _crm_stock_order_label((options or {}).get("order_id"))
        return f"{base_label} - Order {order}" if order else base_label

    def _crm_auto_splitter_queue_label(options, dry_run=False):
        order = _crm_auto_splitter_order_label(options.get("order_target"))
        prefix = "Auto Splitter Dry Run" if dry_run else "Auto Splitter"
        return f"{prefix} - Order {order}" if order else prefix

    def _crm_auto_splitter_queue_details(options):
        parts = []
        tab_count = options.get("tab_count")
        divisions = options.get("divisions")
        workers = options.get("parallel_workers")
        if tab_count not in (None, ""):
            parts.append(f"Tabs {tab_count}")
        if divisions not in (None, ""):
            parts.append(f"Divisions {divisions}")
        if workers not in (None, ""):
            parts.append(f"Workers {workers}")
        return " | ".join(parts)

    def _crm_mass_emailer_request_options():
        data = request.get_json(silent=True) if request.method == "POST" else None
        data = data if isinstance(data, dict) else {}
        return {
            "limit": _first_present(data.get("limit"), data.get("row_limit"), data.get("rowLimit"), request.args.get("limit"), request.args.get("row_limit"), request.args.get("rowLimit")),
            "retry_errors": is_trueish(
                _first_present(
                    data.get("retry_errors"),
                    data.get("retryErrors"),
                    request.args.get("retry_errors"),
                    request.args.get("retryErrors"),
                )
            ),
        }

    def _crm_mass_emailer_queue_details(options):
        parts = []
        limit = options.get("limit")
        if limit not in (None, ""):
            parts.append(f"Limit {limit}")
        if options.get("retry_errors"):
            parts.append("Retry errors")
        return " | ".join(parts)

    @app.route("/crm/mass-emailer", methods=["POST", "GET"])
    @app.route("/crm/mass-email", methods=["POST", "GET"])
    def crm_mass_emailer():
        options = _crm_mass_emailer_request_options()
        return _queue_response(
            "Sheets Scanner",
            "Processing",
            lambda: run_crm_mass_emailer_run_queued(action="process_queue", dry_run=False, **options),
            get_crm_mass_emailer_status_payload,
            queue_details=_crm_mass_emailer_queue_details(options),
            task_type="crm.mass_emailer",
            task_arguments={"action": "process_queue", "dry_run": False, **options},
        )

    @app.route("/crm/mass-emailer/dry-run", methods=["POST", "GET"])
    @app.route("/crm/mass-email/dry-run", methods=["POST", "GET"])
    def crm_mass_emailer_dry_run():
        options = _crm_mass_emailer_request_options()
        return _queue_response(
            "Sheets Scanner Dry Run",
            "Processing",
            lambda: run_crm_mass_emailer_run_queued(action="process_queue", dry_run=True, **options),
            get_crm_mass_emailer_status_payload,
            queue_details=_crm_mass_emailer_queue_details(options),
            task_type="crm.mass_emailer",
            task_arguments={"action": "process_queue", "dry_run": True, **options},
        )

    @app.route("/crm/mass-emailer/scan", methods=["POST", "GET"])
    @app.route("/crm/mass-email/scan", methods=["POST", "GET"])
    def crm_mass_emailer_scan():
        options = _crm_mass_emailer_request_options()
        return _queue_response(
            "Sheets Scanner Sheet Scan",
            "Processing",
            lambda: run_crm_mass_emailer_run_queued(action="scan_sheet", dry_run=True, **options),
            get_crm_mass_emailer_status_payload,
            queue_details=_crm_mass_emailer_queue_details(options),
            task_type="crm.mass_emailer",
            task_arguments={"action": "scan_sheet", "dry_run": True, **options},
        )

    @app.route("/crm/mass-emailer/status", methods=["GET"])
    @app.route("/crm/mass-email/status", methods=["GET"])
    def crm_mass_emailer_status():
        return jsonify(get_crm_mass_emailer_status_payload()), 200

    @app.route("/crm/mass-emailer/history/clear", methods=["POST"])
    @app.route("/crm/mass-email/history/clear", methods=["POST"])
    def crm_mass_emailer_history_clear():
        ok, msg = clear_crm_mass_emailer_history()
        payload = get_crm_mass_emailer_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/address-validator", methods=["POST", "GET"])
    def crm_address_validator():
        options = _crm_address_request_options()
        return _queue_response(
            "Address Validator",
            "Processing",
            lambda: run_crm_address_run_queued(dry_run=False, **options),
            get_crm_address_status_payload,
            task_type="crm.address_validator",
            task_arguments={"dry_run": False, **options},
        )

    @app.route("/crm/address-validator/dry-run", methods=["POST", "GET"])
    def crm_address_validator_dry_run():
        options = _crm_address_request_options()
        return _queue_response(
            "Address Validator Dry Run",
            "Processing",
            lambda: run_crm_address_run_queued(dry_run=True, **options),
            get_crm_address_status_payload,
            task_type="crm.address_validator",
            task_arguments={"dry_run": True, **options},
        )

    @app.route("/crm/address-validator/status", methods=["GET"])
    def crm_address_validator_status():
        return jsonify(get_crm_address_status_payload()), 200

    @app.route("/crm/address-validator/state", methods=["GET"])
    def crm_address_validator_state():
        return jsonify(get_crm_address_state_payload()), 200

    @app.route("/crm/address-validator/history/clear", methods=["POST"])
    def crm_address_validator_history_clear():
        ok, msg = clear_crm_address_history()
        payload = get_crm_address_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/address-validator/filter", methods=["POST"])
    def crm_address_validator_filter():
        data = request.get_json(silent=True) or {}
        ok, msg = set_crm_address_filter(
            data.get("filter") or data.get("shipping_filter") or request.args.get("filter") or request.args.get("shipping_filter")
        )
        payload = get_crm_address_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/address-validator/preferences", methods=["POST"])
    def crm_address_validator_preferences():
        data = request.get_json(silent=True) or {}
        ok, msg, _state = update_crm_address_preferences(
            batch_size=_first_present(data.get("batch_size"), data.get("batchSize")),
            parallel_workers=_first_present(data.get("parallel_workers"), data.get("parallelWorkers")),
        )
        payload = get_crm_address_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/order-goods", methods=["POST", "GET"])
    def crm_order_goods():
        options = _crm_order_goods_request_options()
        return _queue_response(
            _crm_stock_queue_label("Rush Order Goods", options),
            "Processing",
            lambda: run_crm_order_goods_run_queued(dry_run=False, **options),
            get_crm_order_goods_status_payload,
            task_type="crm.order_goods",
            task_arguments={"dry_run": False, **options},
        )

    @app.route("/crm/order-goods/dry-run", methods=["POST", "GET"])
    def crm_order_goods_dry_run():
        options = _crm_order_goods_request_options()
        return _queue_response(
            _crm_stock_queue_label("Rush Order Goods Dry Run", options),
            "Processing",
            lambda: run_crm_order_goods_run_queued(dry_run=True, **options),
            get_crm_order_goods_status_payload,
            task_type="crm.order_goods",
            task_arguments={"dry_run": True, **options},
        )

    @app.route("/crm/order-goods/status", methods=["GET"])
    def crm_order_goods_status():
        return jsonify(get_crm_order_goods_status_payload()), 200

    @app.route("/crm/shipping-bypasser", methods=["POST", "GET"])
    @app.route("/crm/shipping-bypass", methods=["POST", "GET"])
    def crm_shipping_bypasser():
        options = _crm_shipping_bypasser_request_options()
        return _queue_response(
            _crm_stock_queue_label("Shipping Bypasser", options),
            "Processing",
            lambda: run_crm_shipping_bypasser_run_queued(dry_run=False, **options),
            get_crm_shipping_bypasser_status_payload,
            task_type="crm.shipping_bypasser",
            task_arguments={"dry_run": False, **options},
        )

    @app.route("/crm/shipping-bypasser/dry-run", methods=["POST", "GET"])
    @app.route("/crm/shipping-bypass/dry-run", methods=["POST", "GET"])
    def crm_shipping_bypasser_dry_run():
        options = _crm_shipping_bypasser_request_options()
        return _queue_response(
            _crm_stock_queue_label("Shipping Bypasser Dry Run", options),
            "Processing",
            lambda: run_crm_shipping_bypasser_run_queued(dry_run=True, **options),
            get_crm_shipping_bypasser_status_payload,
            task_type="crm.shipping_bypasser",
            task_arguments={"dry_run": True, **options},
        )

    @app.route("/crm/shipping-bypasser/status", methods=["GET"])
    @app.route("/crm/shipping-bypass/status", methods=["GET"])
    def crm_shipping_bypasser_status():
        return jsonify(get_crm_shipping_bypasser_status_payload()), 200

    @app.route("/crm/push-back", methods=["POST", "GET"])
    def crm_push_back():
        options = _crm_push_back_request_options()
        return _queue_response(
            _crm_stock_queue_label("Push Back", options),
            "Processing",
            lambda: run_crm_push_back_run_queued(dry_run=False, **options),
            get_crm_push_back_status_payload,
            task_type="crm.push_back",
            task_arguments={"dry_run": False, **options},
        )

    @app.route("/crm/push-back/dry-run", methods=["POST", "GET"])
    def crm_push_back_dry_run():
        options = _crm_push_back_request_options()
        return _queue_response(
            _crm_stock_queue_label("Push Back Dry Run", options),
            "Processing",
            lambda: run_crm_push_back_run_queued(dry_run=True, **options),
            get_crm_push_back_status_payload,
            task_type="crm.push_back",
            task_arguments={"dry_run": True, **options},
        )

    @app.route("/crm/push-back/status", methods=["GET"])
    def crm_push_back_status():
        return jsonify(get_crm_push_back_status_payload()), 200

    @app.route("/crm/shipping-bypasser/sanmar-cart/open", methods=["POST", "GET"])
    @app.route("/crm/shipping-bypass/sanmar-cart/open", methods=["POST", "GET"])
    def crm_shipping_bypasser_open_sanmar_cart():
        ok, msg, details = open_sanmar_cart_browser()
        payload = get_crm_shipping_bypasser_status_payload()
        payload.update({"success": ok, "message": msg, "sanmar_cart": details})
        return jsonify(payload), (200 if ok else 500)

    @app.route("/crm/order-goods/preferences", methods=["POST"])
    def crm_order_goods_preferences():
        data = request.get_json(silent=True) or {}
        ok, msg, _state = update_crm_order_goods_preferences(
            parallel_workers=_first_present(data.get("parallel_workers"), data.get("parallelWorkers")),
        )
        payload = get_crm_order_goods_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/product-separator", methods=["POST", "GET"])
    @app.route("/crm/product-seperator", methods=["POST", "GET"])
    @app.route("/crm/separator", methods=["POST", "GET"])
    @app.route("/crm/seperator", methods=["POST", "GET"])
    def crm_product_separator():
        options = _crm_product_separator_request_options()
        return _queue_response(
            _crm_stock_queue_label("Product Separator", options),
            "Processing",
            lambda: run_crm_product_separator_run_queued(dry_run=False, **options),
            get_crm_product_separator_status_payload,
            task_type="crm.product_separator",
            task_arguments={"dry_run": False, **options},
        )

    @app.route("/crm/product-separator/dry-run", methods=["POST", "GET"])
    @app.route("/crm/product-seperator/dry-run", methods=["POST", "GET"])
    @app.route("/crm/separator/dry-run", methods=["POST", "GET"])
    @app.route("/crm/seperator/dry-run", methods=["POST", "GET"])
    def crm_product_separator_dry_run():
        options = _crm_product_separator_request_options()
        return _queue_response(
            _crm_stock_queue_label("Product Separator Dry Run", options),
            "Processing",
            lambda: run_crm_product_separator_run_queued(dry_run=True, **options),
            get_crm_product_separator_status_payload,
            task_type="crm.product_separator",
            task_arguments={"dry_run": True, **options},
        )

    @app.route("/crm/product-separator/status", methods=["GET"])
    @app.route("/crm/product-seperator/status", methods=["GET"])
    @app.route("/crm/separator/status", methods=["GET"])
    @app.route("/crm/seperator/status", methods=["GET"])
    def crm_product_separator_status():
        return jsonify(get_crm_product_separator_status_payload()), 200

    @app.route("/crm/auto-splitter", methods=["POST", "GET"])
    def crm_auto_splitter():
        options = _crm_auto_splitter_request_options()
        return _queue_response(
            _crm_auto_splitter_queue_label(options, dry_run=False),
            "Processing",
            lambda: run_crm_auto_splitter_run_queued(dry_run=False, **options),
            get_crm_auto_splitter_status_payload,
            queue_details=_crm_auto_splitter_queue_details(options),
            task_type="crm.auto_splitter",
            task_arguments={"dry_run": False, **options},
        )

    @app.route("/crm/auto-splitter/dry-run", methods=["POST", "GET"])
    def crm_auto_splitter_dry_run():
        options = _crm_auto_splitter_request_options()
        return _queue_response(
            _crm_auto_splitter_queue_label(options, dry_run=True),
            "Processing",
            lambda: run_crm_auto_splitter_run_queued(dry_run=True, **options),
            get_crm_auto_splitter_status_payload,
            queue_details=_crm_auto_splitter_queue_details(options),
            task_type="crm.auto_splitter",
            task_arguments={"dry_run": True, **options},
        )

    @app.route("/crm/auto-splitter/status", methods=["GET"])
    def crm_auto_splitter_status():
        return jsonify(get_crm_auto_splitter_status_payload()), 200

    @app.route("/crm/auto-splitter/history/clear", methods=["POST"])
    def crm_auto_splitter_history_clear():
        ok, msg = clear_crm_auto_splitter_history()
        payload = get_crm_auto_splitter_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200
