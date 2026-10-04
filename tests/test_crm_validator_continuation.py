import json
import shutil
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_validate_address as validator
import server


def completed_validator_step():
    return {
        "key": "address_validator_batch",
        "label": "Address Validator (Batch)",
        "success": False,
        "order_count": 5,
        "successful_order_count": 4,
        "error_count": 1,
        "message": "Processed 5 order(s) with 1 parallel worker(s) across 2 CRM list refresh pass(es). 4 succeeded. 1 require manual review.",
        "errors": [{"order_id": "5331333", "status": "Manual review", "message": "Timed out waiting for State field."}],
    }


def successful_order(order_id):
    return {
        "order_id": order_id, "success": True, "message": "Validated.",
        "manual_review_required": False, "resolution": "validated", "warnings": [],
    }


class ValidatorContinuationTests(unittest.TestCase):
    def test_shared_batch_skips_repeated_timeout_and_processes_remaining_orders(self):
        evaluated = []
        scans = []
        first_driver, next_driver = mock.Mock(), mock.Mock()

        def collect(driver, shipping_filter, limit, **kwargs):
            scans.append(set(kwargs["exclude_order_ids"]))
            if len(scans) == 1:
                return ["1000001", "5331333", "1000002"]
            if len(scans) == 2:
                return ["1000003"]
            return []

        def evaluate(driver, order_id, **kwargs):
            evaluated.append((driver, order_id))
            if order_id == "5331333":
                raise validator.TimeoutException("Timed out waiting for State field.")
            return successful_order(order_id)

        with (
            mock.patch.object(validator, "_build_crm_session_driver", side_effect=[first_driver, next_driver]) as build,
            mock.patch.object(validator, "_collect_batch_order_ids_with_driver", side_effect=collect),
            mock.patch.object(validator, "_evaluate_and_resolve_order", side_effect=evaluate),
            mock.patch.object(validator, "_crm_attempt_modes", return_value=[True]),
            mock.patch.object(validator, "safe_get_with_partial_load") as reload_order,
            mock.patch.object(validator, "login_if_needed", return_value=False),
            mock.patch.object(validator, "safe_take_screenshot"),
            mock.patch.object(validator, "safe_driver_quit") as quit_driver,
        ):
            payload = validator._run_batch(shipping_filter="all", batch_size=None, parallel_workers=1)

        self.assertEqual(payload["order_ids"], ["1000001", "5331333", "1000002", "1000003"])
        self.assertEqual([order_id for _, order_id in evaluated], ["1000001", "5331333", "5331333", "1000002", "1000003"])
        self.assertIs(evaluated[3][0], next_driver)
        self.assertEqual(scans[1], {"1000001", "5331333", "1000002"})
        self.assertEqual(scans[2], set(payload["order_ids"]))
        self.assertFalse(payload["success"])
        self.assertTrue(payload["manual_review_required"])
        self.assertEqual([row["success"] for row in payload["report"]], [True, False, True, True])
        self.assertEqual(payload["report"][1]["order_id"], "5331333")
        self.assertIn("3 succeeded. 1 require manual review.", payload["message"])
        self.assertEqual(build.call_count, 2)
        reload_order.assert_called_once()
        quit_driver.assert_any_call(first_driver, profile_path=validator.PROFILE_PATH)

    def test_parallel_batch_exhausts_order_retry_then_continues_list_scans(self):
        evaluated = []
        scans = []

        def collect(shipping_filter, limit, profile_path, **kwargs):
            scans.append(set(kwargs["exclude_order_ids"]))
            if len(scans) == 1:
                return ["1000001", "5331333", "1000002"]
            if len(scans) == 2:
                return ["1000003"]
            return []

        def evaluate(order_id, **kwargs):
            evaluated.append(order_id)
            row = successful_order(order_id)
            if order_id == "5331333":
                row.update(success=False, message="Timed out waiting for State field.", outcome="worker_exception", manual_review_required=True)
            return {"success": row["success"], "target_order_id": order_id, "report": [row], "retryable": not row["success"]}

        with (
            mock.patch.object(validator, "_collect_batch_order_ids", side_effect=collect),
            mock.patch.object(validator, "_run_single_payload", side_effect=evaluate),
            mock.patch.object(validator, "_clone_profile_for_worker", return_value=(None, str(ROOT / "runtime" / "debug" / "validator_test_profile"))),
            mock.patch.object(validator, "write_result_payload"),
        ):
            payload = validator._run_batch(shipping_filter="all", batch_size=None, parallel_workers=2)

        self.assertEqual(Counter(evaluated), Counter({"1000001": 1, "5331333": 2, "1000002": 1, "1000003": 1}))
        self.assertEqual(payload["order_ids"], ["1000001", "5331333", "1000002", "1000003"])
        self.assertEqual(scans[1], {"1000001", "5331333", "1000002"})
        self.assertEqual(scans[2], set(payload["order_ids"]))
        self.assertEqual([row["success"] for row in payload["report"]], [True, False, True, True])
        self.assertTrue(payload["report"][1]["retry_attempted"])
        self.assertTrue(payload["manual_review_required"])

    def test_processing_runs_next_selected_step_after_validator_order_error(self):
        runtime = {}
        with (
            mock.patch.object(server, "load_crm_processing_state", return_value={}),
            mock.patch.object(server, "_automation_stop_is_blocking", return_value=False),
            mock.patch.object(server, "_run_crm_processing_step", side_effect=[
                completed_validator_step(),
                {"key": "order_goods", "label": "Order Goods", "success": True, "message": "Finished.", "order_count": 1, "error_count": 0},
            ]) as run_step,
            mock.patch.object(server, "_persist_crm_processing_run_result", return_value={}) as persist,
            mock.patch.object(server, "_audit_result"),
            mock.patch.object(server, "crm_processing_runtime", runtime),
            mock.patch.object(server, "crm_lock") as crm_lock,
        ):
            crm_lock.locked.return_value = True
            server._crm_processing_run_thread(["address_validator_batch", "order_goods"], "all")

        self.assertEqual([call.args[0] for call in run_step.call_args_list], ["address_validator_batch", "order_goods"])
        self.assertEqual(len(persist.call_args.args[3]), 2)
        self.assertFalse(runtime["running"])
        self.assertFalse(runtime["lastSuccess"])
        crm_lock.release.assert_called_once()


class ValidatorSummaryTests(unittest.TestCase):
    def test_queue_summary_distinguishes_completed_validator_from_whole_run_timeout(self):
        step = completed_validator_step()
        task = {
            "task_type": "crm.processing", "status": "failed",
            "message": "Automate Processing finished with failures: Address Validator (Batch).",
            "result_context": {"report": {"step_results": [step]}},
        }
        payload = server._automation_queue_task_payload(task)
        self.assertEqual(payload["message"], "Validator finished: 4 succeeded; 1 need review.")
        self.assertEqual(payload["result_context"]["report"]["step_results"][0]["errors"], step["errors"])

        step.update(order_count=0, successful_order_count=0, message="CRMAddressValidator timed out after 180 seconds.")
        self.assertEqual(server._automation_queue_task_payload(task)["message"], "Automation timed out.")

    def test_validator_runtime_keeps_completed_batch_counts(self):
        summary = completed_validator_step()["message"]
        payload = {"action": "validate_batch", "resolution": "batch", "report": [
            {"order_id": "5331333", "success": False, "message": "Timed out waiting for State field."},
        ]}
        self.assertEqual(server._automation_runtime_display_message("crm.address_validator", False, summary, payload), summary)
        payload["resolution"] = "list_collection_failed"
        self.assertEqual(server._automation_runtime_display_message("crm.address_validator", False, "List scan timed out.", payload), "Automation timed out.")

    @unittest.skipUnless(shutil.which("node"), "Node is required for dashboard message checks")
    def test_dashboard_handles_processing_and_queue_summaries_without_hiding_other_failures(self):
        html = (ROOT / "ui_panel.html").read_text(encoding="utf-8")
        function = html[html.index("function conciseAutomationFailureMessage("):html.index("function fmtHours(")]
        program = function + "\nconst step = " + json.dumps(completed_validator_step()) + ";\n" + r"""
        const assert = require('assert');
        const expected = 'Validator finished: 4 succeeded; 1 need review.';
        assert.strictEqual(conciseAutomationFailureMessage('Automation timed out.', {step_results: [step]}), expected);
        assert.strictEqual(conciseAutomationFailureMessage('Automation timed out.', {report: {step_results: [step]}}), expected);
        assert.strictEqual(conciseAutomationFailureMessage('Automation timed out.', {runtime: {payload: {
          action: 'validate_batch', resolution: 'batch', message: step.message, report: step.errors,
        }}}), step.message);
        assert.strictEqual(conciseAutomationFailureMessage('CRMAddressValidator timed out after 180 seconds.'), 'Automation timed out.');
        const stopped = {...step, message: 'CRMAddressValidator timed out after 180 seconds.'};
        assert.strictEqual(conciseAutomationFailureMessage(stopped.message, {step_results: [stopped]}), 'Automation timed out.');
        const goods = {key: 'order_goods', success: false, message: 'Required stock is unavailable.',
          errors: [{message: 'out of stock'}]};
        assert.strictEqual(conciseAutomationFailureMessage('Needs attention', {step_results: [step, goods]}), 'Required stock is unavailable.');
        """
        subprocess.run(["node", "-e", program], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
