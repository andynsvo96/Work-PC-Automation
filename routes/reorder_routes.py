"""Reorder is available only from explicit Custom Processing order inputs."""

from functools import partial

from flask import jsonify, request

from crm_list_url import normalize_custom_crm_order_ids


def register_reorder_routes(app, *, enqueue_automation, run_reorder, get_status):
    @app.post("/crm/reorder")
    def reorder_stock():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get("processing_filter") != "custom" or data.get("custom_input_type") != "orders" or "custom_list_url" in data:
            return jsonify(success=False, message="Reorder requires Custom Processing with Order IDs or order links."), 400
        try:
            order_ids = normalize_custom_crm_order_ids(data.get("custom_order_ids"))
        except ValueError as exc:
            return jsonify(success=False, message=str(exc)), 400
        tasks = []
        for order_id in order_ids:
            arguments = {"order_id": order_id}
            ok, message, task = enqueue_automation(
                f"Reorder Stock Order {order_id}", "Processing", partial(run_reorder, **arguments),
                details=f"Replace red vendor stock; customer PO {order_id}.",
                status_fn=get_status, task_type="crm.reorder", task_arguments=arguments,
                automation_signature={"type": "crm.reorder", "order_id": order_id},
            )
            if not ok:
                return jsonify(success=False, message=message, tasks=tasks), 409
            tasks.append(task)
        return jsonify(success=True, message=f"Queued Reorder Stock for {len(tasks)} order(s).", tasks=tasks)

    app.add_url_rule("/crm/reorder/status", "reorder_status", lambda: jsonify(get_status()), methods=["GET"])
