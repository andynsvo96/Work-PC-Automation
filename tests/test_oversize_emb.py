"""Offline selection, queue, and template checks for Oversize EMB."""
import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
sys.path.insert(0, str(ROOT))
import complicated_emb
import oversize_emb
import crm_copyright_cancel as worker
import server

DESIGNS = [{"tab_number": 2, "design_name": "Team Logo", "print_areas": ["Back"]}]


class OversizeEmbTests(unittest.TestCase):
    def test_normalization_rejects_missing_duplicate_and_placeholder_areas(self):
        invalid = [None, [], [{"tab_number": 2, "design_name": "Logo"}]]
        invalid += [[{**DESIGNS[0], "print_areas": areas}] for areas in
                    ([], [None], [""], ["Back", "back"], ["[PRINT]"], ["[DESIGN]"], "Back")]
        invalid += [[{**DESIGNS[0], "design_name": "[PRINT]"}], DESIGNS * 2]
        for designs in invalid:
            with self.subTest(designs=designs), self.assertRaises(ValueError):
                oversize_emb.normalize_designs(designs)
        self.assertEqual(oversize_emb.normalize_designs([{**DESIGNS[0], "print_areas": [" Back "]}]), DESIGNS)

    def test_multiple_designs_keep_each_print_area_with_its_name(self):
        self.assertEqual(oversize_emb.print_design_text(DESIGNS), "Back for Team Logo")
        self.assertEqual(oversize_emb.print_design_text([
            {"tab_number": 3, "design_name": "Crest", "print_areas": ["Front-left Chest"]},
            {"tab_number": 1, "design_name": "Team Logo", "print_areas": ["Front", "Back"]},
        ]), "Front and Back for Team Logo and the Front-left Chest for Crest")

    def test_queue_retains_areas_and_worker_process_without_reason(self):
        key = "oversize_emb_to_hdd"
        with mock.patch.object(server, "enqueue_automation", return_value=(True, "Queued", {"id": "oversize-test"})) as enqueue:
            ok, _, _ = server.queue_crm_extension_manual_order_run("1234567", key, request_payload={"designs": DESIGNS})
        self.assertTrue(ok)
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"], {
            "order_id": "1234567", "process": key, "reason": "", "designs": DESIGNS,
        })
        with mock.patch.object(server, "run_crm_sheet_scanner_order_queued", return_value=(True, "Done")) as run:
            server.CRM_EXTENSION_MANUAL_ORDER_AUTOMATIONS[key]["runner"]("1234567", "", {"designs": DESIGNS})
        run.assert_called_once_with("1234567", key, "", designs=DESIGNS)
        with mock.patch.object(server, "_run_script", return_value=(True, "Done", {})) as run:
            server._execute_crm_mass_emailer_worker(action="process_order", order_id="1234567", process=key, designs=DESIGNS)
        args = run.call_args.args[1]
        self.assertEqual(json.loads(args[args.index("--emb-designs-json") + 1]), DESIGNS)
        with mock.patch.object(server, "enqueue_automation") as enqueue:
            ok, message, _ = server.queue_crm_extension_manual_order_run("1234567", key)
        self.assertFalse(ok)
        enqueue.assert_not_called()

    def test_worker_rechecks_names_and_selected_areas(self):
        metadata = [{"tab_number": 2, "eligible": True, "print_areas": [
            {"description": "Front-left Chest", "method": "Embroidery"},
            {"description": "Back", "method": "HD Digital"},
        ]}]
        with mock.patch.object(complicated_emb, "resolve_designs", return_value=DESIGNS) as names, \
             mock.patch.object(oversize_emb, "_read_methods", return_value=metadata):
            self.assertEqual(oversize_emb.resolve_designs(mock.sentinel.driver, DESIGNS), DESIGNS)
        names.assert_called_once_with(mock.sentinel.driver, DESIGNS)
        for areas in (["Front"], ["Back", "Back"], ["Back", "back"], [""]):
            rows = [{"tab_number": 2, "eligible": True, "print_areas": [{"description": area} for area in areas]}]
            with self.subTest(areas=areas), mock.patch.object(complicated_emb, "resolve_designs"), \
                 mock.patch.object(oversize_emb, "_read_methods", return_value=rows), self.assertRaises(ValueError):
                oversize_emb.resolve_designs(mock.Mock(), DESIGNS)
        with mock.patch.object(complicated_emb, "resolve_designs", side_effect=ValueError("Design Name changed")), \
             self.assertRaisesRegex(ValueError, "Design Name changed"):
            oversize_emb.resolve_designs(mock.Mock(), DESIGNS)

    def test_sheet_default_requires_exactly_one_design_and_one_area(self):
        row = {"tab_number": 2, "eligible": True, "print_areas": [{"description": "Back"}]}
        with mock.patch.object(oversize_emb, "_read_methods", return_value=[row]), \
             mock.patch.object(complicated_emb, "resolve_designs", return_value=DESIGNS):
            self.assertEqual(oversize_emb.resolve_designs(mock.Mock()), DESIGNS)
        for rows in ([], [row, {**row, "tab_number": 3}], [{**row, "print_areas": row["print_areas"] * 2}]):
            with self.subTest(rows=rows), mock.patch.object(oversize_emb, "_read_methods", return_value=rows), \
                 mock.patch.object(complicated_emb, "resolve_designs") as resolve, self.assertRaisesRegex(ValueError, "Choose the applicable"):
                oversize_emb.resolve_designs(mock.Mock())
            resolve.assert_not_called()

    def test_template_replaces_both_fields_and_checks_result(self):
        process = worker.OVERSIZE_EMB_TO_HDD_PROCESS
        body = "For embroidery, we have updated the [PRINT] for [DESIGN], from embroidery to ink printing instead."
        before = {"subject": "Order 1234567 - Embroidery Updated", "body": body}
        replacement = "Back for Team Logo"
        after = {**before, "body": body.replace("[PRINT] for [DESIGN]", replacement)}
        with mock.patch.object(worker, "_insert_cancel_template"), \
             mock.patch.object(worker, "_read_salesforce_email_state", side_effect=[before, before, after]), \
             mock.patch.object(oversize_emb, "replace_print_design_placeholder", return_value=1) as replace, \
             mock.patch.object(worker.time, "sleep"):
            result = worker._fill_salesforce_email_from_salesforce_template(mock.sentinel.driver, "1234567", process=process, designs=DESIGNS)
        replace.assert_called_once_with(mock.sentinel.driver, replacement)
        self.assertEqual(result["state"], after)
        self.assertEqual(result["body_placeholder_replacement"]["replacement"], replacement)

    def test_template_missing_fields_or_unverified_replacement_stops(self):
        process = worker.OVERSIZE_EMB_TO_HDD_PROCESS
        for body, after_body, replaced in (
            ("Embroidery [DESIGN]", "", 1),
            ("Embroidery [PRINT] for [DESIGN]", "Embroidery unchanged", 1),
            ("Embroidery [PRINT] for [DESIGN]", "Embroidery Back for Team Logo [PRINT]", 1),
            ("Embroidery [PRINT] for [DESIGN]", "", 0),
        ):
            before = {"subject": "Order 1234567", "body": body}
            with self.subTest(body=body, after=after_body), mock.patch.object(worker, "_insert_cancel_template"), \
                 mock.patch.object(worker, "_read_salesforce_email_state", side_effect=[before, before, {**before, "body": after_body}]), \
                 mock.patch.object(oversize_emb, "replace_print_design_placeholder", return_value=replaced), \
                 mock.patch.object(worker.time, "sleep"), self.assertRaises(worker.CopyrightCancelError):
                worker._fill_salesforce_email_from_salesforce_template(mock.Mock(), "1234567", process=process, designs=DESIGNS)
        with self.assertRaisesRegex(worker.CopyrightCancelError, "PRINT"):
            worker._validate_no_unresolved_email_placeholders("Order 1234567", "Review [PRINT]")

    def test_selection_is_resolved_before_crm_notes_and_email(self):
        for resolution in (DESIGNS, ValueError("Print area changed")):
            with self.subTest(resolution=resolution), ExitStack() as stack:
                driver = mock.Mock(current_window_handle="crm-tab")
                for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                             "_wait_for_order_scope", "safe_driver_quit", "safe_take_screenshot"):
                    stack.enter_context(mock.patch.object(worker, name))
                stack.enter_context(mock.patch.object(worker, "_open_driver", return_value=driver))
                stack.enter_context(mock.patch.object(worker, "_wait_for_crm_contact_info", return_value={"email": "buyer@example.test"}))
                resolve = stack.enter_context(mock.patch.object(oversize_emb, "resolve_designs", **(
                    {"side_effect": resolution} if isinstance(resolution, Exception) else {"return_value": resolution})))
                note = stack.enter_context(mock.patch.object(worker, "_prepare_no_cancel_crm_action", return_value={}))
                email = stack.enter_context(mock.patch.object(worker, "_prepare_and_maybe_send_salesforce_email", return_value={"sent": True}))
                if isinstance(resolution, Exception):
                    with self.assertRaises(ValueError):
                        worker.process_single_order("1234567", "", process="oversize_emb_to_hdd", designs=DESIGNS)
                    note.assert_not_called()
                    email.assert_not_called()
                else:
                    worker.process_single_order("1234567", "", process="oversize_emb_to_hdd", designs=DESIGNS)
                    self.assertEqual(note.call_args.kwargs["designs"], DESIGNS)
                    self.assertEqual(email.call_args.kwargs["designs"], DESIGNS)
                resolve.assert_called_once_with(driver, DESIGNS)

    def test_email_recovery_rechecks_selections_and_preserves_contact_skip(self):
        for label in ("Salesforce Account", "Salesforce Contact"):
            with self.subTest(label=label), ExitStack() as stack:
                driver = mock.Mock(current_window_handle="crm-tab")
                for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                             "_wait_for_order_scope", "safe_driver_quit", "safe_take_screenshot"):
                    stack.enter_context(mock.patch.object(worker, name))
                stack.enter_context(mock.patch.object(worker, "_open_driver", return_value=driver))
                stack.enter_context(mock.patch.object(worker, "_wait_for_crm_contact_info", return_value={
                    "email": "buyer@example.test", "salesforce_label": label,
                }))
                resolve = stack.enter_context(mock.patch.object(oversize_emb, "resolve_designs", return_value=DESIGNS))
                email = stack.enter_context(mock.patch.object(worker, "_prepare_and_maybe_send_salesforce_email", return_value={"sent": True}))
                worker.send_salesforce_email_single_order("1234567", process="oversize_emb_to_hdd", designs=DESIGNS)
                if label == "Salesforce Contact":
                    resolve.assert_not_called()
                else:
                    resolve.assert_called_once_with(driver, DESIGNS)
                self.assertEqual(email.call_args.kwargs["designs"], DESIGNS)


if __name__ == "__main__":
    unittest.main()
