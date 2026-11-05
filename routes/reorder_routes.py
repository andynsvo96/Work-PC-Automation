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
        arguments = {"order_ids": order_ids}
        label = f"Reorder Stock Order {order_ids[0]}" if len(order_ids) == 1 else f"Reorder Stock — {len(order_ids)} orders"
        ok, message, task = enqueue_automation(
            label, "Processing", partial(run_reorder, **arguments),
            details="Replace red vendor stock; each customer PO uses its CRM order number.",
            status_fn=get_status, task_type="crm.reorder_batch", task_arguments=arguments,
            automation_signature={"type": "crm.reorder_batch", "order_ids": order_ids},
        )
        if not ok:
            return jsonify(success=False, message=message), 409
        return jsonify(success=True, message=f"Queued one Reorder Stock batch for {len(order_ids)} order(s).", tasks=[task])

    app.add_url_rule("/crm/reorder/status", "reorder_status", lambda: jsonify(get_status()), methods=["GET"])
