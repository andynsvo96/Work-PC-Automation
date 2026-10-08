import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKERS_DIR = ROOT / "workers"
for path in (ROOT, WORKERS_DIR):
    path_text = str(path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

import crm_shipping_bypasser  # noqa: E402
import shipping_bypasser_mappings  # noqa: E402


class ShippingBypassProductColorMappingTests(unittest.TestCase):
    def test_repository_mapping_file_supplies_product_and_color_overrides(self):
        self.assertEqual(
            crm_shipping_bypasser.SANMAR_PRODUCT_SEARCH_OVERRIDES["G500VL"]["search_id"],
            "5V00L",
        )
        self.assertEqual(
            crm_shipping_bypasser.SANMAR_PRODUCT_COLOR_ALIASES[("ST404", "BLACKTRIADSO")],
            ["Black Triad Solid"],
        )

    def test_blacktop_heather_uses_generic_color_abbreviation_matching(self):
        self.assertTrue(
            crm_shipping_bypasser._sanmar_color_keys_match(
                "Blacktop Heather",
                "BktpHthr",
            )
        )
        self.assertNotIn(
            ("OG160", "BKTPHTHR"),
            crm_shipping_bypasser.SANMAR_PRODUCT_COLOR_ALIASES,
        )

    def test_user_mapping_normalizes_product_and_color_ids(self):
        payload = {
            "products": [
                {
                    "crm_product_id": "test-100",
                    "sanmar_product_id": "sm-200",
                    "colors": [
                        {
                            "crm_color_id": "Blue / White",
                            "sanmar_color_id": "Blue/ White",
                        }
                    ],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mappings.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            products, colors = shipping_bypasser_mappings.build_runtime_indexes(str(path))

        self.assertEqual(products["TEST-100"]["search_id"], "SM-200")
        self.assertEqual(colors[("TEST100", "BLUEWHITE")], ["Blue/ White"])


class ShippingBypassColorConfirmationTests(unittest.TestCase):
    def test_already_selected_drawer_color_does_not_retrigger_navigation(self):
        driver = mock.Mock()

        with (
            mock.patch.object(
                crm_shipping_bypasser,
                "_sanmar_selected_color_label",
                return_value="Graphite Heather",
            ),
            mock.patch.object(crm_shipping_bypasser, "_ensure_sanmar_inventory_view", return_value=True),
        ):
            crm_shipping_bypasser._select_sanmar_color(driver, "Graphite Heather")

        driver.execute_script.assert_not_called()

    def test_new_sanmar_selected_label_confirms_color(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"success": True}

        with (
            mock.patch.object(
                crm_shipping_bypasser,
                "_sanmar_selected_color_label",
                side_effect=["", "Carolina Blue"],
            ),
            mock.patch.object(crm_shipping_bypasser, "_ensure_sanmar_inventory_view", return_value=True),
            mock.patch.object(crm_shipping_bypasser.time, "sleep"),
        ):
            crm_shipping_bypasser._select_sanmar_color(driver, "Carolina Blue")

        selection_script = driver.execute_script.call_args.args[0]
        self.assertIn('[data-testid="pdp-color-link-option"]', selection_script)
        self.assertIn("visibleDrawer.contains(best.node)", selection_script)
        self.assertIn("best.node.click()", selection_script)

    def test_selected_color_reader_prefers_new_drawer_header(self):
        driver = mock.Mock()
        driver.execute_script.return_value = "Graphite Heather"

        self.assertEqual(
            crm_shipping_bypasser._sanmar_selected_color_label(driver),
            "Graphite Heather",
        )
        script = driver.execute_script.call_args.args[0]
        self.assertIn('[data-testid="pdp-header-color-selected"] p', script)
        self.assertIn('[data-testid="pdp-drawer-product-color"]', script)


class ShippingBypassProductSearchTests(unittest.TestCase):
    def test_product_search_submits_new_drawer_form_without_selenium_keystrokes(self):
        driver = mock.Mock()
        driver.current_url = "https://www.sanmar.com/p/example?text=PC54#drawer"
        driver.execute_script.side_effect = [
            {"success": True, "method": "requestSubmit"},
            "PC54 Check inventory and pricing",
        ]

        with (
            mock.patch.object(crm_shipping_bypasser, "_ensure_sanmar_inventory_view", return_value=True),
            mock.patch.object(crm_shipping_bypasser, "_assert_sanmar_active_style", return_value=True),
        ):
            self.assertTrue(crm_shipping_bypasser._search_sanmar_product(driver, "PC54"))

        search_script = driver.execute_script.call_args_list[0].args[0]
        self.assertIn("#offcanvas-drawer.show #search-widget-input", search_script)
        self.assertIn("form.requestSubmit", search_script)
        driver.find_elements.assert_not_called()


class ShippingBypassInventoryReadinessTests(unittest.TestCase):
    def test_inventory_reader_supports_new_drawer_grid_cells(self):
        driver = mock.Mock()
        driver.execute_script.return_value = [
            {"warehouse": "Robbinsville, NJ", "stock": {"S": 964}}
        ]

        rows = crm_shipping_bypasser._sanmar_inventory(driver)

        self.assertEqual(rows[0]["stock"]["S"], 964)
        script = driver.execute_script.call_args.args[0]
        self.assertIn('[data-testid="inventory-size-value-cell"]', script)
        self.assertIn('[data-testid="inventory-warehouse-input-cell"][data-available]', script)

    def test_quantity_filler_supports_new_drawer_grid_cells(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {"success": True}

        with mock.patch.object(crm_shipping_bypasser.time, "sleep"):
            crm_shipping_bypasser._fill_sanmar_quantities(
                driver,
                "Robbinsville, NJ",
                {"S": 2},
            )

        script = driver.execute_script.call_args.args[0]
        self.assertIn('[data-testid="inventory-size-value-cell"]', script)

    def test_empty_warehouse_placeholders_are_not_inventory(self):
        self.assertFalse(
            crm_shipping_bypasser._sanmar_inventory_has_size_data(
                [{"warehouse": "Robbinsville, NJ", "stock": {}}]
            )
        )

    def test_zero_stock_cells_are_still_valid_inventory(self):
        self.assertTrue(
            crm_shipping_bypasser._sanmar_inventory_has_size_data(
                [{"warehouse": "Robbinsville, NJ", "stock": {"S": 0, "M": 0}}]
            )
        )


class ShippingAddressRadioTests(unittest.TestCase):
    def test_waits_for_delayed_address_radio(self):
        driver = mock.Mock()
        radio = object()
        driver.execute_script.side_effect = [None, None, radio]

        with (
            mock.patch.object(crm_shipping_bypasser, "_click_with_fallback") as click,
            mock.patch.object(crm_shipping_bypasser.time, "sleep"),
        ):
            crm_shipping_bypasser._click_radio_near_text(driver, "123 EZ TEES INC")

        self.assertEqual(driver.execute_script.call_count, 3)
        click.assert_called_once_with(driver, radio)

    def test_timeout_preserves_address_name_in_error(self):
        driver = mock.Mock()
        driver.execute_script.return_value = None

        with (
            mock.patch.object(crm_shipping_bypasser.time, "monotonic", side_effect=[0.0, 0.0, 1.0]),
            mock.patch.object(crm_shipping_bypasser.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "123 EZ TEES INC"):
                crm_shipping_bypasser._click_radio_near_text(
                    driver,
                    "123 EZ TEES INC",
                    timeout=0.5,
                )


class ShippingBypassSingleCleanupTests(unittest.TestCase):
    def test_worker_exception_cleans_sanmar_cart(self):
        crm_driver = object()
        sanmar_driver = object()

        with (
            mock.patch.object(crm_shipping_bypasser, "_build_crm_session_driver", return_value=crm_driver),
            mock.patch.object(crm_shipping_bypasser, "_build_sanmar_driver", return_value=sanmar_driver),
            mock.patch.object(crm_shipping_bypasser, "_run_order_with_drivers", side_effect=RuntimeError("address failed")),
            mock.patch.object(crm_shipping_bypasser, "_cleanup_after_failed_order", return_value=True) as cleanup,
            mock.patch.object(crm_shipping_bypasser, "safe_take_screenshot"),
            mock.patch.object(crm_shipping_bypasser, "safe_driver_quit"),
            mock.patch.object(crm_shipping_bypasser, "_publish_status"),
        ):
            payload = crm_shipping_bypasser._run_single_with_mode(False, "5039567")

        self.assertFalse(payload["success"])
        self.assertEqual(payload["report"][0]["outcome"], "worker_exception")
        cleanup.assert_called_once_with(sanmar_driver, "5039567", payload["report"])


class ShippingBypassStockBufferTests(unittest.TestCase):
    def _product_lines(self, available, needed=1):
        return [
            {
                "product": {"index": 1, "product_id": "PC54"},
                "quantities": {"M": needed},
                "inventory": [
                    {
                        "warehouse": "Robbinsville, NJ",
                        "stock": {"M": available},
                    }
                ],
            }
        ]

    def test_default_plan_keeps_ten_piece_safety_buffer(self):
        warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
            self._product_lines(available=1),
            "inhouse",
        )

        self.assertIsNone(warehouse)
        self.assertIsNone(plan)

    def test_manual_override_uses_available_stock_without_safety_buffer(self):
        warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
            self._product_lines(available=1),
            "inhouse",
            stock_buffer=0,
        )

        self.assertEqual(warehouse, "Robbinsville, NJ")
        self.assertEqual(plan["warehouses"], ["Robbinsville, NJ"])

    def test_manual_override_still_rejects_actual_stock_shortages(self):
        warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
            self._product_lines(available=1, needed=2),
            "inhouse",
            stock_buffer=0,
        )

        self.assertIsNone(warehouse)
        self.assertIsNone(plan)

    def test_split_across_products_excludes_nj_for_both_destinations(self):
        product_lines = [
            {
                "product": {"index": 1, "product_id": "PC54"},
                "quantities": {"M": 2},
                "inventory": [
                    {"warehouse": "Robbinsville, NJ", "stock": {"M": 20}},
                    {"warehouse": "Richmond, VA", "stock": {"M": 12}},
                ],
            },
            {
                "product": {"index": 2, "product_id": "PC61"},
                "quantities": {"L": 3},
                "inventory": [{"warehouse": "Cincinnati, OH", "stock": {"L": 13}}],
            },
        ]
        for order_type in ("inhouse", "mach6"):
            for stock_buffer in (10, 0):
                with self.subTest(order_type=order_type, stock_buffer=stock_buffer):
                    warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
                        product_lines, order_type, stock_buffer=stock_buffer,
                    )

                    self.assertIsNone(warehouse)
                    self.assertEqual(plan["warehouses"], ["Richmond, VA", "Cincinnati, OH"])
                    self.assertEqual(plan["pieces_by_warehouse"], {"Richmond, VA": 2, "Cincinnati, OH": 3})
                    self.assertEqual([line["quantities"] for line in plan["expanded_lines"]], [{"M": 2}, {"L": 3}])

    def test_same_size_split_is_rejected_when_nj_is_needed_to_complete_quantity(self):
        for order_type in ("inhouse", "mach6"):
            for stock_buffer in (10, 0):
                with self.subTest(order_type=order_type, stock_buffer=stock_buffer):
                    lines = self._product_lines(available=stock_buffer + 4, needed=5)
                    lines[0]["inventory"].append(
                        {"warehouse": "Richmond, VA", "stock": {"M": stock_buffer + 1}}
                    )
                    warehouse, plan = crm_shipping_bypasser._choose_warehouse_plan(
                        lines, order_type, stock_buffer=stock_buffer,
                    )

                    self.assertIsNone(warehouse)
                    self.assertIsNone(plan)


class ShippingBypassDeliveryFallbackTests(unittest.TestCase):
    def _run_order(
        self,
        *,
        eta_states=None,
        inventory=None,
        allow_low_stock_buffer=False,
        cleanup_success=True,
        split_cart_mismatch=False,
        shipping_class="rush",
        dry_run=False,
    ):
        day = lambda value: crm_shipping_bypasser.datetime(2026, 10, value).date()
        product = {"index": 1, "product_id": "LST350", "color": "Lime Shock", "quantities": {"3XL": 1, "4XL": 1}}
        order = {
            "order_id": "5000001",
            "po": "H-TestSplit",
            "product_id": product["product_id"],
            "color": product["color"],
            "quantities": product["quantities"],
            "products": [product],
            "production_date": day(6),
            "due_date": day(7),
            "order_type": "inhouse",
            "shipping_class": shipping_class,
            "stock_tab_index": 1,
        }
        if inventory is None:
            inventory = [
                {"warehouse": "Robbinsville, NJ", "stock": {"3XL": 17, "4XL": 0}},
                {"warehouse": "Richmond, VA", "stock": {"3XL": 17, "4XL": 0}},
                {"warehouse": "Cincinnati, OH", "stock": {"3XL": 0, "4XL": 11}},
                {"warehouse": "Phoenix, AZ", "stock": {"3XL": 1, "4XL": 13}},
                {"warehouse": "Seattle, WA", "stock": {"3XL": 20, "4XL": 22}},
            ]
        if eta_states is None:
            eta_states = [
                {"Phoenix, AZ": day(8), "Seattle, WA": day(8)},
                {"Richmond, VA": day(5), "Cincinnati, OH": day(6)},
            ]
        eta_states = iter(eta_states)
        cart = []
        pending = []
        cart_reads = 0

        def fill_quantities(_driver, warehouse, quantities):
            pending[:] = [
                {"style": "LST350", "color": "Lime Shock", "warehouse": warehouse, "size": size, "quantity": qty}
                for size, qty in quantities.items()
            ]

        def add_to_box(_driver):
            cart.extend(pending)
            pending.clear()

        def read_cart(_driver, timeout):
            nonlocal cart_reads
            cart_reads += 1
            rows = [dict(row) for row in cart]
            if split_cart_mismatch and cart_reads == 2:
                rows[-1]["quantity"] += 1
            return rows

        def clear_cart(_driver, order_id):
            if cleanup_success:
                cart.clear()
            return {"success": cleanup_success, "message": "Cart cleared." if cleanup_success else "Cart could not be cleared."}

        def read_eta(_driver, _order_type, **kwargs):
            state = next(eta_states)
            if isinstance(state, Exception):
                raise state
            if state is None:
                return {"eta": None, "eta_by_warehouse": {}, "freight_calculation_unavailable": True}
            dates = {warehouse: state[warehouse] for warehouse in kwargs["selected_warehouses"]}
            return {"eta": max(dates.values()), "eta_by_warehouse": {name: str(value) for name, value in dates.items()}}

        patches = {
            "_require_crm_order_ready_once_with_refresh": {},
            "_publish_status": {},
            "_extract_order_data": {"return_value": order},
            "_pending_shipping_bypass_submission": {"return_value": None},
            "_saved_shipping_bypass_confirmation": {"return_value": None},
            "_crm_stock_order_yellow_visual_exists": {"return_value": False},
            "_crm_manual_order_row_exists": {"return_value": False},
            "_historical_shipping_bypass_po_exists": {"return_value": False},
            "safe_get_with_partial_load": {},
            "safe_take_screenshot": {},
            "_ensure_sanmar_logged_in": {},
            "_sanmar_cart_has_items": {"side_effect": lambda _driver: {"hasItems": bool(cart)}},
            "_search_sanmar_product": {},
            "_select_sanmar_color": {},
            "_wait_for_sanmar_inventory": {"return_value": inventory},
            "_sanmar_closed_warehouses": {"return_value": set()},
            "_fill_sanmar_quantities": {"side_effect": fill_quantities},
            "_add_current_product_to_box": {"side_effect": add_to_box},
            "_wait_for_sanmar_cart_lines": {"side_effect": read_cart},
            "_click_sanmar_button": {},
            "_wait_for_text": {},
            "_select_shipping_destination": {"return_value": {"ship_mode": "ship", "address": "123 EZ TEES INC"}},
            "_select_ups_eta_for_shipping_plan": {"side_effect": read_eta},
            "_clear_sanmar_cart": {"side_effect": clear_cart},
            "_change_crm_production_date": {"side_effect": lambda _driver, _order_id, target: target},
            "_change_crm_due_date": {"side_effect": lambda _driver, _order_id, target: target},
            "_fill_review_and_submit": {"return_value": "dry_run_ready" if dry_run else "submitted"},
            "_capture_sanmar_confirmation": {"return_value": {"po_confirmed": True, "po": order["po"]}},
            "_remember_pending_shipping_bypass_submission": {},
            "_mark_pending_shipping_bypass_submission_recorded": {},
            "_record_crm_manual_order": {"return_value": "recorded"},
            "_append_crm_production_note": {"return_value": "recorded"},
        }
        crm_driver = object()
        sanmar_driver = object()
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(mock.patch.object(crm_shipping_bypasser, name, **options)) for name, options in patches.items()}
            result = crm_shipping_bypasser._process_open_order(
                crm_driver,
                sanmar_driver,
                order["order_id"],
                dry_run=dry_run,
                stock_tab_index=1,
                stock_tab_count=1,
                allow_low_stock_buffer=allow_low_stock_buffer,
            )
        return result, mocks, cart

    def _assert_no_purchase(self, mocks):
        mocks["_fill_review_and_submit"].assert_not_called()
        mocks["_capture_sanmar_confirmation"].assert_not_called()
        mocks["_record_crm_manual_order"].assert_not_called()
        mocks["_append_crm_production_note"].assert_not_called()
        mocks["_change_crm_due_date"].assert_not_called()
        mocks["_change_crm_production_date"].assert_not_called()

    def test_nj_only_complete_order_remains_eligible(self):
        result, mocks, cart = self._run_order(
            inventory=[{"warehouse": "Robbinsville, NJ", "stock": {"3XL": 17, "4XL": 11}}],
            eta_states=[{"Robbinsville, NJ": crm_shipping_bypasser.datetime(2026, 10, 5).date()}],
        )

        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["warehouses"], ["Robbinsville, NJ"])
        self.assertEqual({row["warehouse"] for row in cart}, {"Robbinsville, NJ"})
        mocks["_fill_review_and_submit"].assert_called_once()
        mocks["_append_crm_production_note"].assert_not_called()

    def test_nj_plus_other_warehouse_is_skipped_before_adding_partial_stock(self):
        for override in (False, True):
            with self.subTest(buffer_override=override):
                result, mocks, cart = self._run_order(
                    inventory=[
                        {"warehouse": "Robbinsville, NJ", "stock": {"3XL": 17, "4XL": 0}},
                        {"warehouse": "Richmond, VA", "stock": {"3XL": 0, "4XL": 11}},
                    ],
                    allow_low_stock_buffer=override,
                )

                self.assertFalse(result["success"])
                self.assertEqual(result["outcome"], "no_single_warehouse")
                self.assertIn("NJ excluded", result["message"])
                self.assertIn("Skipped for now", result["message"])
                self.assertEqual(cart, [])
                mocks["_fill_sanmar_quantities"].assert_not_called()
                mocks["_add_current_product_to_box"].assert_not_called()
                mocks["_select_ups_eta_for_shipping_plan"].assert_not_called()
                self._assert_no_purchase(mocks)

    def test_late_complete_order_does_not_use_nj_to_make_an_on_time_split(self):
        day = lambda value: crm_shipping_bypasser.datetime(2026, 10, value).date()
        result, mocks, _cart = self._run_order(
            inventory=[
                {"warehouse": "Robbinsville, NJ", "stock": {"3XL": 17, "4XL": 0}},
                {"warehouse": "Richmond, VA", "stock": {"3XL": 0, "4XL": 11}},
                {"warehouse": "Seattle, WA", "stock": {"3XL": 20, "4XL": 22}},
            ],
            eta_states=[{"Seattle, WA": day(8)}, {"Seattle, WA": day(8), "Richmond, VA": day(5)}],
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "eta_after_due_date")
        self.assertEqual(result["warehouses"], ["Seattle, WA", "Richmond, VA"])
        self.assertNotIn("Robbinsville, NJ", [call.args[1] for call in mocks["_fill_sanmar_quantities"].call_args_list])
        self._assert_no_purchase(mocks)

    def test_late_complete_warehouse_falls_back_to_on_time_split_with_same_po(self):
        for override, first_warehouse in [(False, "Seattle, WA"), (True, "Phoenix, AZ")]:
            with self.subTest(buffer_override=override):
                result, mocks, cart = self._run_order(allow_low_stock_buffer=override)

                self.assertTrue(result["success"], result["message"])
                self.assertEqual(result["outcome"], "shipping_bypass_ordered")
                self.assertEqual(result["warehouses"], ["Richmond, VA", "Cincinnati, OH"])
                self.assertEqual(result["eta"], "2026-10-06")
                self.assertEqual(result["shipping_plan_attempts"][0]["warehouses"], [first_warehouse])
                self.assertEqual(
                    [(call.args[1], call.args[2]) for call in mocks["_fill_sanmar_quantities"].call_args_list],
                    [(first_warehouse, {"3XL": 1, "4XL": 1}), ("Richmond, VA", {"3XL": 1}), ("Cincinnati, OH", {"4XL": 1})],
                )
                self.assertEqual([(row["warehouse"], row["size"], row["quantity"]) for row in cart], [("Richmond, VA", "3XL", 1), ("Cincinnati, OH", "4XL", 1)])
                mocks["_clear_sanmar_cart"].assert_called_once()
                mocks["_fill_review_and_submit"].assert_called_once()
                self.assertEqual(mocks["_fill_review_and_submit"].call_args.args[1], "H-TestSplit")
                mocks["_capture_sanmar_confirmation"].assert_called_once()
                mocks["_remember_pending_shipping_bypass_submission"].assert_called_once()
                mocks["_record_crm_manual_order"].assert_called_once()
                self.assertEqual(mocks["_record_crm_manual_order"].call_args.args[2], "H-TestSplit")
                mocks["_mark_pending_shipping_bypass_submission_recorded"].assert_called_once()
                note = "tab 1: 2 boxes from sanmar with the same PO\n1 pc from VA\n1 pc from OH"
                self.assertEqual(result["production_note"], note)
                self.assertEqual(mocks["_append_crm_production_note"].call_args.args[2], note)
                mocks["_change_crm_due_date"].assert_not_called()

    def test_on_time_complete_warehouse_keeps_single_warehouse_preference(self):
        result, mocks, _cart = self._run_order(eta_states=[{"Seattle, WA": crm_shipping_bypasser.datetime(2026, 10, 5).date()}])

        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["warehouses"], ["Seattle, WA"])
        self.assertEqual(len(result["shipping_plan_attempts"]), 1)
        mocks["_clear_sanmar_cart"].assert_not_called()
        mocks["_append_crm_production_note"].assert_not_called()

    def test_split_rejects_latest_arrival_on_due_date(self):
        day = lambda value: crm_shipping_bypasser.datetime(2026, 10, value).date()
        result, mocks, _cart = self._run_order(eta_states=[{"Seattle, WA": day(8)}, {"Richmond, VA": day(5), "Cincinnati, OH": day(7)}])

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "eta_after_due_date")
        self.assertEqual(result["eta"], "2026-10-07")
        self.assertEqual(result["warehouses"], ["Richmond, VA", "Cincinnati, OH"])
        self._assert_no_purchase(mocks)

    def test_split_stops_when_delivery_dates_are_unavailable(self):
        result, mocks, _cart = self._run_order(eta_states=[{"Seattle, WA": crm_shipping_bypasser.datetime(2026, 10, 8).date()}, None])

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "sanmar_ups_unavailable")
        self.assertIn("Split-warehouse delivery dates could not be confirmed", result["message"])
        self._assert_no_purchase(mocks)

    def test_split_stops_when_a_warehouse_eta_cannot_be_read(self):
        result, mocks, _cart = self._run_order(eta_states=[{"Seattle, WA": crm_shipping_bypasser.datetime(2026, 10, 8).date()}, RuntimeError("Missing Richmond, VA delivery date")])

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "sanmar_ups_unavailable")
        self.assertIn("Richmond, VA", result["message"])
        self._assert_no_purchase(mocks)

    def test_split_stops_if_original_cart_cannot_be_cleared(self):
        result, mocks, cart = self._run_order(cleanup_success=False)

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "sanmar_cart_cleanup_failed")
        self.assertTrue(result["stop_run"])
        self.assertEqual(mocks["_fill_sanmar_quantities"].call_count, 1)
        self.assertEqual({row["warehouse"] for row in cart}, {"Seattle, WA"})
        self._assert_no_purchase(mocks)

    def test_split_cart_is_validated_again_before_shipping_and_submission(self):
        result, mocks, _cart = self._run_order(split_cart_mismatch=True)

        self.assertFalse(result["success"])
        self.assertEqual(result["outcome"], "sanmar_cart_mismatch")
        self.assertEqual(mocks["_wait_for_sanmar_cart_lines"].call_count, 2)
        self.assertEqual(mocks["_select_ups_eta_for_shipping_plan"].call_count, 1)
        self._assert_no_purchase(mocks)

    def test_split_retains_extension_override_and_default_buffer(self):
        inventory = [
            {"warehouse": "Robbinsville, NJ", "stock": {"3XL": 1, "4XL": 0}},
            {"warehouse": "Richmond, VA", "stock": {"3XL": 1, "4XL": 0}},
            {"warehouse": "Cincinnati, OH", "stock": {"3XL": 0, "4XL": 1}},
            {"warehouse": "Phoenix, AZ", "stock": {"3XL": 1, "4XL": 13}},
            {"warehouse": "Seattle, WA", "stock": {"3XL": 20, "4XL": 22}},
        ]
        late = crm_shipping_bypasser.datetime(2026, 10, 8).date()
        result, mocks, _cart = self._run_order(
            inventory=inventory,
            eta_states=[{"Seattle, WA": late}, {"Seattle, WA": late, "Phoenix, AZ": late}],
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["warehouses"], ["Seattle, WA", "Phoenix, AZ"])
        self.assertNotIn("Robbinsville, NJ", result["warehouses"])
        self.assertNotIn("Richmond, VA", result["warehouses"])
        self.assertNotIn("Cincinnati, OH", result["warehouses"])
        self._assert_no_purchase(mocks)

        result, _mocks, _cart = self._run_order(inventory=inventory, allow_low_stock_buffer=True)
        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["warehouses"], ["Richmond, VA", "Cincinnati, OH"])

    def test_existing_split_plan_uses_same_po_without_an_extra_cart_attempt(self):
        result, mocks, _cart = self._run_order(
            inventory=[
                {"warehouse": "Robbinsville, NJ", "stock": {"3XL": 17, "4XL": 0}},
                {"warehouse": "Richmond, VA", "stock": {"3XL": 17, "4XL": 0}},
                {"warehouse": "Cincinnati, OH", "stock": {"3XL": 0, "4XL": 11}},
            ],
            eta_states=[{"Richmond, VA": crm_shipping_bypasser.datetime(2026, 10, 5).date(), "Cincinnati, OH": crm_shipping_bypasser.datetime(2026, 10, 6).date()}],
        )

        self.assertTrue(result["success"], result["message"])
        self.assertIn("2 boxes from sanmar with the same PO", result["production_note"])
        self.assertEqual(len(result["shipping_plan_attempts"]), 1)
        mocks["_clear_sanmar_cart"].assert_not_called()

    def test_free_shipping_preserves_existing_due_date_extension(self):
        result, mocks, _cart = self._run_order(shipping_class="free")

        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["order"]["due_date"], "2026-10-09")
        self.assertEqual(result["warehouses"], ["Seattle, WA"])
        mocks["_change_crm_due_date"].assert_called_once()
        mocks["_clear_sanmar_cart"].assert_not_called()

    def test_split_dry_run_stops_at_review_with_production_note_ready(self):
        result, mocks, _cart = self._run_order(dry_run=True)

        self.assertTrue(result["success"], result["message"])
        self.assertEqual(result["outcome"], "shipping_bypass_ready")
        self.assertEqual(result["production_note_state"], "dry_run_production_note_ready")
        self.assertTrue(mocks["_fill_review_and_submit"].call_args.kwargs["dry_run"])
        self.assertTrue(mocks["_record_crm_manual_order"].call_args.kwargs["dry_run"])
        mocks["_capture_sanmar_confirmation"].assert_not_called()
        mocks["_remember_pending_shipping_bypass_submission"].assert_not_called()
        mocks["_append_crm_production_note"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
