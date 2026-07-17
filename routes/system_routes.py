"""
System-domain route registration for the automation server.
"""

import time
from datetime import datetime, timedelta

from flask import jsonify, request


def register_system_routes(
    app,
    *,
    enqueue_automation,
    read_desktop_metrics,
    get_power_countdown_payload,
    cancel_power_countdown,
    trigger_pc_shutdown,
    trigger_pc_sleep,
    trigger_pc_restart,
    trigger_pc_restart_explorer,
    schedule_power_countdown,
    schedule_power_at_datetime,
    resolve_power_schedule_datetime,
    safe_float,
    platform_capabilities,
    get_shared_node_status,
    cancel_scheduled_power_tasks,
):
    def _requested_target_node():
        return str(
            request.headers.get("X-Automation-Target-Node")
            or request.args.get("target_node")
            or ""
        ).strip()

    def _requested_target_record():
        node_key = _requested_target_node()
        return get_shared_node_status(node_key) if node_key else None

    def _capability_available(name):
        try:
            target = _requested_target_record()
            if target is not None:
                return bool((target.get("capabilities") or {}).get(name))
            capabilities = platform_capabilities()
            return bool(capabilities.get(name))
        except Exception:
            return False

    def _windows_only_response():
        return jsonify({"success": False, "available": False, "message": "Windows only"}), 400

    def _power_route_label(action):
        key = str(action or "").strip().lower()
        return {"shutdown": "Shutdown", "sleep": "Sleep", "restart": "Restart"}.get(key, "Power")

    def _queue_response(label, fn, *, task_type="system.power", task_arguments=None, queue_options=None):
        action_by_label = {
            "Shutdown PC": "shutdown",
            "Sleep PC": "sleep",
            "Restart PC": "restart",
            "Restart Explorer": "restart_explorer",
        }
        action = action_by_label.get(label)
        requested_target = _requested_target_node()
        ok, msg, task = enqueue_automation(
            label,
            "System Power",
            fn,
            task_type=task_type,
            task_arguments=task_arguments if isinstance(task_arguments, dict) else ({"action": action} if action else {}),
            target_node=requested_target if requested_target and requested_target.lower() != "any" else None,
            required_capability="system_power",
            **(queue_options if isinstance(queue_options, dict) else {}),
        )
        payload = get_power_countdown_payload()
        return jsonify({"success": ok, "message": msg, "queued": ok, "queue_task": task, "countdown": payload}), (202 if ok else 500)

    @app.route("/api/metrics", methods=["GET"])
    def api_metrics():
        target = _requested_target_record()
        if target is not None:
            if not target.get("online"):
                return jsonify({"success": False, "available": False, "message": "Selected computer is offline."}), 503
            if not bool((target.get("capabilities") or {}).get("metrics")):
                return _windows_only_response()
            payload = (target.get("runtime_status") or {}).get("metrics") or {
                "success": False,
                "available": False,
                "message": "Metrics have not been reported by that computer yet.",
            }
            return jsonify(payload), (200 if payload.get("available") else 503)
        if not _capability_available("metrics"):
            return _windows_only_response()
        payload = read_desktop_metrics()
        return jsonify(payload), (200 if payload.get("available") else 500)

    @app.route("/pc/shutdown", methods=["POST", "GET"])
    def pc_shutdown():
        if not _capability_available("system_power"):
            return _windows_only_response()
        return _queue_response("Shutdown PC", lambda: (cancel_power_countdown(audit=False), time.sleep(1), trigger_pc_shutdown())[-1])

    @app.route("/pc/sleep", methods=["POST", "GET"])
    def pc_sleep():
        if not _capability_available("system_power"):
            return _windows_only_response()
        return _queue_response("Sleep PC", lambda: (cancel_power_countdown(audit=False), time.sleep(1), trigger_pc_sleep())[-1])

    @app.route("/pc/restart", methods=["POST", "GET"])
    def pc_restart():
        if not _capability_available("system_power"):
            return _windows_only_response()
        return _queue_response("Restart PC", lambda: (cancel_power_countdown(audit=False), time.sleep(1), trigger_pc_restart())[-1])

    @app.route("/pc/restart-explorer", methods=["POST", "GET"])
    def pc_restart_explorer():
        if not _capability_available("restart_explorer"):
            return _windows_only_response()
        return _queue_response("Restart Explorer", trigger_pc_restart_explorer)

    @app.route("/pc/schedule", methods=["POST", "GET"])
    def pc_schedule():
        if not _capability_available("system_power"):
            return _windows_only_response()
        try:
            if request.method == "POST":
                data = request.get_json(silent=True) or {}
            else:
                data = {}
            action = data.get("action") if request.method == "POST" else request.args.get("action")
            action = str(action or "").strip().lower()
            if action not in {"shutdown", "sleep", "restart"}:
                return jsonify({"success": False, "message": "Invalid action. Use shutdown, sleep, or restart."}), 400
            raw_seconds = data.get("delay_seconds") if request.method == "POST" else request.args.get("delay_seconds")
            raw_minutes = data.get("delay_minutes") if request.method == "POST" else request.args.get("delay_minutes")
            raw_execute_at = data.get("execute_at") if request.method == "POST" else request.args.get("execute_at")
            raw_schedule_date = data.get("schedule_date") if request.method == "POST" else request.args.get("schedule_date")
            raw_schedule_time = data.get("schedule_time") if request.method == "POST" else request.args.get("schedule_time")

            if raw_seconds in (None, ""):
                if raw_minutes not in (None, ""):
                    raw_seconds = safe_float(raw_minutes, -1) * 60.0
                    if raw_seconds < 1 or raw_seconds > 7 * 24 * 3600:
                        return jsonify({"success": False, "message": "Countdown must be between 1 second and 7 days."}), 400
                    execute_at, dt_err = resolve_power_schedule_datetime(
                        raw_execute_at=(datetime.now() + timedelta(seconds=raw_seconds)).isoformat()
                    )
                else:
                    execute_at, dt_err = resolve_power_schedule_datetime(
                        raw_execute_at=raw_execute_at,
                        raw_date=raw_schedule_date,
                        raw_time=raw_schedule_time,
                    )
            else:
                delay_seconds = safe_float(raw_seconds, -1)
                if delay_seconds < 1 or delay_seconds > 7 * 24 * 3600:
                    return jsonify({"success": False, "message": "Countdown must be between 1 second and 7 days."}), 400
                execute_at, dt_err = resolve_power_schedule_datetime(
                    raw_execute_at=(datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
                )
            if dt_err:
                return jsonify({"success": False, "message": dt_err}), 400
            if execute_at <= datetime.now():
                return jsonify({"success": False, "message": "Scheduled time must be in the future."}), 400
            return _queue_response(
                f"Scheduled {_power_route_label(action)}",
                lambda: {
                    "shutdown": trigger_pc_shutdown,
                    "sleep": trigger_pc_sleep,
                    "restart": trigger_pc_restart,
                }.get(str(action or "").strip().lower(), lambda: (False, "Unsupported power action."))(),
                task_arguments={"action": str(action or "").strip().lower()},
                queue_options={"queue_mode": "scheduled", "scheduled_for": execute_at.isoformat()},
            )
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/cancel-schedule", methods=["POST", "GET"])
    def pc_cancel_schedule():
        if not _capability_available("system_power"):
            return _windows_only_response()
        try:
            ok, msg = cancel_scheduled_power_tasks()
            payload = get_power_countdown_payload()
            return jsonify({"success": ok, "message": msg, "countdown": payload}), 200
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/status", methods=["GET"])
    def pc_status():
        target = _requested_target_record()
        if target is not None:
            if not target.get("online"):
                return jsonify({"success": False, "available": False, "message": "Selected computer is offline."}), 503
            if not bool((target.get("capabilities") or {}).get("system_power")):
                return _windows_only_response()
            payload = (target.get("runtime_status") or {}).get("power") or {
                "success": True,
                "active": False,
                "status_text": "No active local countdown. Check the global queue for scheduled power tasks.",
            }
            return jsonify(payload), 200
        if not _capability_available("system_power"):
            return _windows_only_response()
        payload = get_power_countdown_payload()
        return jsonify(payload), 200
