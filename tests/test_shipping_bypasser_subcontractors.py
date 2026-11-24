"""Subcontractor purchasing checks using isolated mocks and saved-page fixtures."""

import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "workers"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import crm_shipping_bypasser as worker
from shipping_bypasser_subcontractors import (
    SUBCONTRACTOR_DESTINATIONS,
    latest_ship_deadline,
    subcontractor_destination,
    subcontractor_order_type,
)
import test_crm_shipping_bypasser as delivery_tests


def DeliveryHarness():
    return delivery_tests.ShippingBypassDeliveryFallbackTests()


class SubcontractorMappingTests(unittest.TestCase):
    def test_all_reviewed_mappings_have_safe_routing_and_state_suffixes(self):
        self.assertEqual(len(SUBCONTRACTOR_DESTINATIONS), 45)
        for order_type, destination in SUBCONTRACTOR_DESTINATIONS.items():
            with self.subTest(destination=destination["crm_name"]):
                self.assertEqual(subcontractor_order_type(destination["crm_name"]), order_type)
                self.assertEqual(subcontractor_order_type(destination["crm_name"] + " (" + destination["state"] + ")"), order_type)
                self.assertEqual(set(worker.WAREHOUSE_DISTANCE[order_type]), set(worker.WAREHOUSE_DISTANCE["inhouse"]))
                self.assertEqual(len(worker.WAREHOUSE_DISTANCE[order_type]), 9)
                self.assertIsNone(worker._production_destination_error({"order_type": order_type, "subcontractor": destination["crm_name"]}))

    def test_different_apollo_locations_and_reviewed_address_exceptions_are_explicit(self):
        for name, expected in (("Central Apollo", "APOLLO - IL"), ("SOUTH APOLLO", "Apollo Dallas"), ("APOLLO USA", "Apollo - USA")):
            self.assertEqual(subcontractor_destination(subcontractor_order_type(name))["name"], expected)
        sew = subcontractor_destination(subcontractor_order_type("SEW NICE EMBROIDERY & GIFTS"))
        self.assertEqual((sew["city"], sew["zip"]), ("Odenville", "35120-3521"))
        self.assertEqual(subcontractor_destination(subcontractor_order_type("PRINT WORKS"))["city"], "Reno")

    def test_unknown_similar_names_or_wrong_state_never_default_to_a_destination(self):
        for name in ("WEST APOLLO", "Apollo", "DESIGNER T'S (CA)", "DESIGNER T'S / Mach6", "Sew Nearly Nice", "FULL PRESS APPAREL", ""):
            self.assertIsNone(subcontractor_order_type(name), name)
        self.assertIsNotNone(worker._production_destination_error({"order_type": subcontractor_order_type("DESIGNER T'S"), "subcontractor": "Other Manufacturing"}))

    def test_saved_crm_reader_uses_production_notes_and_recognizes_the_vendor(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {
            "bodyText": "Due Date: 12/07/26 Production Date: 11/30/26 Subcontractor: DESIGNER T'S (NJ) Preferred File Types: None",
            "productionNotes": [
                {"timestamp": "Nov 23rd, 2026 @ 04:50 PM", "text": "LATEST SHIP DATE 12/2"},
                {"timestamp": "Nov 24, 2026 @ 09:18 AM", "text": "Approved by subcontractor"},
            ],
        }
        with mock.patch.object(worker, "refresh_if_crm_challenge_attempts_exceeded", return_value=False):
            order = worker._extract_order_data(driver, "5000001")
        self.assertEqual(order["order_type"], subcontractor_order_type("DESIGNER T'S"))
        self.assertEqual(order["due_date"], date(2026, 12, 7))
        self.assertEqual(order["latest_ship_date"], date(2026, 12, 2))
        self.assertEqual(order["latest_ship_date_error"], "")


class LatestShipDateTests(unittest.TestCase):
    def note(self, text, timestamp="Nov 23rd, 2026 @ 04:50 PM"):
        return {"text": text, "timestamp": timestamp}

    def test_yearless_formats_and_trailing_stars(self):
        for value in ("LATEST SHIP DATE 12/3", "LATEST SHIP DATE - 12/3***", "Latest Ship Date: 12 / 3", "LATEST SHIP DATE — 12/3"):
            self.assertEqual(latest_ship_deadline([self.note(value)]), date(2026, 12, 3))

    def test_newest_matching_timestamp_overrides_dom_order_and_later_unrelated_notes(self):
        notes = [self.note("LATEST SHIP DATE 12/4", "Nov 24, 2026 @ 10:00 AM"), self.note("LATEST SHIP DATE 12/2"), self.note("Approved", "Nov 25, 2026 @ 10:00 AM")]
        self.assertEqual(latest_ship_deadline(notes), date(2026, 12, 4))

    def test_december_january_rollover_uses_note_year(self):
        self.assertEqual(latest_ship_deadline([self.note("LATEST SHIP DATE - 1/3***", "Dec 28th, 2026 @ 12:01 PM")]), date(2027, 1, 3))
        self.assertEqual(latest_ship_deadline([self.note("LATEST SHIP DATE 1/4", "Jan 2nd, 2027 @ 01:00 PM")]), date(2027, 1, 4))

    def test_missing_invalid_or_conflicting_newest_dates_stop_instead_of_using_older_values(self):
        cases = (
            [],
            [self.note("Approved")],
            [self.note("LATEST SHIP DATE 12/3", "unreadable")],
            [self.note("LATEST SHIP DATE 12/3"), self.note("LATEST SHIP DATE TBD", "Nov 24, 2026 @ 10:00 AM")],
            [self.note("LATEST SHIP DATE 13/3")],
            [self.note("LATEST SHIP DATE 2/30", "Jan 1, 2027 @ 10:00 AM")],
            [self.note("LATEST SHIP DATE 11/20")],
            [self.note("LATEST SHIP DATE 12/3 and LATEST SHIP DATE 12/4")],
            [self.note("LATEST SHIP DATE 12/3"), self.note("LATEST SHIP DATE 12/4")],
        )
        for notes in cases:
            with self.subTest(notes=notes), self.assertRaises(ValueError):
                latest_ship_deadline(notes)

    def test_sanmar_yearless_january_eta_also_rolls_over(self):
        with mock.patch.object(worker, "datetime", wraps=worker.datetime) as clock:
            clock.now.return_value = worker.datetime(2026, 12, 28)
            self.assertEqual(worker._parse_sanmar_eta("Jan 3"), date(2027, 1, 3))


class SubcontractorPurchaseTests(unittest.TestCase):
    def run_order(self, *, eta=date(2026, 10, 6), overrides=None, **kwargs):
        order = {
            "subcontractor": "DESIGNER T'S (NJ)",
            "latest_ship_date": date(2026, 10, 7),
            "latest_ship_date_error": "",
            "due_date": date(2026, 10, 15),
            "production_date": date(2026, 10, 5),
        }
        order.update(overrides or {})
        return DeliveryHarness()._run_order(
            order_type=subcontractor_order_type("DESIGNER T'S"),
            order_overrides=order,
            inventory=[{"warehouse": worker.NJ_WAREHOUSE, "stock": {"3XL": 20, "4XL": 20}}],
            eta_states=[{worker.NJ_WAREHOUSE: eta}] if eta else [None],
            **kwargs,
        )

    def test_arrival_and_production_on_day_before_deadline_purchase_without_changing_due_date(self):
        result, mocks, cart = self.run_order(shipping_class="free")
        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["order"]["production_date"], "2026-10-06")
        self.assertEqual(result["order"]["due_date"], "2026-10-15")
        self.assertEqual(result["shipping"]["ship_mode"], "ship")
        self.assertEqual(result["warehouses"], [worker.NJ_WAREHOUSE])
        self.assertEqual(len(cart), 2)
        mocks["_change_crm_production_date"].assert_called_once_with(mock.ANY, "5000001", date(2026, 10, 6), expected_due_date=date(2026, 10, 15))
        mocks["_change_crm_due_date"].assert_not_called()
        mocks["_fill_review_and_submit"].assert_called_once()
        mocks["_reload_crm_order_for_production_verify"].assert_called_once()

    def test_eta_on_or_after_latest_ship_date_blocks_rush_and_free_orders(self):
        for eta in (date(2026, 10, 7), date(2026, 10, 8)):
            for shipping_class in ("rush", "free"):
                with self.subTest(eta=eta, shipping=shipping_class):
                    result, mocks, _cart = self.run_order(eta=eta, shipping_class=shipping_class)
                    self.assertFalse(result["success"])
                    self.assertEqual(result["outcome"], "eta_after_latest_ship_date")
                    self.assertIn("LATEST SHIP DATE 2026-10-07", result["message"])
                    DeliveryHarness()._assert_no_purchase(mocks)

    def test_missing_deadline_or_invalid_existing_production_stops_before_sanmar(self):
        for overrides, outcome in (({"latest_ship_date": None}, "latest_ship_date_unavailable"), ({"latest_ship_date_error": "conflicting newest dates"}, "latest_ship_date_unavailable"), ({"production_date": date(2026, 10, 7)}, "production_not_before_latest_ship_date")):
            with self.subTest(overrides=overrides):
                result, mocks, _cart = self.run_order(overrides=overrides)
                self.assertEqual(result["outcome"], outcome)
                mocks["safe_get_with_partial_load"].assert_not_called()
                DeliveryHarness()._assert_no_purchase(mocks)

    def test_freight_error_cannot_bypass_missing_eta(self):
        result, mocks, _cart = self.run_order(eta=None)
        self.assertEqual(result["outcome"], "sanmar_eta_unavailable")
        DeliveryHarness()._assert_no_purchase(mocks)

    def test_weekend_arrival_cannot_move_production_onto_deadline(self):
        result, mocks, _cart = self.run_order(eta=date(2026, 10, 10), overrides={"latest_ship_date": date(2026, 10, 12)})
        self.assertEqual(result["outcome"], "eta_after_latest_ship_date")
        DeliveryHarness()._assert_no_purchase(mocks)

    def test_dry_run_proposes_date_without_saving_crm_or_purchasing(self):
        result, mocks, _cart = self.run_order(dry_run=True)
        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["order"]["proposed_production_date"], "2026-10-06")
        self.assertEqual(result["order"]["production_date"], "2026-10-05")
        mocks["_change_crm_production_date"].assert_not_called()
        mocks["_change_crm_due_date"].assert_not_called()
        mocks["_remember_pending_shipping_bypass_submission"].assert_not_called()
        self.assertTrue(mocks["_fill_review_and_submit"].call_args.kwargs["dry_run"])
        self.assertTrue(mocks["_record_crm_manual_order"].call_args.kwargs["dry_run"])

    def test_split_uses_latest_arrival_and_same_po_with_nj_shipping(self):
        for last_arrival, succeeds in ((date(2026, 10, 6), True), (date(2026, 10, 7), False)):
            with self.subTest(last_arrival=last_arrival):
                result, mocks, _cart = DeliveryHarness()._run_order(
                    order_type=subcontractor_order_type("DESIGNER T'S"),
                    order_overrides={"subcontractor": "DESIGNER T'S (NJ)", "latest_ship_date": date(2026, 10, 7), "due_date": date(2026, 10, 15)},
                    inventory=[{"warehouse": worker.NJ_WAREHOUSE, "stock": {"3XL": 20, "4XL": 0}}, {"warehouse": "Richmond, VA", "stock": {"3XL": 0, "4XL": 20}}],
                    eta_states=[{worker.NJ_WAREHOUSE: date(2026, 10, 5), "Richmond, VA": last_arrival}],
                )
                self.assertEqual(result["success"], succeeds, result["message"])
                self.assertEqual(result["eta"], str(last_arrival))
                mocks["_change_crm_due_date"].assert_not_called()
                if succeeds:
                    self.assertEqual(result["shipping"]["ship_mode"], "ship")
                    self.assertEqual(mocks["_fill_review_and_submit"].call_args.args[1], "H-TestSplit")
                    self.assertIn("same PO", result["production_note"])
                else:
                    self.assertEqual(result["outcome"], "eta_after_latest_ship_date")
                    DeliveryHarness()._assert_no_purchase(mocks)


class SubcontractorFinalVerificationTests(unittest.TestCase):
    def order(self):
        return {"order_id": "5000001", "order_type": subcontractor_order_type("DESIGNER T'S"), "subcontractor": "DESIGNER T'S (NJ)", "due_date": date(2026, 12, 7), "production_date": date(2026, 12, 1), "latest_ship_date": date(2026, 12, 2)}

    def test_an_unrelated_checkout_date_cannot_replace_a_missing_selected_warehouse_eta(self):
        with mock.patch.object(worker, "_wait_for_sanmar_checkout_shipping_methods"), mock.patch.object(worker, "_wait_for_sanmar_checkout_ups_option"), mock.patch.object(worker, "_select_ups_and_latest_eta_by_warehouse", side_effect=RuntimeError("Missing NJ ETA")), mock.patch.object(worker, "_select_ups_and_eta", return_value=date(2026, 12, 1)) as fallback:
            with self.assertRaisesRegex(RuntimeError, "Missing NJ ETA"):
                worker._select_ups_eta_for_shipping_plan(object(), subcontractor_order_type("DESIGNER T'S"), warehouse=worker.NJ_WAREHOUSE)
        fallback.assert_not_called()

    def test_newest_deadline_destination_due_date_and_saved_production_are_rechecked(self):
        original = self.order()
        for updates, allowed in (({}, True), ({"latest_ship_date": date(2026, 12, 1)}, False), ({"latest_ship_date": None}, False), ({"due_date": date(2026, 12, 8)}, False), ({"subcontractor": "Other Manufacturing"}, False), ({"production_date": date(2026, 11, 30)}, False), ({"production_date": date(2026, 12, 2)}, False)):
            with self.subTest(updates=updates), mock.patch.object(worker, "_reload_crm_order_for_production_verify", return_value=True), mock.patch.object(worker, "_extract_order_data", return_value={**original, **updates}):
                if allowed:
                    worker._verify_subcontractor_deadline_before_submit(object(), original, date(2026, 12, 1))
                else:
                    with self.assertRaises(RuntimeError):
                        worker._verify_subcontractor_deadline_before_submit(object(), original, date(2026, 12, 1))

    def test_date_save_rejects_changed_due_date_before_clicking_save(self):
        driver = mock.Mock()
        driver.execute_script.return_value = "2026-12-08"
        with mock.patch.object(worker, "_find_clickable_by_text", return_value=object()), mock.patch.object(worker, "_click_with_fallback") as click, mock.patch.object(worker, "_set_crm_edit_date_field", return_value=date(2026, 12, 1)), mock.patch.object(worker, "_wait_for_crm_shipping_method_selection"), mock.patch.object(worker, "_acknowledge_crm_production_date_warning"), mock.patch.object(worker.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Due Date changed"):
                worker._change_crm_production_date(driver, "5000001", date(2026, 12, 1), expected_due_date=date(2026, 12, 7))
        self.assertEqual(click.call_count, 1)  # Only Edit Order; never Save Order.

    def test_date_save_checks_the_due_date_after_persistence_too(self):
        driver = mock.Mock()
        driver.execute_script.return_value = "2026-12-07"
        with mock.patch.object(worker, "_find_clickable_by_text", return_value=object()), mock.patch.object(worker, "_click_with_fallback"), mock.patch.object(worker, "_set_crm_edit_date_field", return_value=date(2026, 12, 1)), mock.patch.object(worker, "_wait_for_crm_shipping_method_selection"), mock.patch.object(worker, "_acknowledge_crm_production_date_warning", return_value=False), mock.patch.object(worker.time, "sleep"), mock.patch.object(worker, "_wait_for_text"), mock.patch.object(worker, "_reload_crm_order_for_production_verify", return_value=True), mock.patch.object(worker, "_extract_order_data", return_value={"production_date": date(2026, 12, 1), "due_date": date(2026, 12, 8)}):
            with self.assertRaisesRegex(RuntimeError, "Due Date changed after"):
                worker._change_crm_production_date(driver, "5000001", date(2026, 12, 1), expected_due_date=date(2026, 12, 7))


class SubcontractorTabContinuationTests(unittest.TestCase):
    def test_failed_middle_tab_does_not_prevent_later_tabs_for_mach6_or_new_subcontractors(self):
        from contextlib import ExitStack

        for order_type in ("mach6", subcontractor_order_type("DESIGNER T'S")):
            with self.subTest(order_type=order_type), ExitStack() as stack:
                tabs = [{"label": f"H-Tab{index}"} for index in (1, 2, 3)]
                order = {"order_type": order_type, "po": "H-Tab", "order_id": "5000001"}
                patches = {
                    "_open_target_order": {}, "_require_crm_order_ready_once_with_refresh": {}, "_publish_status": {},
                    "_find_stock_tabs": {"return_value": tabs}, "_visible_design_tab_number_hints": {"return_value": [1, 2, 3]},
                    "_activate_stock_tab": {"side_effect": lambda _driver, index: tabs[index]}, "_extract_order_data": {"return_value": order},
                    "_saved_shipping_bypass_confirmation": {"return_value": None}, "_nj_stock_order_known": {"return_value": False},
                    "_crm_stock_order_yellow_visual_exists": {"return_value": False}, "_crm_manual_order_row_exists": {"return_value": False},
                    "_historical_shipping_bypass_po_exists": {"return_value": False}, "_cleanup_after_failed_order": {"return_value": True},
                    "_process_open_order": {"side_effect": [
                        worker._result("5000001", True, "shipping_bypass_ordered", "Ordered tab 1"),
                        worker._result("5000001", False, "eta_after_latest_ship_date", "Tab 2 stock arrives too late"),
                        worker._result("5000001", True, "shipping_bypass_ordered", "Ordered tab 3"),
                    ]},
                }
                mocks = {name: stack.enter_context(mock.patch.object(worker, name, **options)) for name, options in patches.items()}
                result = worker._run_order_with_drivers(object(), object(), "5000001")
                self.assertEqual([row["success"] for row in result], [True, False, True])
                self.assertEqual([row["stock_tab_index"] for row in result], [1, 2, 3])
                self.assertEqual(result[1]["stock_tab_label"], "H-Tab2")
                self.assertEqual(mocks["_process_open_order"].call_count, 3)
                mocks["_cleanup_after_failed_order"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
