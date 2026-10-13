import copy
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "workers"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import crm_auto_splitter as splitter
import server


def source_report():
    designs = [
        {"design_id": str(100 + index), "design_name": f"Design {index}",
         "tab_number": index, "quantity": 1, "subtotal": "10.00", "stock": {"state": "not_ordered"}}
        for index in range(1, 18)
    ]
    plan = splitter._plan_with_original_retained(splitter._build_split_plan(
        designs, 2, "5422414", shipping_amount=splitter.Decimal("20.00"),
    ))
    return {
        "original_order_id": "5422414", "retain_original": True,
        "expected_tab_count": 17, "divisions": 2, "minimum_tabs": 10,
        "designs": designs, "split_plan": plan,
        "totals": {"shipping": "20.00", "grand_total": "190.00", "paid": "190.00"},
    }


class RetainedOriginalVerificationTests(unittest.TestCase):
    def verify(self, quantity=0, subtotal="0.00", marker_quantity=0, ids=None, keep_quantity=1, shipping="10.00"):
        plan = {"keep_design_ids": [101], "delete_design_ids": [202],
                "keep_design_quantities": {"101": 1}, "shipping_charge": "10.00"}
        state = {
            "designs": [{"design_id": 101, "tab_number": 1}, {"design_id": 202, "tab_number": 2}],
            "shipping_charges": shipping,
            "body_text": f"Design 1 Total: {keep_quantity} pieces $10.00\nDesign 2 Total: {quantity} pieces ${subtotal}",
        }
        markers = [{"tab_number": 1, "text": f"1 - QTY: {keep_quantity}"},
                   {"tab_number": 2, "text": f"2 - QTY: {marker_quantity}"}]
        with mock.patch.object(splitter, "_order_design_ids", return_value=ids or [101, 202]), \
             mock.patch.object(splitter, "_order_scope", return_value=state), \
             mock.patch.object(splitter, "_visible_design_tab_numbers", return_value=markers):
            return splitter._verify_retained_original_designs(mock.Mock(), plan)

    def test_accepts_persisted_empty_moved_tab(self):
        self.assertEqual(self.verify(), {"passed": True, "empty_design_ids": [202]})

    def test_rejects_moved_products_even_when_total_is_zero(self):
        with self.assertRaisesRegex(splitter.RecoverableCrmError, "quantity verification"):
            self.verify(quantity=1, marker_quantity=1)

    def test_rejects_empty_tab_with_remaining_charge(self):
        with self.assertRaisesRegex(splitter.RecoverableCrmError, "nonzero or unreadable total"):
            self.verify(subtotal="5.00")

    def test_rejects_unknown_or_disagreeing_quantities(self):
        for quantity, marker in (("unknown", 0), (0, 1)):
            with self.subTest(quantity=quantity, marker=marker), self.assertRaises(splitter.RecoverableCrmError):
                self.verify(quantity=quantity, marker_quantity=marker)

    def test_rejects_missing_kept_or_unplanned_designs(self):
        for ids in ([202], [101, 202, 303]):
            with self.subTest(ids=ids), self.assertRaisesRegex(splitter.RecoverableCrmError, "design verification"):
                self.verify(ids=ids)

    def test_rejects_changed_retained_quantity_and_shipping(self):
        for changed in ({"keep_quantity": 0}, {"shipping": "20.00"}):
            with self.subTest(changed=changed), self.assertRaises(splitter.RecoverableCrmError):
                self.verify(**changed)


class RetainedSplitStockTests(unittest.TestCase):
    def recheck(self, *, ordered=True, readable=True, trimmed=True):
        report = source_report()
        scan = {"designs": copy.deepcopy(report["designs"])}
        for design in scan["designs"]:
            if design["tab_number"] > 9:
                if trimmed:
                    design.update(quantity=0, subtotal="0.00")
                design["stock"] = {"state": "ordered" if ordered else "not_ordered"}
                if ordered and readable:
                    design["stock"]["manual_order_rows"] = [{"vendor": "SanMar", "po": "PO-5422414"}]
        scan["stock_summary"] = splitter._summarize_original_stock(scan["designs"], {
            "stock_status_ordered": ordered, "stock_status_needs_order": not ordered,
        })
        splits = [{"split_index": 2, "order_id": "5422490"}]
        with mock.patch.object(splitter, "_open_order_scope_with_reload"), \
             mock.patch.object(splitter, "_scan_original_order", return_value=scan), \
             mock.patch.object(splitter, "_copy_stock_records_to_split_orders", return_value={"verified": True}) as stock_copy:
            if ordered and not readable:
                with self.assertRaisesRegex(splitter.SplitterError, "requires manual review"):
                    splitter._recheck_split_stock(mock.Mock(), "5422414", "original", report["split_plan"], splits, report)
                stock_copy.assert_not_called()
                return
            splitter._recheck_split_stock(mock.Mock(), "5422414", "original", report["split_plan"], splits, report)
        return report, splits, stock_copy

    def test_new_stock_follows_existing_split_with_original_product_quantities(self):
        for trimmed in (False, True):
            with self.subTest(trimmed=trimmed):
                report, splits, stock_copy = self.recheck(trimmed=trimmed)
                stock_copy.assert_not_called()
                self.assertEqual(splits[0]["order_id"], "5422490")
                self.assertEqual(len(splits[0]["stock_transfer_records"]), 8)
                self.assertEqual({r["source_quantity"] for r in splits[0]["stock_transfer_records"]}, {1})
                self.assertEqual({r["source_subtotal"] for r in splits[0]["stock_transfer_records"]}, {"10.00"})
                self.assertEqual(report["split_plan"][1]["stock_transfer_records"], splits[0]["stock_transfer_records"])

    def test_unknown_new_stock_stops_for_review(self):
        self.recheck(readable=False)

    def test_no_stock_does_not_copy_records(self):
        report = source_report()
        scan = {"designs": report["designs"], "stock_summary": {
            "stock_ordered": False, "order_stock_status": {"stock_status_needs_order": True},
        }}
        with mock.patch.object(splitter, "_open_order_scope_with_reload"), \
             mock.patch.object(splitter, "_scan_original_order", return_value=scan), \
             mock.patch.object(splitter, "_copy_stock_records_to_split_orders") as stock_copy:
            splitter._recheck_split_stock(mock.Mock(), "5422414", "original", report["split_plan"], [{"split_index": 2, "order_id": "5422490"}], report)
        stock_copy.assert_not_called()

    def test_previously_copied_stock_survives_physical_removal_of_source_tabs(self):
        report, splits, _ = self.recheck()
        scan = {"designs": report["designs"][:9], "stock_summary": {"stock_ordered": True}}
        with mock.patch.object(splitter, "_open_order_scope_with_reload"), \
             mock.patch.object(splitter, "_scan_original_order", return_value=scan), \
             mock.patch.object(splitter, "_copy_stock_records_to_split_orders") as stock_copy:
            splitter._recheck_split_stock(mock.Mock(), "5422414", "original", report["split_plan"], splits, report)
        stock_copy.assert_not_called()
        self.assertEqual(len(splits[0]["stock_transfer_records"]), 8)


class RetainedSplitRecoveryTests(unittest.TestCase):
    def test_empty_source_tabs_without_recovery_block_new_splits(self):
        report = source_report()
        scan = {"detected_tab_count": 17, "designs": copy.deepcopy(report["designs"])}
        scan["designs"][9]["quantity"] = 0
        with ExitStack() as stack:
            for name, value in {
                "kill_stale_chrome": None, "_build_splitter_driver": mock.Mock(),
                "safe_get_with_partial_load": None, "_handle_login_if_needed": None,
                "_switch_to_crm_app_frame": None, "_scan_original_order": scan,
                "safe_take_screenshot": None, "safe_driver_quit": None,
            }.items():
                stack.enter_context(mock.patch.object(splitter, name, return_value=value))
            create = stack.enter_context(mock.patch.object(splitter, "_prepare_and_save_split_quote"))
            write = stack.enter_context(mock.patch.object(splitter, "_write_result"))
            code = splitter.run_split_order(order_id="5422414", dry_run=False)
        self.assertEqual(code, 4)
        self.assertIn("saved split recovery", write.call_args.args[1])
        create.assert_not_called()

    def test_existing_split_quantity_mismatch_stops_before_payment_or_fee_changes(self):
        report = source_report()
        scan = {"designs": copy.deepcopy(report["designs"][9:])}
        scan["designs"][0]["quantity"] = 2
        with mock.patch.object(splitter, "_open_order_scope_with_reload"), \
             mock.patch.object(splitter, "_scan_original_order", return_value=scan), \
             mock.patch.object(splitter, "_add_discount_fee_to_split_order") as fee, \
             mock.patch.object(splitter, "_reconcile_existing_split_payment_after_discount") as payment:
            with self.assertRaisesRegex(splitter.SplitterError, "quantity does not match"):
                splitter._inspect_existing_split_order(mock.Mock(), "5422490", report["split_plan"], {1})
        fee.assert_not_called()
        payment.assert_not_called()

    def test_legacy_receipt_recovers_original_plan_after_stock_arrives_and_tabs_are_empty(self):
        report = source_report()
        for split in report["split_plan"]:
            split.pop("keep_design_quantities")
        scan = {"designs": copy.deepcopy(report["designs"])}
        for design in scan["designs"][9:]:
            design["quantity"] = 0
        recovered = splitter._retained_resume_plan(report, "5422414", None, None, 10, scan)
        self.assertTrue(recovered[0]["retained_original"])
        self.assertEqual(recovered[0]["shipping_charge"], "10.00")
        self.assertEqual(recovered[1]["keep_design_quantities"]["110"], 1)

    def test_trimmed_original_can_resume_below_split_threshold(self):
        report = source_report()
        scan = {"designs": report["designs"][:9]}
        self.assertEqual(len(splitter._retained_resume_plan(report, "5422414", 17, 2, 10, scan)), 2)

    def test_recovery_rejects_changed_plan_or_products(self):
        for field in ("plan", "name", "quantity", "order"):
            report = source_report()
            scan = {"designs": copy.deepcopy(report["designs"])}
            if field == "plan":
                report["split_plan"][0]["shipping_charge"] = "20.00"
            elif field == "name":
                scan["designs"][0]["design_name"] = "Changed design"
            elif field == "quantity":
                scan["designs"][0]["quantity"] = 2
            else:
                report["original_order_id"] = "9999999"
            with self.subTest(field=field), self.assertRaises(splitter.SplitterError):
                splitter._retained_resume_plan(report, "5422414", 17, 2, 10, scan)

    def test_server_passes_saved_plan_to_preflight_and_live_worker_and_removes_temp_file(self):
        report = source_report()
        payload = {"success": False, "dry_run": False, "target_order_id": "5422414",
                   "expected_tab_count": 17, "divisions": 2, "new_order_ids": ["5422490"],
                   "report": {**report, "partial": True}}
        switched_report = copy.deepcopy(payload)
        switched_report["report"]["retain_original"] = False
        switched_report["report"]["stock_method_transition"] = {"from": "retain_as_split_1", "to": "cancel_after_split"}
        for receipt, dry_run in ((payload, True), (payload, False), (switched_report, True), (switched_report, False)):
            with self.subTest(dry_run=dry_run, switched=not receipt["report"]["retain_original"]), tempfile.TemporaryDirectory() as folder:
                captured = []

                def run_script(_script, args, *_pos, **_kwargs):
                    path = Path(args[args.index("--resume-report-file") + 1])
                    captured.append(path)
                    self.assertEqual(json.loads(path.read_text())["split_plan"], report["split_plan"])
                    self.assertEqual(args[args.index("--resume-existing-order-id") + 1], "5422490")
                    return True, "Ready", {}

                with mock.patch.object(server, "load_crm_state", return_value={"last_auto_splitter_recovery_payload": receipt}), \
                     mock.patch.object(server, "result_file", side_effect=lambda name: str(Path(folder) / name)), \
                     mock.patch.object(server, "_run_script", side_effect=run_script):
                    server._execute_crm_auto_splitter_worker("5422414", None, None, dry_run=dry_run)
                self.assertEqual(len(captured), 1)
                self.assertFalse(captured[0].exists())

    def test_live_recovery_reuses_split_and_finishes_retained_original_without_creating_or_cancelling_orders(self):
        report = source_report()
        current_scan = {"detected_tab_count": 9, "designs": copy.deepcopy(report["designs"][:9]),
                        "totals": {"shipping": "10.00", "grand_total": "100.00", "paid": "190.00"},
                        "stock_summary": {"stock_ordered": False, "order_stock_status": {"stock_status_needs_order": True}}}
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / "resume.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            driver = mock.Mock()
            patches = {
                "kill_stale_chrome": None, "_build_splitter_driver": driver,
                "safe_get_with_partial_load": None, "_handle_login_if_needed": None,
                "_switch_to_crm_app_frame": None, "_scan_original_order": current_scan,
                "_get_order_live_state": {"grand_total": "100.00", "amount_paid": "190.00"},
                "_get_original_payment_info": {"payment_type": "Stripe.com", "transaction_id": "fixture"},
                "_inspect_existing_split_order": {"split_index": 2, "order_id": "5422490", "totals": {"grand_total": "90.00"}},
                "_open_order_scope_with_reload": None, "_recheck_split_stock": current_scan,
                "_original_transfer_note_is_present": True,
                "_configure_retained_original_order": {"split_index": 1, "order_id": "5422414", "retained_original": True, "totals": {"grand_total": "100.00"}, "payment_allocation": "100.00"},
                "_allocate_retained_split_payments": {"passed": True},
                "_verify_retained_original_designs": {"passed": True},
                "_read_order_totals": {"grand_total": "100.00", "paid": "100.00", "balance_due": "0.00"},
                "_original_order_is_cancelled": False, "_existing_original_refund_fee_amount": "0.00",
                "safe_take_screenshot": None, "safe_driver_quit": None,
            }
            mocks = {name: stack.enter_context(mock.patch.object(splitter, name, return_value=value)) for name, value in patches.items()}
            create = stack.enter_context(mock.patch.object(splitter, "_prepare_and_save_split_quote"))
            cancel = stack.enter_context(mock.patch.object(splitter, "_finalize_original_order_after_split"))
            write = stack.enter_context(mock.patch.object(splitter, "_write_result"))
            code = splitter.run_split_order(order_id="5422414", dry_run=False, resume_existing_order_ids=["5422490"], resume_report_file=str(path))
        self.assertEqual(code, 0, write.call_args)
        create.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual(mocks["_recheck_split_stock"].call_count, 3)
        self.assertEqual(write.call_args.kwargs["new_order_ids"], ["5422490"])
        self.assertEqual(write.call_args.kwargs["report"]["original_grand_total"], "190.00")


class StockMethodTransitionTests(unittest.TestCase):
    def transition(self, *, trimmed=False, previous_transfer=False, paid="190.00", fail_creation=False, fail_conversion=False):
        report = source_report()
        report.update(stock_routing={"action": "copy_to_split_orders"}, original_order_final_steps={})
        scan = {"detected_tab_count": 9 if trimmed else 17,
                "designs": copy.deepcopy(report["designs"][:9] if trimmed else report["designs"]),
                "totals": {"subtotal_before_tax": "100.00" if trimmed else "190.00"}}
        state = {"amount_paid": paid, "grand_total": "100.00" if trimmed else "190.00", "transactions": []}
        if previous_transfer:
            state["transactions"] = [{"tag": "Refund", "amount": "-90.00", "note": "split 1 retained on original 5422414; transferred to 5422490"}]
        orders = [{"split_index": 2, "order_id": "5422490", "totals": {"grand_total": "90.00", "paid": "90.00" if previous_transfer else "0.00"}}]
        events = []

        def record_payment(_driver, order_id, _tag, _tx, amount, expected_grand_total=None):
            events.append(("payment", order_id, str(amount)))
            return {"grand_total": str(expected_grand_total), "paid": str(amount),
                    "balance_due": str(expected_grand_total - amount), "passed": True}

        def verify_order(_driver, order_id, _plan, used_split_indexes=None):
            return dict(next(order for order in orders if order["order_id"] == order_id))

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(splitter, "_get_order_live_state", return_value=state))
            stack.enter_context(mock.patch.object(splitter, "_existing_original_refund_fee_amount", return_value=splitter.Decimal("0.00")))
            stack.enter_context(mock.patch.object(splitter, "_open_order_scope_with_reload"))
            stack.enter_context(mock.patch.object(splitter, "_original_order_is_cancelled", return_value=False))
            stack.enter_context(mock.patch.object(splitter, "_inspect_existing_split_order", side_effect=verify_order))
            prepare = stack.enter_context(mock.patch.object(splitter, "_prepare_and_save_split_quote", return_value=({}, {}, {})))
            if fail_creation:
                prepare.side_effect = splitter.RecoverableCrmError("fixture quote error")
            finalize = stack.enter_context(mock.patch.object(splitter, "_finalize_split_quote_with_order_discount", return_value=(
                "5422491", {}, {"grand_total": "100.00", "paid": "0.00"}, {"passed": True})))
            if fail_conversion:
                finalize.side_effect = splitter.SplitterError("fixture conversion unconfirmed")
            stack.enter_context(mock.patch.object(splitter, "_copy_stock_records_to_split_orders", side_effect=lambda *_a: events.append(("stock",))))
            stack.enter_context(mock.patch.object(splitter, "_record_split_payment_on_order", side_effect=record_payment))
            checkpoint = stack.enter_context(mock.patch.object(splitter, "_write_result"))
            if fail_creation or fail_conversion:
                with self.assertRaisesRegex(splitter.SplitterError, "fixture quote error|fixture conversion unconfirmed"):
                    splitter._switch_split_method_for_ordered_stock(mock.Mock(), "5422414", "original", report["split_plan"], orders, report, scan, "Stripe.com", "fixture", None, 17, 2, 0)
            else:
                note = splitter._switch_split_method_for_ordered_stock(mock.Mock(), "5422414", "original", report["split_plan"], orders, report, scan, "Stripe.com", "fixture", None, 17, 2, 0)
                self.assertEqual(note, "transferred to 5422491 and 5422490")
        return report, orders, scan, prepare, finalize, checkpoint, events

    def test_stock_switch_creates_only_replacement_first_split_and_copies_stock_before_payment(self):
        report, orders, _scan, prepare, finalize, _checkpoint, events = self.transition()
        self.assertFalse(report["retain_original"])
        self.assertEqual([item["order_id"] for item in orders], ["5422491", "5422490"])
        self.assertTrue(all(not split["retained_original"] for split in report["split_plan"]))
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args[4]["split_index"], 1)
        self.assertEqual(finalize.call_args.args[2], "")
        self.assertEqual(events, [("stock",), ("payment", "5422491", "100.00"), ("payment", "5422490", "90.00")])

    def test_trimmed_source_uses_current_subtotal_and_original_payment_amount(self):
        report, _orders, _scan, prepare, *_ = self.transition(trimmed=True)
        self.assertEqual(prepare.call_args.args[3], 9)
        method = report["stock_method_transition"]
        self.assertEqual(method["original_refund_fee_amount"], "100.00")
        self.assertEqual(method["original_refund_payment_amount"], "190.00")
        self.assertEqual(method["allocation_original_paid"], "190.00")

    def test_stock_after_retained_payment_moves_only_remaining_original_payment(self):
        report, _orders, _scan, _prepare, _finalize, _checkpoint, events = self.transition(trimmed=True, previous_transfer=True, paid="100.00")
        method = report["stock_method_transition"]
        self.assertEqual(method["original_refund_payment_amount"], "100.00")
        self.assertEqual(method["allocation_original_paid"], "190.00")
        self.assertEqual(events[-1], ("payment", "5422490", "90.00"))

    def test_partial_payment_is_preserved_across_new_orders(self):
        report, _orders, _scan, _prepare, _finalize, _checkpoint, events = self.transition(paid="70.00")
        method = report["stock_method_transition"]
        self.assertEqual(method["original_refund_payment_amount"], "70.00")
        self.assertEqual(events[-2:], [("payment", "5422491", "36.84"), ("payment", "5422490", "33.16")])

    def test_switch_checkpoint_survives_replacement_creation_failure(self):
        report, orders, scan, _prepare, _finalize, checkpoint, events = self.transition(trimmed=True, fail_creation=True)
        self.assertEqual(events, [])
        self.assertEqual([order["order_id"] for order in orders], ["5422490"])
        self.assertEqual(checkpoint.call_args.kwargs["checkpoint_stage"], "stock_method_switched")
        recovered = splitter._retained_resume_plan(report, "5422414", 17, 2, 10, scan)
        self.assertTrue(all(not split["retained_original"] for split in recovered))

    def test_retry_after_original_refund_reuses_orders_and_saved_financial_amounts(self):
        report, orders, scan, *_ = self.transition(trimmed=True)
        with mock.patch.object(splitter, "_get_order_live_state", return_value={"amount_paid": "0.00"}), \
             mock.patch.object(splitter, "_open_order_scope_with_reload"), \
             mock.patch.object(splitter, "_original_refund_transaction_is_present", return_value=True), \
             mock.patch.object(splitter, "_inspect_existing_split_order", side_effect=lambda _d, order_id, _p, **_k: dict(next(order for order in orders if order["order_id"] == order_id))), \
             mock.patch.object(splitter, "_prepare_and_save_split_quote") as create, \
             mock.patch.object(splitter, "_copy_stock_records_to_split_orders"), \
             mock.patch.object(splitter, "_allocate_retained_split_payments"), \
             mock.patch.object(splitter, "_write_result"):
            splitter._switch_split_method_for_ordered_stock(mock.Mock(), "5422414", "original", report["split_plan"], orders, report, scan, "Stripe.com", "fixture", None, 17, 2, 0)
        create.assert_not_called()
        self.assertEqual(report["stock_method_transition"]["original_refund_payment_amount"], "190.00")
        self.assertEqual(len(orders), 2)

    def test_unconfirmed_conversion_blocks_duplicate_replacement_on_retry(self):
        report, orders, scan, *_ = self.transition(fail_conversion=True)
        self.assertEqual(report["stock_method_transition"]["replacement_creation"]["state"], "conversion_started")
        with mock.patch.object(splitter, "_get_order_live_state", return_value={"amount_paid": "190.00"}), \
             mock.patch.object(splitter, "_prepare_and_save_split_quote") as create, \
             mock.patch.object(splitter, "_write_result"):
            with self.assertRaisesRegex(splitter.SplitterError, "conversion is unconfirmed"):
                splitter._switch_split_method_for_ordered_stock(mock.Mock(), "5422414", "original", report["split_plan"], orders, report, scan, "Stripe.com", "fixture", None, 17, 2, 0)
        create.assert_not_called()

    def test_worker_switches_before_trim_after_save_and_after_payment(self):
        for stage in ("before_trim", "after_save", "save_verification_error", "after_payment"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                report = source_report()
                path = Path(folder) / "resume.json"
                path.write_text(json.dumps(report), encoding="utf-8")
                no_stock = {"detected_tab_count": 17, "designs": report["designs"],
                            "totals": report["totals"], "stock_summary": {"stock_ordered": False, "order_stock_status": {"stock_status_needs_order": True}}}
                trimmed = stage != "before_trim"
                stock = {"detected_tab_count": 9 if trimmed else 17,
                         "designs": report["designs"][:9] if trimmed else report["designs"],
                         "totals": {"subtotal_before_tax": "100.00" if trimmed else "190.00"},
                         "stock_summary": {"stock_ordered": True}}
                initial_state = {"amount_paid": "190.00", "grand_total": "190.00"}
                switch_state = {"amount_paid": "100.00" if stage == "after_payment" else "190.00", "grand_total": "100.00" if trimmed else "190.00"}
                if stage == "after_payment":
                    switch_state["transactions"] = [{"tag": "Refund", "amount": "-90.00", "note": "split 1 retained on original 5422414; transferred to 5422490"}]
                checks = iter([no_stock] * (2 if stage == "after_payment" else 1 if trimmed else 0) + [stock])

                def recheck(*args):
                    checked = next(checks)
                    args[-1]["stock_routing"] = {"action": "copy_to_split_orders" if checked["stock_summary"]["stock_ordered"] else "none"}
                    return checked

                def inspect(_driver, order_id, _plan, **_kwargs):
                    index = 2 if order_id == "5422490" else 1
                    return {"split_index": index, "order_id": order_id, "totals": {"grand_total": "90.00" if index == 2 else "100.00"}}

                def pay(_d, _id, _tag, _tx, amount, expected_grand_total=None):
                    return {"grand_total": str(expected_grand_total), "paid": str(amount), "balance_due": str(expected_grand_total - amount), "passed": True}

                patches = {
                    "kill_stale_chrome": None, "_build_splitter_driver": mock.Mock(),
                    "safe_get_with_partial_load": None, "_handle_login_if_needed": None,
                    "_switch_to_crm_app_frame": None, "_scan_original_order": no_stock,
                    "_get_original_payment_info": {"payment_type": "Stripe.com", "transaction_id": "fixture"},
                    "_open_order_scope_with_reload": None, "_original_transfer_note_is_present": True,
                    "_configure_retained_original_order": {"split_index": 1, "order_id": "5422414", "retained_original": True, "totals": {"grand_total": "100.00"}},
                    "_verify_retained_original_designs": {"passed": True},
                    "_read_order_totals": {"grand_total": "100.00", "paid": "100.00", "balance_due": "0.00"},
                    "_record_retained_original_payment_allocation": {"totals": {"grand_total": "100.00", "paid": "100.00", "balance_due": "0.00"}, "verification": {"passed": True}},
                    "_original_order_is_cancelled": False, "_existing_original_refund_fee_amount": splitter.Decimal("0.00"),
                    "_copy_stock_records_to_split_orders": {"verified": True},
                    "_prepare_and_save_split_quote": ({}, {}, {"quote_id": 123}),
                    "_finalize_split_quote_with_order_discount": ("5422491", {}, {"grand_total": "100.00", "paid": "0.00"}, {"passed": True}),
                    "_finalize_original_order_after_split": {"cancelled": True, "verification": {"passed": True}},
                    "safe_take_screenshot": None, "safe_driver_quit": None,
                }
                mocks = {name: stack.enter_context(mock.patch.object(splitter, name, return_value=value)) for name, value in patches.items()}
                if stage == "save_verification_error":
                    mocks["_configure_retained_original_order"].side_effect = splitter.RecoverableCrmError("fixture verification failed")
                stack.enter_context(mock.patch.object(splitter, "_get_order_live_state", side_effect=[initial_state, switch_state, switch_state]))
                stack.enter_context(mock.patch.object(splitter, "_recheck_split_stock", side_effect=recheck))
                stack.enter_context(mock.patch.object(splitter, "_inspect_existing_split_order", side_effect=inspect))
                stack.enter_context(mock.patch.object(splitter, "_record_split_payment_on_order", side_effect=pay))
                write = stack.enter_context(mock.patch.object(splitter, "_write_result"))
                code = splitter.run_split_order(order_id="5422414", dry_run=False, resume_existing_order_ids=["5422490"], resume_report_file=str(path))
                self.assertEqual(code, 0, write.call_args)
                mocks["_prepare_and_save_split_quote"].assert_called_once()
                self.assertEqual(mocks["_prepare_and_save_split_quote"].call_args.args[3], 9 if trimmed else 17)
                self.assertEqual(mocks["_configure_retained_original_order"].call_count, 1 if trimmed else 0)
                mocks["_finalize_original_order_after_split"].assert_called_once()
                self.assertEqual(mocks["_finalize_original_order_after_split"].call_args.args[2:4], (
                    splitter.Decimal("100.00" if trimmed else "190.00"), splitter.Decimal("100.00" if stage == "after_payment" else "190.00")))
                self.assertEqual(write.call_args.kwargs["new_order_ids"], ["5422491", "5422490"])
                self.assertFalse(write.call_args.kwargs["report"]["retain_original"])

    def test_stocked_order_header_uses_existing_header_only_routing(self):
        report = source_report()
        scan = {"detected_tab_count": 17, "designs": copy.deepcopy(report["designs"])}
        for design in scan["designs"]:
            design["stock"] = {"state": "ordered_header_only"}
        scan["stock_summary"] = splitter._summarize_original_stock(scan["designs"])
        with mock.patch.object(splitter, "_open_order_scope_with_reload"), mock.patch.object(splitter, "_scan_original_order", return_value=scan):
            checked = splitter._recheck_split_stock(mock.Mock(), "5422414", "original", report["split_plan"], [], report)
        self.assertTrue(checked["stock_summary"]["stock_ordered"])
        self.assertEqual(report["stock_routing"]["action"], "header_only_no_transfer")

    def test_worker_resumes_switched_method_after_original_payment_was_transferred(self):
        receipt, existing_orders, scan, *_ = self.transition(trimmed=True)
        receipt["payment_transfer"] = {"original_payment_type": "Stripe.com", "transaction_id": "fixture"}
        scan.update(stock_summary={"stock_ordered": True}, totals={"grand_total": "0.00", "paid": "0.00"})
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            path = Path(folder) / "resume.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            patches = {
                "kill_stale_chrome": None, "_build_splitter_driver": mock.Mock(),
                "safe_get_with_partial_load": None, "_handle_login_if_needed": None,
                "_switch_to_crm_app_frame": None, "_scan_original_order": scan,
                "_get_order_live_state": {"grand_total": "0.00", "amount_paid": "0.00"},
                "_open_order_scope_with_reload": None, "_recheck_split_stock": scan,
                "_original_refund_transaction_is_present": True, "_existing_original_refund_fee_amount": splitter.Decimal("100.00"),
                "_copy_stock_records_to_split_orders": {"verified": True},
                "_allocate_retained_split_payments": {"passed": True},
                "_finalize_original_order_after_split": {"cancelled": True, "verification": {"passed": True}},
                "safe_take_screenshot": None, "safe_driver_quit": None,
            }
            mocks = {name: stack.enter_context(mock.patch.object(splitter, name, return_value=value)) for name, value in patches.items()}
            stack.enter_context(mock.patch.object(splitter, "_inspect_existing_split_order", side_effect=lambda _d, order_id, _p, **_k: dict(next(item for item in existing_orders if item["order_id"] == order_id))))
            create = stack.enter_context(mock.patch.object(splitter, "_prepare_and_save_split_quote"))
            payment_info = stack.enter_context(mock.patch.object(splitter, "_get_original_payment_info"))
            write = stack.enter_context(mock.patch.object(splitter, "_write_result"))
            code = splitter.run_split_order(order_id="5422414", dry_run=False, resume_existing_order_ids=["5422491", "5422490"], resume_report_file=str(path))
        self.assertEqual(code, 0, write.call_args)
        create.assert_not_called()
        payment_info.assert_not_called()
        self.assertEqual(mocks["_allocate_retained_split_payments"].call_args.args[2], splitter.Decimal("190.00"))
        self.assertEqual(mocks["_finalize_original_order_after_split"].call_args.args[2:4], (splitter.Decimal("100.00"), splitter.Decimal("190.00")))
        self.assertEqual(write.call_args.kwargs["new_order_ids"], ["5422491", "5422490"])


@unittest.skipUnless(os.environ.get("SPLITTER_FIXTURE_CHROMEDRIVER"), "Set SPLITTER_FIXTURE_CHROMEDRIVER for local browser verification")
class RetainedOrderBrowserTests(unittest.TestCase):
    def test_real_dom_accepts_empty_moved_tab_and_rejects_remaining_pieces(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service

        profile_root = ROOT / "runtime" / "generated_profiles"
        profile_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="splitter_fixture_", dir=profile_root) as folder:
            options = webdriver.ChromeOptions()
            if os.environ.get("SPLITTER_FIXTURE_CHROME"):
                options.binary_location = os.environ["SPLITTER_FIXTURE_CHROME"]
            for flag in ("--headless=new", "--disable-gpu", "--disable-background-networking", "--disable-sync", "--no-first-run", "--no-default-browser-check"):
                options.add_argument(flag)
            options.add_argument(f"--user-data-dir={folder}/chrome")
            service = Service(os.environ["SPLITTER_FIXTURE_CHROMEDRIVER"], log_output=str(Path(folder) / "driver.log"))
            driver = webdriver.Chrome(service=service, options=options)
            try:
                driver.get((ROOT / "tests" / "fixtures" / "crm_retained_order.html").as_uri())
                plan = {"keep_design_ids": [101], "delete_design_ids": [202], "keep_design_quantities": {"101": 1}, "shipping_charge": "10.00"}
                self.assertEqual(splitter._verify_retained_original_designs(driver, plan)["empty_design_ids"], [202])
                driver.execute_script("document.getElementById('moved-tab').innerText = 'Design 2\\n2 - QTY: 1\\nDesign Previews'; document.getElementById('moved-quantity').innerText = '1 pieces';")
                with self.assertRaisesRegex(splitter.RecoverableCrmError, "quantity verification"):
                    splitter._verify_retained_original_designs(driver, plan)
            finally:
                driver.quit()


if __name__ == "__main__":
    unittest.main()
