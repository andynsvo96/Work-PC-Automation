"""Complicated EMB feedback routing and ordered CRM actions (no live requests)."""
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
sys.path.insert(0, str(ROOT))
import crm_copyright_cancel as worker
import server


class ComplicatedEmbTests(unittest.TestCase):
    def test_feedback_template_changes_subject_only(self):
        process = worker.COMPLICATED_EMB_FEEDBACK_PROCESS
        body = "Please review the embroidery options. Keep all formatting and links."
        before = {"subject": "Order [Order-Number] embroidery", "body": body}
        after = {"subject": "Order 1234567 embroidery", "body": body}
        with mock.patch.object(worker, "_insert_cancel_template") as insert, \
             mock.patch.object(worker, "_read_salesforce_email_state", side_effect=[before, after]), \
             mock.patch.object(worker, "_replace_subject_order_number") as subject, \
             mock.patch.object(worker, "_replace_salesforce_body_placeholder_with_reason") as replace_body, \
             mock.patch.object(worker, "_fill_salesforce_email_from_local_template") as local_fill, \
             mock.patch.object(worker.time, "sleep"):
            result = worker._fill_salesforce_email_from_salesforce_template(
                mock.sentinel.driver, "1234567", process=process,
            )
        insert.assert_called_once_with(mock.sentinel.driver, process)
        subject.assert_called_once_with(mock.sentinel.driver, "1234567")
        replace_body.assert_not_called()
        local_fill.assert_not_called()
        self.assertEqual(result["state"]["body"], body)
        self.assertEqual(result["template"], "[AUTO] Complicated Embroidery")
        self.assertEqual(worker._cancel_sales_note("", process), "Complicated embroidery\nEmailed txted")
        self.assertTrue(worker._missing_body_markers("", process))

    def test_feedback_orders_notes_email_and_issue_and_stops_on_email_failure(self):
        for email_result in ({"sent": True}, {"sent": False}, RuntimeError("Email failed")):
            with self.subTest(email_result=email_result), ExitStack() as stack:
                driver = mock.Mock(current_window_handle="crm-tab")
                steps = []
                for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed",
                             "_switch_to_crm_app_frame", "_wait_for_order_scope",
                             "safe_driver_quit", "safe_take_screenshot"):
                    stack.enter_context(mock.patch.object(worker, name))
                stack.enter_context(mock.patch.object(worker, "_open_driver", return_value=driver))
                stack.enter_context(mock.patch.object(worker, "_wait_for_crm_contact_info",
                                                     return_value={"email": "buyer@example.com"}))
                note = stack.enter_context(mock.patch.object(
                    worker, "_append_copyright_cancel_sales_note",
                    side_effect=lambda *a, **kw: steps.append("note") or {"updated": True},
                ))
                def send(*args, **kwargs):
                    steps.append("email")
                    if isinstance(email_result, Exception):
                        raise email_result
                    return email_result
                stack.enter_context(mock.patch.object(worker, "_prepare_and_maybe_send_salesforce_email", side_effect=send))
                activate = stack.enter_context(mock.patch.object(worker, "_activate_crm_context"))
                apply = stack.enter_context(mock.patch.object(
                    worker, "_apply_order_status",
                    side_effect=lambda *a, **kw: steps.append("issue") or {"status_applied": True},
                ))
                cancel = stack.enter_context(mock.patch.object(worker, "_cancel_and_refund_crm_order"))
                if email_result == {"sent": True}:
                    result = worker.process_single_order("1234567", "", dry_run=False, process="complicated_emb_feedback")
                    self.assertEqual(steps, ["note", "email", "issue"])
                    self.assertTrue(result["crm_action"]["order_status"]["status_applied"])
                    driver.switch_to.window.assert_called_once_with("crm-tab")
                    activate.assert_called_once_with(driver)
                    apply.assert_called_once_with(driver, "issue - design / placement", dry_run=False, search_text="design")
                else:
                    with self.assertRaises((RuntimeError, worker.CopyrightCancelError)):
                        worker.process_single_order("1234567", "", dry_run=False, process="complicated_emb_feedback")
                    self.assertEqual(steps, ["note", "email"])
                    apply.assert_not_called()
                note.assert_called_once_with(driver, "", dry_run=False, process=worker.COMPLICATED_EMB_FEEDBACK_PROCESS)
                cancel.assert_not_called()

    def test_design_status_search_selects_full_status_and_verifies_apply(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"success": True}
        with mock.patch.object(worker, "_order_status_already_applied", return_value=False), \
             mock.patch.object(worker, "_click_order_status_apply", return_value=True) as apply, \
             mock.patch.object(worker, "_order_status_values", return_value=["issue - design / placement"]), \
             mock.patch.object(worker.time, "sleep"):
            result = worker._apply_order_status(driver, "issue - design / placement", search_text="design")
        self.assertEqual(driver.execute_script.call_args_list[0].args[1], "design")
        self.assertEqual(driver.execute_script.call_args_list[1].args[1], "issue - design / placement")
        apply.assert_called_once_with(driver)
        self.assertTrue(result["status_applied"])

    def test_each_choice_queues_its_own_worker_process_without_reason(self):
        for key in ("complicated_emb_to_hdd", "complicated_emb_feedback"):
            with self.subTest(key=key), mock.patch.object(
                server, "enqueue_automation", return_value=(True, "Queued", {"id": "emb-test"})
            ) as enqueue:
                ok, _, _ = server.queue_crm_extension_manual_order_run("1234567", key)
                self.assertTrue(ok)
                self.assertEqual(enqueue.call_args.kwargs["task_arguments"], {
                    "order_id": "1234567", "process": key, "reason": "",
                })
                with mock.patch.object(server, "run_crm_sheet_scanner_order_queued", return_value=(True, "Done")) as run:
                    server.CRM_EXTENSION_MANUAL_ORDER_AUTOMATIONS[key]["runner"]("1234567", "")
                    run.assert_called_once_with("1234567", key, "")


if __name__ == "__main__":
    unittest.main()
