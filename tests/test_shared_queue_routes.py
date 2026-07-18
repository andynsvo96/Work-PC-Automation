import types
import unittest
from unittest import mock

import server


class _FakeSharedRuntime:
    def __init__(self):
        self.enqueued = []
        self.client = types.SimpleNamespace(config=types.SimpleNamespace(node_key="windows-test"))
        self.client.list_nodes = lambda: []

    def enqueue(self, **task):
        self.enqueued.append(task)
        return {"id": f"task-{len(self.enqueued)}", "sequence": len(self.enqueued), "status": "queued"}

    def state(self):
        return {"connected": True, "eligible": True}

    def snapshot(self):
        return {"success": True, "mode": "shared", "tasks": [], "queued_count": 0, "running_count": 0, "idle_count": 0}

    def wake(self):
        pass


class SharedQueueRouteTests(unittest.TestCase):
    def setUp(self):
        self.previous_mode = server.AUTOMATION_QUEUE_MODE
        self.previous_runtime = server.shared_queue_runtime
        self.previous_pin_required = server.APP_PIN_REQUIRED
        self.previous_version_monitor_state = dict(server.version_monitor_state)
        server.AUTOMATION_QUEUE_MODE = "shared"
        server.APP_PIN_REQUIRED = False
        self.runtime = _FakeSharedRuntime()
        server.shared_queue_runtime = self.runtime
        server.app.config.update(TESTING=True)
        self.client = server.app.test_client()

    def tearDown(self):
        server.AUTOMATION_QUEUE_MODE = self.previous_mode
        server.shared_queue_runtime = self.previous_runtime
        server.APP_PIN_REQUIRED = self.previous_pin_required
        server.version_monitor_state.clear()
        server.version_monitor_state.update(self.previous_version_monitor_state)

    def test_communications_route_has_portable_descriptor(self):
        response = self.client.post("/clock/test/in")
        self.assertEqual(response.status_code, 202)
        task = self.runtime.enqueued[-1]
        self.assertEqual(task["task_type"], "communications.paycom_clock")
        self.assertEqual(task["arguments"], {"action": "in", "dry_run": True})

    def test_crm_route_has_portable_arguments_and_capability(self):
        response = self.client.post(
            "/crm/address-validator/dry-run",
            json={"order_id": "1234567", "batch_size": 2},
        )
        self.assertEqual(response.status_code, 202)
        task = self.runtime.enqueued[-1]
        self.assertEqual(task["task_type"], "crm.address_validator")
        self.assertEqual(task["arguments"]["order_id"], "1234567")
        self.assertTrue(task["arguments"]["dry_run"])
        self.assertEqual(task["required_capability"], "crm")

    def test_registered_executors_cover_route_task_types(self):
        registered = set(server.register_shared_queue_task_executors())
        self.assertIn("communications.paycom_clock", registered)
        self.assertIn("crm.processing", registered)
        self.assertIn("system.power", registered)

    def test_scheduled_power_action_is_durable_and_targeted(self):
        response = self.client.post(
            "/pc/schedule",
            json={"action": "shutdown", "delay_seconds": 60},
            headers={"X-Automation-Target-Node": "windows-test"},
        )
        self.assertEqual(response.status_code, 202)
        task = self.runtime.enqueued[-1]
        self.assertEqual(task["task_type"], "system.power")
        self.assertEqual(task["arguments"], {"action": "shutdown"})
        self.assertEqual(task["target_node"], "windows-test")
        self.assertEqual(task["queue_mode"], "scheduled")
        self.assertTrue(task["available_at"])

    def test_outdated_node_rejects_new_actions(self):
        server.version_monitor_state["block_reason"] = (
            "Update required: this computer is behind origin/main. Run Safe Sync & Start."
        )

        response = self.client.post("/clock/test/in")

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["update_required"])
        self.assertIn("Update required", response.get_json()["message"])
        self.assertEqual(self.runtime.enqueued, [])

    def test_outdated_node_rejects_nonqueued_mutating_actions(self):
        server.version_monitor_state["block_reason"] = "Update required: behind origin/main."

        response = self.client.post("/crm/shipping-bypasser/sanmar-cart/open")

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["update_required"])

    def test_loaded_server_commit_change_requires_restart(self):
        git_state = {"available": True, "dirty": False, "relation": "current", "commit": "new"}
        with mock.patch.object(server, "SERVER_APP_COMMIT", "old"):
            reason = server._git_update_block_reason(git_state)

        self.assertIn("server started", reason)
        self.assertIn("Safe Sync & Start", reason)

    def test_remote_refresh_marks_behind_checkout_blocked(self):
        git_state = {"available": True, "dirty": False, "relation": "behind"}
        with mock.patch("server.refresh_origin_main", return_value=git_state):
            state = server.refresh_remote_version_state()

        self.assertIn("behind origin/main", state["block_reason"])


if __name__ == "__main__":
    unittest.main()
