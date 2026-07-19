"""Cross-device clipboard route registration."""

from flask import jsonify, request

from clipboard_runtime import ClipboardError, ClipboardItem


MAX_PEER_REQUEST_BYTES = 12 * 1024 * 1024


def register_connectivity_routes(
    app,
    *,
    clipboard_runtime,
    authenticate_peer_request,
):
    def _peer_authenticated():
        if request.content_length and request.content_length > MAX_PEER_REQUEST_BYTES:
            raise ClipboardError("Clipboard request exceeds the 12 MB transport limit.")
        body = request.get_data(cache=True) or b""
        if len(body) > MAX_PEER_REQUEST_BYTES:
            raise ClipboardError("Clipboard request exceeds the 12 MB transport limit.")
        authenticate_peer_request(request.method, request.path, body, request.headers)

    @app.route("/api/clipboard/status", methods=["GET"])
    def api_clipboard_status():
        return jsonify(clipboard_runtime.state())

    @app.route("/api/clipboard/toggle", methods=["POST"])
    def api_clipboard_toggle():
        data = request.get_json(silent=True) or {}
        try:
            state = clipboard_runtime.set_enabled(bool(data.get("enabled")))
            state["message"] = (
                "Automatic clipboard sync enabled."
                if state["enabled"]
                else "Automatic clipboard sync disabled."
            )
            return jsonify(state)
        except ClipboardError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400

    @app.route("/api/clipboard/send", methods=["POST"])
    def api_clipboard_send():
        try:
            item = clipboard_runtime.manual_send()
            return jsonify({
                "success": True,
                "message": f"{item.kind.title()} clipboard sent to the other computer.",
                "kind": item.kind,
            })
        except ClipboardError as exc:
            return jsonify({"success": False, "message": str(exc)}), 503
        except Exception:
            return jsonify({"success": False, "message": "Local clipboard could not be read."}), 500

    @app.route("/api/clipboard/pull", methods=["POST"])
    def api_clipboard_pull():
        try:
            item = clipboard_runtime.manual_pull()
            return jsonify({
                "success": True,
                "message": f"{item.kind.title()} clipboard received from the other computer.",
                "kind": item.kind,
            })
        except ClipboardError as exc:
            return jsonify({"success": False, "message": str(exc)}), 503
        except Exception:
            return jsonify({"success": False, "message": "Clipboard transfer could not be completed."}), 500

    @app.route("/api/clipboard/peer/status", methods=["GET"])
    def api_clipboard_peer_status():
        try:
            _peer_authenticated()
            state = clipboard_runtime.state()
            return jsonify({
                "success": True,
                "available": state.get("available"),
                "enabled": state.get("enabled"),
                "protocol_version": 1,
            })
        except ClipboardError as exc:
            return jsonify({"success": False, "message": str(exc)}), 401

    @app.route("/api/clipboard/peer/read", methods=["GET"])
    def api_clipboard_peer_read():
        try:
            _peer_authenticated()
            item = clipboard_runtime.read_for_peer()
            return jsonify({"success": True, "item": item.to_payload()})
        except ClipboardError as exc:
            return jsonify({"success": False, "message": str(exc)}), 503
        except Exception:
            return jsonify({"success": False, "message": "Peer clipboard could not be read."}), 500

    @app.route("/api/clipboard/peer/receive", methods=["POST"])
    def api_clipboard_peer_receive():
        try:
            _peer_authenticated()
            data = request.get_json(silent=True) or {}
            item = ClipboardItem.from_payload(data.get("item") or {})
            clipboard_runtime.apply_remote(item, automatic=bool(data.get("automatic")))
            return jsonify({"success": True, "message": "Clipboard applied.", "kind": item.kind})
        except ClipboardError as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        except Exception:
            return jsonify({"success": False, "message": "Peer clipboard could not be applied."}), 500
