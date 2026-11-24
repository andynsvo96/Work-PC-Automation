"""FREE/ALL report selection and queue snapshots, without running workers."""

import inspect
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from flask import Flask

import server
import test_crm_processing_custom as custom_tests
from routes.work_routes import register_work_routes


class SubcontractorProcessingTests(unittest.TestCase):
    def test_preferences_survive_mode_switches_and_reload_independently(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            server, "CRM_PROCESSING_STATE_FILE", str(Path(directory) / "processing.json")
        ):
            for mode in ("free", "all"):
                ok, _, state = server.update_crm_processing_preferences(
                    processing_filter=mode, shipping_bypasser_enabled=True,
                    push_back_enabled=True, stock_unlocker_enabled=False,
                    order_goods_enabled=False,
                )
                self.assertTrue(ok)
                self.assertEqual(server._crm_processing_selected_steps_from_state(state)[-2:], ["shipping_bypasser", "push_back"])
            server.update_crm_processing_preferences(processing_filter="rush")
            for mode in ("free", "all"):
                ok, _, _ = server.update_crm_processing_preferences(processing_filter=mode)
                restored = server.load_crm_processing_state()
                self.assertTrue(ok)
                self.assertTrue(restored["shipping_bypasser_enabled"])
                self.assertTrue(restored["push_back_enabled"])
                self.assertFalse(restored["stock_unlocker_enabled"])
                self.assertFalse(restored["order_goods_enabled"])
            self.assertFalse(restored["mode_preferences"]["rush"]["push_back_enabled"])
            self.assertFalse(restored["mode_preferences"]["813"]["stock_unlocker_enabled"])

    def test_selected_tools_use_only_their_mode_specific_report(self):
        for mode in ("free", "all"):
            urls = {
                step: f"https://crm.example/report/{mode}/{step}"
                for step in ("stock_unlocker", "order_goods", "shipping_bypasser", "push_back")
            }
            with self.subTest(mode=mode), ExitStack() as stack:
                workers = custom_tests.CustomCrmProcessingTests()._mock_workers(stack)
                for step, url in urls.items():
                    key = server._crm_processing_mode_url_config_key_for_step(mode, step)
                    stack.enter_context(mock.patch.object(server, key, url))
                for step, url in urls.items():
                    result = server._run_crm_processing_step(step, mode)
                    self.assertTrue(result["success"], result)
                    self.assertEqual(workers[step].call_args.kwargs["list_url"], url)
                self.assertEqual(workers["push_back"].call_args.kwargs["processing_filter"], mode)

    def test_missing_report_fails_before_dispatch_instead_of_using_rush(self):
        for mode in ("free", "all"):
            for step in ("shipping_bypasser", "push_back"):
                key = server._crm_processing_mode_url_config_key_for_step(mode, step)
                with self.subTest(mode=mode, step=step), ExitStack() as stack:
                    workers = custom_tests.CustomCrmProcessingTests()._mock_workers(stack)
                    stack.enter_context(mock.patch.object(server, key, ""))
                    result = server._run_crm_processing_step(step, mode)
                    self.assertFalse(result["success"])
                    self.assertIn(key + " is empty", result["message"])
                    workers[step].assert_not_called()

    def test_push_back_subprocess_accepts_free_and_all(self):
        for mode in ("free", "all"):
            with self.subTest(mode=mode), mock.patch.object(
                server, "_run_script", return_value=(True, "Completed", {})
            ) as run:
                ok, _, payload = server._execute_crm_push_back_worker(
                    processing_filter=mode, list_url=f"https://crm.example/report/{mode}", dry_run=True,
                )
            self.assertTrue(ok)
            args = run.call_args.args[1]
            self.assertEqual(args[args.index("--processing-filter") + 1], mode)
            self.assertIn("--dry-run", args)
            self.assertEqual(payload["shipping_filter"], mode)

    def test_queue_schedule_and_repeat_keep_both_tools_and_mode(self):
        # Register the real routes against an isolated app. The mock queue never
        # starts a thread, restores schedules, or touches persistent live state.
        app = Flask(__name__)
        deps = {
            name: mock.Mock(return_value={})
            for name in inspect.signature(register_work_routes).parameters
            if name != "app"
        }
        queue = mock.Mock(return_value=(True, "Queued", {"id": "fixture"}))
        deps.update(enqueue_automation=queue, is_trueish=bool)
        deps["get_crm_processing_state_payload"].return_value = {"state": {}}
        register_work_routes(app, **deps)
        for mode in ("free", "all"):
            for endpoint in ("/crm/process", f"/crm/process/{mode}"):
                with self.subTest(mode=mode, endpoint=endpoint):
                    response = app.test_client().post(endpoint, json={
                        "processing_filter": mode,
                        "address_validator_enabled": False, "product_separator_enabled": False,
                        "auto_splitter_enabled": False, "stock_unlocker_enabled": False,
                        "order_goods_enabled": False, "shipping_bypasser_enabled": True,
                        "push_back_enabled": True, "advanced_mode": "scheduled_repeat",
                        "scheduled_time": "2026-11-24T18:30", "repeat_interval_minutes": 15,
                    })
                    self.assertEqual(response.status_code, 202)
                    call = queue.call_args
                    self.assertIn("Shipping Bypasser, Push Back", call.args[0])
                    options = call.kwargs
                    self.assertEqual(options["task_type"], "crm.processing")
                    self.assertEqual(options["task_arguments"]["processing_filter"], mode)
                    self.assertTrue(options["task_arguments"]["shipping_bypasser_enabled"])
                    self.assertTrue(options["task_arguments"]["push_back_enabled"])
                    self.assertEqual(options["automation_signature"]["steps"], ["shipping_bypasser", "push_back"])
                    self.assertEqual(options["automation_signature"]["mode"], mode)
                    self.assertEqual(options["queue_mode"], "repeat")
                    self.assertEqual(options["repeat_interval_minutes"], 15)
                    self.assertEqual(options["scheduled_for"], "2026-11-24T18:30")
        deps["run_crm_processing_run_queued"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
