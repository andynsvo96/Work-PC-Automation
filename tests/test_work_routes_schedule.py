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

    def test_custom_processing_snapshots_link_and_all_selected_tools(self):
        run = mock.Mock()
        state = {"state": {"custom_list_url": "https://crm.example/app#reports?list=original"}}
        app, captured = self._app_with_captured_queue(
            run_crm_processing_run_queued=run,
            get_crm_processing_state_payload=lambda: state,
        )
        response = app.test_client().post("/crm/process", json={
            "processing_filter": "custom", "shipping_bypasser_enabled": True, "push_back_enabled": True,
            "advanced_mode": "repeat", "repeat_interval_minutes": 0,
        })
        self.assertEqual(response.status_code, 202)
        self.assertIn("Custom", captured["label"])
        self.assertEqual(len(captured["automation_signature"]["steps"]), 7)
        self.assertEqual(captured["repeat_interval_minutes"], 0)
        link = captured["task_arguments"]["custom_list_url"]
        self.assertEqual(captured["automation_signature"]["custom_list_url"], link)
        state["state"]["custom_list_url"] = "https://crm.example/report/changed"
        captured["fn"]()
        self.assertEqual(run.call_args.kwargs["custom_list_url"], link)

    def test_custom_processing_signatures_distinguish_links_and_keep_schedule(self):
        app, captured = self._app_with_captured_queue()
        signatures = []
        for link in ("https://crm.example/report/one", "https://crm.example/report/two"):
            response = app.test_client().post("/crm/process/custom", json={
                "custom_list_url": link, "advanced_mode": "scheduled", "scheduled_time": "2026-10-07T09:00:00",
            })
            self.assertEqual(response.status_code, 202)
            self.assertEqual(captured["task_arguments"]["custom_list_url"], link)
            self.assertEqual(captured["scheduled_for"], "2026-10-07T09:00:00")
            signatures.append(captured["automation_signature"])
        self.assertNotEqual(*signatures)

    def test_custom_processing_rejects_missing_and_invalid_links_before_queue(self):
        app, captured = self._app_with_captured_queue()
        for link in (None, "", "not a link", "javascript:alert(1)", "https://user:password@crm.example/report"):
            with self.subTest(link=link):
                response = app.test_client().post("/crm/process", json={"processing_filter": "custom", "custom_list_url": link})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["success"])
                self.assertEqual(captured, {})

    def test_standard_processing_ignores_custom_link(self):
        app, captured = self._app_with_captured_queue()
        response = app.test_client().post("/crm/process/free", json={"custom_list_url": "https://crm.example/report/custom"})
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("custom_list_url", captured["task_arguments"])
        self.assertNotIn("custom_list_url", captured["automation_signature"])

    def test_custom_order_list_snapshots_ids_and_deduplicates_for_scheduled_runs(self):
        run = mock.Mock()
        app, captured = self._app_with_captured_queue(run_crm_processing_run_queued=run)
        response = app.test_client().post("/crm/process/custom", json={
            "custom_order_ids": "2345678,1234567\n2345678", "advanced_mode": "scheduled", "scheduled_time": "2026-10-07T09:00:00",
        })
        self.assertEqual(response.status_code, 202)
        args = captured["task_arguments"]
        self.assertEqual(args["custom_input_type"], "orders")
        self.assertEqual(args["custom_order_ids"], ["2345678", "1234567"])
        self.assertEqual(captured["automation_signature"]["custom_order_ids"], args["custom_order_ids"])
        self.assertNotIn("custom_list_url", args)
        captured["fn"]()
        self.assertEqual(run.call_args.kwargs["custom_order_ids"], args["custom_order_ids"])

    def test_order_list_is_not_replaced_by_saved_link_after_invalid_input(self):
        app, captured = self._app_with_captured_queue(get_crm_processing_state_payload=lambda: {"state": {"custom_list_url": "https://crm.example/report"}})
        for orders in ("", "1234567, bad", [], [True]):
            response = app.test_client().post("/crm/process", json={"processing_filter": "custom", "custom_order_ids": orders})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(captured, {})

    def test_saved_order_list_is_used_and_distinct_lists_have_distinct_signatures(self):
        state = {"custom_input_type": "orders", "custom_order_ids": ["1234567"]}
        app, captured = self._app_with_captured_queue(get_crm_processing_state_payload=lambda: {"state": state})
        app.test_client().post("/crm/process", json={"processing_filter": "custom"})
        original = captured["automation_signature"]
        state["custom_order_ids"] = ["2345678"]
        self.assertEqual(captured["task_arguments"]["custom_order_ids"], ["1234567"])
        app.test_client().post("/crm/process", json={"processing_filter": "custom"})
        self.assertNotEqual(original, captured["automation_signature"])


if __name__ == "__main__":
    unittest.main()
