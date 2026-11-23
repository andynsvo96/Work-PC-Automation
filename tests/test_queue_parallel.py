"""Offline queue scheduling, isolation, cancellation, and per-order status."""

import json
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import server
from queue_order_work import PARALLEL_ORDER_WORKFLOWS, parallel_order_request, worker_invocation
from runtime_paths import DEBUG_DIR


class ParallelOrderRequestTests(unittest.TestCase):
    def task(self, automation="copyright_cancel", order_id="1000001", **updates):
        task = {"queue_mode": "normal", "execution_descriptor": {"kind": "crm.extension_manual", "arguments": {
            "automation_key": automation, "order_id": order_id, "reason": "Original reason",
            "request_data": {"designs": {"order_id": order_id, "selected_design_ids": ["order-design:12"],
                                          "all_designs": [{"order_design_id": "12"}]}}
        }}}
        task.update(updates)
        return task

    def test_only_explicit_supported_single_order_requests_are_eligible(self):
        self.assertEqual(parallel_order_request(self.task(order_id=1000001))["order_id"], "1000001")
        for automation in PARALLEL_ORDER_WORKFLOWS:
            with self.subTest(automation=automation):
                self.assertIsNotNone(parallel_order_request(self.task(automation)))
        for automation in ("auto_splitter", "shipping_bypasser", "stock_issue_size", "stock_swap", "sleeve_prints"):
            self.assertIsNone(parallel_order_request(self.task(automation)))
        for task in ({}, self.task(order_id="https://example.com/report/1000001"), self.task(queue_mode="repeat"),
                     self.task(restore_error="Interrupted"), {"task_type": "crm.processing", "task_arguments": {"order_id": "1000001"}},
                     {"task_arguments": ["invalid"]}, self.task(execution_descriptor={"kind": "crm.extension_manual", "arguments": []}),
                     {"task_type": "address_validator", "task_arguments": {"order_id": "1000001"}}):
            self.assertIsNone(parallel_order_request(task))

    def test_legacy_explicit_orders_supported_but_sheet_edits_and_batches_are_barriers(self):
        task = {"task_type": "crm.sheet_scanner_order", "task_arguments": {
            "order_id": "1000001", "process": "copyright_reachout", "reason": "Reason"}}
        self.assertEqual(parallel_order_request(task)["automation_key"], "copyright_reachout")
        task["task_arguments"]["delete_sheet_row"] = True
        self.assertIsNone(parallel_order_request(task))
        self.assertIsNone(parallel_order_request({"task_type": "crm.address_validator", "task_arguments": {
            "order_id": "1000001", "action": "validate_batch"}}))
        self.assertIsNone(parallel_order_request({"task_type": "crm.order_goods", "task_arguments": {
            "order_id": "1000001", "dry_run": True}}))

    def test_copyright_and_emb_invocations_preserve_selections_and_exact_order(self):
        for automation, flag in (("copyright_cancel", "--copyright-designs-json"), ("complicated_emb_feedback", "--emb-designs-json"),
                                 ("oversize_emb_to_hdd", "--emb-designs-json")):
            original = self.task(automation)
            request = parallel_order_request(original)
            filename, args = worker_invocation(request, "isolated-profile", "isolated-result.json")
            self.assertEqual(filename, "crm_copyright_cancel.py")
            self.assertEqual(args[args.index("--action") + 1], "process_order")
            self.assertEqual(args[args.index("--order-id") + 1], "1000001")
            self.assertEqual(json.loads(args[args.index(flag) + 1]), request["request_data"]["designs"])
            request["request_data"]["designs"]["selected_design_ids"].clear()
            self.assertEqual(original["execution_descriptor"]["arguments"]["request_data"]["designs"]["selected_design_ids"], ["order-design:12"])

    def test_manual_invocations_are_targeted_and_cannot_spawn_nested_pools(self):
        for automation in ("address_validator", "product_separator", "order_goods", "push_back"):
            filename, args = worker_invocation(parallel_order_request(self.task(automation)), "profile", "result.json")
            self.assertEqual(filename, f"crm_{'validate_address' if automation == 'address_validator' else automation}.py")
            self.assertIn("--order-id", args)
            self.assertNotIn("--list-url", args)
            self.assertNotIn("--parallel-workers", args)
            if automation == "address_validator":
                self.assertEqual(args[args.index("--shipping-filter") + 1], "free")
        with self.assertRaises(ValueError):
            worker_invocation({"automation_key": "copyright_cancel", "order_id": "report"}, "profile", "result")


class ParallelQueueTests(unittest.TestCase):
    def setUp(self):
        self.real_queue_profiles = server._queue_order_profiles
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.temp = tempfile.TemporaryDirectory(prefix="parallel-queue-", dir=DEBUG_DIR)
        self.addCleanup(self.temp.cleanup)
        for key, value in {
            "automation_queue_tasks": [], "automation_queue_current_task_id": None,
            "automation_queue_persistence_enabled": False, "automation_queue_persistence_error": None,
            "automation_stop_requested_at": 0, "automation_stop_block_until": 0,
            "crm_lock": threading.Lock(), "active_automation_processes": {}, "force_stopped_process_pids": set(),
        }.items():
            self.patches.enter_context(mock.patch.object(server, key, value))
        for key, value in {"_ensure_automation_queue_worker": None, "log_automation_event": None,
                           "_automation_version_block_reason": "", "_persist_queue_order_result": None,
                           "_saved_crm_automation_parallel_workers": 2}.items():
            self.patches.enter_context(mock.patch.object(server, key, return_value=value))
        self.patches.enter_context(mock.patch.object(server, "_queue_order_profiles", return_value=("crm-slot", "slack-slot")))
        self.patches.enter_context(mock.patch.object(server, "_report_salesforce_email_result", side_effect=lambda *args: args))
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.started = {}
        self.releases = {}
        self.errors = []
        self.pool_thread = None
        self.addCleanup(self.cleanup_pool)

    def cleanup_pool(self):
        self.release.set()
        for event in list(self.releases.values()):
            event.set()
        if self.pool_thread:
            self.pool_thread.join(10)
            self.assertFalse(self.pool_thread.is_alive(), "Pool did not drain")

    def enqueue(self, order_id, automation="copyright_reachout"):
        task_type = server.CRM_EXTENSION_MANUAL_ORDER_AUTOMATIONS[automation]["task_type"]
        ok, message, payload = server.enqueue_automation(
            f"{automation} Order {order_id}", "Processing", lambda: self.fail("Pool used the shared legacy runner"),
            task_type=task_type, task_arguments={"order_id": order_id, "process": automation},
            execution_descriptor={"kind": "crm.extension_manual", "arguments": {
                "order_id": order_id, "automation_key": automation, "reason": "Reason", "request_data": None}},
        )
        self.assertTrue(ok, message)
        return next(task for task in server.automation_queue_tasks if task["id"] == payload["id"])

    def start_pool(self, runner):
        self.patches.enter_context(mock.patch.object(server, "_run_script", side_effect=runner))
        def run():
            try:
                server._run_automation_queue_order_pool()
            except BaseException as exc:
                self.errors.append(exc)
        self.pool_thread = threading.Thread(target=run)
        self.pool_thread.start()

    def controlled_runner(self, _script, args, _label, **kwargs):
        request = json.loads(args[args.index("--request-json") + 1])
        order_id = request["order_id"]
        self.started.setdefault(order_id, threading.Event()).set()
        event = self.releases.setdefault(order_id, threading.Event())
        deadline = time.monotonic() + 8
        while not self.release.is_set() and not event.wait(0.02):
            if time.monotonic() > deadline:
                raise TimeoutError("Test worker was not released")
        return True, f"Completed {order_id}", {"order_id": order_id, "salesforce": {"sent": True}}

    def wait_started(self, order_id):
        event = self.started.setdefault(order_id, threading.Event())
        self.assertTrue(event.wait(5), f"Order {order_id} never started: {self.errors}")

    def test_new_arrivals_fill_free_slots_without_waiting_for_running_orders(self):
        first = self.enqueue("1000001")
        self.start_pool(self.controlled_runner)
        self.wait_started("1000001")
        self.assertFalse(server.crm_lock.acquire(blocking=False), "Pool must own the existing CRM lock")
        second = self.enqueue("1000002", "address_validator")
        self.wait_started("1000002")
        self.assertEqual(server.get_automation_queue_payload()["running_count"], 2)
        self.assertEqual({task["id"] for task in server.get_automation_queue_payload()["running_tasks"]}, {first["id"], second["id"]})
        third = self.enqueue("1000003")
        self.assertFalse(self.started.setdefault("1000003", threading.Event()).wait(0.1))
        second_slot = second["worker_slot"]
        self.releases["1000002"].set()
        self.wait_started("1000003")
        self.assertEqual(third["worker_slot"], second_slot)
        self.assertEqual(first["status"], "running")
        self.cleanup_pool()
        self.assertFalse(self.errors)
        self.assertTrue(all(task["status"] == "completed" for task in (first, second, third)))
        self.assertEqual(first["result_context"]["report"]["order_details"][0]["order_id"], "1000001")

    def test_incompatible_head_is_a_barrier_even_with_free_slots(self):
        first = self.enqueue("1000001")
        self.start_pool(self.controlled_runner)
        self.wait_started("1000001")
        barrier = self.enqueue("1000002", "shipping_bypasser")
        later = self.enqueue("1000003")
        self.assertFalse(self.started.setdefault("1000003", threading.Event()).wait(0.15))
        self.cleanup_pool()
        self.assertEqual(first["status"], "completed")
        self.assertEqual(barrier["status"], "queued")
        self.assertEqual(later["status"], "queued")

    def test_same_order_cannot_overlap_or_let_later_work_overtake_it(self):
        first = self.enqueue("1000001")
        self.start_pool(self.controlled_runner)
        self.wait_started("1000001")
        second = self.enqueue("1000001", "order_goods")
        later = self.enqueue("1000002")
        self.assertFalse(self.started.setdefault("1000002", threading.Event()).wait(0.15))
        self.assertEqual(second["status"], "queued")
        self.releases["1000001"].set()
        self.wait_started("1000002")
        self.cleanup_pool()
        self.assertTrue(all(task["status"] == "completed" for task in (first, second, later)))

    def test_eight_slots_are_shared_across_workflow_types(self):
        self.patches.enter_context(mock.patch.object(server, "_saved_crm_automation_parallel_workers", return_value=8))
        tasks = [self.enqueue(str(1000001 + index), "address_validator" if index % 2 else "copyright_reachout") for index in range(10)]
        def runner(*args, **kwargs):
            self.controlled_runner(*args, **kwargs)
            return True, "Done", {}
        self.start_pool(runner)
        for index in range(8):
            self.wait_started(str(1000001 + index))
        self.assertEqual(server.get_automation_queue_payload()["running_count"], 8)
        self.assertEqual([task["status"] for task in tasks[8:]], ["queued", "queued"])
        self.cleanup_pool()
        self.assertTrue(all(task["status"] == "completed" for task in tasks))

    def test_worker_files_and_nested_results_are_isolated_per_attempt(self):
        seen = []
        def runner(_script, args, _label, **kwargs):
            seen.append(kwargs)
            self.assertNotEqual(kwargs["result_path"], server.RESULT_FILE)
            self.assertNotEqual(kwargs["status_path"], server.AUTOMATION_STATUS_FILE)
            self.assertNotEqual(kwargs["env_overrides"]["AUTOMATION_RESULT_FILE"], kwargs["result_path"])
            return True, "Done", {}
        self.enqueue("1000001")
        self.enqueue("1000002")
        self.start_pool(runner)
        self.pool_thread.join(5)
        self.assertFalse(self.errors)
        self.assertEqual(len({entry["result_path"] for entry in seen}), 2)
        self.assertEqual(len({entry["status_path"] for entry in seen}), 2)

    def test_one_failed_order_does_not_fail_or_retry_its_neighbor(self):
        first, second = self.enqueue("1000001"), self.enqueue("1000002")
        def runner(_script, args, _label, **kwargs):
            request = json.loads(args[args.index("--request-json") + 1])
            ok = request["order_id"] == "1000002"
            return ok, "Confirmed" if ok else "Review this refund", {"order_id": request["order_id"]}
        self.start_pool(runner)
        self.pool_thread.join(5)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(first["run_count"], 1)
        self.assertEqual(second["run_count"], 1)
        self.assertEqual(first["result_context"]["report"]["order_details"][0]["order_id"], "1000001")
        self.assertEqual(second["result_context"]["report"]["order_details"][0]["order_id"], "1000002")

    def test_actual_subprocess_handoffs_leave_shared_result_and_status_files_untouched(self):
        root = Path(self.temp.name)
        script = root / "local_worker.py"
        script.write_text("import json, sys, time\nfrom pathlib import Path\ntime.sleep(0.1)\nPath(sys.argv[1]).write_text(json.dumps({'success': True, 'message': sys.argv[2]}))\n", encoding="utf-8")
        shared_result, shared_status = root / "legacy.json", root / "legacy_status.json"
        shared_result.write_text("shared result", encoding="utf-8")
        shared_status.write_text("shared status", encoding="utf-8")
        output = {}
        def run(index):
            output[index] = server._run_script(str(script), [str(root / f"result-{index}.json"), f"Order {index}"],
                f"Fixture{index}", require_result=True, result_path=str(root / f"result-{index}.json"),
                status_path=str(root / f"status-{index}.json"))
        with mock.patch.object(server, "RESULT_FILE", str(shared_result)), \
             mock.patch.object(server, "AUTOMATION_STATUS_FILE", str(shared_status)), \
             mock.patch.object(server, "load_node_preferences", return_value={"headless": True}):
            threads = [threading.Thread(target=run, args=(index,)) for index in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(output[1][:2], (True, "Order 1"))
        self.assertEqual(output[2][:2], (True, "Order 2"))
        self.assertEqual(shared_result.read_text(encoding="utf-8"), "shared result")
        self.assertEqual(shared_status.read_text(encoding="utf-8"), "shared status")

    def test_running_cancel_targets_one_process_and_blocks_late_launch(self):
        first, second = self.enqueue("1000001"), self.enqueue("1000002")
        first.update(status="running", worker_slot=1)
        second.update(status="running", worker_slot=2)
        server._register_automation_process(mock.Mock(pid=101), "First", task_id=first["id"])
        server._register_automation_process(mock.Mock(pid=202), "Second", task_id=second["id"])
        with mock.patch.object(server, "_kill_process_tree") as kill, mock.patch.object(server, "force_stop_automation") as stop:
            self.assertTrue(server.cancel_automation_queue_task(first["id"])[0])
            kill.assert_called_once_with(101)
            stop.assert_not_called()
        self.assertFalse(second.get("cancel_requested"))
        self.assertEqual(server.automation_stop_requested_at, 0)
        with mock.patch.object(server.subprocess, "Popen") as launch:
            result = server._run_script("unused", [], "Canceled worker", task_id=first["id"])
            self.assertFalse(result[0])
            launch.assert_not_called()

    def test_global_stop_blocks_a_worker_prepared_before_stop_even_after_grace_period(self):
        task = self.enqueue("1000001")
        server.automation_stop_requested_at = 20
        # The eight-second block has ended, but this worker was admitted at 10.
        with mock.patch.object(server.subprocess, "Popen") as launch:
            result = server._run_script("unused", [], "Late worker", task_id=task["id"], stop_requested_after=10)
            self.assertFalse(result[0])
            launch.assert_not_called()

    def test_unexpected_worker_start_failure_is_terminal_and_does_not_stall_queue(self):
        task = self.enqueue("1000001")
        with mock.patch.object(server, "_run_queue_order_worker", side_effect=OSError("Cannot prepare worker files")):
            server._run_automation_queue_order_pool()
        self.assertEqual(task["status"], "failed")
        self.assertIn("Cannot prepare worker files", task["message"])
        self.assertFalse(server.crm_lock.locked())

    def test_product_separator_keeps_its_existing_bounded_transient_retry(self):
        task = self.enqueue("1000001", "product_separator")
        task.update(status="running", started_at=server._automation_queue_now_iso(), worker_slot=1, run_count=1)
        with mock.patch.object(server, "_crm_product_separator_payload_retryable", return_value=True), \
             mock.patch.object(server, "_run_script", side_effect=[(False, "Load timeout", {}), (True, "Saved", {})]) as run:
            server._run_queue_order_worker(task, parallel_order_request(task), 1)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(task["status"], "completed")

    def test_status_endpoint_never_uses_another_orders_shared_runtime(self):
        first, second = self.enqueue("1000001"), self.enqueue("1000002")
        first.update(status="completed", success=True, completed_at="2026-11-23T10:00:00", message="First complete", run_count=1)
        second.update(status="running", worker_slot=2, message="Second running")
        with mock.patch.object(server, "crm_extension_order_runtime", {"orderId": "9999999", "lastMessage": "Wrong order"}):
            client = server.app.test_client()
            response = client.get("/api/extension/bridge/process-order/status?order_id=1000001")
            runtime = response.get_json()["runtime"]
            self.assertEqual(runtime["orderId"], "1000001")
            self.assertEqual(runtime["taskId"], first["id"])
            self.assertTrue(runtime["lastSuccess"])
            self.assertFalse(runtime["running"])
            self.assertEqual(server.get_crm_extension_order_status_payload("1000002")["runtime"]["lastMessage"], "Second running")
            self.assertEqual(client.get("/api/extension/bridge/process-order/status?order_id=report").status_code, 409)

    def test_scanner_slots_reuse_the_salesforce_profiles_configured_in_settings(self):
        import sys
        if server.WORKERS_DIR not in sys.path:
            sys.path.insert(0, server.WORKERS_DIR)
        import crm_validate_address as profiles
        with mock.patch.object(profiles, "_clone_profile_for_worker", return_value=(None, "isolated")) as clone, \
             mock.patch.object(server.os.path, "isdir", return_value=True):
            self.real_queue_profiles({"automation_key": "copyright_cancel"}, 3)
            self.assertEqual(clone.call_args_list[0].kwargs["pool_name"], "sheet_scanner_crm")
            self.assertEqual(clone.call_args_list[0].kwargs["worker_slot"], 3)
            self.assertEqual(clone.call_args_list[1].kwargs["pool_name"], "sheet_scanner_slack")


if __name__ == "__main__":
    unittest.main()
