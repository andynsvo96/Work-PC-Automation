import unittest
from unittest import mock

import server


class LocalQueueOnlyTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_queue_payload_is_always_local(self):
        with mock.patch.object(server, "automation_queue_tasks", []):
            payload = server.get_automation_queue_payload()

        self.assertTrue(payload["success"])
        self.assertEqual(payload["mode"], "local")
        self.assertNotIn("nodes", payload)
        self.assertNotIn("coordinator", payload)

    def test_target_header_is_ignored_and_task_runs_on_receiving_server(self):
        tasks = []
        with (
            mock.patch.object(server, "APP_PIN_REQUIRED", False),
            mock.patch.object(server, "automation_queue_tasks", tasks),
            mock.patch.object(server, "_ensure_automation_queue_worker"),
            mock.patch.object(server, "log_automation_event"),
        ):
            response = self.client.post(
                "/clock/test/in",
                headers={"X-Automation-Target-Node": "other-computer"},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(len(tasks), 1)
        self.assertNotIn("target_node", payload["queue_task"])

    def test_shared_queue_control_routes_do_not_exist(self):
        with mock.patch.object(server, "APP_PIN_REQUIRED", False):
            self.assertEqual(self.client.post("/api/queue/example/reassign").status_code, 404)
            self.assertEqual(self.client.post("/api/queue/resume").status_code, 404)


if __name__ == "__main__":
    unittest.main()
