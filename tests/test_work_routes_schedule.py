import inspect
import unittest
from unittest import mock

from flask import Flask

from routes.work_routes import register_work_routes


class WorkRouteScheduleTests(unittest.TestCase):
    def _app_with_captured_queue(self):
        app = Flask(__name__)
        captured = {}

        def enqueue(label, category, fn, **kwargs):
            captured.update({"label": label, "category": category, "fn": fn, **kwargs})
            return True, "Queued", {"id": "task-1"}

        parameters = inspect.signature(register_work_routes).parameters
        kwargs = {}
        for name in parameters:
            if name == "app":
                continue
            kwargs[name] = mock.Mock(return_value={})
        kwargs.update(
            enqueue_automation=enqueue,
            automation_test_catalog=[],
            is_trueish=lambda value: str(value or "").lower() in {"1", "true", "yes", "on"},
            get_crm_mass_emailer_status_payload=lambda: {"state": {}, "runtime": {}, "running": False},
            get_crm_processing_state_payload=lambda: {"state": {}},
        )
        register_work_routes(app, **kwargs)
        return app, captured

    def test_sheet_scanner_accepts_scheduled_queue_controls(self):
        app, captured = self._app_with_captured_queue()
        response = app.test_client().post(
            "/crm/mass-emailer",
            json={"advanced_mode": "scheduled", "scheduled_time": "2026-08-07T15:30:00"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["queue_mode"], "scheduled")
        self.assertEqual(captured["scheduled_for"], "2026-08-07T15:30:00")
        self.assertEqual(captured["automation_signature"]["type"], "crm_mass_emailer")
        self.assertNotIn("advanced_mode", captured["task_arguments"])

    def test_sheet_scanner_accepts_repeat_queue_controls(self):
        app, captured = self._app_with_captured_queue()
        response = app.test_client().post(
            "/crm/mass-emailer",
            json={"advanced_mode": "repeat", "repeat_interval_minutes": 12},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["queue_mode"], "repeat")
        self.assertEqual(captured["repeat_interval_minutes"], 12)
        self.assertIn("Repeat every 12 minutes", captured["advanced_summary"])


if __name__ == "__main__":
    unittest.main()
