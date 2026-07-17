"""Background coordinator for a single Supabase-backed global queue."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

from shared_queue import SharedQueueBlocked, SharedQueueError, SupabaseQueueClient
from task_registry import TaskRegistry


logger = logging.getLogger(__name__)


class SharedQueueRuntime:
    def __init__(
        self,
        client: SupabaseQueueClient,
        registry: TaskRegistry,
        *,
        commit_provider: Callable[[], str],
        capabilities_provider: Callable[[], Mapping[str, Any]],
        node_status_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
        heartbeat_interval_seconds: float = 10.0,
        poll_interval_seconds: float = 2.0,
        lease_renew_interval_seconds: float = 15.0,
    ):
        self.client = client
        self.registry = registry
        self.commit_provider = commit_provider
        self.capabilities_provider = capabilities_provider
        self.node_status_provider = node_status_provider or (lambda: {})
        self.heartbeat_interval_seconds = max(1.0, float(heartbeat_interval_seconds))
        self.poll_interval_seconds = max(0.25, float(poll_interval_seconds))
        self.lease_renew_interval_seconds = max(1.0, float(lease_renew_interval_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {
            "mode": "shared",
            "connected": False,
            "eligible": False,
            "block_reason": "Waiting for Supabase heartbeat.",
            "running_task_id": None,
            "last_heartbeat_at": None,
            "last_error": None,
        }

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="shared-automation-queue", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def wake(self) -> None:
        self._wake.set()

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _update_state(self, **changes) -> None:
        with self._lock:
            self._state.update(changes)

    def enqueue(self, **task):
        state = self.state()
        if not state.get("connected") or not state.get("eligible"):
            raise SharedQueueBlocked(str(state.get("block_reason") or "Shared queue is not ready."))
        task.setdefault("commit", self.commit_provider())
        result = self.client.enqueue(**task)
        self.wake()
        return result

    def snapshot(self):
        rows = self.client.snapshot()
        nodes = self.client.list_nodes()
        now = datetime.now(timezone.utc)
        node_map = {}
        for node in nodes:
            seen = str(node.get("last_seen_at") or "")
            try:
                seen_at = datetime.fromisoformat(seen.replace("Z", "+00:00"))
                if seen_at.tzinfo is None:
                    seen_at = seen_at.replace(tzinfo=timezone.utc)
                online = (now - seen_at.astimezone(timezone.utc)).total_seconds() <= 30
            except (TypeError, ValueError):
                online = False
            node["online"] = bool(online and node.get("enabled", True))
            node["os_icon"] = "🍎" if node.get("os_name") == "macos" else ("🪟" if node.get("os_name") == "windows" else "💻")
            node_map[node.get("node_key")] = node
        for row in rows:
            source = node_map.get(row.get("requested_by_node"), {})
            claimed = node_map.get(row.get("claimed_by_node"), {})
            target = node_map.get(row.get("target_node"), {})
            client_os = str(row.get("requested_client_os") or source.get("os_name") or "unknown").lower()
            client_labels = {"windows": "Windows", "macos": "Mac", "darwin": "Mac", "android": "Android tablet"}
            client_icons = {"windows": "🪟", "macos": "🍎", "darwin": "🍎", "android": "🤖"}
            row["source_os"] = client_os
            row["source_icon"] = client_icons.get(client_os, source.get("os_icon") or "💻")
            row["source_display_name"] = client_labels.get(client_os, source.get("display_name") or row.get("requested_by_node"))
            row["runner_os"] = claimed.get("os_name")
            row["runner_icon"] = claimed.get("os_icon") if claimed else None
            row["target_online"] = target.get("online") if row.get("target_node") else None
            if row.get("status") == "queued" and row.get("target_node") and target and not target.get("online"):
                row["waiting_reason"] = f"Waiting for {target.get('display_name') or row.get('target_node')} to come online."
        running = [row for row in rows if row.get("status") == "running"]
        queued = []
        idle = []
        for row in rows:
            if row.get("status") != "queued":
                continue
            available = str(row.get("available_at") or "")
            try:
                due_at = datetime.fromisoformat(available.replace("Z", "+00:00"))
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
                is_idle = due_at.astimezone(timezone.utc) > now
            except (TypeError, ValueError):
                is_idle = False
            if is_idle:
                display_row = dict(row)
                display_row["status"] = "idle"
                display_row["next_run_at"] = row.get("available_at")
                idle.append(display_row)
            else:
                queued.append(row)
        history = [row for row in rows if row.get("status") not in {"running", "queued"}]
        for index, row in enumerate(queued, start=1):
            row["position"] = index
        return {
            "success": True,
            "mode": "shared",
            "coordinator": self.state(),
            "nodes": nodes,
            "running": running[0] if running else None,
            "queued": queued,
            "idle": idle,
            "history": list(reversed(history[-30:])),
            "tasks": running + queued + idle + list(reversed(history[-30:])),
            "running_count": len(running),
            "queued_count": len(queued),
            "idle_count": len(idle),
        }

    def _heartbeat(self) -> bool:
        commit = self.commit_provider()
        response = self.client.heartbeat(
            commit=commit,
            capabilities=self.capabilities_provider(),
            runtime_status=self.node_status_provider(),
        )
        eligible = bool(response and response.get("eligible"))
        reason = None
        if not eligible:
            reason = (response or {}).get("pause_reason") or "Strict version gate rejected this computer."
        self._update_state(
            connected=True,
            eligible=eligible,
            block_reason=reason,
            last_heartbeat_at=time.time(),
            last_error=None,
        )
        return eligible

    def _execute_claim(self, task: Mapping[str, Any]) -> None:
        task_id = str(task.get("id") or "")
        lease_token = str(task.get("lease_token") or "")
        self._update_state(running_task_id=task_id)
        lease_stop = threading.Event()

        def renew_loop():
            while not lease_stop.wait(self.lease_renew_interval_seconds):
                try:
                    heartbeat = self.client.heartbeat(
                        commit=str(task.get("app_commit") or self.commit_provider()),
                        capabilities=self.capabilities_provider(),
                        runtime_status=self.node_status_provider(),
                    )
                    response = self.client.renew_lease(task_id, lease_token)
                    self._update_state(
                        connected=True,
                        eligible=bool(heartbeat and heartbeat.get("eligible")),
                        block_reason=(heartbeat or {}).get("pause_reason"),
                        last_heartbeat_at=time.time(),
                        last_error=None,
                    )
                    if response and response.get("cancel_requested"):
                        self._update_state(last_error="Cancellation requested; waiting for the automation to stop safely.")
                except SharedQueueError as exc:
                    self._update_state(connected=False, eligible=False, block_reason=str(exc), last_error=str(exc))
                    return

        renewer = threading.Thread(target=renew_loop, name=f"queue-lease-{task_id[:8]}", daemon=True)
        renewer.start()
        ok = False
        message = "Task did not run."
        try:
            ok, message = self.registry.execute(str(task.get("task_type") or ""), task.get("arguments"))
        except Exception as exc:
            logger.exception("Shared queue task failed")
            ok, message = False, str(exc)
        finally:
            lease_stop.set()
            renewer.join(timeout=2.0)
        try:
            self.client.finish(task_id, lease_token, success=ok, message=message)
        except SharedQueueError as exc:
            logger.error("Could not finish shared queue task %s: %s", task_id, exc)
            self._update_state(connected=False, eligible=False, block_reason=str(exc), last_error=str(exc))
        finally:
            self._update_state(running_task_id=None)

    def _run(self) -> None:
        next_heartbeat = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now >= next_heartbeat:
                    eligible = self._heartbeat()
                    next_heartbeat = now + self.heartbeat_interval_seconds
                else:
                    eligible = bool(self.state().get("eligible"))
                if eligible:
                    task = self.client.claim_next(commit=self.commit_provider())
                    if task:
                        self._execute_claim(task)
                        next_heartbeat = 0.0
                        continue
            except SharedQueueError as exc:
                self._update_state(
                    connected=False,
                    eligible=False,
                    block_reason=str(exc),
                    last_error=str(exc),
                )
            except Exception as exc:
                logger.exception("Unexpected shared queue coordinator failure")
                self._update_state(
                    connected=False,
                    eligible=False,
                    block_reason=f"Shared queue error: {exc}",
                    last_error=str(exc),
                )
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()
