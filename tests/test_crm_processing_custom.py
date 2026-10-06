import tempfile
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import server
from crm_list_url import normalize_custom_crm_list_url, normalize_custom_crm_order_ids, normalize_custom_crm_target
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workers"))
import crm_unlock_orders


LINK = "https://crm.example/app#reports/orders?status=locked&shippingCharges%5Blow%5D=0"
STEPS = ["address_validator_batch", "product_separator", "auto_splitter", "stock_unlocker", "order_goods", "shipping_bypasser", "push_back"]


class CustomCrmProcessingTests(unittest.TestCase):
    def test_order_list_accepts_common_separators_and_deduplicates_in_input_order(self):
        self.assertEqual(normalize_custom_crm_order_ids("2345678, 1234567\n2345678;3456789"), ["2345678", "1234567", "3456789"])
        self.assertEqual(normalize_custom_crm_order_ids([1234567, "2345678", "1234567"]), ["1234567", "2345678"])
        for value in (None, [], "", "123456", "12345678", "1234567, bad", [True], ["1234567", {}], [str(1000000 + i) for i in range(101)]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_custom_crm_order_ids(value)

    def test_custom_target_infers_new_order_input_and_preserves_old_link_requests(self):
        orders = {"custom_input_type": "orders", "custom_order_ids": ["1234567"]}
        self.assertEqual(normalize_custom_crm_target({"custom_order_ids": "1234567"}), orders)
        self.assertEqual(normalize_custom_crm_target({"custom_list_url": LINK}, orders), {"custom_input_type": "link", "custom_list_url": LINK})
        with self.assertRaises(ValueError):
            normalize_custom_crm_target({"custom_input_type": "orders", "custom_order_ids": "bad"}, {"custom_list_url": LINK})
        with self.assertRaises(ValueError):
            normalize_custom_crm_target({"custom_input_type": []})

    def test_link_validation_preserves_report_filters_and_rejects_unsafe_input(self):
        self.assertEqual(normalize_custom_crm_list_url(" " + LINK + " "), LINK)
        for value in (None, {}, "", "/report", "file:///report", "https://", "https://crm.example:bad/report",
                      "https://user:password@crm.example/report", "https://crm.example/\nreport", "https://crm.example\\report"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_custom_crm_list_url(value)

    def test_custom_preferences_are_separate_and_reject_empty_link_without_saving(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(Path(directory) / "state.json")):
            ok, _, state = server.update_crm_processing_preferences(
                processing_filter="custom", custom_list_url=LINK, shipping_bypasser_enabled=True,
                push_back_enabled=True, stock_unlocker_enabled=False,
            )
            self.assertTrue(ok)
            self.assertNotIn("stock_unlocker", server._crm_processing_selected_steps_from_state(state))
            self.assertIn("push_back", server._crm_processing_selected_steps_from_state(state))
            server.update_crm_processing_preferences(processing_filter="rush")
            ok, _, restored = server.update_crm_processing_preferences(processing_filter="custom")
            self.assertTrue(ok)
            self.assertEqual(restored["custom_list_url"], LINK)
            self.assertFalse(restored["stock_unlocker_enabled"])
            self.assertTrue(restored["mode_preferences"]["rush"]["stock_unlocker_enabled"])
            with mock.patch.object(server, "save_crm_processing_state") as save:
                ok, _, _ = server.update_crm_processing_preferences(processing_filter="custom", custom_list_url="")
            self.assertFalse(ok)
            save.assert_not_called()

    def _mock_workers(self, stack):
        for name in (
            "_start_crm_runtime", "_finish_crm_runtime", "_start_crm_address_runtime", "_finish_crm_address_runtime",
            "_start_crm_product_separator_runtime", "_finish_crm_product_separator_runtime",
            "_start_crm_auto_splitter_batch_runtime", "_finish_crm_auto_splitter_runtime",
            "_start_crm_order_goods_runtime", "_finish_crm_order_goods_runtime",
            "_start_crm_shipping_bypasser_runtime", "_finish_crm_shipping_bypasser_runtime",
            "_start_crm_push_back_runtime", "_finish_crm_push_back_runtime", "_notify_shipping_bypasser_problem_orders",
        ):
            stack.enter_context(mock.patch.object(server, name))
        for name in (
            "_persist_crm_run_result", "_persist_crm_address_run_result", "_persist_crm_product_separator_run_result",
            "_persist_crm_auto_splitter_run_result", "_persist_crm_order_goods_run_result",
            "_persist_crm_shipping_bypasser_run_result", "_persist_crm_push_back_run_result",
        ):
            stack.enter_context(mock.patch.object(server, name, return_value={}))
        stack.enter_context(mock.patch.object(server, "load_crm_address_state", return_value={"saved_batch_size": 0, "saved_parallel_workers": 1}))
        stack.enter_context(mock.patch.object(server, "_saved_crm_automation_parallel_workers", return_value=1))
        stack.enter_context(mock.patch.object(server, "_automation_stop_is_blocking", return_value=False))
        names = (
            "_run_crm_address_with_retry", "_execute_crm_product_separator_worker", "_execute_crm_auto_splitter_batch",
            "_run_crm_unlock_with_retry", "_execute_crm_order_goods_worker", "_execute_crm_shipping_bypasser_worker",
            "_execute_crm_push_back_worker",
        )
        return {step: stack.enter_context(mock.patch.object(server, name, return_value=(True, "Completed", {}))) for step, name in zip(STEPS, names)}

    def test_every_selected_worker_gets_custom_link_with_existing_worker_modes(self):
        state = {"processing_filter": "custom", "custom_list_url": LINK, **{key: True for key in server.CRM_PROCESSING_MODE_PREF_KEYS}}
        self.assertEqual(server._crm_processing_selected_steps_from_state(state), STEPS)
        with ExitStack() as stack:
            workers = self._mock_workers(stack)
            for step in STEPS:
                with self.subTest(step=step):
                    result = server._run_crm_processing_step(step, "custom", processing_state=state)
                    self.assertTrue(result["success"])
                    call = workers[step].call_args
                    link = call.args[0] if step == "auto_splitter" else call.kwargs["list_url"]
                    self.assertEqual(link, LINK)
            self.assertEqual(workers["address_validator_batch"].call_args.args[1], "all")
            self.assertEqual(workers["product_separator"].call_args.kwargs["list_mode"], "rush")
            self.assertEqual(workers["push_back"].call_args.kwargs["processing_filter"], "rush")

    def test_missing_custom_link_cannot_fall_back_to_configured_reports(self):
        with mock.patch.object(server, "_run_crm_unlock_with_retry") as worker:
            for step in STEPS:
                self.assertFalse(server._run_crm_processing_step(step, "custom", processing_state={})["success"])
        worker.assert_not_called()

    def test_explicit_list_targets_every_tool_without_needing_a_report_link(self):
        state = {"custom_input_type": "orders", "custom_order_ids": ["1234567", "2345678"], "custom_list_url": LINK}
        with ExitStack() as stack:
            workers = self._mock_workers(stack)
            stack.enter_context(mock.patch.object(server.config_module, "CRM_LOCKED_URL", "https://crm.example/report/locked"))
            splitter = stack.enter_context(mock.patch.object(server, "_run_crm_processing_auto_splitter_order", return_value={"success": True}))
            for step in STEPS:
                with self.subTest(step=step):
                    self.assertFalse(server._run_crm_processing_step(step, "custom", processing_state=state)["success"])
                    result = server._run_crm_processing_step(step, "custom", processing_state=state, target_order_id="1234567")
                    self.assertTrue(result["success"])
                    if step == "auto_splitter":
                        splitter.assert_called_once_with("1234567")
                        workers[step].assert_not_called()
                        continue
                    call = workers[step].call_args
                    order_id = call.args[0] if step == "address_validator_batch" else call.kwargs["order_id"]
                    self.assertEqual(order_id, "1234567")
                    if step == "stock_unlocker":
                        self.assertIn("_orderIds=1234567", call.kwargs["list_url"])
                        self.assertNotIn("shippingCharges", call.kwargs["list_url"])
                    else:
                        self.assertIsNone(call.kwargs["list_url"])

    def test_order_preferences_and_queue_snapshot_keep_both_inputs_independent(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(Path(directory) / "state.json")))
            stack.enter_context(mock.patch.object(server, "crm_lock"))
            stack.enter_context(mock.patch.object(server, "crm_processing_runtime", {}))
            stack.enter_context(mock.patch.object(server, "log_automation_event"))
            thread = stack.enter_context(mock.patch.object(server.threading, "Thread"))
            server.update_crm_processing_preferences(processing_filter="custom", custom_list_url=LINK)
            ok, _, state = server.update_crm_processing_preferences(processing_filter="custom", custom_order_ids="1234567,2345678,1234567")
            self.assertTrue(ok)
            self.assertEqual(state["custom_input_type"], "orders")
            self.assertEqual(state["custom_list_url"], LINK)
            ok, _ = server.start_crm_processing_run(processing_filter="custom", custom_order_ids=["3456789"], persist_preferences=False)
            self.assertTrue(ok)
            self.assertEqual(thread.call_args.kwargs["args"][3]["custom_order_ids"], ["3456789"])
            self.assertEqual(server.load_crm_processing_state()["custom_order_ids"], ["1234567", "2345678"])
            server.update_crm_processing_preferences(processing_filter="rush")
            ok, _, state = server.update_crm_processing_preferences(processing_filter="custom")
            self.assertTrue(ok)
            self.assertEqual(state["custom_input_type"], "orders")

    def test_order_list_continues_other_orders_stops_failed_order_and_retries_only_failures(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(Path(directory) / "state.json")))
            stack.enter_context(mock.patch.object(server, "crm_lock"))
            stack.enter_context(mock.patch.object(server, "crm_processing_runtime", {}))
            stack.enter_context(mock.patch.object(server, "_audit_result"))
            stack.enter_context(mock.patch.object(server, "_automation_stop_is_blocking", return_value=False))
            def run_step(step, mode, processing_state=None, target_order_id=None):
                return {"success": not (step == "address_validator_batch" and target_order_id == "1234567"), "message": "Needs review"}
            worker = stack.enter_context(mock.patch.object(server, "_run_crm_processing_step", side_effect=run_step))
            snapshot = {"custom_input_type": "orders", "custom_order_ids": ["1234567", "2345678"]}
            steps = ["address_validator_batch", "order_goods"]
            server._crm_processing_run_thread(steps, "custom", processing_state=snapshot)
            self.assertEqual([(call.args[0], call.kwargs["target_order_id"]) for call in worker.call_args_list], [
                ("address_validator_batch", "1234567"), ("address_validator_batch", "2345678"), ("order_goods", "2345678"),
            ])
            state = server.load_crm_processing_state()
            self.assertEqual(state["run_history"][0]["custom_order_ids"], snapshot["custom_order_ids"])
            report = {"step_results": state["last_step_results"]}
            plan = server._crm_processing_retry_plan(report)
            self.assertEqual(plan, {"address_validator_batch": ["1234567"], "order_goods": ["1234567"]})
            worker.reset_mock(side_effect=True)
            worker.return_value = {"success": True, "message": "Completed"}
            server._crm_processing_run_thread(steps, "custom", retry_plan=plan, processing_state=snapshot)
            self.assertEqual([call.kwargs["target_order_id"] for call in worker.call_args_list], ["1234567", "1234567"])

    def test_stopped_order_list_cannot_launch_a_worker(self):
        with mock.patch.object(server, "_run_crm_processing_step") as worker, mock.patch.object(server, "_automation_stop_is_blocking", return_value=True):
            result = server._run_crm_processing_order_list_step("order_goods", "custom", ["1234567"], blocked_orders={})
        self.assertFalse(result["success"])
        worker.assert_not_called()

    def test_custom_retries_keep_single_order_scope(self):
        with ExitStack() as stack:
            workers = self._mock_workers(stack)
            state = {"custom_list_url": LINK}
            server._run_crm_processing_retry_step("order_goods", "custom", ["1234567"], processing_state=state)
            self.assertEqual(workers["order_goods"].call_args.kwargs["order_id"], "1234567")
            self.assertIsNone(workers["order_goods"].call_args.kwargs["list_url"])
            server._run_crm_processing_retry_step("stock_unlocker", "custom", ["1234567"], processing_state=state)
            targeted = workers["stock_unlocker"].call_args.kwargs["list_url"]
            self.assertIn("1234567", targeted)
            self.assertIn("reports/orders", targeted)

    def test_start_passes_queued_link_snapshot_to_thread_without_overwriting_preferences(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(Path(directory) / "state.json")))
            stack.enter_context(mock.patch.object(server, "crm_lock"))
            stack.enter_context(mock.patch.object(server, "crm_processing_runtime", {}))
            stack.enter_context(mock.patch.object(server, "log_automation_event"))
            thread = stack.enter_context(mock.patch.object(server.threading, "Thread"))
            server.update_crm_processing_preferences(processing_filter="custom", custom_list_url="https://crm.example/report/current")
            ok, _ = server.start_crm_processing_run(processing_filter="custom", custom_list_url=LINK, persist_preferences=False)
            self.assertTrue(ok)
            self.assertEqual(thread.call_args.kwargs["args"][3]["custom_list_url"], LINK)
            self.assertEqual(server.load_crm_processing_state()["custom_list_url"], "https://crm.example/report/current")

    def test_thread_uses_snapshot_and_records_custom_history_and_report(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(Path(directory) / "state.json")))
            stack.enter_context(mock.patch.object(server, "crm_lock"))
            stack.enter_context(mock.patch.object(server, "crm_processing_runtime", {}))
            stack.enter_context(mock.patch.object(server, "_audit_result"))
            stack.enter_context(mock.patch.object(server, "_automation_stop_is_blocking", return_value=False))
            worker = stack.enter_context(mock.patch.object(server, "_run_crm_processing_step", return_value={
                "key": "order_goods", "success": True, "message": "Completed", "order_count": 2,
            }))
            server.update_crm_processing_preferences(processing_filter="custom", custom_list_url="https://crm.example/report/changed")
            snapshot = {"custom_list_url": LINK}
            server._crm_processing_run_thread(["order_goods"], "custom", processing_state=snapshot)
            worker.assert_called_once_with("order_goods", "custom", processing_state=snapshot)
            state = server.load_crm_processing_state()
            self.assertEqual(state["custom_list_url"], "https://crm.example/report/changed")
            self.assertEqual(state["run_history"][0]["custom_list_url"], LINK)
            self.assertEqual(state["last_filter_used"], "custom")
            report = server._build_crm_processing_report(state)
            self.assertEqual(report["filters"]["custom"]["all"]["total_orders_processed"], 2)


class TargetedUnlockerTests(unittest.TestCase):
    def test_unlocker_rejects_missing_or_extra_report_ids_before_selection(self):
        for ids, rows in (([], [object()]), (["2345678"], [object()]), (["1234567", "2345678"], [object(), object()])):
            with self.subTest(ids=ids), mock.patch.object(crm_unlock_orders, "_open_locked_report_rows", return_value=rows), mock.patch.object(crm_unlock_orders, "_collect_order_ids", return_value=ids), mock.patch.object(crm_unlock_orders, "select_all_orders_with_preview") as select:
                result = crm_unlock_orders.unlock_single_order_with_driver(mock.Mock(), "1234567", list_url=LINK)
            self.assertFalse(result["success"])
            select.assert_not_called()

    def test_targeted_unlocker_dry_run_skips_apply(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_unlock_orders, "_open_locked_report_rows", return_value=[object()]))
            stack.enter_context(mock.patch.object(crm_unlock_orders, "_collect_order_ids", return_value=["1234567"]))
            stack.enter_context(mock.patch.object(crm_unlock_orders, "select_all_orders_with_preview", return_value=(1, object())))
            stack.enter_context(mock.patch.object(crm_unlock_orders, "choose_unlock_status"))
            stack.enter_context(mock.patch.object(crm_unlock_orders, "get_apply_button"))
            apply = stack.enter_context(mock.patch.object(crm_unlock_orders, "click_apply"))
            result = crm_unlock_orders.unlock_single_order_with_driver(mock.Mock(), "1234567", list_url=LINK, dry_run=True)
        self.assertTrue(result["success"])
        apply.assert_not_called()

    def test_unlocker_worker_runs_single_order_path_and_preserves_failure_result(self):
        with ExitStack() as stack:
            for name in ("kill_stale_chrome", "build_chrome_driver", "safe_driver_quit", "_validate_runtime_config"):
                stack.enter_context(mock.patch.object(crm_unlock_orders, name))
            batch = stack.enter_context(mock.patch.object(crm_unlock_orders, "_open_locked_report_rows"))
            stack.enter_context(mock.patch.object(crm_unlock_orders, "_crm_attempt_modes", return_value=[True]))
            stack.enter_context(mock.patch.object(crm_unlock_orders, "unlock_single_order_with_driver", return_value={"success": False, "message": "Wrong order", "order_id": "1234567"}))
            write = stack.enter_context(mock.patch.object(crm_unlock_orders, "write_result_payload"))
            code = crm_unlock_orders.run("unlock_all", order_id="1234567", list_url=LINK)
        self.assertEqual(code, 1)
        self.assertFalse(write.call_args.args[2])
        self.assertEqual(write.call_args.kwargs["extra_fields"]["order_ids"], ["1234567"])
        batch.assert_not_called()

    def test_server_passes_order_id_to_unlocker_subprocess(self):
        with mock.patch.object(server, "_run_script", return_value=(True, "Completed", {})) as run:
            server._execute_crm_worker(order_id="1234567")
        self.assertIn("--order-id", run.call_args.args[1])
        self.assertIn("1234567", run.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
