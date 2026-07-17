import threading
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
