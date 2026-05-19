"""
System-domain route registration for the automation server.
"""

import threading
import time

from flask import jsonify, request


def register_system_routes(
    app,
    *,
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
):
    @app.route("/api/metrics", methods=["GET"])
    def api_metrics():
        payload = read_desktop_metrics()
        return jsonify(payload), (200 if payload.get("available") else 500)

    @app.route("/pc/shutdown", methods=["POST", "GET"])
    def pc_shutdown():
        try:
            cancel_power_countdown(audit=False)
            threading.Thread(target=lambda: (time.sleep(1), trigger_pc_shutdown()), daemon=True).start()
            return jsonify({"success": True, "message": "PC is shutting down."}), 200
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/sleep", methods=["POST", "GET"])
    def pc_sleep():
        try:
            cancel_power_countdown(audit=False)
            threading.Thread(target=lambda: (time.sleep(1), trigger_pc_sleep()), daemon=True).start()
            return jsonify({"success": True, "message": "PC is going to sleep."}), 200
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/restart", methods=["POST", "GET"])
    def pc_restart():
        try:
            cancel_power_countdown(audit=False)
            threading.Thread(target=lambda: (time.sleep(1), trigger_pc_restart()), daemon=True).start()
            return jsonify({"success": True, "message": "PC is restarting."}), 200
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/restart-explorer", methods=["POST", "GET"])
    def pc_restart_explorer():
        try:
            ok, msg = trigger_pc_restart_explorer()
            return jsonify({"success": ok, "message": msg}), (200 if ok else 500)
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/schedule", methods=["POST", "GET"])
    def pc_schedule():
        try:
            if request.method == "POST":
                data = request.get_json(silent=True) or {}
            else:
                data = {}
            action = data.get("action") if request.method == "POST" else request.args.get("action")
            raw_seconds = data.get("delay_seconds") if request.method == "POST" else request.args.get("delay_seconds")
            raw_minutes = data.get("delay_minutes") if request.method == "POST" else request.args.get("delay_minutes")
            raw_execute_at = data.get("execute_at") if request.method == "POST" else request.args.get("execute_at")
            raw_schedule_date = data.get("schedule_date") if request.method == "POST" else request.args.get("schedule_date")
            raw_schedule_time = data.get("schedule_time") if request.method == "POST" else request.args.get("schedule_time")

            if raw_seconds in (None, ""):
                if raw_minutes not in (None, ""):
                    raw_seconds = safe_float(raw_minutes, -1) * 60.0
                    ok, msg = schedule_power_countdown(action, raw_seconds)
                else:
                    execute_at, dt_err = resolve_power_schedule_datetime(
                        raw_execute_at=raw_execute_at,
                        raw_date=raw_schedule_date,
                        raw_time=raw_schedule_time,
                    )
                    if dt_err:
                        return jsonify({"success": False, "message": dt_err}), 400
                    ok, msg = schedule_power_at_datetime(action, execute_at)
            else:
                ok, msg = schedule_power_countdown(action, raw_seconds)
            payload = get_power_countdown_payload()
            return jsonify({"success": ok, "message": msg, "countdown": payload}), (200 if ok else 400)
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/cancel-schedule", methods=["POST", "GET"])
    def pc_cancel_schedule():
        try:
            ok, msg = cancel_power_countdown(audit=True)
            payload = get_power_countdown_payload()
            return jsonify({"success": ok, "message": msg, "countdown": payload}), 200
        except Exception as e:
            return jsonify({"success": False, "message": str(e)}), 500

    @app.route("/pc/status", methods=["GET"])
    def pc_status():
        payload = get_power_countdown_payload()
        return jsonify(payload), 200
