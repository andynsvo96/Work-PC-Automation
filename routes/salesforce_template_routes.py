"""Local email template editing; app authentication and CSRF still apply."""

from flask import jsonify, request

from salesforce_templates import TemplateConflict, TemplateLibraryError, TemplateNotFound


def register_salesforce_template_routes(app, *, store):
    @app.route("/api/salesforce-templates", methods=["GET"])
    def api_salesforce_templates():
        try:
            return jsonify({"success": True, "templates": store.list_templates()})
        except (TemplateLibraryError, OSError):
            return jsonify({"success": False, "message": "The local template library could not be loaded. Check its JSON file or backup."}), 500

    @app.route("/api/salesforce-templates/<template_id>", methods=["POST"])
    def api_salesforce_template_save(template_id):
        if request.content_length is not None and request.content_length > 8 * 1024 * 1024:
            return jsonify({"success": False, "message": "Template request is too large."}), 413
        try:
            template = store.save_template(template_id, request.get_json(silent=True))
            return jsonify({"success": True, "message": "Template saved on this computer.", "template": template})
        except TemplateConflict as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        except TemplateNotFound as exc:
            return jsonify({"success": False, "message": str(exc)}), 404
        except TemplateLibraryError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        except OSError:
            return jsonify({"success": False, "message": "Template could not be saved. Your draft is still open."}), 500
