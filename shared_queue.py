"""Durable cross-device automation queue primitives.

The server keeps automation execution local, but coordination lives in Supabase.
This module intentionally has no dependency on the Supabase Python SDK: PostgREST
and Auth are called with the standard library so Mac and Windows install the same
small dependency set.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from cryptography.fernet import Fernet, InvalidToken

from credential_store import SHARED_QUEUE_CREDENTIAL_TARGET, read_credential
from version_state import QUEUE_PROTOCOL_VERSION


ACTIVE_QUEUE_STATUSES = frozenset({"queued", "running", "idle", "waiting"})
FINISHED_QUEUE_STATUSES = frozenset({"completed", "failed", "canceled", "interrupted"})


class SharedQueueError(RuntimeError):
    """Base error for shared queue operations."""


class SharedQueueUnavailable(SharedQueueError):
    """Supabase could not be reached or returned an unusable response."""


class SharedQueueBlocked(SharedQueueError):
    """The queue deliberately refused work for a safety/version reason."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_node_key(value: Optional[str] = None) -> str:
    raw = str(value or os.environ.get("AUTOMATION_NODE_KEY") or socket.gethostname()).strip().lower()
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in raw)
    safe = safe.strip("-_")
    if not safe:
        raise ValueError("Node key cannot be empty.")
    return safe[:64]


@dataclass(frozen=True)
class SharedQueueConfig:
    supabase_url: str
    anon_key: str
    workspace_id: str
    node_key: str
    encryption_key: str
    access_token: str = field(default="", repr=False)
    user_email: str = ""
    user_password: str = field(default="", repr=False)
    request_timeout_seconds: float = 12.0
    lease_seconds: int = 45

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any], *, node_key: Optional[str] = None) -> "SharedQueueConfig":
        aliases = {
            "supabase_url": ("supabase_url", "url"),
            "anon_key": ("anon_key", "supabase_anon_key"),
            "workspace_id": ("workspace_id", "automation_workspace_id"),
            "encryption_key": ("encryption_key", "fernet_key", "queue_fernet_key"),
        }
        normalized = {}
        for destination, source_names in aliases.items():
            normalized[destination] = next(
                (values.get(name) for name in source_names if str(values.get(name) or "").strip()),
                None,
            )
        missing = [name for name, value in normalized.items() if not str(value or "").strip()]
        if missing:
            raise SharedQueueBlocked(f"Shared queue keychain entry is missing: {', '.join(missing)}.")
        access_token = str(values.get("access_token") or values.get("supabase_access_token") or "").strip()
        user_email = str(values.get("email") or values.get("user_email") or "").strip()
        user_password = str(values.get("password") or values.get("user_password") or "")
        if not access_token and not (user_email and user_password):
            raise SharedQueueBlocked(
                "Shared queue keychain entry needs access_token or both email and password."
            )
        return cls(
            supabase_url=str(normalized["supabase_url"]).rstrip("/"),
            anon_key=str(normalized["anon_key"]),
            workspace_id=str(normalized["workspace_id"]),
            node_key=normalize_node_key(node_key),
            encryption_key=str(normalized["encryption_key"]),
            access_token=access_token,
            user_email=user_email,
            user_password=user_password,
            request_timeout_seconds=float(values.get("request_timeout_seconds") or 12.0),
            lease_seconds=int(values.get("lease_seconds") or 45),
        )

    @classmethod
    def from_keychain(cls) -> "SharedQueueConfig":
        credential = read_credential(SHARED_QUEUE_CREDENTIAL_TARGET)
        try:
            values = json.loads(credential.secret)
        except json.JSONDecodeError as exc:
            raise SharedQueueBlocked("Shared queue keychain entry does not contain valid JSON.") from exc
        if not isinstance(values, dict):
            raise SharedQueueBlocked("Shared queue keychain entry must contain a JSON object.")
        return cls.from_mapping(values, node_key=credential.username)

    @classmethod
    def from_env(cls) -> "SharedQueueConfig":
        required = {
            "supabase_url": os.environ.get("SUPABASE_URL"),
            "anon_key": os.environ.get("SUPABASE_ANON_KEY"),
            "workspace_id": os.environ.get("AUTOMATION_WORKSPACE_ID"),
            "encryption_key": os.environ.get("AUTOMATION_QUEUE_FERNET_KEY"),
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            env_names = ", ".join(name.upper() for name in missing)
            raise SharedQueueBlocked(f"Shared queue is not configured. Missing: {env_names}.")
        access_token = str(os.environ.get("SUPABASE_ACCESS_TOKEN") or "").strip()
        user_email = str(os.environ.get("SUPABASE_USER_EMAIL") or "").strip()
        user_password = str(os.environ.get("SUPABASE_USER_PASSWORD") or "")
        if not access_token and not (user_email and user_password):
            raise SharedQueueBlocked(
                "Shared queue needs SUPABASE_ACCESS_TOKEN or SUPABASE_USER_EMAIL and SUPABASE_USER_PASSWORD."
            )
        return cls(
            supabase_url=str(required["supabase_url"]).rstrip("/"),
            anon_key=str(required["anon_key"]),
            workspace_id=str(required["workspace_id"]),
            node_key=normalize_node_key(),
            encryption_key=str(required["encryption_key"]),
            access_token=access_token,
            user_email=user_email,
            user_password=user_password,
        )


class TaskPayloadCipher:
    """Fernet encryption for queue arguments stored in Supabase."""

    def __init__(self, key: str | bytes):
        raw = key.encode("ascii") if isinstance(key, str) else key
        try:
            self._fernet = Fernet(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("AUTOMATION_QUEUE_FERNET_KEY is not a valid Fernet key.") from exc

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")

    def encrypt(self, payload: Mapping[str, Any]) -> str:
        if not isinstance(payload, Mapping):
            raise TypeError("Queue task payload must be an object.")
        serialized = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self._fernet.encrypt(serialized).decode("ascii")

    def decrypt(self, token: str) -> Dict[str, Any]:
        try:
            decoded = self._fernet.decrypt(str(token).encode("ascii"))
            payload = json.loads(decoded.decode("utf-8"))
        except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise SharedQueueBlocked("Queue task payload could not be decrypted safely.") from exc
        if not isinstance(payload, dict):
            raise SharedQueueBlocked("Queue task payload is not an object.")
        return payload


class SupabaseQueueClient:
    """Small authenticated PostgREST client for the queue SQL API."""

    def __init__(
        self,
        config: SharedQueueConfig,
        *,
        opener: Optional[Callable[..., Any]] = None,
    ):
        self.config = config
        self.cipher = TaskPayloadCipher(config.encryption_key)
        self._opener = opener or urllib.request.urlopen
        self._access_token = str(config.access_token or "").strip()
        self._token_expires_at = 0.0

    def _authenticate(self) -> str:
        if not self.config.user_email or not self.config.user_password:
            if self._access_token:
                return self._access_token
            raise SharedQueueBlocked("Supabase authentication credentials are missing.")
        url = f"{self.config.supabase_url}/auth/v1/token?grant_type=password"
        body = json.dumps(
            {"email": self.config.user_email, "password": self.config.user_password},
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"apikey": self.config.anon_key, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self.config.request_timeout_seconds)
            result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise SharedQueueBlocked(detail or "Supabase sign-in was rejected.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SharedQueueUnavailable(f"Supabase sign-in is unavailable: {exc}") from exc
        token = str(result.get("access_token") or "").strip() if isinstance(result, dict) else ""
        if not token:
            raise SharedQueueBlocked("Supabase sign-in did not return an access token.")
        self._access_token = token
        self._token_expires_at = time.time() + max(60, int(result.get("expires_in") or 3600)) - 30
        return token

    def _current_access_token(self) -> str:
        if self.config.user_email and (not self._access_token or time.time() >= self._token_expires_at):
            return self._authenticate()
        return self._access_token or self._authenticate()

    def _headers(self, *, prefer: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "apikey": self.config.anon_key,
            "Authorization": f"Bearer {self._current_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _request(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None, *, prefer=None):
        url = f"{self.config.supabase_url}/rest/v1/{path.lstrip('/')}"
        encoded = None if body is None else json.dumps(dict(body), separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=encoded, headers=self._headers(prefer=prefer), method=method)
        try:
            response = self._opener(request, timeout=self.config.request_timeout_seconds)
            raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            if exc.code in {400, 409, 422}:
                raise SharedQueueBlocked(detail or f"Supabase rejected the request ({exc.code}).") from exc
            raise SharedQueueUnavailable(detail or f"Supabase request failed ({exc.code}).") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SharedQueueUnavailable(f"Supabase queue is unavailable: {exc}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SharedQueueUnavailable("Supabase returned an invalid JSON response.") from exc

    def _rpc(self, name: str, body: Mapping[str, Any]):
        result = self._request("POST", f"rpc/{name}", body)
        if isinstance(result, dict) and result.get("ok") is False:
            raise SharedQueueBlocked(str(result.get("message") or "Queue operation was blocked."))
        return result

    def heartbeat(
        self,
        *,
        commit: str,
        capabilities: Mapping[str, Any],
        display_name: Optional[str] = None,
        runtime_status: Optional[Mapping[str, Any]] = None,
    ):
        return self._rpc(
            "automation_node_heartbeat",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_node_key": self.config.node_key,
                "p_display_name": display_name or socket.gethostname(),
                "p_os_name": "macos" if platform.system().lower() == "darwin" else platform.system().lower(),
                "p_architecture": platform.machine().lower(),
                "p_app_commit": str(commit or "unknown"),
                "p_protocol_version": QUEUE_PROTOCOL_VERSION,
                "p_capabilities": dict(capabilities),
                "p_runtime_status": dict(runtime_status or {}),
            },
        )

    def enqueue(
        self,
        *,
        label: str,
        category: str,
        task_type: str,
        arguments: Mapping[str, Any],
        target_node: Optional[str] = None,
        required_capability: Optional[str] = None,
        details: Optional[str] = None,
        queue_mode: str = "normal",
        available_at: Optional[str] = None,
        repeat_interval_minutes: Optional[int] = None,
        requested_client_os: Optional[str] = None,
        commit: str,
    ):
        task_type = str(task_type or "").strip()
        if not task_type:
            raise ValueError("task_type is required for safe cross-device execution.")
        return self._rpc(
            "automation_enqueue_task",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_label": str(label or "Automation Task"),
                "p_category": str(category or "Automation"),
                "p_task_type": task_type,
                "p_encrypted_payload": self.cipher.encrypt(arguments),
                "p_requested_by_node": self.config.node_key,
                "p_requested_client_os": str(requested_client_os or platform.system() or "unknown").strip().lower(),
                "p_target_node": normalize_node_key(target_node) if target_node else None,
                "p_required_capability": str(required_capability or "").strip() or None,
                "p_details": str(details or "").strip() or None,
                "p_queue_mode": str(queue_mode or "normal").strip().lower(),
                "p_available_at": available_at,
                "p_repeat_interval_minutes": repeat_interval_minutes,
                "p_app_commit": str(commit or "unknown"),
                "p_protocol_version": QUEUE_PROTOCOL_VERSION,
            },
        )

    def claim_next(self, *, commit: str):
        result = self._rpc(
            "automation_claim_next_task",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_node_key": self.config.node_key,
                "p_app_commit": str(commit or "unknown"),
                "p_protocol_version": QUEUE_PROTOCOL_VERSION,
                "p_lease_seconds": max(30, min(120, int(self.config.lease_seconds))),
            },
        )
        if not result:
            return None
        task = result[0] if isinstance(result, list) else result
        if not isinstance(task, dict) or not task.get("id"):
            return None
        task = dict(task)
        task["arguments"] = self.cipher.decrypt(task.pop("encrypted_payload", ""))
        return task

    def renew_lease(self, task_id: str, lease_token: str):
        return self._rpc(
            "automation_renew_task_lease",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_task_id": str(task_id),
                "p_node_key": self.config.node_key,
                "p_lease_token": str(lease_token),
                "p_lease_seconds": max(30, min(120, int(self.config.lease_seconds))),
            },
        )

    def finish(self, task_id: str, lease_token: str, *, success: bool, message: str):
        return self._rpc(
            "automation_finish_task",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_task_id": str(task_id),
                "p_node_key": self.config.node_key,
                "p_lease_token": str(lease_token),
                "p_success": bool(success),
                "p_message": str(message or "Task finished."),
            },
        )

    def snapshot(self):
        rows = self._rpc("automation_queue_snapshot", {"p_workspace_id": self.config.workspace_id}) or []
        return rows if isinstance(rows, list) else []

    def list_nodes(self):
        workspace = urllib.parse.quote(f"eq.{self.config.workspace_id}", safe=".")
        columns = (
            "node_key,display_name,os_name,architecture,app_commit,protocol_version,"
            "capabilities,runtime_status,last_seen_at,enabled"
        )
        path = f"automation_nodes?select={columns}&workspace_id={workspace}&order=display_name.asc"
        rows = self._request("GET", path) or []
        return rows if isinstance(rows, list) else []

    def get_version_gate(self):
        workspace = urllib.parse.quote(f"eq.{self.config.workspace_id}", safe=".")
        columns = "required_commit,required_protocol_version,paused,pause_reason,updated_at"
        path = f"automation_queue_control?select={columns}&workspace_id={workspace}&limit=1"
        rows = self._request("GET", path) or []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return dict(rows[0])
        return {}

    def cancel(self, task_id: str):
        return self._rpc(
            "automation_cancel_task",
            {"p_workspace_id": self.config.workspace_id, "p_task_id": str(task_id)},
        )

    def clear_finished(self):
        """Delete finished queue history while preserving queued/running work."""
        return self._rpc(
            "automation_clear_finished_tasks",
            {"p_workspace_id": self.config.workspace_id},
        )

    def reassign(self, task_id: str, target_node: Optional[str]):
        return self._rpc(
            "automation_reassign_task",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_task_id": str(task_id),
                "p_target_node": normalize_node_key(target_node) if target_node else None,
            },
        )

    def resume_after_review(self, review_note: str):
        return self._rpc(
            "automation_resume_queue",
            {"p_workspace_id": self.config.workspace_id, "p_review_note": str(review_note or "").strip()},
        )

    def create_workspace(self, name: str):
        return self._rpc("automation_create_workspace", {"p_name": str(name or "Automation")})

    def add_workspace_member(self, user_id: str, role: str = "operator"):
        return self._rpc(
            "automation_add_workspace_member",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_user_id": str(user_id),
                "p_role": str(role or "operator"),
            },
        )

    def set_version_gate(self, commit: str, protocol_version: int = QUEUE_PROTOCOL_VERSION):
        return self._rpc(
            "automation_set_version_gate",
            {
                "p_workspace_id": self.config.workspace_id,
                "p_required_commit": str(commit or "").strip(),
                "p_required_protocol_version": int(protocol_version),
            },
        )


def make_test_config(**overrides) -> SharedQueueConfig:
    """Convenience for unit tests and local diagnostic tools."""
    values = {
        "supabase_url": "https://example.supabase.co",
        "anon_key": "anon",
        "access_token": "token",
        "workspace_id": str(uuid.uuid4()),
        "node_key": "test-node",
        "encryption_key": base64.urlsafe_b64encode(b"x" * 32).decode("ascii"),
    }
    values.update(overrides)
    return SharedQueueConfig(**values)
