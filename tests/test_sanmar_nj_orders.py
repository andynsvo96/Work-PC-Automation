import copy
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

import crm_shipping_bypasser as worker
import server


NJ = worker.NJ_WAREHOUSE
VA = "Richmond, VA"
OH = "Cincinnati, OH"


class NjStockOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(worker, "NJ_STOCK_ORDER_STATE_DIR", self.temp.name))
        self.product = {"index": 1, "product_id": "PC54", "color": "Navy", "quantities": {"S": 2, "M": 3, "XL": 5, "2XL": 4}}
        self.order = {
            "order_id": "5000001", "po": "H-CJVillafra302", "product_id": "PC54", "color": "Navy",
            "products": [self.product], "quantities": self.product["quantities"],
            "production_date": worker.datetime(2026, 10, 5).date(),
            "due_date": worker.datetime(2026, 10, 7).date(), "shipping_class": "rush",
            "order_type": "inhouse", "stock_tab_index": 1,
        }
        self.inventory = [
            {"warehouse": NJ, "stock": {"S": 12, "M": 13}},
            {"warehouse": VA, "stock": {"S": 50, "M": 50, "XL": 15}},
            {"warehouse": OH, "stock": {"2XL": 14}},
        ]
        self.cart = []
        self.pending = []
        self.rows = set()
        self.events = []
        self.current_style = "PC54"
        self.current_color = "Navy"
        self.shipping_mode = None
        self.checkout_counts = {}
        self.fail_add_checkout = None
        self.fail_submit = None
        self.fail_record = None
        self.cleanup_ok = True
        self.note_visible = True
        self.note = ""
        self.eta = worker.datetime(2026, 10, 6).date()
        self.eta_by_warehouse = {}
        self.crm = mock.Mock()
        self.sanmar = object()
        self.crm.execute_script.side_effect = lambda script, note: self.note_visible and note == self.note

        def search(_driver, style, **kwargs):
            self.current_style = style

        def color(_driver, label, **kwargs):
            self.current_color = label

        def quantities(_driver, warehouse, sizes):
            self.pending[:] = [
                {"warehouse": warehouse, "size": size, "quantity": qty, "style": self.current_style, "color": self.current_color}
                for size, qty in sizes.items()
            ]
            self.events.append(("fill", warehouse, dict(sizes)))

        def add(_driver):
            self.cart.extend(self.pending)
            self.pending.clear()

        def clear(_driver, order_id):
            if self.cleanup_ok:
                self.cart.clear()
            return {"success": self.cleanup_ok, "message": "Cart cleared"}

        def shipping(_driver, order_type, warehouse, **kwargs):
            self.shipping_mode = "pickup" if warehouse == NJ else "ship"
            self.events.append(("shipping", warehouse, self.shipping_mode))
            return {"ship_mode": self.shipping_mode, "address": NJ if warehouse == NJ else "123 EZ TEES INC"}

        def eta(_driver, order_type, **kwargs):
            names = tuple(kwargs["selected_warehouses"])
            self.checkout_counts[names] = self.checkout_counts.get(names, 0) + 1
            if self.fail_add_checkout == self.checkout_counts[names]:
                self.fail_add_checkout = None
                raise RuntimeError("ADD checkout could not be read")
            dates = {name: self.eta_by_warehouse.get(name, self.eta) for name in names}
            return {"eta": max(dates.values()), "eta_by_warehouse": {name: str(value) for name, value in dates.items()}}

        def submit(_driver, po, dry_run=False):
            if dry_run:
                self.events.append(("review", po))
                return "dry_run_review_ready"
            receipt = worker._load_nj_stock_order(self.order["order_id"], self.order["po"])
            if receipt:
                group = next(group for group in receipt["groups"] if group["po"] == po)
                self.assertEqual(group["state"], "submitting", "Submit intent must be durable before purchase")
            self.events.append(("buy", po, self.shipping_mode, copy.deepcopy(self.cart)))
            self.cart.clear()
            if self.fail_submit == po:
                self.fail_submit = None
                raise RuntimeError("Thank-you page timed out after clicking Submit")
            return "submitted"

        def confirmation(_driver, order_id, po):
            return {"po": po, "po_confirmed": True, "url": f"https://www.sanmar.com/orders/{po}", "web_reference": po}

        def record(_driver, order_id, po, **kwargs):
            if self.fail_record == po:
                self.fail_record = None
                raise RuntimeError("CRM manual-order save failed")
            self.rows.add(po)
            self.order["active_panel_stock_ordered"] = True
            self.events.append(("record", po, kwargs.get("add_box", False)))
            return "recorded"

        def save_note(_driver, order_id, note):
            self.note = note
            self.events.append(("note", note))
            return "recorded"

        patches = {
            "_require_crm_order_ready_once_with_refresh": {}, "_publish_status": {},
            "_extract_order_data": {"side_effect": lambda *args, **kwargs: self.order},
            "_pending_shipping_bypass_submission": {"return_value": None},
            "_saved_shipping_bypass_confirmation": {"return_value": None},
            "_historical_shipping_bypass_po_exists": {"return_value": False},
            "_crm_stock_order_yellow_visual_exists": {"side_effect": lambda _driver, po: po in self.rows},
            "_crm_manual_order_row_exists": {"side_effect": lambda _driver, po, **kwargs: po in self.rows},
            "safe_get_with_partial_load": {}, "safe_take_screenshot": {}, "_ensure_sanmar_logged_in": {},
            "_sanmar_cart_has_items": {"side_effect": lambda _driver: {"hasItems": bool(self.cart)}},
            "_search_sanmar_product": {"side_effect": search}, "_select_sanmar_color": {"side_effect": color},
            "_wait_for_sanmar_inventory": {"side_effect": lambda *args, **kwargs: self.inventory},
            "_sanmar_closed_warehouses": {"return_value": set()},
            "_fill_sanmar_quantities": {"side_effect": quantities}, "_add_current_product_to_box": {"side_effect": add},
            "_wait_for_sanmar_cart_lines": {"side_effect": lambda *args, **kwargs: copy.deepcopy(self.cart)},
            "_click_sanmar_button": {}, "_wait_for_text": {},
            "_select_shipping_destination": {"side_effect": shipping},
            "_select_ups_eta_for_shipping_plan": {"side_effect": eta}, "_clear_sanmar_cart": {"side_effect": clear},
            "_change_crm_production_date": {"side_effect": lambda _driver, order_id, target: target},
            "_change_crm_due_date": {"side_effect": lambda _driver, order_id, target: target},
            "_fill_review_and_submit": {"side_effect": submit}, "_capture_sanmar_confirmation": {"side_effect": confirmation},
            "_remember_pending_shipping_bypass_submission": {}, "_mark_pending_shipping_bypass_submission_recorded": {},
            "_record_crm_manual_order": {"side_effect": record}, "_append_crm_production_note": {"side_effect": save_note},
        }
        self.mocks = {name: self.stack.enter_context(mock.patch.object(worker, name, **options)) for name, options in patches.items()}

    def run_order(self, **kwargs):
        return worker._process_open_order(self.crm, self.sanmar, self.order["order_id"], stock_tab_index=1, stock_tab_count=1, **kwargs)

    def purchases(self):
        return [event for event in self.events if event[0] == "buy"]

    def test_nj_is_purchased_first_with_original_po_and_add_has_exact_remaining_stock(self):
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertTrue(result["stock_order_complete"])
        purchases = self.purchases()
        self.assertEqual([event[1] for event in purchases], ["H-CJVillafra302", "ADD-H-CJVillafra302"])
        self.assertEqual([event[2] for event in purchases], ["pickup", "ship"])
        self.assertEqual([(row["size"], row["quantity"], row["warehouse"]) for row in purchases[0][3]], [("S", 2, NJ), ("M", 3, NJ)])
        self.assertEqual([(row["size"], row["quantity"], row["warehouse"]) for row in purchases[1][3]], [("XL", 5, VA), ("2XL", 4, OH)])
        self.assertEqual([event for event in self.events if event[0] == "record"], [("record", "H-CJVillafra302", False), ("record", "ADD-H-CJVillafra302", True)])
        first_buy = self.events.index(purchases[0])
        self.assertIn(("review", "ADD-H-CJVillafra302"), self.events[:first_buy])
        for call in self.mocks["_select_ups_eta_for_shipping_plan"].call_args_list:
            self.assertNotIn(NJ, call.kwargs["selected_warehouses"])
        self.assertEqual(result["production_note"],
            "tab 1: Add box PO ADD-H-CJVillafra302 — 2 boxes from sanmar with the same PO\n"
            "Add box for Navy PC54 for size XL (5 pc) from VA.\n"
            "Add box for Navy PC54 for size 2XL (4 pc) from OH.")
        receipt = worker._load_nj_stock_order(self.order["order_id"], self.order["po"])
        self.assertTrue(receipt["complete"])
        self.assertEqual([group["state"] for group in receipt["groups"]], ["recorded", "recorded"])

    def test_nj_takes_priority_even_when_va_can_fulfill_the_entire_order(self):
        self.inventory[1]["stock"]["2XL"] = 50
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"], f"ADD-{self.order['po']}"])
        self.assertEqual(result["stock_orders"][1]["warehouses"], [VA])
        self.assertIn("sizes XL (5 pc), 2XL (4 pc) from VA", result["production_note"])
        self.assertNotIn("boxes from sanmar", result["production_note"])

    def test_same_size_quantity_is_split_between_nj_and_add(self):
        self.product["quantities"] = self.order["quantities"] = {"XL": 5}
        self.inventory = [{"warehouse": NJ, "stock": {"XL": 14}}, {"warehouse": VA, "stock": {"XL": 20}}]
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertEqual([event[3][0]["quantity"] for event in self.purchases()], [4, 1])
        self.assertIn("XL (1 pc) from VA", result["production_note"])

    def test_same_add_size_from_two_warehouses_includes_each_quantity(self):
        self.product["quantities"] = self.order["quantities"] = {"S": 2, "XL": 5}
        self.inventory = [{"warehouse": NJ, "stock": {"S": 12}}, {"warehouse": VA, "stock": {"XL": 13}}, {"warehouse": OH, "stock": {"XL": 12}}]
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertIn("XL (3 pc) from VA", result["production_note"])
        self.assertIn("XL (2 pc) from OH", result["production_note"])

    def test_normalized_youth_sizes_use_crm_labels_in_add_note(self):
        self.product["product_name"] = "Youth Tee"
        self.product["quantities"] = self.order["quantities"] = {"YS": 2, "YXL": 3}
        self.inventory = [{"warehouse": NJ, "stock": {"S": 12}}, {"warehouse": VA, "stock": {"XL": 13}}]
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertIn("YXL (3 pc) from VA", result["production_note"])

    def test_buffer_override_can_use_low_nj_stock_without_changing_add_po_rule(self):
        self.inventory = [{"warehouse": NJ, "stock": {"S": 2, "M": 3}}, {"warehouse": VA, "stock": {"XL": 5, "2XL": 4}}]
        result = self.run_order(allow_low_stock_buffer=True)
        self.assertTrue(result["success"], result["message"])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"], f"ADD-{self.order['po']}"])

    def test_multiple_products_and_colors_keep_separate_add_note_lines(self):
        second = {"index": 2, "product_id": "PC61", "color": "Red", "quantities": {"S": 2, "M": 3, "XL": 5, "2XL": 4}}
        self.order["products"].append(second)
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertIn("Navy PC54 for size XL (5 pc) from VA", result["production_note"])
        self.assertIn("Red PC61 for size XL (5 pc) from VA", result["production_note"])
        self.assertIn("Red PC61 for size 2XL (4 pc) from OH", result["production_note"])

    def test_no_nj_stock_keeps_original_po_and_existing_process(self):
        self.inventory = [{"warehouse": VA, "stock": {size: 50 for size in self.product["quantities"]}}]
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"]])
        self.assertIsNone(worker._load_nj_stock_order(self.order["order_id"], self.order["po"]))
        self.assertIsNone(result["production_note"])

    def test_closed_nj_is_not_used_and_does_not_add_po_prefix(self):
        self.mocks["_sanmar_closed_warehouses"].return_value = {NJ}
        self.inventory[1]["stock"]["2XL"] = 50
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"]])
        self.assertNotIn(NJ, result["warehouses"])

    def test_complete_nj_order_uses_pickup_and_never_selects_ups(self):
        self.inventory = [{"warehouse": NJ, "stock": {size: 50 for size in self.product["quantities"]}}]
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertEqual([event[2] for event in self.purchases()], ["pickup"])
        self.mocks["_select_ups_eta_for_shipping_plan"].assert_not_called()

    def test_shortage_in_add_stock_prevents_nj_purchase(self):
        self.inventory = self.inventory[:2]
        result = self.run_order()
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "no_single_warehouse")
        self.assertEqual(self.purchases(), [])
        self.mocks["_fill_sanmar_quantities"].assert_not_called()

    def test_late_add_delivery_prevents_both_purchases(self):
        self.eta = self.order["due_date"]
        result = self.run_order()
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "eta_after_due_date")
        self.assertEqual(self.purchases(), [])
        self.mocks["_record_crm_manual_order"].assert_not_called()

    def test_late_complete_add_warehouse_falls_back_to_non_nj_split_before_purchase(self):
        self.inventory.append({"warehouse": "Seattle, WA", "stock": {size: 50 for size in self.product["quantities"]}})
        self.eta_by_warehouse["Seattle, WA"] = worker.datetime(2026, 10, 8).date()
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["stock_orders"][1]["warehouses"], [VA, OH])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"], f"ADD-{self.order['po']}"])
        self.assertIn("2 boxes from sanmar with the same PO", result["production_note"])
        self.assertNotIn("Seattle", result["production_note"])

    def test_warehouse_closure_after_nj_purchase_stops_add_and_keeps_nj_receipt(self):
        self.mocks["_sanmar_closed_warehouses"].side_effect = lambda _driver: {VA} if self.purchases() else set()
        result = self.run_order()
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "sanmar_warehouse_closed")
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"]])
        receipt = worker._load_nj_stock_order(self.order["order_id"], self.order["po"])
        self.assertEqual(receipt["groups"][0]["state"], "recorded")

    def test_preflight_cleanup_failure_prevents_both_purchases(self):
        self.cleanup_ok = False
        result = self.run_order()
        self.assertFalse(result["success"])
        self.assertTrue(result["stop_run"])
        self.assertEqual(self.purchases(), [])

    def test_production_date_save_failure_prevents_nj_purchase(self):
        self.mocks["_change_crm_production_date"].side_effect = RuntimeError("Production date did not persist")
        result = self.run_order()
        self.assertFalse(result["success"])
        self.assertEqual(self.purchases(), [])

    def test_free_shipping_due_date_extension_is_kept_for_add_stock(self):
        self.order["shipping_class"] = "free"
        self.eta = worker.datetime(2026, 10, 8).date()
        result = self.run_order()
        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["order"]["due_date"], "2026-10-09")
        self.mocks["_change_crm_due_date"].assert_called_once()

    def test_dry_run_checks_both_pos_without_purchases_crm_saves_or_receipts(self):
        result = self.run_order(dry_run=True)
        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["outcome"], "shipping_bypass_ready")
        self.assertTrue(worker._report_orders_succeeded_or_partially_succeeded([result]))
        self.assertTrue(server._build_crm_shipping_bypasser_order_results({"report": [result]})[0]["success"])
        self.assertEqual([event[1] for event in self.events if event[0] == "review"], [self.order["po"], f"ADD-{self.order['po']}"])
        self.assertEqual(self.purchases(), [])
        self.mocks["_record_crm_manual_order"].assert_not_called()
        self.mocks["_append_crm_production_note"].assert_not_called()
        self.mocks["_change_crm_production_date"].assert_not_called()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_add_checkout_failure_resumes_add_without_rebuying_recorded_nj(self):
        self.fail_add_checkout = 2
        first = self.run_order()
        self.assertFalse(first["success"])
        self.assertFalse(first["stock_order_complete"])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"]])
        self.assertTrue(self.order["active_panel_stock_ordered"])
        self.cart.clear()  # Existing outer worker cleanup removes the unsubmitted cart.
        second = self.run_order()
        self.assertTrue(second["success"], second["message"])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"], f"ADD-{self.order['po']}"])

    def test_multi_tab_resume_bypasses_original_po_already_ordered_gate(self):
        self.fail_add_checkout = 2
        self.assertFalse(self.run_order()["success"])
        self.cart.clear()
        tabs = [{"label": f"{self.order['po']} 1 - QTY: 14"}, {"label": "H-Other303 2 - QTY: 14"}]
        other = dict(self.order, po="H-Other303", stock_tab_index=2)
        self.rows.add(other["po"])
        active = [0]

        def activate(_driver, index):
            active[0] = index
            return tabs[index]

        self.mocks["_extract_order_data"].side_effect = lambda *args, **kwargs: self.order if active[0] == 0 else other
        with mock.patch.object(worker, "_open_target_order"), \
             mock.patch.object(worker, "_wait_for_order_goods_page_ready"), \
             mock.patch.object(worker, "_find_stock_tabs", return_value=tabs), \
             mock.patch.object(worker, "_visible_design_tab_number_hints", return_value=[1, 2]), \
             mock.patch.object(worker, "_activate_stock_tab", side_effect=activate):
            results = worker._run_order_with_drivers(self.crm, self.sanmar, self.order["order_id"])
        self.assertTrue(results[0]["success"], results[0]["message"])
        self.assertEqual(results[1]["outcome"], "already_stock_ordered")
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"], f"ADD-{self.order['po']}"])

    def test_nj_crm_save_failure_retries_recording_without_rebuying_nj(self):
        self.fail_record = self.order["po"]
        first = self.run_order()
        self.assertFalse(first["success"])
        receipt = worker._load_nj_stock_order(self.order["order_id"], self.order["po"])
        self.assertEqual(receipt["groups"][0]["state"], "confirmed")
        second = self.run_order()
        self.assertTrue(second["success"], second["message"])
        self.assertEqual([event[1] for event in self.purchases()], [self.order["po"], f"ADD-{self.order['po']}"])

    def test_add_crm_save_failure_retries_recording_without_any_new_purchase(self):
        self.fail_record = f"ADD-{self.order['po']}"
        self.assertFalse(self.run_order()["success"])
        second = self.run_order()
        self.assertTrue(second["success"], second["message"])
        self.assertEqual(len(self.purchases()), 2)

    def test_uncertain_submission_blocks_rebuy_and_add_purchase(self):
        self.fail_submit = self.order["po"]
        first = self.run_order()
        self.assertEqual(first["outcome"], "nj_stock_submission_uncertain")
        self.assertTrue(first["stop_run"])
        second = self.run_order()
        self.assertEqual(second["outcome"], "nj_stock_submission_uncertain")
        self.assertEqual(len(self.purchases()), 1)
        self.mocks["_record_crm_manual_order"].assert_not_called()

    def test_uncertain_add_submission_keeps_nj_receipt_and_never_repeats_either_purchase(self):
        self.fail_submit = f"ADD-{self.order['po']}"
        first = self.run_order()
        self.assertEqual(first["outcome"], "nj_stock_submission_uncertain")
        second = self.run_order()
        self.assertEqual(second["outcome"], "nj_stock_submission_uncertain")
        self.assertEqual(len(self.purchases()), 2)
        receipt = worker._load_nj_stock_order(self.order["order_id"], self.order["po"])
        self.assertEqual([group["state"] for group in receipt["groups"]], ["recorded", "submitting"])

    def test_unconfirmed_add_po_keeps_its_confirmation_evidence_for_review(self):
        self.mocks["_capture_sanmar_confirmation"].side_effect = lambda _driver, order_id, po: {
            "po": po, "po_confirmed": not po.startswith("ADD-"), "url": f"https://www.sanmar.com/orders/{po}", "screenshot": "local-evidence.png",
        }
        result = self.run_order()
        self.assertEqual(result["outcome"], "nj_stock_submission_uncertain")
        receipt = worker._load_nj_stock_order(self.order["order_id"], self.order["po"])
        self.assertEqual(receipt["groups"][1]["confirmation"]["screenshot"], "local-evidence.png")
        self.assertFalse(receipt["groups"][1]["confirmation"]["po_confirmed"])
        self.assertEqual(result["sanmar_confirmation"]["po"], self.order["po"])
        self.assertEqual(len(self.purchases()), 2)

    def test_note_persistence_failure_resumes_note_only_after_both_purchases(self):
        verify = worker._verify_crm_production_note
        with mock.patch.object(worker, "_verify_crm_production_note", side_effect=lambda *args: verify(*args, timeout=0)):
            self.note_visible = False
            first = self.run_order()
            self.assertFalse(first["success"])
            self.assertFalse(first["stock_order_complete"])
            self.assertEqual(len(self.purchases()), 2)
            self.note_visible = True
            self.mocks["_ensure_sanmar_logged_in"].side_effect = RuntimeError("SanMar login unavailable")
            second = self.run_order()
        self.assertTrue(second["success"], second["message"])
        self.assertEqual(len(self.purchases()), 2)

    def test_completed_receipt_skips_all_new_purchases_and_note_edits(self):
        self.assertTrue(self.run_order()["success"])
        before = len(self.events)
        second = self.run_order()
        self.assertTrue(second["success"])
        self.assertEqual(second["outcome"], "already_stock_ordered")
        self.assertEqual(len(self.events), before)

    def test_changed_crm_quantities_stop_resume_before_any_purchase(self):
        self.fail_add_checkout = 2
        self.assertFalse(self.run_order()["success"])
        self.product["quantities"]["XL"] += 1
        second = self.run_order()
        self.assertEqual(second["outcome"], "nj_stock_allocation_changed")
        self.assertEqual(len(self.purchases()), 1)

    def test_corrupt_receipt_stops_before_duplicate_guards_can_skip_it(self):
        path = Path(worker._nj_stock_order_path(self.order["order_id"], self.order["po"]))
        path.write_text("broken receipt", encoding="utf-8")
        self.rows.add(self.order["po"])
        with self.assertRaisesRegex(RuntimeError, "receipt cannot be read"):
            self.run_order()
        self.assertEqual(self.purchases(), [])

    def test_missing_allocation_with_nj_purchase_receipt_stops_for_review(self):
        self.rows.add(self.order["po"])
        with mock.patch.object(worker, "_saved_shipping_bypass_submission", return_value={"nj_order_po": self.order["po"]}):
            result = self.run_order()
        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "nj_stock_receipt_missing")
        self.assertEqual(self.purchases(), [])

    def test_incomplete_receipt_remains_eligible_despite_success_for_another_tab(self):
        self.fail_add_checkout = 2
        self.assertFalse(self.run_order()["success"])
        history = Path(self.temp.name) / "history.json"
        history.write_text(json.dumps({"run_history": [{"automation_key": "shipping_bypasser", "order_results": [{"order_id": self.order["order_id"], "success": True}]}]}), encoding="utf-8")
        self.assertEqual(worker._load_historical_shipping_bypass_order_ids(str(history)), set())

    def test_partial_result_is_not_promoted_to_complete_by_server_or_history(self):
        self.fail_add_checkout = 2
        result = self.run_order()
        payload = {"report": [result], "order_ids": [self.order["order_id"]], "success": False}
        rows = server._build_crm_shipping_bypasser_order_results(payload)
        self.assertFalse(rows[0]["success"])
        self.assertEqual(rows[0]["status"], "Partially successful")
        self.assertFalse(server._crm_shipping_bypasser_order_results_success(rows))
        self.assertFalse(worker._report_orders_succeeded_or_partially_succeeded([result]))
        self.assertEqual(server._shipping_bypasser_failed_order_ids(payload), [self.order["order_id"]])
        normalized = server._normalize_crm_stock_order_results(rows)
        self.assertEqual(normalized[0]["stock_orders"], rows[0]["stock_orders"])
        self.assertFalse(normalized[0]["stock_order_complete"])
        self.assertIn("partially succeeded", worker._summary_message([result]))

    def test_successful_history_retains_both_po_confirmations(self):
        result = self.run_order()
        rows = server._build_crm_shipping_bypasser_order_results({"report": [result]})
        history = Path(self.temp.name) / "history.json"
        history.write_text(json.dumps({"run_history": [{"automation_key": "shipping_bypasser", "order_results": server._normalize_crm_stock_order_results(rows)}]}), encoding="utf-8")
        self.assertEqual(worker._load_historical_shipping_bypass_customer_pos(str(history)), {self.order["po"].lower(), f"add-{self.order['po']}".lower()})
        for po in (self.order["po"], f"ADD-{self.order['po']}"):
            self.assertEqual(worker._historical_shipping_bypass_confirmation(po, str(history))["po"], po)


class NjPickupAndPoGuardTests(unittest.TestCase):
    def test_nj_pickup_is_required_for_every_destination(self):
        for destination in ("inhouse", "mach6"):
            with self.subTest(destination=destination), mock.patch.object(worker, "_click_radio_near_text") as radio, \
                 mock.patch.object(worker, "_select_dropdown_option_containing") as dropdown, \
                 mock.patch.object(worker, "_sanmar_nj_pickup_selected", return_value=True), \
                 mock.patch.object(worker, "_click_sanmar_button") as button:
                result = worker._select_shipping_destination(object(), destination, NJ)
                self.assertEqual(result["ship_mode"], "pickup")
                self.assertEqual([call.args[1] for call in radio.call_args_list], ["Pick Up at warehouse"])
                self.assertEqual(dropdown.call_args.args[1], "Robbinsville")
                self.assertEqual(button.call_args.args[1], r"Proceed\s+To\s+Payment")

    def test_unconfirmed_nj_pickup_stops_before_payment(self):
        with mock.patch.object(worker, "_click_radio_near_text"), \
             mock.patch.object(worker, "_select_dropdown_option_containing"), \
             mock.patch.object(worker, "_sanmar_nj_pickup_selected", return_value=False), \
             mock.patch.object(worker, "_click_sanmar_button") as button:
            with self.assertRaisesRegex(RuntimeError, "pickup was not confirmed"):
                worker._select_shipping_destination(object(), "inhouse", NJ)
            button.assert_not_called()

    def test_base_po_is_not_confirmed_by_an_add_po_or_longer_po(self):
        for text in ("Customer PO ADD-H-CJVillafra302", "Customer PO H-CJVillafra3029"):
            driver = mock.Mock(current_url="https://www.sanmar.com/confirmation")
            driver.execute_script.return_value = text
            with mock.patch.object(worker.os, "makedirs"):
                result = worker._capture_sanmar_confirmation(driver, "5000001", "H-CJVillafra302")
            self.assertFalse(result["po_confirmed"])


if __name__ == "__main__":
    unittest.main()
