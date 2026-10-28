"""Machine-local editable Salesforce email templates, separate from captures."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
import threading


MAX_BODY_BYTES = 1_000_000
MAX_LIBRARY_BYTES = 32 * 1024 * 1024
EDITABLE_FIELDS = ("name", "subject", "body_html")


class TemplateLibraryError(ValueError):
    pass


class TemplateConflict(TemplateLibraryError):
    pass


class TemplateNotFound(TemplateLibraryError):
    pass


def _revision(template):
    values = {key: template[key] for key in ("id", *EDITABLE_FIELDS)}
    return sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _validate_fields(values):
    if not isinstance(values, dict):
        raise TemplateLibraryError("Template must be an object.")
    result = {}
    for key, label, limit in (("name", "Template name", 200), ("subject", "Title", 500)):
        value = values.get(key)
        if not isinstance(value, str) or not value.strip():
            raise TemplateLibraryError(f"{label} is required.")
        if len(value) > limit or any(ord(char) < 32 for char in value):
            raise TemplateLibraryError(f"{label} must be one line, with at most {limit} characters.")
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise TemplateLibraryError(f"{label} contains invalid Unicode.") from exc
        result[key] = value
    body = values.get("body_html")
    if not isinstance(body, str) or not body.strip():
        raise TemplateLibraryError("Email body is required.")
    try:
        body_size = len(body.encode("utf-8"))
    except UnicodeError as exc:
        raise TemplateLibraryError("Email body contains invalid Unicode.") from exc
    if body_size > MAX_BODY_BYTES:
        raise TemplateLibraryError("Email body must be smaller than 1 MB.")
    result["body_html"] = body
    return result


class SalesforceTemplateStore:
    def __init__(self, path, capture_root=None):
        self.path = Path(path)
        self.capture_root = Path(capture_root) if capture_root is not None else None
        self._lock = threading.RLock()

    @staticmethod
    def _read_json(path):
        try:
            if path.stat().st_size > MAX_LIBRARY_BYTES:
                raise TemplateLibraryError("The local template library is too large.")
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise TemplateLibraryError("The local template library contains invalid JSON. Restore it before editing.") from exc

    def _write(self, document, *, backup=False):
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        if len(content.encode("utf-8")) > MAX_LIBRARY_BYTES:
            raise TemplateLibraryError("The local template library would exceed 32 MB.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=self.path.parent, prefix=self.path.name + ".",
                                             suffix=".tmp", delete=False) as stream:
                pending = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if backup and self.path.exists():
                backup_path = self.path.with_suffix(self.path.suffix + ".bak")
                # Retain a complete previous version without moving the active file.
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=backup_path.name + ".",
                                                 suffix=".tmp", delete=False) as stream:
                    backup_pending = Path(stream.name)
                    try:
                        stream.write(self.path.read_bytes())
                        stream.flush()
                        os.fsync(stream.fileno())
                    except BaseException:
                        stream.close()
                        backup_pending.unlink(missing_ok=True)
                        raise
                try:
                    os.replace(backup_pending, backup_path)
                finally:
                    backup_pending.unlink(missing_ok=True)
            os.replace(pending, self.path)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)

    def _initialize(self):
        if self.path.exists() or self.capture_root is None:
            return
        captures = sorted(self.capture_root.glob("*/email_templates.json"), reverse=True)
        if not captures:
            return
        snapshot = self._read_json(captures[0])
        if not isinstance(snapshot, dict) or not snapshot:
            raise TemplateLibraryError("The captured template library is invalid.")
        templates = []
        for name, entry in snapshot.items():
            if not isinstance(entry, dict) or entry.get("name", name) != name:
                raise TemplateLibraryError("The captured template names do not match their records.")
            if not name.startswith("[AUTO]"):
                continue
            fields = _validate_fields({**entry, "name": name})
            template_id = entry.get("salesforce_id") or "local_" + sha256(name.encode()).hexdigest()[:24]
            templates.append({"id": template_id, **fields})
        document = {"schema_version": 1, "templates": templates}
        self._validate_document(document)
        self._write(document)

    @staticmethod
    def _validate_document(document):
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise TemplateLibraryError("The local template library has an unsupported format.")
        templates = document.get("templates")
        if not isinstance(templates, list) or len(templates) > 500:
            raise TemplateLibraryError("The local template library must contain a list of up to 500 templates.")
        ids, names = set(), set()
        for template in templates:
            if not isinstance(template, dict):
                raise TemplateLibraryError("A local template record is invalid.")
            template_id = template.get("id")
            if not isinstance(template_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", template_id):
                raise TemplateLibraryError("A local template ID is invalid.")
            fields = _validate_fields(template)
            name_key = fields["name"].strip().casefold()
            if template_id in ids or name_key in names:
                raise TemplateLibraryError("Template IDs and names must be unique.")
            ids.add(template_id)
            names.add(name_key)
        return document

    def _load(self):
        self._initialize()
        if not self.path.exists():
            return {"schema_version": 1, "templates": []}
        return self._validate_document(self._read_json(self.path))

    @staticmethod
    def _public(template):
        return {key: template[key] for key in ("id", *EDITABLE_FIELDS)} | {"revision": _revision(template)}

    def list_templates(self):
        with self._lock:
            document = self._load()
            return [self._public(template) for template in sorted(
                document["templates"], key=lambda item: item["name"].casefold()
            )]

    def save_template(self, template_id, values):
        fields = _validate_fields(values)
        revision = values.get("revision")
        if not isinstance(revision, str) or not revision:
            raise TemplateLibraryError("Reload the template before saving; its revision is missing.")
        with self._lock:
            document = self._load()
            template = next((item for item in document["templates"] if item["id"] == template_id), None)
            if template is None:
                raise TemplateNotFound("Template was not found. Reload the template list.")
            if revision != _revision(template):
                raise TemplateConflict("This template changed since you opened it. Reload templates to review the latest version.")
            if any(item["id"] != template_id and item["name"].strip().casefold() == fields["name"].strip().casefold()
                   for item in document["templates"]):
                raise TemplateLibraryError("Another template already uses that name.")
            if any(template[key] != fields[key] for key in EDITABLE_FIELDS):
                template.update(fields)
                template["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._write(document, backup=True)
            return self._public(template)
