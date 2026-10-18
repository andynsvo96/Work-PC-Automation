"""Isolated regression coverage for full/partial copyright cancellation."""
from contextlib import ExitStack
from copy import deepcopy
from decimal import Decimal
import itertools
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
import crm_copyright_cancel as shared
from workers import copyright_cancel_designs as copyright
import server


DESIGNS = [
    {"tab_number": 1, "design_id": "101", "design_name": "Unaffected", "quantity": 1, "subtotal": "120.00"},
    {"tab_number": 2, "design_id": "202", "design_name": "Pokemon Crypt", "quantity": 1, "subtotal": "80.00"},
]
SELECTION = {"order_id": "5451301", "all_designs": DESIGNS, "selected_design_ids": ["202"]}
CONTACT = {"email": "customer@example.test", "salesforce_label": "Salesforce Account"}


class CopyrightSelectionTests(unittest.TestCase):
    def test_partial_and_full_are_decided_from_the_complete_original_scan(self):
        self.assertTrue(copyright.is_partial(copyright.normalize_selection(SELECTION)))
        full = copyright.normalize_selection({**SELECTION, "selected_design_ids": ["202", "101"]})
        self.assertFalse(copyright.is_partial(full))
        self.assertEqual(copyright.selected_designs(SELECTION), [DESIGNS[1]])

    def test_invalid_missing_duplicate_or_foreign_selections_are_rejected(self):
        invalid = [None, [], {}, {**SELECTION, "selected_design_ids": []},
                   {**SELECTION, "selected_design_ids": ["202", "202"]},
                   {**SELECTION, "selected_design_ids": ["303"]},
                   {**SELECTION, "all_designs": DESIGNS * 2},
                   {**SELECTION, "all_designs": [{**DESIGNS[0], "quantity": True}]},
                   {**SELECTION, "all_designs": [{**DESIGNS[0], "subtotal": "unknown"}]},
                   {**SELECTION, "all_designs": [{**DESIGNS[0], "design_name": "[DESIGN]"}]},
                   {**SELECTION, "all_designs": [{**DESIGNS[1], "quantity": 0}]}]
        for selection in invalid:
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                copyright.normalize_selection(selection)

    def test_removed_design_or_retained_zero_tab_is_valid(self):
        for rows, key in ((DESIGNS[:1], "removed_design_ids"),
                          ([DESIGNS[0], {**DESIGNS[1], "quantity": 0, "subtotal": "0.00"}], "zeroed_design_ids")):
            with self.subTest(key=key):
                self.assertEqual(copyright.verify_removed(SELECTION, rows)[key], ["202"])

    def test_tab_renumbering_does_not_change_the_selected_identity_or_mode(self):
        selection = {**SELECTION, "selected_design_ids": ["101"]}
        remaining = [{**DESIGNS[1], "tab_number": 1}]
        self.assertEqual(copyright.verify_removed(selection, remaining)["removed_design_ids"], ["101"])
        self.assertTrue(copyright.is_partial(selection))

    def test_persisted_verification_rejects_changed_retained_or_nonzero_removed_tabs(self):
        for rows in (DESIGNS, [{**DESIGNS[0], "quantity": 2}], [{**DESIGNS[0], "subtotal": "119.99"}],
                     [DESIGNS[0], {**DESIGNS[1], "quantity": 0, "subtotal": "1.00"}],
                     [DESIGNS[0], {**DESIGNS[1], "design_name": "Different", "quantity": 0, "subtotal": "0.00"}],
                     [DESIGNS[0], {**DESIGNS[1], "design_id": "303"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                copyright.verify_removed(SELECTION, rows)

    def test_changed_complete_scan_blocks_processing(self):
        for rows in (DESIGNS[:1], [{**DESIGNS[0], "design_name": "Changed"}, DESIGNS[1]],
                     [DESIGNS[0], {**DESIGNS[1], "subtotal": "79.99"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                copyright.verify_original(SELECTION, rows)

    def test_all_tab_selection_and_names_are_not_limited_to_the_embroidery_picker_size(self):
        rows = [{**DESIGNS[0], "design_id": str(number), "tab_number": number, "design_name": f"Design {number}"}
                for number in range(1, 102)]
        selection = copyright.normalize_selection({**SELECTION, "all_designs": rows,
                                                    "selected_design_ids": [row["design_id"] for row in rows]})
        self.assertFalse(copyright.is_partial(selection))
        self.assertIn("Design 101", copyright.design_text(copyright.selected_designs(selection)))


class CopyrightRefundTests(unittest.TestCase):
    def setUp(self):
        self.totals = {"grand_total": "120.00", "paid": "200.00", "balance_due": "-80.00"}
        self.context = {**self.totals, "refund_amount": "80.00"}
        self.modal = {"max_refund": "200.00", "input_amount": "80.00"}

    def test_partial_refund_uses_credit_balance_while_full_guard_stays_strict(self):
        self.assertTrue(copyright.validate_partial_refund(self.totals, self.context, self.modal)["matched"])
        with mock.patch.object(shared, "_read_order_totals", return_value=self.totals), self.assertRaises(shared.CopyrightCancelError):
            shared._validate_live_refund_amounts(mock.Mock(), "80.00")

    def test_partial_refund_rejects_inconsistent_changed_or_excess_amounts(self):
        cases = [({**self.totals, "balance_due": "-80.01"}, self.context, self.modal),
                 ({**self.totals, "paid": None}, self.context, self.modal),
                 ({**self.totals, "grand_total": "121.00", "balance_due": "-79.00"}, self.context, self.modal),
                 (self.totals, {**self.context, "refund_amount": "200.00"}, self.modal),
                 (self.totals, self.context, {**self.modal, "max_refund": "79.99"}),
                 (self.totals, self.context, {**self.modal, "input_amount": "200.00"})]
        for totals, context, modal in cases:
            with self.subTest(totals=totals, context=context, modal=modal), self.assertRaises(ValueError):
                copyright.validate_partial_refund(totals, context, modal)

    def test_shared_save_allows_partial_only_after_checks_and_records_before_click(self):
        driver, before = mock.Mock(), mock.Mock()
        driver.execute_script.side_effect = lambda *_: bool(before.called)
        with mock.patch.object(shared, "_read_refund_modal_state", return_value=self.modal), \
             mock.patch.object(copyright, "read_financials", return_value=self.totals), \
             mock.patch.object(shared.time, "sleep"), \
             mock.patch.object(shared, "_validate_live_refund_amounts", side_effect=AssertionError("full guard used")):
            result = shared._save_refund_modal(driver, False, "80.00", partial_context=self.context, before_submit=before)
        self.assertTrue(result["refunded"])
        before.assert_called_once()
        driver.execute_script.assert_called_once()

    def test_invalid_partial_modal_never_records_a_submission_or_clicks_save(self):
        driver, before = mock.Mock(), mock.Mock()
        with mock.patch.object(shared, "_read_refund_modal_state", return_value={**self.modal, "input_amount": "200.00"}), \
             mock.patch.object(copyright, "read_financials", return_value=self.totals), self.assertRaises(ValueError):
            shared._save_refund_modal(driver, False, "80.00", partial_context=self.context, before_submit=before)
        before.assert_not_called()
        driver.execute_script.assert_not_called()

    def test_missing_computed_payment_total_cannot_be_coerced_to_zero(self):
        state = {"grand_total": None, "amount_paid": "200.00", "amount_due": "-200.00"}
        with mock.patch.object(shared, "_get_order_live_state", return_value=state), self.assertRaises(ValueError):
            copyright.read_financials(shared, mock.Mock())

    def test_active_status_requires_readable_current_heading_and_rejects_cancellation(self):
        for status in ('', 'Cancel Order', 'Cancelled'):
            with self.subTest(status=status), self.assertRaises(ValueError):
                copyright._active_status(shared, mock.Mock(execute_script=mock.Mock(return_value=status)))
        self.assertEqual(copyright._active_status(shared, mock.Mock(execute_script=mock.Mock(return_value='Approved'))), 'Approved')


class CopyrightTemplateTests(unittest.TestCase):
    def test_both_templates_replace_only_selected_designs_before_the_reason(self):
        for process in (shared.COPYRIGHT_CANCEL_PROCESS, shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS):
            with self.subTest(process=process.key), ExitStack() as stack:
                body = 'While reviewing your order, [DESIGN] contains [REASON]. ' + ' '.join(process.body_markers)
                state = {"subject": "Order #5451301 - A Refund Has Been Issued", "body": body}
                stack.enter_context(mock.patch.object(shared.time, 'sleep'))
                insert = stack.enter_context(mock.patch.object(shared, '_insert_cancel_template'))
                stack.enter_context(mock.patch.object(shared, '_read_salesforce_email_state', side_effect=lambda *_: dict(state)))

                def replace_name(_driver, replacement):
                    state['body'] = state['body'].replace('[DESIGN]', replacement)
                    return 1

                def replace_reason(_driver, reason, **_kwargs):
                    self.assertNotIn('[DESIGN]', state['body'])
                    state['body'] = state['body'].replace('[REASON]', reason)
                    return {'state': dict(state)}

                stack.enter_context(mock.patch('workers.complicated_emb.replace_design_placeholder', side_effect=replace_name))
                stack.enter_context(mock.patch.object(shared, '_replace_salesforce_body_placeholder_with_reason', side_effect=replace_reason))
                result = shared._fill_salesforce_email_from_salesforce_template(
                    mock.Mock(), '5451301', process=process, reason='Protected character', designs=[DESIGNS[1]])
                insert.assert_called_once_with(mock.ANY, process)
                self.assertIn('Pokemon Crypt contains Protected character', result['state']['body'])
                self.assertNotIn('Unaffected', result['state']['body'])
                shared._validate_no_unresolved_email_placeholders(result['state']['subject'], result['state']['body'])

    def test_partial_signature_rejects_full_cancel_body_and_exact_names_stay_distinct(self):
        partial = shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS
        self.assertEqual(shared._template_match_labels(partial), ['[AUTO] Copyright Cancel - Partial'])
        self.assertEqual(shared._template_match_labels(shared.COPYRIGHT_CANCEL_PROCESS), ['[AUTO] Copyright Cancel'])
        self.assertTrue(shared._missing_body_markers(' '.join(shared.COPYRIGHT_CANCEL_PROCESS.body_markers), partial))


class PartialWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(mock.patch.object(copyright, "STATE_DIR", folder))
        self.stack.enter_context(mock.patch.object(copyright.time, "sleep"))
        # Bound unconfirmed-refund polling without waiting or bypassing its checks.
        self.stack.enter_context(mock.patch.object(copyright.time, "monotonic", side_effect=itertools.count(0, 31)))
        self.driver = mock.Mock()
        self.rows = deepcopy(DESIGNS)
        self.totals = {"grand_total": "200.00", "paid": "200.00", "balance_due": "0.00"}
        self.state = {"transactions": [{"amount": "200.00", "tag": "Stripe.com", "type": "Stripe.com"}],
                      "order_fees": [], "sales_notes": ""}
        self.zero_tab = False
        self.payment_type = "Stripe.com"
        self.scan = self.stack.enter_context(mock.patch.object(copyright, "scan_designs", side_effect=lambda *_: deepcopy(self.rows)))
        self.removal = self.stack.enter_context(mock.patch.object(copyright, "remove_design", side_effect=self.remove))
        self.save = self.stack.enter_context(mock.patch.object(shared, "_save_order_and_wait", side_effect=self.save_order))
        self.note = self.stack.enter_context(mock.patch.object(shared, "_append_copyright_cancel_sales_note", side_effect=self.add_note))
        self.refund = self.stack.enter_context(mock.patch.object(shared, "_refund_via_stripe_payment_modal", side_effect=self.refund_payment))
        self.email = self.stack.enter_context(mock.patch.object(shared, "_prepare_and_maybe_send_salesforce_email", side_effect=self.send_email))
        self.case = self.stack.enter_context(mock.patch.object(shared, "_create_salesforce_refund_pending_case", return_value={"created": True}))
        self.stack.enter_context(mock.patch.object(shared, "_read_order_totals", side_effect=lambda *_: deepcopy(self.totals)))
        self.stack.enter_context(mock.patch.object(shared, "_get_order_live_state", side_effect=lambda *_: {
            **deepcopy(self.state), "grand_total": self.totals["grand_total"], "amount_paid": self.totals["paid"],
            "amount_due": self.totals["balance_due"]}))
        self.stack.enter_context(mock.patch.object(shared, "_read_payment_summary", side_effect=lambda *_: {
            "payment_type": self.payment_type, "amount": "200.00", "panel_text": self.payment_type}))
        for name in ("_activate_crm_context", "_wait_for_order_scope", "_open_salesforce_account", "_verify_salesforce_email"):
            self.stack.enter_context(mock.patch.object(shared, name))
        self.stack.enter_context(mock.patch.object(shared, "_crm_order_already_cancelled", return_value=False))
        self.status = self.stack.enter_context(mock.patch.object(copyright, "_active_status", return_value="Approved"))
        for name in ("_cancel_original_order", "_cancel_and_refund_crm_order", "_add_refund_fee_to_original", "_handle_post_cancel_stock_return"):
            self.stack.enter_context(mock.patch.object(shared, name, side_effect=AssertionError(f"Partial workflow reached {name}")))

    def remove(self, _shared, _driver, identifier):
        if self.zero_tab:
            self.rows = [{**row, "quantity": 0, "subtotal": "0.00"} if row["design_id"] == identifier else row for row in self.rows]
        else:
            self.rows = [row for row in self.rows if row["design_id"] != identifier]

    def save_order(self, _driver):
        total = sum(Decimal(row["subtotal"]) for row in self.rows)
        self.totals.update(grand_total=f"{total:.2f}", balance_due=f"{total - Decimal(self.totals['paid']):.2f}")

    def add_note(self, _driver, _reason, *, process):
        self.state["sales_notes"] = process.fixed_sales_note

    def refund_payment(self, _driver, dry_run, **kwargs):
        self.assertFalse(dry_run)
        self.assertEqual(kwargs["payment_amount"], "80.00")
        self.assertEqual(kwargs["partial_context"]["grand_total"], "120.00")
        kwargs["before_submit"]()
        self.totals.update(paid="120.00", balance_due="0.00")
        self.state["transactions"].append({"amount": "-80.00", "tag": "Refund", "type": "Refund"})

    def send_email(self, _driver, _handle, _order_id, _email, dry_run, **kwargs):
        self.assertEqual(kwargs["process"].salesforce_template, "[AUTO] Copyright Cancel - Partial")
        self.assertEqual(kwargs["designs"], [DESIGNS[1]])
        return {"sent": not dry_run, "dry_run": dry_run, "activity_verified": not dry_run}

    def run_partial(self, **kwargs):
        return copyright.process_partial(shared, self.driver, "crm", SELECTION, "Protected character", CONTACT,
                                         dry_run=kwargs.pop("dry_run", False), process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS, **kwargs)

    def test_partial_removes_only_selected_design_and_refunds_only_its_credit(self):
        result = self.run_partial()
        self.assertEqual(self.rows, DESIGNS[:1])
        self.assertFalse(result["cancel"]["cancelled"])
        self.assertTrue(result["cancel"]["verified_active"])
        self.assertEqual(result["refund"]["amount"], "80.00")
        self.assertTrue(result["refund"]["verified"])
        self.assertEqual(self.totals, {"grand_total": "120.00", "paid": "120.00", "balance_due": "0.00"})
        self.assertEqual(self.removal.call_args.args[2], "202")
        self.assertTrue(self.email.call_args.kwargs["receipt_scope"].startswith("copyright_cancel_partial:"))
        self.assertEqual(copyright.load_receipt(copyright.receipt_path(SELECTION, "Protected character"))["stage"], "complete")
        self.assertTrue(self.run_partial()["already_completed"])
        self.refund.assert_called_once()
        self.removal.assert_called_once()

    def test_ordered_stock_zero_tab_is_accepted_and_completed_retry_does_nothing(self):
        self.zero_tab = True
        result = self.run_partial()
        again = self.run_partial()
        self.assertEqual(result["removal"]["zeroed_design_ids"], ["202"])
        self.assertTrue(again["already_completed"])
        self.assertEqual(self.rows[0], DESIGNS[0])
        self.refund.assert_called_once()
        self.removal.assert_called_once()
        self.assertEqual(len([call for call in self.email.call_args_list if call.args[4] is False]), 1)

    def test_dry_run_verifies_partial_template_without_crm_saves_receipts_or_sends(self):
        result = self.run_partial(dry_run=True)
        self.assertTrue(result["salesforce"]["dry_run"])
        self.assertFalse(result["salesforce"]["sent"])
        self.email.assert_called_once()
        self.assertEqual(self.rows, DESIGNS)
        for mutation in (self.removal, self.save, self.note, self.refund, self.case):
            mutation.assert_not_called()
        self.assertFalse(copyright.receipt_path(SELECTION, "Protected character").exists())

    def test_missing_or_wrong_partial_template_stops_before_removal(self):
        self.email.side_effect = shared.CopyrightCancelError("Wrong template")
        with self.assertRaisesRegex(shared.CopyrightCancelError, "Wrong template"):
            self.run_partial()
        self.removal.assert_not_called()
        self.refund.assert_not_called()

    def test_uncertain_refund_is_not_reissued_and_later_persisted_refund_can_resume(self):
        def uncertain(*_args, **kwargs):
            kwargs["before_submit"]()
            raise RuntimeError("Connection lost during Save")
        self.refund.side_effect = uncertain
        with self.assertRaisesRegex(RuntimeError, "Connection lost"):
            self.run_partial()
        with self.assertRaisesRegex(ValueError, "unconfirmed"):
            self.run_partial()
        self.refund.assert_called_once()
        self.totals.update(paid="120.00", balance_due="0.00")
        self.state["transactions"].append({"amount": "-80.00", "tag": "Refund", "type": "Refund"})
        result = self.run_partial()
        self.assertTrue(result["refund"]["verified"])
        self.refund.assert_called_once()
        self.removal.assert_called_once()

    def test_email_failure_after_refund_resumes_without_another_refund(self):
        self.email.side_effect = [{"sent": False, "dry_run": True}, RuntimeError("Email unavailable"), {"sent": True}]
        with self.assertRaisesRegex(RuntimeError, "Email unavailable"):
            self.run_partial()
        self.assertTrue(self.run_partial()["refund"]["refunded"])
        self.refund.assert_called_once()
        self.removal.assert_called_once()

    def test_non_stripe_uses_partial_amount_case_and_never_stripe_refund(self):
        self.payment_type = "PayPal"
        result = self.run_partial()
        self.assertTrue(result["salesforce_refund_case"]["created"])
        self.assertEqual(self.case.call_args.kwargs["process"].refund_case_subject, "Copyright - Partial - $80.00")
        self.refund.assert_not_called()
        self.assertFalse(result["cancel"]["cancelled"])

    def test_uncertain_case_is_not_submitted_twice(self):
        self.payment_type = "PayPal"
        self.case.side_effect = RuntimeError("Case Save interrupted")
        with self.assertRaisesRegex(RuntimeError, "Case Save interrupted"):
            self.run_partial()
        with self.assertRaisesRegex(ValueError, "Case submission is unconfirmed"):
            self.run_partial()
        self.case.assert_called_once()

    def test_skipped_refund_does_not_send_the_refund_email(self):
        result = self.run_partial(click_refund_button=False)
        self.assertTrue(result["email_skipped"])
        self.refund.assert_not_called()
        self.assertEqual([call.args[4] for call in self.email.call_args_list], [True])

    def test_changed_order_and_preexisting_credit_stop_before_removal(self):
        self.totals.update(grand_total="199.00", balance_due="-1.00")
        with self.assertRaisesRegex(ValueError, "already has a credit"):
            self.run_partial()
        self.removal.assert_not_called()

    def test_changed_customer_email_cannot_bypass_existing_refund_email_receipt(self):
        self.run_partial(click_refund_button=False)
        with self.assertRaisesRegex(ValueError, 'customer email changed'):
            copyright.process_partial(shared, self.driver, 'crm', SELECTION, 'Protected character',
                {**CONTACT, 'email': 'changed@example.test'}, dry_run=False, process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS)
        self.refund.assert_not_called()

    def test_non_stripe_resume_after_email_reopens_salesforce_without_resending_email(self):
        self.payment_type = 'PayPal'
        self.case.side_effect = RuntimeError('Case Save interrupted')
        with self.assertRaises(RuntimeError):
            self.run_partial()
        path = copyright.receipt_path(SELECTION, 'Protected character')
        receipt = copyright.load_receipt(path)
        # Model an interruption after confirmed email, before Case submission.
        receipt['stage'] = 'email_sent'
        copyright.save_receipt(path, receipt)
        self.case.side_effect = None
        self.assertTrue(self.run_partial()['salesforce_refund_case']['created'])
        self.assertEqual([call.args[4] for call in self.email.call_args_list], [True, False])


class CopyrightRoutingTests(unittest.TestCase):
    def test_whole_order_helper_refuses_a_partial_selection_before_any_edits(self):
        with mock.patch.object(shared, '_append_copyright_cancel_sales_note') as note, \
             mock.patch.object(shared, '_cancel_original_order') as cancel, self.assertRaisesRegex(shared.CopyrightCancelError, 'every design tab'):
            shared._cancel_and_refund_crm_order(mock.Mock(), 'crm', '5451301', False, full_selection=SELECTION)
        note.assert_not_called()
        cancel.assert_not_called()

    def test_full_cancel_rechecks_saved_design_set_before_cancelling(self):
        full = {**SELECTION, 'selected_design_ids': ['101', '202']}
        with ExitStack() as stack:
            for name in ('_activate_crm_context', '_wait_for_order_scope', '_append_copyright_cancel_sales_note'):
                stack.enter_context(mock.patch.object(shared, name))
            stack.enter_context(mock.patch.object(copyright, 'scan_designs', return_value=DESIGNS[:1]))
            cancel = stack.enter_context(mock.patch.object(shared, '_cancel_original_order'))
            with self.assertRaisesRegex(ValueError, 'changed'):
                shared._cancel_and_refund_crm_order(mock.Mock(), 'crm', '5451301', False, full_selection=full)
            cancel.assert_not_called()

    def test_partial_never_enters_whole_order_cancellation_refund_or_stock_routing(self):
        driver = mock.Mock()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(shared, "_open_driver", return_value=driver))
            for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                         "_wait_for_order_scope", "safe_driver_quit"):
                stack.enter_context(mock.patch.object(shared, name))
            stack.enter_context(mock.patch.object(shared, "_wait_for_crm_contact_info", return_value=CONTACT))
            partial = stack.enter_context(mock.patch.object(copyright, "process_partial", return_value={"cancel": {"cancelled": False}}))
            forbidden = [stack.enter_context(mock.patch.object(shared, name, side_effect=AssertionError(name)))
                         for name in ("_cancel_and_refund_crm_order", "_handle_post_cancel_stock_return", "_read_payment_summary")]
            result = shared.process_single_order("5451301", "Reason", designs=SELECTION, dry_run=False)
        self.assertEqual(result["cancellation_scope"], "partial")
        self.assertEqual(partial.call_args.args[3], SELECTION)
        for function in forbidden:
            function.assert_not_called()

    def test_all_tabs_use_existing_full_cancel_and_template_with_selected_names(self):
        full = {**SELECTION, "selected_design_ids": ["101", "202"]}
        with ExitStack() as stack:
            driver = mock.Mock()
            stack.enter_context(mock.patch.object(shared, "_open_driver", return_value=driver))
            for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                         "_wait_for_order_scope", "safe_driver_quit", "_handle_post_cancel_stock_return"):
                stack.enter_context(mock.patch.object(shared, name))
            stack.enter_context(mock.patch.object(shared, "_wait_for_crm_contact_info", return_value=CONTACT))
            stack.enter_context(mock.patch.object(shared, "_read_payment_summary", return_value={}))
            stack.enter_context(mock.patch.object(copyright, "assert_no_other_pending"))
            stack.enter_context(mock.patch.object(copyright, "scan_designs", return_value=DESIGNS))
            partial = stack.enter_context(mock.patch.object(copyright, "process_partial", side_effect=AssertionError("partial path")))
            cancel = stack.enter_context(mock.patch.object(shared, "_cancel_and_refund_crm_order", return_value={}))
            email = stack.enter_context(mock.patch.object(shared, "_prepare_and_maybe_send_salesforce_email", return_value={"sent": True}))
            result = shared.process_single_order("5451301", "Reason", designs=full, dry_run=False)
        self.assertEqual(result["cancellation_scope"], "full")
        cancel.assert_called_once()
        partial.assert_not_called()
        self.assertEqual(email.call_args.kwargs["process"], shared.COPYRIGHT_CANCEL_PROCESS)
        self.assertEqual(email.call_args.kwargs["designs"], DESIGNS)

    def test_queue_retains_complete_scan_and_worker_receives_copyright_argument(self):
        with mock.patch.object(server, "enqueue_automation", return_value=(True, "Queued", {})) as enqueue:
            ok, _, _ = server.queue_crm_extension_manual_order_run("5451301", "copyright_cancel", "Reason",
                                                                  request_payload={"order_id": "5451301", "designs": SELECTION})
        self.assertTrue(ok)
        self.assertEqual(enqueue.call_args.kwargs["task_arguments"]["designs"], SELECTION)
        with mock.patch.object(server, "_run_script", return_value=(True, "Done", {})) as runner:
            server._execute_crm_mass_emailer_worker(action="process_order", order_id="5451301", process="copyright_cancel", designs=SELECTION)
        args = runner.call_args.args[1]
        self.assertEqual(json.loads(args[args.index("--copyright-designs-json") + 1]), SELECTION)
        self.assertNotIn("--emb-designs-json", args)


if __name__ == "__main__":
    unittest.main()
