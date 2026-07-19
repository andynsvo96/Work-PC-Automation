import unittest
from unittest import mock

import server


class RepeatQueueIntervalTests(unittest.TestCase):
    def test_repeat_interval_accepts_zero_and_non_five_minute_values(self):
        self.assertEqual(server._normalize_automation_repeat_interval_minutes(0), 0)
        self.assertEqual(server._normalize_automation_repeat_interval_minutes(1), 1)
        self.assertEqual(server._normalize_automation_repeat_interval_minutes(7), 7)
        self.assertEqual(server._normalize_automation_repeat_interval_minutes(75), 75)

    def test_repeat_interval_rejects_negative_values(self):
        self.assertEqual(server._normalize_automation_repeat_interval_minutes(-1), 0)

    def test_local_repeat_task_keeps_zero_interval(self):
        tasks = []
        with (
            mock.patch.object(server, "AUTOMATION_QUEUE_MODE", "local"),
            mock.patch.object(server, "automation_queue_tasks", tasks),
            mock.patch.object(server, "_automation_version_block_reason", return_value=""),
            mock.patch.object(server, "_ensure_automation_queue_worker"),
            mock.patch.object(server, "log_automation_event"),
        ):
            ok, _message, task = server.enqueue_automation(
                "Immediate Repeat",
                "Processing",
                lambda: (True, "Finished."),
                queue_mode="repeat",
                repeat_interval_minutes=0,
            )

        self.assertTrue(ok)
        self.assertEqual(task["repeat_interval_minutes"], 0)
        self.assertEqual(tasks[0]["repeat_interval_minutes"], 0)

    def test_processing_route_preserves_zero_in_queue_summary(self):
        tasks = []
        with (
            mock.patch.object(server, "APP_PIN_REQUIRED", False),
            mock.patch.object(server, "AUTOMATION_QUEUE_MODE", "local"),
            mock.patch.object(server, "automation_queue_tasks", tasks),
            mock.patch.object(server, "_automation_version_block_reason", return_value=""),
            mock.patch.object(server, "_ensure_automation_queue_worker"),
            mock.patch.object(server, "log_automation_event"),
        ):
            response = server.app.test_client().post(
                "/crm/process",
                json={"advanced_mode": "repeat", "repeat_interval_minutes": 0},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["queue_task"]["repeat_interval_minutes"], 0)
        self.assertIn("Repeat immediately", payload["queue_task"]["advanced_summary"])


if __name__ == "__main__":
    unittest.main()
