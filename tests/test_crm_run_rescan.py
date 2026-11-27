"""New runs reconsider detected orders without replaying them within a run."""

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_push_back as push_back
import crm_shipping_bypasser as bypasser
import server


class CrmRunRescanTests(unittest.TestCase):
    def test_old_scanner_requests_include_errors_when_dispatching_scan_and_process_workers(self):
        for action in ("scan_sheet", "process_queue"):
            with self.subTest(action=action), mock.patch.object(
                server, "_run_script", return_value=(True, "Completed", {"success": True}),
            ) as run:
                server._execute_crm_mass_emailer_worker(action=action, retry_errors=False, dry_run=True)
                self.assertIn("--retry-errors", run.call_args.args[1])

    def test_shipping_history_never_excludes_detected_orders_from_a_new_run(self):
        orders = ["5000001", "5000002", "5000003"]
        history = {"run_history": [{
            "automation_key": "shipping_bypasser", "dry_run": False,
            "order_ids": orders, "order_results": [
                {"order_id": orders[0], "success": False, "outcome": "worker_exception"},
                {"order_id": orders[1], "success": True, "outcome": "shipping_bypass_ordered"},
                {"order_id": orders[1], "success": False, "outcome": "no_single_warehouse"},
                {"order_id": orders[2], "success": True, "outcome": "shipping_bypass_ordered"},
            ],
        }]}
        folder = ROOT / "runtime/debug/run_rescan_tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as directory, ExitStack() as stack:
            configured_url = "https://crm.example/app#/report/967?salesNotes=High%20shipping%20cost"
            stack.enter_context(mock.patch.object(bypasser, "CRM_SHIPPING_BYPASS_URL", configured_url))
            history_path = Path(directory) / "history.json"
            original = json.dumps(history)
            history_path.write_text(original, encoding="utf-8")
            # Redirect the history read without editing the machine's live state.
            stack.enter_context(mock.patch.object(
                bypasser, "open", side_effect=lambda *_a, **_k: history_path.open(encoding="utf-8"), create=True,
            ))
            for name in ("_build_crm_session_driver", "_build_sanmar_driver", "safe_driver_quit", "_publish_status"):
                stack.enter_context(mock.patch.object(bypasser, name))
            stack.enter_context(mock.patch.object(bypasser, "_cleanup_after_failed_order", return_value=True))
            exclusions = []

            def collect(*_args, exclude_order_ids, **_kwargs):
                exclusions.append(set(exclude_order_ids))
                # The third order becomes available on the next list refresh.
                visible_orders = orders[:2] if not exclude_order_ids else orders
                return [order for order in visible_orders if order not in exclude_order_ids]

            stack.enter_context(mock.patch.object(bypasser, "_collect_batch_order_ids_with_driver", side_effect=collect))
            process = stack.enter_context(mock.patch.object(bypasser, "_run_order_with_drivers"))
            for attempt, override in enumerate((None, "https://crm.example/app#/report/967")):
                process.side_effect = lambda _crm, _vendor, order_id, **_kw: [{
                    "order_id": order_id, "success": bool(attempt),
                    "outcome": "already_stock_ordered" if attempt else "no_single_warehouse",
                }]
                payload = bypasser._run_batch_with_mode(True, list_url=override)
                self.assertEqual(payload["list_url"], override or configured_url)
                self.assertEqual(payload["order_ids"], orders)
                self.assertEqual(payload["refresh_passes"], 3)
                self.assertEqual(payload["skipped_historical_order_count"], 0)
                self.assertNotIn("clear Stock Tools history", payload["message"])
            self.assertEqual([call.args[2] for call in process.call_args_list], orders * 2)
            self.assertEqual(exclusions, [set(), set(orders[:2]), set(orders)] * 2)
            self.assertEqual(history_path.read_text(encoding="utf-8"), original)

    def test_push_back_failures_are_attempted_once_per_run_in_every_report_mode(self):
        rows = [{"orderId": order_id, "colorLabel": "purple"} for order_id in ("5000001", "5000002")]
        for mode in sorted(push_back.VALID_FILTERS):
            for workers in (1, 2):
                with self.subTest(mode=mode, workers=workers), ExitStack() as stack:
                    configured_url = "https://crm.example/app#/report/122?salesNotes=Delivery%20delay"
                    stack.enter_context(mock.patch.object(push_back, f"CRM_PUSH_BACK_{mode.upper()}_URL", configured_url))
                    for name in ("_build_crm_session_driver", "safe_driver_quit", "_publish_status"):
                        stack.enter_context(mock.patch.object(push_back, name))
                    stack.enter_context(mock.patch.object(push_back, "_clone_profile_for_worker", return_value=(None, "fixture-profile")))
                    exclusions = []

                    def collect(*_args, exclude_order_ids, **_kwargs):
                        exclusions.append(set(exclude_order_ids))
                        # A newly scheduled order appears after the first fails.
                        visible_rows = rows[:1] if not exclude_order_ids else rows
                        return [dict(row) for row in visible_rows if row["orderId"] not in exclude_order_ids]

                    for name in ("_collect_push_back_rows", "_collect_push_back_rows_with_driver"):
                        stack.enter_context(mock.patch.object(push_back, name, side_effect=collect))
                    process_name = "_run_order_with_driver" if workers == 1 else "_run_order_worker_payload"
                    process = stack.enter_context(mock.patch.object(push_back, process_name))
                    for override in (None, "https://crm.example/app#/report/122"):
                        def process_row(*args, **_kwargs):
                            row = args[1] if workers == 1 else args[0]
                            return push_back._result(row["orderId"], False, "worker_exception", "Temporary failure")

                        process.side_effect = process_row
                        result = push_back._run_batch_with_mode(
                            True, processing_filter=mode, parallel_workers=workers,
                            list_url=override,
                        )
                        self.assertFalse(result["success"])
                        self.assertEqual(result["list_url"], override or configured_url)
                        self.assertEqual(result["order_ids"], [row["orderId"] for row in rows])
                        self.assertEqual(result["refresh_passes"], 3)
                    self.assertEqual(process.call_count, 4)
                    self.assertEqual(exclusions, [set(), {"5000001"}, {"5000001", "5000002"}] * 2)


if __name__ == "__main__":
    unittest.main()
