"""Complicated EMB feedback routing and ordered CRM actions (no live requests)."""
import sys
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
sys.path.insert(0, str(ROOT))
import crm_copyright_cancel as worker
import server
import complicated_emb

DESIGNS = [{"tab_number": 2, "design_name": "Schuti Hats"}]


class ComplicatedEmbTests(unittest.TestCase):
    def test_feedback_template_replaces_design_and_subject(self):
        process = worker.COMPLICATED_EMB_FEEDBACK_PROCESS
        body = "Please review the embroidery options. Keep all formatting and links."
        before = {"subject": "Order [Order-Number] embroidery", "body": body + " [DESIGN]"}
        after = {"subject": "Order 1234567 embroidery", "body": body + " [DESIGN]"}
        replaced = {"subject": after["subject"], "body": body + " Schuti Hats"}
        with mock.patch.object(worker, "_insert_cancel_template") as insert, \
             mock.patch.object(worker, "_read_salesforce_email_state", side_effect=[before, after, replaced]), \
             mock.patch.object(complicated_emb, "replace_design_placeholder", return_value=1) as design_replace, \
             mock.patch.object(worker, "_replace_subject_order_number") as subject, \
             mock.patch.object(worker, "_replace_salesforce_body_placeholder_with_reason") as replace_body, \
             mock.patch.object(worker, "_fill_salesforce_email_from_local_template") as local_fill, \
             mock.patch.object(worker.time, "sleep"):
            result = worker._fill_salesforce_email_from_salesforce_template(
                mock.sentinel.driver, "1234567", process=process, designs=DESIGNS,
            )
        insert.assert_called_once_with(mock.sentinel.driver, process)
        subject.assert_called_once_with(mock.sentinel.driver, "1234567")
        replace_body.assert_not_called()
        local_fill.assert_not_called()
        design_replace.assert_called_once_with(mock.sentinel.driver, "Schuti Hats")
        self.assertEqual(result["state"]["body"], replaced["body"])
        self.assertEqual(result["template"], "[AUTO] Complicated Embroidery")
        self.assertEqual(worker._cancel_sales_note("", process), "Complicated embroidery\nEmailed txted")
        self.assertTrue(worker._missing_body_markers("", process))

    def test_feedback_orders_notes_email_and_issue_and_stops_on_email_failure(self):
        for email_result in ({"sent": True}, {"sent": False}, RuntimeError("Email failed")):
            with self.subTest(email_result=email_result), ExitStack() as stack:
                driver = mock.Mock(current_window_handle="crm-tab")
                steps = []
                stack.enter_context(mock.patch.object(complicated_emb, "resolve_designs", return_value=DESIGNS))
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
                ok, _, _ = server.queue_crm_extension_manual_order_run("1234567", key, request_payload={"designs": DESIGNS})
                self.assertTrue(ok)
                self.assertEqual(enqueue.call_args.kwargs["task_arguments"], {
                    "order_id": "1234567", "process": key, "reason": "",
                    "designs": DESIGNS,
                })
                with mock.patch.object(server, "run_crm_sheet_scanner_order_queued", return_value=(True, "Done")) as run:
                    server.CRM_EXTENSION_MANUAL_ORDER_AUTOMATIONS[key]["runner"]("1234567", "", {"designs": DESIGNS})
                    run.assert_called_once_with("1234567", key, "", designs=DESIGNS)

    def test_invalid_selection_and_unresolved_design_are_rejected(self):
        for designs in (None, [], [{"tab_number": True, "design_name": "Hat"}], DESIGNS * 2):
            with self.subTest(designs=designs), self.assertRaises(ValueError):
                complicated_emb.normalize_designs(designs)
        with self.assertRaises(worker.CopyrightCancelError):
            worker._validate_no_unresolved_email_placeholders("Order 1234567", "Review [DESIGN]")

    def test_names_are_ordered_and_joined(self):
        self.assertEqual(complicated_emb.design_text([
            {"tab_number": 3, "design_name": "Third"},
            {"tab_number": 1, "design_name": "First & <Hat>"},
            {"tab_number": 2, "design_name": "Second"},
        ]), "First & <Hat>, Second, and Third")

    def test_worker_rejects_changed_method_or_name(self):
        import crm_auto_splitter as splitter
        driver = mock.Mock()
        for eligible, name in ((False, "Schuti Hats"), (True, "Different Hat")):
            driver.execute_script.return_value = [{"tab_number": 2, "eligible": eligible}]
            with mock.patch.object(splitter, "_click_design_tab", return_value=True), \
                 mock.patch.object(splitter, "_scan_current_design_detail", return_value={"design_name": name}), \
                 mock.patch.object(complicated_emb.time, "sleep"), self.assertRaises(ValueError):
                complicated_emb.resolve_designs(driver, DESIGNS)

    def test_selection_reaches_worker_command_and_order_processing(self):
        with mock.patch.object(server, "_run_script", return_value=(True, "Done", {})) as run:
            server._execute_crm_mass_emailer_worker(action="process_order", order_id="1234567", process="complicated_emb_feedback", designs=DESIGNS)
        args = run.call_args.args[1]
        self.assertEqual(json.loads(args[args.index("--emb-designs-json") + 1]), DESIGNS)
        args = mock.Mock(order_id="1234567", order_url="", reason="", process="complicated_emb_feedback",
                         emb_designs_json=json.dumps(DESIGNS), dry_run=True, delete_sheet_row=False)
        with mock.patch.object(worker, "process_single_order", return_value={"order_id": "1234567"}) as process, \
             mock.patch.object(worker, "_write_result"):
            worker.run_process_order(args)
        self.assertEqual(process.call_args.kwargs["designs"], DESIGNS)


if __name__ == "__main__":
    unittest.main()
