import threading
import time
import unittest
from unittest import mock

from shared_queue import SharedQueueUnavailable
from shared_queue_runtime import SharedQueueRuntime
from task_registry import TaskRegistry


class _FakeClient:
    def __init__(self, task=None):
        self.task = task
        self.heartbeats = 0
        self.finished = []
        self.renewed = []

    def heartbeat(self, **_kwargs):
        self.heartbeats += 1
        return {"eligible": True}

    def claim_next(self, **_kwargs):
        task, self.task = self.task, None
        return task

    def renew_lease(self, task_id, token):
        self.renewed.append((task_id, token))
        return {"ok": True, "cancel_requested": False}

    def finish(self, task_id, token, **result):
        self.finished.append((task_id, token, result))
        return {"ok": True}

    def snapshot(self):
        return []


class SharedQueueRuntimeTests(unittest.TestCase):
    def test_windows_owner_repairs_stale_version_gate_then_becomes_eligible(self):
        client = mock.Mock()
        client.heartbeat.side_effect = [
            {
                "eligible": False,
                "paused": False,
                "required_commit": "old-commit",
                "required_protocol_version": 1,
            },
            {"eligible": True, "paused": False, "required_commit": "new-commit"},
        ]
        client.set_version_gate.return_value = {"ok": True, "required_commit": "new-commit"}
        runtime = SharedQueueRuntime(
            client,
            TaskRegistry(),
            commit_provider=lambda: "new-commit",
            capabilities_provider=lambda: {"crm": True},
            auto_sync_version_gate=True,
            version_gate_sync_guard=lambda commit: commit == "new-commit",
        )

        self.assertTrue(runtime._heartbeat())
        client.set_version_gate.assert_called_once_with("new-commit")
        self.assertEqual(client.heartbeat.call_count, 2)

    def test_operator_does_not_modify_stale_version_gate(self):
        client = mock.Mock()
        client.heartbeat.return_value = {
            "eligible": False,
            "paused": False,
            "required_commit": "old-commit",
            "required_protocol_version": 1,
        }
        runtime = SharedQueueRuntime(
            client,
            TaskRegistry(),
            commit_provider=lambda: "new-commit",
            capabilities_provider=lambda: {"crm": True},
            auto_sync_version_gate=False,
        )

        self.assertFalse(runtime._heartbeat())
        client.set_version_gate.assert_not_called()

    def test_outdated_owner_cannot_move_version_gate_backward(self):
        client = mock.Mock()
        client.heartbeat.return_value = {
            "eligible": False,
            "paused": False,
            "required_commit": "new-commit",
            "required_protocol_version": 1,
        }
        runtime = SharedQueueRuntime(
            client,
            TaskRegistry(),
            commit_provider=lambda: "old-commit",
            capabilities_provider=lambda: {"crm": True},
            auto_sync_version_gate=True,
            version_gate_sync_guard=lambda _commit: False,
        )

        self.assertFalse(runtime._heartbeat())
        client.set_version_gate.assert_not_called()

    def test_claimed_task_executes_and_finishes(self):
        client = _FakeClient(
            {
                "id": "task-1",
                "lease_token": "lease-1",
                "task_type": "test.run",
                "arguments": {"value": "ok"},
            }
        )
        ran = threading.Event()
        registry = TaskRegistry()

        def execute(value):
            ran.set()
            return True, value

        registry.register("test.run", execute)
        runtime = SharedQueueRuntime(
            client,
            registry,
            commit_provider=lambda: "abc",
            capabilities_provider=lambda: {"crm": True},
            heartbeat_interval_seconds=1,
            poll_interval_seconds=0.01,
            lease_renew_interval_seconds=1,
        )
        runtime.start()
        self.assertTrue(ran.wait(1.0))
        deadline = time.monotonic() + 1.0
        while not client.finished and time.monotonic() < deadline:
            time.sleep(0.01)
        runtime.stop()

        self.assertEqual(client.finished[0][0:2], ("task-1", "lease-1"))
        self.assertEqual(client.finished[0][2], {"success": True, "message": "ok"})

    def test_remote_cancel_invokes_runner_force_stop_once(self):
        task = {
            "id": "task-cancel",
            "lease_token": "lease-cancel",
            "task_type": "test.cancel",
            "arguments": {},
        }
        client = _FakeClient(task)
        client.renew_lease = mock.Mock(return_value={"ok": True, "cancel_requested": True})
        released = threading.Event()
        registry = TaskRegistry()

        def execute():
            self.assertTrue(released.wait(2.0))
            return False, "Stopped by user."

        def force_stop():
            released.set()
            return True, "Force stop requested."

        registry.register("test.cancel", execute)
        cancel_callback = mock.Mock(side_effect=force_stop)
        runtime = SharedQueueRuntime(
            client,
            registry,
            commit_provider=lambda: "abc",
            capabilities_provider=lambda: {"crm": True},
            cancel_running_task=cancel_callback,
            lease_renew_interval_seconds=1,
        )

        runtime._execute_claim(task)

        cancel_callback.assert_called_once_with()
        self.assertEqual(client.finished[0][2], {"success": False, "message": "Stopped by user."})

    def test_transient_lease_renewal_failure_is_retried_before_task_finishes(self):
        task = {
            "id": "task-retry",
            "lease_token": "lease-retry",
            "task_type": "test.retry",
            "arguments": {},
        }
        client = _FakeClient(task)
        renewal_recovered = threading.Event()
        attempts = 0

        def renew_lease(task_id, token):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise SharedQueueUnavailable("temporary Supabase connection failure")
            renewal_recovered.set()
            return {"ok": True, "cancel_requested": False}

        client.renew_lease = renew_lease
        registry = TaskRegistry()
        registry.register(
            "test.retry",
            lambda: (renewal_recovered.wait(7.0), "finished after lease renewal recovered"),
        )
        runtime = SharedQueueRuntime(
            client,
            registry,
            commit_provider=lambda: "abc",
            capabilities_provider=lambda: {"crm": True},
            lease_renew_interval_seconds=1,
        )

        runtime._execute_claim(task)

        self.assertTrue(renewal_recovered.is_set())
        self.assertEqual(attempts, 2)
        self.assertEqual(client.finished[0][2], {
            "success": True,
            "message": "finished after lease renewal recovered",
        })


if __name__ == "__main__":
    unittest.main()
