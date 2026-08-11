import unittest
from unittest import mock

import server


class LocalQueueRetryTests(unittest.TestCase):
    @staticmethod
    def _order_task(status="canceled"):
        return {
            "id": "order-task-1",
            "label": "Complicated EMB to HDD Order 5010529",
            "category": "Processing",
            "details": "Single CRM order 5010529",
            "status": status,
            "queue_mode": "normal",
            "task_type": "crm.sheet_scanner_order",
            "task_arguments": {
                "order_id": "5010529",
                "process": "complicated_emb_to_hdd",
            },
            "fn": lambda: (True, "Finished."),
            "success": False,
            "cancel_requested": False,
            "result_context": {},
        }

    def test_canceled_order_retries_in_same_queue_entry(self):
        task = self._order_task()
        tasks = [task]
        with (
            mock.patch.object(server, "AUTOMATION_QUEUE_MODE", "local"),
            mock.patch.object(server, "automation_queue_tasks", tasks),
        ):
            ok, message = server.retry_automation_queue_task(task["id"])

        self.assertTrue(ok)
        self.assertIn("completed CRM work", message)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], "order-task-1")
        self.assertEqual(tasks[0]["status"], "queued")
        self.assertEqual(tasks[0]["task_arguments"]["order_id"], "5010529")
        self.assertTrue(tasks[0]["result_context"]["retrying"])

    def test_completed_order_is_never_retryable(self):
        task = self._order_task(status="completed")
        with (
            mock.patch.object(server, "AUTOMATION_QUEUE_MODE", "local"),
            mock.patch.object(server, "automation_queue_tasks", [task]),
        ):
            ok, message = server.retry_automation_queue_task(task["id"])

        self.assertFalse(ok)
        self.assertIn("canceled or failed", message)
        self.assertEqual(task["status"], "completed")

    def test_cancel_before_start_retains_order_for_retry(self):
        task = self._order_task(status="queued")
        tasks = [task]
        with (
            mock.patch.object(server, "AUTOMATION_QUEUE_MODE", "local"),
            mock.patch.object(server, "automation_queue_tasks", tasks),
            mock.patch.object(server, "log_automation_event"),
        ):
            ok, _message = server.cancel_automation_queue_task(task["id"])
            payload = server._automation_queue_task_payload(task)

        self.assertTrue(ok)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(task["status"], "canceled")
        self.assertTrue(payload["retryable"])


if __name__ == "__main__":
    unittest.main()
