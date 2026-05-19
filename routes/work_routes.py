"""
Work-domain route registration for the automation server.
"""

from flask import jsonify, request


def register_work_routes(
    app,
    *,
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
    get_crm_status_payload,
    get_crm_state_payload,
    clear_crm_history,
    start_crm_address_run,
    get_crm_address_status_payload,
    get_crm_address_state_payload,
    clear_crm_address_history,
    set_crm_address_filter,
    update_crm_address_preferences,
    start_crm_order_goods_run,
    get_crm_order_goods_status_payload,
    update_crm_order_goods_preferences,
    start_crm_auto_splitter_run,
    get_crm_auto_splitter_status_payload,
    clear_crm_auto_splitter_history,
    start_crm_processing_run,
    get_crm_processing_status_payload,
    get_crm_processing_state_payload,
    update_crm_processing_preferences,
):
    def _first_present(*values):
        for value in values:
            if value is not None:
                return value
        return None

    @app.route("/clock/in", methods=["POST", "GET"])
    def clock_in():
        ok, msg = run_clock("in", dry_run=False)
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/clock/out", methods=["POST", "GET"])
    def clock_out():
        ok, msg = run_clock("out", dry_run=False)
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/clock/test/in", methods=["POST", "GET"])
    def clock_test_in():
        ok, msg = run_clock("in", dry_run=True)
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/clock/test/out", methods=["POST", "GET"])
    def clock_test_out():
        ok, msg = run_clock("out", dry_run=True)
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

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
        ok, msg, results = run_automation_test_suite(selected)
        return jsonify({"success": ok, "message": msg, "results": results}), 200

    @app.route("/slack/in", methods=["POST", "GET"])
    def slack_in():
        ok, msg = run_slack("in")
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/slack/out", methods=["POST", "GET"])
    def slack_out():
        ok, msg = run_slack("out")
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/slack/lunch", methods=["POST", "GET"])
    def slack_lunch():
        use_test_url = False
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            use_test_url = is_trueish(data.get("test_url"))
        else:
            use_test_url = is_trueish(request.args.get("test_url"))
        ok, msg = start_slack_lunch_break(force_test_url=use_test_url)
        payload = get_slack_lunch_payload()
        return jsonify({"success": ok, "message": msg, "lunch": payload}), (200 if ok else 500)

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
        ok, msg = run_work("in", automatic=False)
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/work/out", methods=["POST", "GET"])
    def work_out():
        ok, msg = run_work("out", automatic=False)
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

    @app.route("/work/sync", methods=["POST", "GET"])
    def work_sync():
        ok, msg = run_work_sync()
        return jsonify({"success": ok, "message": msg}), (200 if ok else 500)

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
        ok, msg = start_crm_run(dry_run=False)
        payload = get_crm_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

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
            "processing_filter": _first_present(
                data.get("processing_filter"),
                data.get("processingFilter"),
                data.get("filter"),
                request.args.get("processing_filter"),
                request.args.get("processingFilter"),
                request.args.get("filter"),
            ),
        }

    @app.route("/crm/process", methods=["POST", "GET"])
    def crm_process():
        ok, msg = start_crm_processing_run(**_crm_processing_request_options())
        payload = get_crm_processing_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

    def _crm_processing_mode_options(processing_filter):
        options = _crm_processing_request_options()
        options["processing_filter"] = processing_filter
        if processing_filter == "rush":
            options["stock_unlocker_enabled"] = True
            options["address_validator_enabled"] = True
            options["order_goods_enabled"] = True
        else:
            options["stock_unlocker_enabled"] = False
            options["address_validator_enabled"] = True
            options["order_goods_enabled"] = False
        return options

    def _start_crm_processing_mode(processing_filter):
        ok, msg = start_crm_processing_run(**_crm_processing_mode_options(processing_filter))
        payload = get_crm_processing_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

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

    @app.route("/crm/process/status", methods=["GET"])
    def crm_process_status():
        return jsonify(get_crm_processing_status_payload()), 200

    @app.route("/crm/process/state", methods=["GET"])
    def crm_process_state():
        return jsonify(get_crm_processing_state_payload()), 200

    @app.route("/crm/process/preferences", methods=["POST"])
    def crm_process_preferences():
        options = _crm_processing_request_options()
        ok, msg, _state = update_crm_processing_preferences(**options)
        payload = get_crm_processing_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/unlock/dry-run", methods=["POST", "GET"])
    def crm_unlock_dry_run():
        ok, msg = start_crm_run(dry_run=True)
        payload = get_crm_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

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

    @app.route("/crm/address-validator", methods=["POST", "GET"])
    def crm_address_validator():
        ok, msg = start_crm_address_run(dry_run=False, **_crm_address_request_options())
        payload = get_crm_address_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

    @app.route("/crm/address-validator/dry-run", methods=["POST", "GET"])
    def crm_address_validator_dry_run():
        ok, msg = start_crm_address_run(dry_run=True, **_crm_address_request_options())
        payload = get_crm_address_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

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
        ok, msg = start_crm_order_goods_run(dry_run=False, **_crm_order_goods_request_options())
        payload = get_crm_order_goods_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

    @app.route("/crm/order-goods/dry-run", methods=["POST", "GET"])
    def crm_order_goods_dry_run():
        ok, msg = start_crm_order_goods_run(dry_run=True, **_crm_order_goods_request_options())
        payload = get_crm_order_goods_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

    @app.route("/crm/order-goods/status", methods=["GET"])
    def crm_order_goods_status():
        return jsonify(get_crm_order_goods_status_payload()), 200

    @app.route("/crm/order-goods/preferences", methods=["POST"])
    def crm_order_goods_preferences():
        data = request.get_json(silent=True) or {}
        ok, msg, _state = update_crm_order_goods_preferences(
            parallel_workers=_first_present(data.get("parallel_workers"), data.get("parallelWorkers")),
        )
        payload = get_crm_order_goods_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200

    @app.route("/crm/auto-splitter", methods=["POST", "GET"])
    def crm_auto_splitter():
        options = _crm_auto_splitter_request_options()
        ok, msg = start_crm_auto_splitter_run(dry_run=False, **options)
        payload = get_crm_auto_splitter_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

    @app.route("/crm/auto-splitter/dry-run", methods=["POST", "GET"])
    def crm_auto_splitter_dry_run():
        options = _crm_auto_splitter_request_options()
        ok, msg = start_crm_auto_splitter_run(dry_run=True, **options)
        payload = get_crm_auto_splitter_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), (202 if ok else 409)

    @app.route("/crm/auto-splitter/status", methods=["GET"])
    def crm_auto_splitter_status():
        return jsonify(get_crm_auto_splitter_status_payload()), 200

    @app.route("/crm/auto-splitter/history/clear", methods=["POST"])
    def crm_auto_splitter_history_clear():
        ok, msg = clear_crm_auto_splitter_history()
        payload = get_crm_auto_splitter_status_payload()
        payload.update({"success": ok, "message": msg})
        return jsonify(payload), 200
