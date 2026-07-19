import types
import unittest
from unittest import mock

import server


class _FakeSharedRuntime:
    def __init__(self):
        self.enqueued = []
        self.client = types.SimpleNamespace(config=types.SimpleNamespace(node_key="windows-test"))
        self.client.list_nodes = lambda: [
            {
                "node_key": "windows-test",
                "os_name": "windows",
                "enabled": True,
                "capabilities": {"system_power": True, "metrics": True},
                "last_seen_at": "2026-01-02T00:00:00+00:00",
            },
            {
                "node_key": "macbook",
                "os_name": "macos",
                "enabled": True,
                "capabilities": {"system_power": False, "metrics": False},
                "last_seen_at": "2026-01-01T00:00:00+00:00",
            },
        ]
        self.client.clear_finished = lambda: 3

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

    def test_windows_desktop_target_is_automatic(self):
        response = self.client.post(
            "/clock/test/in",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "X-Automation-Target-Node": "macbook",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.runtime.enqueued[-1]["target_node"], "windows-test")

    def test_android_can_choose_either_target(self):
        response = self.client.post(
            "/clock/test/in",
            headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 16; Tablet)",
                "X-Automation-Target-Node": "macbook",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.runtime.enqueued[-1]["target_node"], "macbook")

    def test_desktop_targeting_fails_closed_without_matching_node(self):
        self.runtime.client.list_nodes = lambda: [
            {"node_key": "macbook", "os_name": "macos", "enabled": True}
        ]
        mac_snapshot = types.SimpleNamespace(os_name="macos")

        with mock.patch.object(server, "get_platform_snapshot", return_value=mac_snapshot):
            response = self.client.post(
                "/clock/test/in",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertIn("No registered Windows node", response.get_json()["message"])
        self.assertEqual(self.runtime.enqueued, [])

    def test_clear_finished_shared_queue_history(self):
        response = self.client.post("/api/queue/clear-finished")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertIn("3 finished shared queue", response.get_json()["message"])

    def test_cancel_finalizes_expired_task_owned_by_current_node(self):
        self.runtime.client.cancel = mock.Mock(return_value={
            "id": "stale-task",
            "status": "running",
            "cancel_requested": True,
            "claimed_by_node": "windows-test",
            "lease_token": "stale-lease",
            "lease_expires_at": "2000-01-01T00:00:00+00:00",
        })
        self.runtime.client.finish = mock.Mock(return_value={"status": "canceled"})

        response = self.client.post("/api/queue/stale-task/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertIn("Canceled the expired task", response.get_json()["message"])
        self.runtime.client.finish.assert_called_once_with(
            "stale-task",
            "stale-lease",
            success=False,
            message="Canceled after the original worker heartbeat was lost.",
        )

    def test_cancel_does_not_finalize_task_with_active_lease(self):
        self.runtime.client.cancel = mock.Mock(return_value={
            "id": "active-task",
            "status": "running",
            "cancel_requested": True,
            "claimed_by_node": "windows-test",
            "lease_token": "active-lease",
            "lease_expires_at": "2999-01-01T00:00:00+00:00",
        })
        self.runtime.client.finish = mock.Mock()

        response = self.client.post("/api/queue/active-task/cancel")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["success"])
        self.assertIn("Cancel request sent", response.get_json()["message"])
        self.runtime.client.finish.assert_not_called()

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

    def test_update_endpoint_starts_safe_restart_for_clean_behind_checkout(self):
        git_state = {
            "available": True,
            "dirty": False,
            "relation": "behind",
            "commit": "old-commit",
            "origin_commit": "new-commit",
        }
        with (
            mock.patch("server.refresh_remote_version_state", return_value={"git": git_state}),
            mock.patch("server._automation_version_block_reason", return_value="Update required: behind origin/main."),
            mock.patch("server.get_automation_queue_payload", return_value={"running_count": 0}),
            mock.patch("server.get_power_countdown_payload", return_value={"active": False}),
            mock.patch("server.get_slack_lunch_payload", return_value={"active": False}),
            mock.patch("server._schedule_app_update_restart") as schedule,
        ):
            response = self.client.post("/api/app/update")

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["restarting"])
        schedule.assert_called_once_with()

    def test_update_endpoint_waits_for_running_automation(self):
        git_state = {
            "available": True,
            "dirty": False,
            "relation": "behind",
            "commit": "old-commit",
            "origin_commit": "new-commit",
        }
        with (
            mock.patch("server.refresh_remote_version_state", return_value={"git": git_state}),
            mock.patch("server._automation_version_block_reason", return_value="Update required: behind origin/main."),
            mock.patch("server.get_automation_queue_payload", return_value={"running_count": 1}),
            mock.patch("server._schedule_app_update_restart") as schedule,
        ):
            response = self.client.post("/api/app/update")

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["retryable"])
        schedule.assert_not_called()

    def test_update_endpoint_refuses_dirty_checkout(self):
        git_state = {
            "available": True,
            "dirty": True,
            "relation": "behind",
            "commit": "old-commit",
            "origin_commit": "new-commit",
        }
        with (
            mock.patch("server.refresh_remote_version_state", return_value={"git": git_state}),
            mock.patch("server._automation_version_block_reason", return_value="Update required: local changes."),
            mock.patch("server._schedule_app_update_restart") as schedule,
        ):
            response = self.client.post("/api/app/update")

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["manual_required"])
        schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
