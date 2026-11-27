"""Subcontractor report selection and queue snapshots, without running workers."""

import ast
import inspect
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
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

    def test_high_value_unlocker_prefers_its_override_and_keeps_the_rush_default(self):
        rush = "https://crm.example/report/rush-unlock"
        high_value = "https://crm.example/report/high-value-unlock"
        with ExitStack() as stack:
            unlocker = custom_tests.CustomCrmProcessingTests()._mock_workers(stack)["stock_unlocker"]
            stack.enter_context(mock.patch.object(server.config_module, "CRM_LOCKED_URL", rush, create=True))
            with mock.patch.object(server, "CRM_UNLOCKER_HIGH_VALUE_URL", high_value):
                self.assertTrue(server._run_crm_processing_step("stock_unlocker", "high_value")["success"])
                self.assertEqual(unlocker.call_args.kwargs["list_url"], high_value)
                self.assertTrue(server._run_crm_processing_step("stock_unlocker", "rush")["success"])
                self.assertEqual(unlocker.call_args.kwargs["list_url"], rush)
            with mock.patch.object(server, "CRM_UNLOCKER_HIGH_VALUE_URL", ""):
                self.assertTrue(server._run_crm_processing_step("stock_unlocker", "high_value")["success"])
                self.assertEqual(unlocker.call_args.kwargs["list_url"], rush)

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


class RushContractedProcessingTests(unittest.TestCase):
    def test_contracted_reports_only_override_selected_rush_tools(self):
        with ExitStack() as stack:
            workers = custom_tests.CustomCrmProcessingTests()._mock_workers(stack)
            regular_unlocker = "https://crm.example/regular/unlock"
            stack.enter_context(mock.patch.object(server.config_module, "CRM_LOCKED_URL", regular_unlocker, create=True))
            for step, (_, key) in server.CRM_RUSH_REPORT_KEYS.items():
                with self.subTest(step=step):
                    url = f"https://crm.example/contracted/{step}"
                    with mock.patch.object(server.config_module, key, url, create=True):
                        result = server._run_crm_processing_step(step, "rush", {"rush_contracted_steps": [step]})
                        self.assertTrue(result["success"], result)
                        self.assertEqual(workers[step].call_args.kwargs["list_url"], url)
                    workers[step].reset_mock()
                    with mock.patch.object(server.config_module, key, "", create=True):
                        result = server._run_crm_processing_step(step, "rush", {"rush_contracted_steps": [step]})
                        self.assertFalse(result["success"])
                        self.assertIn("Contracted Orders link", result["message"])
                        workers[step].assert_not_called()
            result = server._run_crm_processing_step("stock_unlocker", "rush", {"rush_contracted_steps": ["order_goods"]})
            self.assertTrue(result["success"])
            self.assertEqual(workers["stock_unlocker"].call_args.kwargs["list_url"], regular_unlocker)
            url = "https://crm.example/contracted/unlock"
            with mock.patch.object(server.config_module, "CRM_UNLOCKER_RUSH_CONTRACTED_URL", url, create=True):
                server._run_crm_processing_retry_step("stock_unlocker", "rush", ["1234567"], {"rush_contracted_steps": ["stock_unlocker"]})
                self.assertTrue(workers["stock_unlocker"].call_args.kwargs["list_url"].startswith(url + "?"))
                self.assertEqual(workers["stock_unlocker"].call_args.kwargs["order_id"], "1234567")

    def test_saved_choices_and_legacy_queued_runs_use_independent_selections(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(Path(directory) / "state.json")))
            stack.enter_context(mock.patch.object(server, "crm_lock"))
            stack.enter_context(mock.patch.object(server, "crm_processing_runtime", {}))
            stack.enter_context(mock.patch.object(server, "log_automation_event"))
            thread = stack.enter_context(mock.patch.object(server.threading, "Thread"))
            server.update_crm_processing_preferences(processing_filter="rush", rush_contracted_steps=["stock_unlocker"])
            _, _, state = server.update_crm_processing_preferences(processing_filter="free")
            self.assertEqual(state["rush_contracted_steps"], [])
            _, _, state = server.update_crm_processing_preferences(processing_filter="rush")
            self.assertEqual(state["rush_contracted_steps"], ["stock_unlocker"])
            for options, expected in (({}, []), ({"rush_contracted_steps": ["order_goods"]}, ["order_goods"])):
                ok, _ = server.start_crm_processing_run(processing_filter="rush", persist_preferences=False, **options)
                self.assertTrue(ok)
                self.assertEqual(thread.call_args.kwargs["args"][3]["rush_contracted_steps"], expected)
            self.assertEqual(server.load_crm_processing_state()["rush_contracted_steps"], ["stock_unlocker"])
            _, _, state = server.update_crm_processing_preferences(stock_unlocker_enabled=False)
            self.assertEqual(state["rush_contracted_steps"], [])

    def test_queued_rush_choice_is_in_the_task_and_signature(self):
        app = Flask(__name__)
        deps = {name: mock.Mock(return_value={}) for name in inspect.signature(register_work_routes).parameters if name != "app"}
        queue = mock.Mock(return_value=(True, "Queued", {"id": "fixture"}))
        deps.update(enqueue_automation=queue, is_trueish=bool)
        register_work_routes(app, **deps)
        for steps in (["push_back"], []):
            response = app.test_client().post("/crm/process/rush", json={
                "push_back_enabled": True, "rush_contracted_steps": steps,
                "advanced_mode": "scheduled_repeat", "scheduled_time": "2026-11-24T18:30",
            })
            self.assertEqual(response.status_code, 202)
            self.assertEqual(queue.call_args.kwargs["task_arguments"]["rush_contracted_steps"], steps)
            self.assertEqual(queue.call_args.kwargs["automation_signature"].get("rush_contracted_steps", []), steps)
            self.assertEqual("Push Back (Contracted)" in queue.call_args.args[0], bool(steps))
            self.assertEqual(queue.call_args.kwargs["queue_mode"], "repeat")

    def test_link_settings_discover_and_save_keys_missing_from_local_config(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "config.py"
            source = 'CRM_PUSH_BACK_RUSH_URL = "https://crm.example/regular"\n'
            path.write_text(source, encoding="utf-8")
            stack.enter_context(mock.patch.object(server, "CONFIG_FILE", path))
            stack.enter_context(mock.patch.object(server, "config_module", SimpleNamespace(CRM_PUSH_BACK_RUSH_URL="https://crm.example/regular")))
            stack.enter_context(mock.patch.object(server.importlib, "reload"))
            stack.enter_context(mock.patch.object(server, "_apply_runtime_config_from_module"))
            stack.enter_context(mock.patch.object(server, "cancel_auto_clock_out_timer"))
            stack.enter_context(mock.patch.object(server, "restore_auto_clock_out_timer_from_state"))
            groups = server.get_config_ui_payload()["groups"]
            fields = {item["key"]: item["value"] for group in (groups["crm"], groups["other"]) for item in group}
            self.assertEqual(set(fields), set(server.CRM_LINK_SETTING_KEYS))
            self.assertEqual(fields["CRM_PUSH_BACK_RUSH_URL"], "https://crm.example/regular")
            new_links = {
                "CRM_PUSH_BACK_RUSH_CONTRACTED_URL": "https://crm.example/contracted",
                "CRM_SHIPPING_BYPASS_FREE_URL": "https://crm.example/free-bypass",
                "CRM_PUSH_BACK_FREE_URL": "https://crm.example/free-push-back",
                "CRM_SHIPPING_BYPASS_ALL_URL": "https://crm.example/all-bypass",
                "CRM_PUSH_BACK_ALL_URL": "https://crm.example/all-push-back",
                "CRM_UNLOCKER_HIGH_VALUE_URL": "https://crm.example/high-value-unlock",
            }
            self.assertTrue(all(fields[key] == "" for key in new_links))
            fields.update(new_links)
            ok, message = server.update_config_values(fields)
            self.assertTrue(ok, message)
            saved = ast.parse(path.read_text(encoding="utf-8"))
            self.assertEqual(
                {node.targets[0].id: ast.literal_eval(node.value) for node in saved.body},
                {"CRM_PUSH_BACK_RUSH_URL": "https://crm.example/regular", **new_links},
            )


if __name__ == "__main__":
    unittest.main()
