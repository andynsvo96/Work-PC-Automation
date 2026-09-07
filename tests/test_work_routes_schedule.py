import inspect
import unittest
from unittest import mock

from flask import Flask

from routes.work_routes import register_work_routes


class WorkRouteScheduleTests(unittest.TestCase):
    def _app_with_captured_queue(self, **overrides):
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
        kwargs.update(overrides)
        register_work_routes(app, **kwargs)
        return app, captured

    def test_hours_target_preview_and_schedule(self):
        preview = {"success": True, "scope": "week", "target_hours": 35,
                   "scheduled_for": "2026-09-07T14:30:00", "clock_in_at": "2026-09-07T08:00:00",
                   "message": "35 paid hours this week"}
        run = mock.Mock()
        app, captured = self._app_with_captured_queue(preview_work_hours_target=mock.Mock(return_value=preview), run_work=run)
        client = app.test_client()
        response = client.post("/work/hours-target", json={"scope": "week", "target_hours": 35})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, {})
        response = client.post("/work/hours-target", json={"scope": "week", "target_hours": 35, "schedule": True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["scheduled_for"], preview["scheduled_for"])
        self.assertEqual(captured["queue_mode"], "scheduled")
        captured["fn"]()
        run.assert_called_once_with("out", automatic=False, expected_clock_in_at=preview["clock_in_at"])

    def test_invalid_hours_target_does_not_queue(self):
        app, captured = self._app_with_captured_queue(preview_work_hours_target=mock.Mock(return_value={"success": False, "message": "Already reached"}))
        response = app.test_client().post("/work/hours-target", json={"scope": "today", "target_hours": 2, "schedule": True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(captured, {})

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

    def test_work_in_accepts_scheduled_queue_controls(self):
        app, captured = self._app_with_captured_queue()
        response = app.test_client().post(
            "/work/in",
            json={"advanced_mode": "scheduled", "scheduled_time": "2026-08-07T08:30:00"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["queue_mode"], "scheduled")
        self.assertEqual(captured["scheduled_for"], "2026-08-07T08:30:00")

    def test_lunch_accepts_scheduled_queue_controls(self):
        app, captured = self._app_with_captured_queue()
        response = app.test_client().post(
            "/slack/lunch",
            json={"advanced_mode": "scheduled", "scheduled_time": "2026-08-07T12:00:00"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(captured["label"], "Slack Lunch Start")
        self.assertEqual(captured["queue_mode"], "scheduled")
        self.assertEqual(captured["scheduled_for"], "2026-08-07T12:00:00")


if __name__ == "__main__":
    unittest.main()
