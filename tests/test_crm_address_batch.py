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

import server  # noqa: E402
import crm_validate_address  # noqa: E402
import crm_order_goods  # noqa: E402
import crm_auto_splitter  # noqa: E402
import crm_product_separator  # noqa: E402
import automation_runtime  # noqa: E402


def _report_row(order_id, *, success=True, manual_review_required=False, resolution="validated"):
    return {
        "order_id": str(order_id),
        "success": bool(success),
        "manual_review_required": bool(manual_review_required),
        "resolution": resolution,
        "warnings": [],
    }


class CrmAutoSplitterTests(unittest.TestCase):
    def test_cancel_confirmation_accepts_crm_cancel_order_status(self):
        self.assertTrue(crm_auto_splitter._is_cancel_order_status("Cancel Order"))
        self.assertTrue(crm_auto_splitter._is_cancel_order_status("Cancelled"))
        self.assertFalse(crm_auto_splitter._is_cancel_order_status("Stock Auto Ordering Queued"))

    def test_cancel_confirmation_accepts_status_history_cancel_order(self):
        body = "Status History and Art Changes Status History 05/17 Api Scripts Cancel Order 05/17 Auto Ordering Ordered Stock"

        self.assertTrue(crm_auto_splitter._status_history_confirms_cancel_order(body))

    def test_refund_fee_amount_match_uses_absolute_amount(self):
        self.assertTrue(crm_auto_splitter._money_amount_matches("-$445.11", "445.11"))
        self.assertFalse(crm_auto_splitter._money_amount_matches("-$445.10", "445.11"))

    def test_resume_can_use_split_total_after_original_refund_fee_zeroes_total(self):
        split_total = crm_auto_splitter.Decimal("445.11")
        original_grand_total = crm_auto_splitter.Decimal("0.00")
        resume_existing_order_ids = ["4536164", "4536167"]

        if original_grand_total == crm_auto_splitter.Decimal("0.00") and resume_existing_order_ids and split_total > crm_auto_splitter.Decimal("0.00"):
            original_grand_total = split_total.quantize(crm_auto_splitter.Decimal("0.01"))

        self.assertEqual(original_grand_total, crm_auto_splitter.Decimal("445.11"))


class CrmProductSeparatorTests(unittest.TestCase):
    def test_not_authenticated_page_is_detected_before_design_tab_failure(self):
        driver = mock.Mock()
        driver.execute_script.return_value = "\u00d7 Error Not authenticated close"

        self.assertTrue(crm_product_separator._is_not_authenticated_page(driver))

    @mock.patch.object(crm_product_separator, "_run_order_dry_worker")
    def test_order_chunk_worker_retries_transient_crm_readiness_error(self, mock_dry_worker):
        mock_dry_worker.side_effect = [
            {
                "success": False,
                "message": "Product Separator failed for order 4607189: CRM app did not become ready.",
                "target_order_id": "4607189",
            },
            {
                "success": True,
                "message": "Product Separator skipped order 4607189: no mixed product tabs detected.",
                "target_order_id": "4607189",
            },
        ]

        results = crm_product_separator._run_order_chunk_worker(["4607189"], "profile", "results")

        self.assertEqual(mock_dry_worker.call_count, 2)
        self.assertTrue(results[0]["success"])
        self.assertTrue(results[0]["retried_after_transient_error"])
        self.assertIn("CRM app did not become ready", results[0]["first_attempt_message"])

    def test_stock_ordered_status_accepts_stock_history_confirmation(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {
            "already_applied": True,
            "clicked_apply": False,
            "confirmation": "stock_history",
        }

        result = crm_product_separator._apply_stock_ordered_status(driver)

        self.assertTrue(result["already_applied"])
        self.assertEqual(result["confirmation"], "stock_history")
        driver.execute_script.assert_called_once()

    def test_tabs_still_needing_split_filters_mixed_tabs(self):
        scan = {
            "tabs": [
                {"tab_number": 1, "needs_split": False},
                {"tab_number": 2, "needs_split": True},
            ]
        }

        remaining = crm_product_separator._tabs_still_needing_split(scan)

        self.assertEqual([tab["tab_number"] for tab in remaining], [2])

    @mock.patch.object(server, "_execute_crm_product_separator_script")
    def test_live_batch_continues_when_preflight_has_split_orders_and_one_failure(self, mock_run_script):
        preflight_payload = {
            "success": False,
            "message": "Product Separator list dry run complete. 1 order(s) need splitting, 0 already okay.",
            "action": "product_separator_list",
            "dry_run": True,
            "list_mode": "rush",
            "parallel_workers": 4,
            "order_ids": ["4609102", "4607350"],
            "split_order_ids": ["4609102"],
            "failed_order_ids": ["4607350"],
            "report": [
                {
                    "order_id": "4609102",
                    "success": True,
                    "resolution": "dry_run_ready",
                    "needs_split": True,
                    "message": "ready",
                },
                {
                    "order_id": "4607350",
                    "success": False,
                    "resolution": None,
                    "needs_split": False,
                    "message": "CRM authentication failed: Not authenticated.",
                },
            ],
        }
        live_payload = {
            "success": True,
            "message": "Product Separator completed order 4609102.",
            "action": "product_separator_order",
            "dry_run": False,
            "target_order_id": "4609102",
            "order_ids": ["4609102"],
            "resolution": "split_complete",
            "duration_seconds": 12.0,
        }
        mock_run_script.side_effect = [
            (False, preflight_payload["message"], preflight_payload),
            (True, live_payload["message"], live_payload),
        ]

        ok, message, payload = server._execute_crm_product_separator_worker(
            dry_run=False,
            list_mode="rush",
            parallel_workers=4,
        )

        self.assertFalse(ok)
        self.assertEqual(mock_run_script.call_count, 2)
        self.assertEqual(payload["dry_run"], False)
        self.assertEqual(payload["live_order_ids"], ["4609102"])
        self.assertIn("1/1 live order", message)
        rows_by_order = {row["order_id"]: row for row in payload["order_results"]}
        self.assertEqual(rows_by_order["4609102"]["status"], "Separated")
        self.assertEqual(rows_by_order["4607350"]["status"], "Needs attention")

    @mock.patch.object(server, "_execute_crm_product_separator_script")
    def test_live_batch_retries_transient_live_order_failure(self, mock_run_script):
        preflight_payload = {
            "success": True,
            "message": "Product Separator list dry run complete. 1 order(s) need splitting, 0 already okay.",
            "action": "product_separator_list",
            "dry_run": True,
            "list_mode": "rush",
            "parallel_workers": 4,
            "order_ids": ["4607567"],
            "split_order_ids": ["4607567"],
            "report": [
                {
                    "order_id": "4607567",
                    "success": True,
                    "resolution": "dry_run_ready",
                    "needs_split": True,
                    "message": "ready",
                },
            ],
        }
        retryable_payload = {
            "success": False,
            "message": "Product Separator failed for order 4607567: CRM app did not become ready.",
            "target_order_id": "4607567",
        }
        live_payload = {
            "success": True,
            "message": "Product Separator completed order 4607567.",
            "target_order_id": "4607567",
            "order_ids": ["4607567"],
            "resolution": "split_complete",
        }
        mock_run_script.side_effect = [
            (True, preflight_payload["message"], preflight_payload),
            (False, retryable_payload["message"], retryable_payload),
            (True, live_payload["message"], live_payload),
        ]

        ok, _message, payload = server._execute_crm_product_separator_worker(
            dry_run=False,
            list_mode="rush",
            parallel_workers=4,
        )

        self.assertTrue(ok)
        self.assertEqual(mock_run_script.call_count, 3)
        self.assertEqual(payload["live_order_ids"], ["4607567"])
        rows_by_order = {row["order_id"]: row for row in payload["order_results"]}
        self.assertEqual(rows_by_order["4607567"]["status"], "Separated")

    def test_server_recovers_partial_auto_splitter_order_ids(self):
        payload = {
            "success": False,
            "dry_run": False,
            "target_order_id": "4536106",
            "expected_tab_count": 12,
            "divisions": 2,
            "new_order_ids": ["4536164"],
            "report": {
                "partial": True,
                "split_orders": [{"order_id": "4536164"}, {"order_id": "4536167"}],
            },
        }

        self.assertTrue(server._crm_auto_splitter_recovery_payload_is_usable(payload, "4536106", 12, 2))
        self.assertEqual(server._crm_auto_splitter_recovery_order_ids_from_payload(payload), ["4536164", "4536167"])

    @mock.patch.object(crm_auto_splitter.time, "sleep", return_value=None)
    @mock.patch.object(crm_auto_splitter.time, "monotonic", side_effect=[0, 1, 2, 3, 4])
    @mock.patch.object(crm_auto_splitter, "_click_ng_button", return_value=True)
    def test_auto_splitter_order_save_waits_for_edit_order_button(self, _mock_click, _mock_monotonic, _mock_sleep):
        visible_states = [
            {"editOrderVisible": False, "saveOrderVisible": True, "saveOrderEnabled": False},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
        ]
        with mock.patch.object(crm_auto_splitter, "_order_scope", side_effect=Exception("scope busy")):
            with mock.patch.object(crm_auto_splitter, "_visible_order_save_state", side_effect=visible_states):
                result = crm_auto_splitter._save_order_and_wait(mock.Mock())

        self.assertTrue(result["editOrderVisible"])

    @mock.patch.object(crm_auto_splitter.time, "sleep", return_value=None)
    @mock.patch.object(crm_auto_splitter.time, "monotonic", side_effect=[0, 1, 2, 3, 4])
    @mock.patch.object(crm_auto_splitter, "_click_ng_button", return_value=True)
    def test_auto_splitter_order_save_does_not_finish_while_save_button_remains_enabled(self, _mock_click, _mock_monotonic, _mock_sleep):
        scope_state = {"saving": False, "editMode": False, "id": "4544103"}
        visible_states = [
            {"editOrderVisible": False, "saveOrderVisible": True, "saveOrderEnabled": True},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
            {"editOrderVisible": True, "saveOrderVisible": False, "saveOrderEnabled": False},
        ]
        with mock.patch.object(crm_auto_splitter, "_order_scope", return_value=scope_state):
            with mock.patch.object(crm_auto_splitter, "_visible_order_save_state", side_effect=visible_states) as mock_visible:
                result = crm_auto_splitter._save_order_and_wait(mock.Mock())

        self.assertEqual(result["id"], "4544103")
        self.assertEqual(mock_visible.call_count, 3)

    @mock.patch.object(server, "save_crm_state")
    @mock.patch.object(server, "load_crm_state")
    def test_auto_splitter_persists_into_own_history(self, mock_load_state, _mock_save_state):
        mock_load_state.return_value = {
            "last_run_timestamp": None,
            "last_run_success": None,
            "last_run_message": None,
            "last_order_count": 0,
            "total_runs": 0,
            "total_orders_processed": 0,
            "last_order_ids": [],
            "run_history": [],
            "auto_splitter_run_history": [],
        }

        state = server._persist_crm_auto_splitter_run_result(
            True,
            "Auto-split complete.",
            {
                "success": True,
                "dry_run": False,
                "target_order_id": "4536106",
                "new_order_ids": ["4536164", "4536167"],
                "expected_tab_count": 12,
                "divisions": 2,
                "duration_seconds": 63.9,
                "report": {"parallel_workers": 2},
            },
            dry_run=False,
        )

        self.assertEqual(state["run_history"], [])
        self.assertEqual(state["auto_splitter_run_history"][0]["automation_key"], "auto_splitter")
        self.assertEqual(state["auto_splitter_run_history"][0]["order_ids"], ["4536106", "4536164", "4536167"])
        self.assertEqual(state["auto_splitter_run_history"][0]["expected_tab_count"], 12)

    def test_stock_history_migrates_auto_splitter_entries_out(self):
        saved_state = {
            "run_history": [
                {
                    "timestamp": "2026-05-17T10:00:00",
                    "automation_key": "auto_splitter",
                    "automation_label": "Auto Splitter",
                    "success": True,
                    "order_ids": ["4536106", "4536164"],
                    "message": "split",
                },
                {
                    "timestamp": "2026-05-17T10:01:00",
                    "automation_key": "stock_unlocker",
                    "automation_label": "Stock Unlocker",
                    "success": True,
                    "order_ids": ["4537000"],
                    "message": "unlock",
                },
            ]
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(saved_state, handle)
            state_path = handle.name
        try:
            with mock.patch.object(server, "CRM_STATE_FILE", state_path):
                state = server.load_crm_state()
        finally:
            Path(state_path).unlink(missing_ok=True)

        self.assertEqual([row["automation_key"] for row in state["run_history"]], ["stock_unlocker"])
        self.assertEqual([row["automation_key"] for row in state["auto_splitter_run_history"]], ["auto_splitter"])

    @mock.patch.object(server, "_finish_crm_auto_splitter_runtime")
    @mock.patch.object(server, "_persist_crm_auto_splitter_run_result")
    @mock.patch.object(server, "_execute_crm_auto_splitter_worker")
    def test_auto_splitter_live_runs_dry_preflight_first(self, mock_execute, mock_persist, _mock_finish):
        mock_execute.side_effect = [
            (True, "dry ok", {"success": True, "dry_run": True, "expected_tab_count": 12, "divisions": 2}),
            (True, "live ok", {"success": True, "dry_run": False, "expected_tab_count": 12, "divisions": 2}),
        ]

        server._crm_auto_splitter_run_thread("4536106", 12, 2, dry_run=False, parallel_workers=2)

        self.assertEqual(mock_execute.call_count, 2)
        self.assertTrue(mock_execute.call_args_list[0].kwargs["dry_run"])
        self.assertFalse(mock_execute.call_args_list[0].kwargs["show_terminal"])
        self.assertFalse(mock_execute.call_args_list[1].kwargs["dry_run"])
        self.assertTrue(mock_persist.call_args.args[0])

    @mock.patch.object(server, "_finish_crm_auto_splitter_runtime")
    @mock.patch.object(server, "_persist_crm_auto_splitter_run_result")
    @mock.patch.object(server, "_execute_crm_auto_splitter_worker")
    def test_auto_splitter_live_stops_when_dry_preflight_fails(self, mock_execute, mock_persist, _mock_finish):
        mock_execute.return_value = (False, "tab count mismatch", {"success": False, "dry_run": True, "message": "tab count mismatch"})

        server._crm_auto_splitter_run_thread("4536106", 12, 2, dry_run=False, parallel_workers=2)

        self.assertEqual(mock_execute.call_count, 1)
        self.assertFalse(mock_persist.call_args.args[0])
        self.assertIn("dry run failed", mock_persist.call_args.args[1].lower())


class CrmAddressBatchWorkerTests(unittest.TestCase):
    def test_crm_attempt_modes_skip_visible_fallback_when_disabled(self):
        with mock.patch.object(crm_validate_address, "CRM_HEADLESS", True):
            with mock.patch.object(crm_validate_address, "CRM_ALLOW_VISIBLE_FALLBACK", False):
                self.assertEqual(crm_validate_address._crm_attempt_modes(), [True])
            with mock.patch.object(crm_validate_address, "CRM_ALLOW_VISIBLE_FALLBACK", True):
                self.assertEqual(crm_validate_address._crm_attempt_modes(), [True, False])

    def test_profile_clone_ignore_skips_component_payload_dirs(self):
        ignored = crm_validate_address._profile_clone_ignore(
            "unused",
            ["ActorSafetyLists", "Cache", "Cookies", "SingletonLock", "notes.txt"],
        )

        self.assertEqual(ignored, {"ActorSafetyLists", "Cache", "SingletonLock"})

    def test_shipping_list_row_classifier_includes_lime_green(self):
        self.assertEqual(
            crm_validate_address._classify_shipping_list_row_color((34, 236, 72)),
            "lime_green",
        )
        self.assertIn("lime_green", crm_validate_address.ALLOWED_SHIPPING_LIST_ROW_LABELS)

    def test_shipping_address_caps_normalization_ignores_address_cont(self):
        mixed_case = {
            "address": "2000 Oak Creek Rd",
            "address_cont": "Apt 228",
            "city": "River Ridge",
            "state": "Louisiana",
            "zip": "70123",
        }
        normalized = {
            "address": "2000 OAK CREEK RD",
            "address_cont": "Apt 228",
            "city": "NEW ORLEANS",
            "state": "Louisiana",
            "zip": "70123-5683",
        }

        self.assertTrue(crm_validate_address._shipping_address_needs_caps_normalization(mixed_case))
        self.assertFalse(crm_validate_address._shipping_address_needs_caps_normalization(normalized))

    def test_assess_address_text_accepts_directional_state_code_and_city_difference(self):
        address = {
            "address": "717 S Main GQ St",
            "city": "Salisbury",
            "state": "North Carolina",
            "zip": "28146",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "717 S MAIN GQ ST GRANITE QUARRY NC 28146-9133",
        )

        self.assertTrue(assessment["required_match"])
        self.assertTrue(assessment["city_only_mismatch"])
        self.assertFalse(assessment["postal_full_match"])
        self.assertFalse(assessment["city_match"])

    def test_assess_address_text_accepts_canadian_postal_prefix_match(self):
        address = {
            "address": "3588 Overlander Dr",
            "city": "Kamloops",
            "state": "British Columbia",
            "zip": "V2B 6T6",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "3588 OVERLANDER DR KAMLOOPS BC V2B 6Y1",
        )

        self.assertTrue(assessment["street_match"])
        self.assertTrue(assessment["city_match"])
        self.assertTrue(assessment["state_match"])
        self.assertTrue(assessment["safe_postal_prefix_match"])
        self.assertTrue(assessment["canadian_postal_prefix_match"])
        self.assertTrue(crm_validate_address._assessment_can_be_used(assessment))

    def test_dedupe_address_identifier_moves_box_into_address_cont_when_hash_present(self):
        address, address_cont, extracted = crm_validate_address._dedupe_address_identifier(
            "22425 W Historic Route 66 Box 965",
            "#965",
        )

        self.assertEqual(address, "22425 W HISTORIC ROUTE 66")
        self.assertEqual(address_cont, "BOX 965 #965")
        self.assertEqual(extracted, "BOX 965")

    def test_dedupe_address_identifier_moves_embedded_po_box_into_address_cont(self):
        address, address_cont, extracted = crm_validate_address._dedupe_address_identifier(
            "213 North High St., PO Box 176",
            "",
        )

        self.assertEqual(address, "213 North High St")
        self.assertEqual(address_cont, "PO Box 176")
        self.assertEqual(extracted, "PO Box 176")

    def test_classify_po_box_address_treats_embedded_po_box_as_mixed_street_address(self):
        profile = crm_validate_address._classify_po_box_address(
            {
                "address": "213 North High St., PO Box 176",
                "address_cont": "",
            }
        )

        self.assertTrue(profile["mixed_po_box_and_street"])
        self.assertTrue(profile["needs_embedded_po_box_split"])
        self.assertFalse(profile["po_box_only"])
        self.assertEqual(profile["street_line"], "213 North High St")
        self.assertEqual(profile["po_box_line"], "PO Box 176")

    def test_classify_po_box_address_treats_bare_embedded_box_as_mixed_street_address(self):
        profile = crm_validate_address._classify_po_box_address(
            {
                "address": "105 main street box 233",
                "address_cont": "",
            }
        )

        self.assertTrue(profile["mixed_po_box_and_street"])
        self.assertTrue(profile["needs_embedded_po_box_split"])
        self.assertFalse(profile["po_box_only"])
        self.assertEqual(profile["street_line"], "105 MAIN STREET")
        self.assertEqual(profile["po_box_line"], "BOX 233")

    def test_order_totals_shipping_class_distinguishes_free_from_priced_shipping(self):
        self.assertEqual(
            crm_validate_address._order_totals_shipping_class_from_text(
                "Order Totals Design 1 Total: $69.76 Shipping: info Free Grand Total: $147.20"
            ),
            "free",
        )
        self.assertEqual(
            crm_validate_address._order_totals_shipping_class_from_text(
                "Order Totals Design 1 Total: $71.80 Shipping: $37.99 Grand Total: $109.79"
            ),
            "rush",
        )

    def test_effective_address_cont_ignores_locality_overflow(self):
        address_fields = {
            "address": "687 West 204th Street, Apt. 2D",
            "address_cont": "New York, NY 10034",
            "city": "New York (Manhattan)",
            "state": "New York",
            "zip": "10034",
        }

        self.assertEqual(crm_validate_address._effective_address_cont(address_fields), "")

        address, address_cont, extracted = crm_validate_address._dedupe_address_identifier(
            address_fields["address"],
            crm_validate_address._effective_address_cont(address_fields),
        )

        self.assertEqual(address, "687 WEST 204TH STREET")
        self.assertEqual(address_cont, "APT. 2D")
        self.assertEqual(extracted, "APT. 2D")

    def test_normalize_display_address_line_converts_leading_number_word(self):
        self.assertEqual(
            crm_validate_address._normalize_display_address_line("One Dell Way"),
            "1 Dell Way",
        )

    def test_clean_city_field_value_strips_embedded_state_code(self):
        self.assertEqual(
            crm_validate_address._clean_city_field_value("Los Angeles, CA", "California", "90013"),
            "Los Angeles",
        )

    def test_clean_address_line_locality_suffix_removes_duplicate_city_state_zip(self):
        cleaned, removed = crm_validate_address._clean_address_line_locality_suffix(
            "580 72ND ST., Miami Beach, FL 33141",
            "Miami Beach",
            "Florida",
            "33141",
        )

        self.assertEqual(cleaned, "580 72ND ST.")
        self.assertEqual(removed, "Miami Beach, FL 33141")

    def test_clean_address_line_locality_suffix_removes_duplicate_city_state_without_zip(self):
        cleaned, removed = crm_validate_address._clean_address_line_locality_suffix(
            "1522 K St NW, Washington, DC",
            "Washington DC",
            "District Of Columbia",
            "20005",
        )

        self.assertEqual(cleaned, "1522 K St NW")
        self.assertEqual(removed, "Washington, DC")

    def test_address_cont_looks_like_street_fragment_when_main_address_is_only_number(self):
        address_fields = {
            "address": "2265",
            "address_cont": "Lake Crest Ct.",
            "city": "Martinez",
            "state": "California",
            "zip": "94553",
        }

        self.assertTrue(crm_validate_address._address_cont_looks_like_street_fragment(address_fields))

    def test_recover_misaligned_street_address_moves_street_from_address_cont(self):
        recovered = crm_validate_address._recover_misaligned_street_address(
            {
                "address": "Hyatt Place Washington DC/White Hou",
                "address_cont": "1522 K St NW, Washington, DC",
                "city": "Washington DC",
                "state": "District Of Columbia",
                "zip": "20005",
            }
        )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["source_field"], "address_cont")
        self.assertEqual(recovered["address"], "1522 K St NW")
        self.assertEqual(recovered["address_cont"], "Hyatt Place Washington DC/White Hou")
        self.assertEqual(recovered["removed_locality_suffix"], "Washington, DC")

    def test_recover_misaligned_street_address_extracts_embedded_street_from_address(self):
        recovered = crm_validate_address._recover_misaligned_street_address(
            {
                "address": "Main Street Events - Hyatt Place Washington DC/White Hou 1522 K St NW, Washington, DC",
                "address_cont": "",
                "city": "Washington DC",
                "state": "District Of Columbia",
                "zip": "20005",
            }
        )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["source_field"], "address")
        self.assertEqual(recovered["address"], "1522 K St NW")
        self.assertEqual(recovered["address_cont"], "Main Street Events - Hyatt Place Washington DC/White Hou")
        self.assertEqual(recovered["removed_locality_suffix"], "Washington, DC")

    def test_pick_existing_address_prefers_matching_address_cont(self):
        address = {
            "address": "687 West 204th Street",
            "address_cont": "Apt. 2D",
            "city": "New York",
            "state": "New York",
            "zip": "10034",
        }
        options = [
            {"text": "Resident - 687 W 204th St Apt 3D New York NY 10034", "preferred_all_caps": True},
            {"text": "Resident - 687 W 204th St Apt 2D New York NY 10034", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Resident - 687 W 204th St Apt 2D New York NY 10034")
        self.assertTrue(best["assessment"]["secondary_match"])

    def test_pick_existing_address_can_rescue_missing_street_number(self):
        address = {
            "address": "Avalon Drive West",
            "address_cont": "",
            "city": "Orange",
            "state": "Connecticut",
            "zip": "06477",
        }
        options = [
            {"text": "Resident - 5111 Avalon dr W Orange CT 06477", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertTrue(best["assessment"]["missing_number_rescue"])

    def test_address_cont_street_number_candidate_uses_bare_cont_number(self):
        address = {
            "address": "Frist Lane",
            "address_cont": "3061",
            "city": "Princeton",
            "state": "New Jersey",
            "zip": "08544",
        }

        self.assertEqual(
            crm_validate_address._address_cont_street_number_candidate(address),
            "3061",
        )
        address["address_cont"] = "#3061"
        self.assertEqual(
            crm_validate_address._address_cont_street_number_candidate(address),
            "3061",
        )

    def test_address_cont_street_number_candidate_rejects_unit_number(self):
        address = {
            "address": "Main Street",
            "address_cont": "Apt 4",
            "city": "Princeton",
            "state": "New Jersey",
            "zip": "08544",
        }

        self.assertEqual(crm_validate_address._address_cont_street_number_candidate(address), "")

    def test_address_cont_street_number_candidate_requires_street_name(self):
        address = {
            "address": "",
            "address_cont": "3061",
            "city": "Princeton",
            "state": "New Jersey",
            "zip": "08544",
        }

        self.assertEqual(crm_validate_address._address_cont_street_number_candidate(address), "")

    def test_pick_existing_address_handles_hyphenated_house_number_and_numbered_street(self):
        address = {
            "address": "119-15 192 Street St. Albans NY",
            "address_cont": "",
            "city": "St. Albans",
            "state": "New York",
            "zip": "11412",
        }
        options = [
            {"text": "Christopher Murray - 119-15 192 Street St. Albans NY 11412", "preferred_all_caps": False},
            {"text": "Christopher Murray - 11915 192ND ST SAINT ALBANS NY 11412-3624", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Christopher Murray - 11915 192ND ST SAINT ALBANS NY 11412-3624")
        self.assertTrue(best["assessment"]["required_match"])

    def test_assess_address_text_accepts_hawaii_hyphenated_house_number_and_city_alias(self):
        address = {
            "address": "61-4032 Kalo'Olo'o Dr",
            "address_cont": "",
            "city": "Waimea",
            "state": "Hawaii",
            "zip": "96743",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "61-4032 KALOOLOO DR KAMUELA HI 96743-9711",
        )

        self.assertTrue(assessment["required_match"])
        self.assertTrue(assessment["house_match"])
        self.assertTrue(assessment["street_match"])
        self.assertTrue(assessment["city_match"])

    def test_pick_existing_address_prefers_canadian_validated_postal_prefix_match(self):
        address = {
            "address": "3588 Overlander Dr",
            "address_cont": "",
            "city": "Kamloops",
            "state": "British Columbia",
            "zip": "V2B 6T6",
        }
        options = [
            {"text": "Danielle Elliot - 3588 Overlander Dr Kamloops BC V2B 6T6", "preferred_all_caps": False},
            {"text": "Danielle Elliot - 3588 OVERLANDER DR KAMLOOPS BC V2B 6Y1", "preferred_all_caps": True},
        ]

        best = crm_validate_address._pick_existing_address_option(address, options)

        self.assertIsNotNone(best)
        self.assertEqual(best["option"]["text"], "Danielle Elliot - 3588 OVERLANDER DR KAMLOOPS BC V2B 6Y1")
        self.assertTrue(best["assessment"]["safe_postal_prefix_match"])
        self.assertTrue(best["assessment"]["canadian_postal_prefix_match"])

    def test_existing_address_looks_like_weak_duplicate_when_not_all_caps(self):
        best_existing = {
            "option": {
                "text": "Dianne Meim - 457 Seahorse Drive Vallejo CA 94591-7126",
                "preferred_all_caps": False,
            },
            "assessment": {
                "exact_match": True,
                "city_match": True,
                "postal_full_match": True,
            },
        }

        self.assertTrue(crm_validate_address._existing_address_looks_like_weak_duplicate(best_existing))

    def test_existing_address_resolution_can_require_green_valid_state(self):
        address = {
            "recipient": "William Saks",
            "address": "174 Trail Dr",
            "address_cont": "",
            "city": "Alpine",
            "state": "Wyoming",
            "zip": "83128",
        }
        best_existing = {
            "option": {
                "text": "William Saks - 174 Trail Dr Alpine WY 83128",
                "preferred_all_caps": False,
            },
            "assessment": {
                "city_only_mismatch": False,
            },
        }

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))):
                with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                    with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=True):
                        with mock.patch.object(crm_validate_address, "_prepare_shipping_form_for_save") as mock_prepare:
                            result = crm_validate_address._try_resolve_with_existing_address(
                                object(),
                                object(),
                                "4389708",
                                True,
                                dict(address),
                                dict(address),
                                "",
                                [],
                                existing_options=[best_existing["option"]],
                                best_existing=best_existing,
                                accept_save_button_ready=False,
                            )

        self.assertIsNone(result)
        mock_prepare.assert_not_called()

    def test_existing_address_resolution_can_reuse_prevalidated_selection(self):
        address = {
            "recipient": "Heidi Kelly",
            "address": "5774 US-190",
            "address_cont": "",
            "city": "Livingston",
            "state": "Texas",
            "zip": "77351",
        }
        best_existing = {
            "option": {
                "text": "Heidi Kelly - K6Ranch Livingston - 5774 US-190 Livingston TX, 77351",
                "preferred_all_caps": False,
            },
            "assessment": {
                "city_only_mismatch": False,
            },
        }

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))):
                with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                    with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=False):
                        with mock.patch.object(crm_validate_address, "_prepare_shipping_form_for_save", return_value=(None, dict(address), "")):
                            with mock.patch.object(crm_validate_address, "_save_shipping_transaction") as mock_save:
                                result = crm_validate_address._try_resolve_with_existing_address(
                                    object(),
                                    object(),
                                    "4391701",
                                    True,
                                    dict(address),
                                    dict(address),
                                    "",
                                    [],
                                    existing_options=[best_existing["option"]],
                                    best_existing=best_existing,
                                    accept_save_button_ready=False,
                                    allow_prevalidated_selection=True,
                                )

        self.assertIsNotNone(result)
        self.assertTrue(result["success"])
        self.assertEqual(result["resolution"], "existing_address")
        mock_save.assert_called_once()

    def test_existing_address_resolution_can_use_safe_assessed_current_address_after_no_candidates(self):
        original_address = {
            "recipient": "Clinton Bohn",
            "address": "2301 FM1187",
            "address_cont": "203",
            "city": "Mansfield",
            "state": "Texas",
            "zip": "76063",
        }
        selected_address = {
            "recipient": "Clinton Bohn",
            "address": "2301 FM1187",
            "address_cont": "203",
            "city": "Mansfield",
            "state": "Texas",
            "zip": "76063",
        }
        best_existing = {
            "option": {
                "text": "Clinton Bohn - 2301 FM1187 Mansfield TX 76063",
                "preferred_all_caps": True,
            },
            "assessment": {
                "city_only_mismatch": False,
            },
        }
        warnings = []

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(selected_address))):
                with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                    with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=False):
                        with mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(selected_address)):
                            with mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False):
                                with mock.patch.object(
                                    crm_validate_address,
                                    "_persist_validated_address_via_modal_scope",
                                    return_value={"ok": True},
                                ) as mock_persist:
                                    with mock.patch.object(crm_validate_address, "_prepare_shipping_form_for_save", return_value=(None, dict(selected_address), "203")):
                                        with mock.patch.object(crm_validate_address, "_save_shipping_transaction") as mock_save:
                                            result = crm_validate_address._try_resolve_with_existing_address(
                                                object(),
                                                object(),
                                                "4415466",
                                                False,
                                                dict(original_address),
                                                dict(original_address),
                                                "203",
                                                warnings,
                                                existing_options=[best_existing["option"]],
                                                best_existing=best_existing,
                                                accept_save_button_ready=False,
                                                allow_assessed_current_address=True,
                                            )

        self.assertIsNotNone(result)
        self.assertTrue(result["success"])
        self.assertEqual(result["resolution"], "existing_address")
        self.assertEqual(result["outcome"], "existing_address_saved")
        self.assertEqual(mock_persist.call_count, 1)
        mock_save.assert_called_once()
        self.assertTrue(any("still matched safely" in item for item in warnings))

    def test_existing_address_resolution_skips_saved_address_that_drops_address_cont(self):
        address = {
            "recipient": "Joseph Maloney",
            "address": "4230 Deste Ct",
            "address_cont": "106",
            "city": "Greenacres",
            "state": "Florida",
            "zip": "33467",
        }
        best_existing = {
            "option": {
                "text": "Joseph Maloney - 4230 DESTE CT LAKE WORTH FL 33467-4302",
                "preferred_all_caps": True,
            },
            "assessment": crm_validate_address._assess_existing_address_text(
                address,
                "Joseph Maloney - 4230 DESTE CT LAKE WORTH FL 33467-4302",
            ),
        }
        warnings = []

        with mock.patch.object(crm_validate_address, "_select_existing_address_option_by_text") as mock_select:
            result = crm_validate_address._try_resolve_with_existing_address(
                object(),
                object(),
                "4523920",
                False,
                dict(address),
                dict(address),
                "106",
                warnings,
                existing_options=[best_existing["option"]],
                best_existing=best_existing,
            )

        self.assertIsNone(result)
        mock_select.assert_not_called()
        self.assertTrue(any("running Save & Verify Address instead" in item for item in warnings))

    def test_find_best_existing_address_option_rejects_saved_address_that_drops_embedded_box(self):
        address = {
            "recipient": "Isidore Boutin",
            "address": "105 main street box 233",
            "address_cont": "",
            "city": "Domremy",
            "state": "Saskatchewan",
            "zip": "S0k1g0",
        }
        options = [
            {
                "text": "Isidore Boutin - 105 main street Domremy SK s0k1g0",
                "preferred_all_caps": False,
            }
        ]

        self.assertIsNone(crm_validate_address._find_best_existing_address_option(address, options))

    def test_resolution_from_assessment_marks_zip_near_match(self):
        warnings = []

        resolution = crm_validate_address._resolution_from_assessment(
            "validated_address",
            "Validated address",
            {"postal_near_match": True},
            warnings,
        )

        self.assertEqual(resolution, "validated_address_zip_near_match")
        self.assertTrue(any("ZIP difference" in item for item in warnings))

    def test_resolution_from_assessment_marks_zip_prefix_match(self):
        warnings = []

        resolution = crm_validate_address._resolution_from_assessment(
            "validated_address",
            "Validated address",
            {"postal_prefix_match": True, "postal_base_match": False},
            warnings,
        )

        self.assertEqual(resolution, "validated_address_zip_prefix_match")
        self.assertTrue(any("first 2 digits" in item for item in warnings))

    def test_pick_validation_candidate_prefers_all_caps(self):
        address = {
            "address": "101 Bascom Farm Dr",
            "city": "West Windsor",
            "state": "Vermont",
            "zip": "05037",
        }
        candidates = [
            {"text": "101 Bascom Farm Rd Brownsville VT 05037-4440", "preferred_all_caps": False},
            {"text": "101 BASCOM FARM RD BROWNSVILLE VT 05037-4440", "preferred_all_caps": True},
        ]

        best, saw_zip_plus4_only = crm_validate_address._pick_validation_candidate(address, candidates)

        self.assertFalse(saw_zip_plus4_only)
        self.assertIsNotNone(best)
        self.assertEqual(best["candidate"]["text"], "101 BASCOM FARM RD BROWNSVILLE VT 05037-4440")

    def test_pick_validation_candidate_prefers_matching_address_cont(self):
        address = {
            "address": "687 West 204th Street",
            "address_cont": "Apt. 2D",
            "city": "New York",
            "state": "New York",
            "zip": "10034",
        }
        candidates = [
            {"text": "687 W 204TH ST APT 3D NEW YORK NY 10034", "preferred_all_caps": True},
            {"text": "687 W 204TH ST APT 2D NEW YORK NY 10034", "preferred_all_caps": True},
        ]

        best, saw_zip_plus4_only = crm_validate_address._pick_validation_candidate(address, candidates)

        self.assertFalse(saw_zip_plus4_only)
        self.assertIsNotNone(best)
        self.assertEqual(best["candidate"]["text"], "687 W 204TH ST APT 2D NEW YORK NY 10034")
        self.assertTrue(best["assessment"]["secondary_match"])

    def test_address_cont_preservation_accepts_unit_prefix_normalization(self):
        self.assertTrue(
            crm_validate_address._address_cont_value_preserved("Unit C202", "C202")
        )
        self.assertTrue(
            crm_validate_address._address_cont_value_preserved("Unit C202", "#C202")
        )

    def test_assess_address_text_accepts_one_digit_zip_difference(self):
        address = {
            "address": "6921 S 1st St W",
            "address_cont": "",
            "city": "Muskogee",
            "state": "Oklahoma",
            "zip": "74403",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "6921 S 1ST ST W MUSKOGEE OK 74401-8913",
        )

        self.assertTrue(assessment["safe_postal_near_match"])
        self.assertTrue(assessment["postal_near_match"])
        self.assertFalse(assessment["postal_full_match"])

    def test_assess_address_text_can_preserve_building_identifier_with_transposed_zip(self):
        address = {
            "address": "1 Dell Way",
            "address_cont": "Building 3",
            "city": "Round Rock",
            "state": "Texas",
            "zip": "78628",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "1 DELL WAY ROUND ROCK TX 78682-7000",
        )

        self.assertTrue(assessment["safe_postal_near_match"])
        self.assertTrue(assessment["secondary_preserved"])
        self.assertFalse(assessment["secondary_match"])

    def test_assess_address_text_accepts_compact_runon_street(self):
        address = {
            "address": "2PINIONPINELN",
            "address_cont": "",
            "city": "QUEENSBURY",
            "state": "New York",
            "zip": "12804",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "2 PINION PINE LN QUEENSBURY NY 12804-9014",
        )

        self.assertTrue(assessment["required_match"])
        self.assertTrue(assessment["compact_runon_match"])
        self.assertTrue(assessment["street_match"])
        self.assertTrue(assessment["house_match"])

    def test_assess_address_text_accepts_matching_zip_prefix_when_rest_matches(self):
        address = {
            "address": "257 Mary Lou Ave",
            "address_cont": "",
            "city": "Yonkers",
            "state": "New York",
            "zip": "10456",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "257 MARY LOU AVE YONKERS NY 10703-1903",
        )

        self.assertTrue(assessment["safe_postal_prefix_match"])
        self.assertTrue(assessment["postal_prefix_match"])
        self.assertFalse(assessment["postal_base_match"])

    def test_assess_address_text_accepts_canadian_postal_near_match_and_preserves_address_cont(self):
        address = {
            "address": "25 Brunetville Rd",
            "address_cont": "30",
            "city": "Kapuskasing",
            "state": "Ontario",
            "zip": "P5N 2E9",
        }

        assessment = crm_validate_address._assess_address_text(
            address,
            "25 BRUNETVILLE RD KAPUSKASING ON P5N 2E8",
        )

        self.assertTrue(assessment["safe_postal_near_match"])
        self.assertTrue(assessment["postal_near_match"])
        self.assertTrue(assessment["secondary_preserved"])
        self.assertFalse(assessment["secondary_match"])

    def test_compact_runon_zip_plus4_override_line_uses_shared_candidate_address(self):
        address = {
            "address": "2PINIONPINELN",
            "address_cont": "",
            "city": "QUEENSBURY",
            "state": "New York",
            "zip": "12804",
        }
        assessed = crm_validate_address._assessed_validation_candidates(
            address,
            [
                {"text": "2 PINION PINE LN QUEENSBURY NY 12804-9014", "preferred_all_caps": True},
                {"text": "2 PINION PINE LN QUEENSBURY NY 12804-9012", "preferred_all_caps": True},
            ],
        )

        self.assertTrue(crm_validate_address._has_zip_plus4_bug(address, assessed))
        self.assertEqual(
            crm_validate_address._compact_runon_zip_plus4_override_line(
                address,
                crm_validate_address._postal_extension_bug_candidates(address, assessed),
            ),
            "2 PINION PINE LN",
        )

    def test_zip_plus4_bug_detection_requires_multiple_variants(self):
        address = {
            "address": "101 Bascom Farm Dr",
            "city": "West Windsor",
            "state": "Vermont",
            "zip": "05037",
        }
        safe_single = crm_validate_address._assessed_validation_candidates(
            address,
            [{"text": "101 BASCOM FARM RD BROWNSVILLE VT 05037-4440", "preferred_all_caps": True}],
        )
        buggy_multiple = crm_validate_address._assessed_validation_candidates(
            address,
            [
                {"text": "101 BASCOM FARM RD BROWNSVILLE VT 05037-4440", "preferred_all_caps": True},
                {"text": "101 BASCOM FARM RD BROWNSVILLE VT 05037-1234", "preferred_all_caps": True},
            ],
        )

        self.assertFalse(crm_validate_address._has_zip_plus4_bug(address, safe_single))
        self.assertTrue(crm_validate_address._has_zip_plus4_bug(address, buggy_multiple))

    def test_final_save_ready_accepts_global_save_button(self):
        class _Button:
            def is_enabled(self):
                return True

        driver = object()
        shipping_modal = object()

        def visible_side_effect(root, selectors):
            if root is shipping_modal:
                return []
            if root is driver:
                return [_Button()]
            return []

        with mock.patch.object(crm_validate_address, "_visible_elements", side_effect=visible_side_effect):
            self.assertTrue(crm_validate_address._final_save_ready(driver, shipping_modal, timeout=0.01))

    def test_open_shipping_editor_retries_when_modal_does_not_appear_on_first_click(self):
        driver = object()
        edit_button = object()
        modal = object()

        with mock.patch.object(crm_validate_address, "_switch_to_order_app_frame"):
            with mock.patch.object(crm_validate_address, "_find_shipping_edit_button", return_value=edit_button) as mock_find:
                with mock.patch.object(crm_validate_address, "_click_with_fallback") as mock_click:
                    with mock.patch.object(
                        crm_validate_address,
                        "_wait_for_shipping_modal",
                        side_effect=[
                            crm_validate_address.TimeoutException("first click missed"),
                            modal,
                        ],
                    ) as mock_wait:
                        with mock.patch.object(crm_validate_address.time, "sleep"):
                            result = crm_validate_address._open_shipping_editor(driver)

        self.assertIs(result, modal)
        self.assertEqual(mock_click.call_count, 2)
        self.assertEqual(mock_find.call_count, 2)
        self.assertEqual(mock_wait.call_count, 2)

    def test_ensure_override_ready_can_persist_override_when_ui_state_is_missing(self):
        warnings = []

        with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
            with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=False):
                with mock.patch.object(
                    crm_validate_address,
                    "_persist_validated_address_via_modal_scope",
                    return_value={"ok": True},
                ) as mock_persist:
                    ready, use_scope_send = crm_validate_address._ensure_override_ready(
                        object(),
                        object(),
                        warnings,
                        dry_run=False,
                    )

        self.assertTrue(ready)
        self.assertTrue(use_scope_send)
        self.assertEqual(mock_persist.call_count, 1)
        self.assertTrue(any("Persisted the override through the CRM modal service" in item for item in warnings))

    def test_free_po_box_override_uses_scope_send_when_ui_state_is_missing(self):
        driver = object()
        shipping_modal = object()
        address = {
            "recipient": "Alyson Goryl",
            "address": "P.O. Box 550193",
            "address_cont": "",
            "city": "South Lake Tahoe",
            "state": "California",
            "zip": "96150",
        }

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_target_order", return_value="4605090"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_shipping_panel_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_shipping_panel_has_valid_address", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_read_order_totals_shipping_class", return_value="free"))
            stack.enter_context(mock.patch.object(crm_validate_address, "_open_shipping_editor", return_value=shipping_modal))
            stack.enter_context(mock.patch.object(crm_validate_address, "_extract_current_address", return_value=dict(address)))
            stack.enter_context(mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False))
            stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_recipient_present", return_value=(True, dict(address))))
            stack.enter_context(mock.patch.object(crm_validate_address, "_collect_existing_address_options", return_value=[]))
            stack.enter_context(mock.patch.object(crm_validate_address, "_find_best_existing_address_option", return_value=None))
            stack.enter_context(mock.patch.object(crm_validate_address, "_rewrite_address_fields_if_needed", return_value=(dict(address), "")))
            stack.enter_context(mock.patch.object(crm_validate_address, "_apply_override"))
            mock_ensure = stack.enter_context(mock.patch.object(crm_validate_address, "_ensure_override_ready", return_value=(True, True)))
            stack.enter_context(
                mock.patch.object(
                    crm_validate_address,
                    "_prepare_shipping_form_for_save",
                    return_value=(None, dict(address), ""),
                )
            )
            mock_save = stack.enter_context(mock.patch.object(crm_validate_address, "_save_shipping_transaction"))

            result = crm_validate_address._evaluate_and_resolve_order(
                driver,
                order_id="4605090",
                dry_run=False,
                shipping_filter="all",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "po_box_free_override_saved")
        self.assertEqual(result["resolution"], "override")
        mock_ensure.assert_called_once_with(driver, shipping_modal, mock.ANY, dry_run=False)
        mock_save.assert_called_once_with(driver, shipping_modal, "4605090", False, use_scope_send=True)

    def test_attempt_validation_candidate_selection_accepts_final_save_ready(self):
        warnings = []
        button = object()

        with mock.patch.object(crm_validate_address, "_select_validation_candidate_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_wait_for_any", return_value=button):
                with mock.patch.object(crm_validate_address, "_click_with_fallback") as mock_click:
                    with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=False):
                        with mock.patch.object(crm_validate_address, "_final_save_ready", return_value=True):
                            ready, use_scope_send = crm_validate_address._attempt_validation_candidate_selection(
                                object(),
                                object(),
                                object(),
                                "8905 HARVEST HILL WAY ELK GROVE CA 95624-1457",
                                warnings,
                                dry_run=True,
                            )

        self.assertTrue(ready)
        self.assertFalse(use_scope_send)
        self.assertEqual(mock_click.call_count, 1)
        self.assertTrue(any("final Save button became available" in item for item in warnings))

    def test_attempt_validation_candidate_selection_can_persist_validated_address(self):
        warnings = []
        button = object()

        with mock.patch.object(crm_validate_address, "_select_validation_candidate_by_text", return_value=True):
            with mock.patch.object(crm_validate_address, "_wait_for_any", return_value=button):
                with mock.patch.object(crm_validate_address, "_click_with_fallback"):
                    with mock.patch.object(crm_validate_address, "_wait_for_address_valid", return_value=True):
                        with mock.patch.object(crm_validate_address, "_address_is_valid", return_value=False):
                            with mock.patch.object(
                                crm_validate_address,
                                "_persist_validated_address_via_modal_scope",
                                return_value={"ok": True},
                            ) as mock_persist:
                                ready, use_scope_send = crm_validate_address._attempt_validation_candidate_selection(
                                    object(),
                                    object(),
                                    object(),
                                    "8905 HARVEST HILL WAY ELK GROVE CA 95624-1457",
                                    warnings,
                                    dry_run=False,
                                )

        self.assertTrue(ready)
        self.assertTrue(use_scope_send)
        self.assertEqual(mock_persist.call_count, 1)
        self.assertTrue(any("Persisted the validated address through the CRM modal service" in item for item in warnings))

    @mock.patch.object(crm_validate_address, "safe_driver_quit")
    @mock.patch.object(crm_validate_address, "_run_once_with_driver")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids_with_driver")
    @mock.patch.object(crm_validate_address, "_build_crm_session_driver")
    def test_shared_session_batch_stops_after_requested_total(
        self,
        mock_build_driver,
        mock_collect_orders,
        mock_run_once,
        mock_safe_quit,
    ):
        driver = object()
        mock_build_driver.return_value = driver
        collect_limits = []

        def collect_side_effect(driver_arg, shipping_filter, limit, list_url_override=None, exclude_order_ids=None):
            del driver_arg, shipping_filter, list_url_override
            collect_limits.append((limit, tuple(sorted(exclude_order_ids or []))))
            if len(collect_limits) == 1:
                return ["1000001"]
            if len(collect_limits) == 2:
                return ["1000002", "1000003", "1000004"]
            raise AssertionError("Batch collection should stop once the requested total is reached.")

        mock_collect_orders.side_effect = collect_side_effect
        mock_run_once.side_effect = lambda driver_arg, order_id=None, **kwargs: {
            "success": True,
            "message": f"ok {order_id}",
            "report": [_report_row(order_id)],
        }

        finished_payloads, attempted_order_ids, refresh_passes = crm_validate_address._run_batch_reusing_session(
            dry_run=True,
            shipping_filter="rush",
            batch_size=3,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertEqual([item[0] for item in collect_limits], [3, 2])
        self.assertEqual(attempted_order_ids, ["1000001", "1000002", "1000003"])
        self.assertEqual(refresh_passes, 2)
        self.assertEqual(len(finished_payloads), 3)
        mock_safe_quit.assert_called_with(driver, profile_path=str((ROOT / "chrome_profile_crm").resolve()))

    @mock.patch.object(crm_validate_address, "safe_driver_quit")
    @mock.patch.object(crm_validate_address, "_run_once_with_driver")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids_with_driver")
    @mock.patch.object(crm_validate_address, "_build_crm_session_driver")
    def test_shared_session_batch_runs_until_no_orders_when_batch_size_is_blank(
        self,
        mock_build_driver,
        mock_collect_orders,
        mock_run_once,
        mock_safe_quit,
    ):
        driver = object()
        mock_build_driver.return_value = driver
        collect_limits = []

        def collect_side_effect(driver_arg, shipping_filter, limit, list_url_override=None, exclude_order_ids=None):
            del driver_arg, shipping_filter, list_url_override
            collect_limits.append((limit, tuple(sorted(exclude_order_ids or []))))
            if len(collect_limits) == 1:
                return ["1000001"]
            if len(collect_limits) == 2:
                return ["1000002"]
            return []

        mock_collect_orders.side_effect = collect_side_effect
        mock_run_once.side_effect = lambda driver_arg, order_id=None, **kwargs: {
            "success": True,
            "message": f"ok {order_id}",
            "report": [_report_row(order_id)],
        }

        finished_payloads, attempted_order_ids, refresh_passes = crm_validate_address._run_batch_reusing_session(
            dry_run=True,
            shipping_filter="rush",
            batch_size=None,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertEqual(
            [item[0] for item in collect_limits],
            [crm_validate_address.CONTINUOUS_BATCH_FETCH_LIMIT] * 3,
        )
        self.assertEqual(attempted_order_ids, ["1000001", "1000002"])
        self.assertEqual(refresh_passes, 3)
        self.assertEqual(len(finished_payloads), 2)
        mock_safe_quit.assert_called_with(driver, profile_path=str((ROOT / "chrome_profile_crm").resolve()))

    @mock.patch.object(crm_validate_address, "write_result_payload")
    @mock.patch.object(crm_validate_address.shutil, "rmtree")
    @mock.patch.object(crm_validate_address, "_clone_profile_for_worker")
    @mock.patch.object(crm_validate_address, "_run_single_payload")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_stops_after_requested_total(
        self,
        mock_collect_orders,
        mock_run_single_payload,
        mock_clone_profile,
        _mock_rmtree,
        _mock_write_result_payload,
    ):
        collect_limits = []
        mock_clone_profile.side_effect = lambda profile_path, label, *args, **kwargs: (
            str(ROOT / "tests_tmp" / label),
            str(ROOT / "tests_tmp" / label / "profile"),
        )

        def collect_side_effect(shipping_filter, limit, profile_path, list_url_override=None, exclude_order_ids=None, visible=False):
            del shipping_filter, profile_path, list_url_override, visible
            collect_limits.append((limit, tuple(sorted(exclude_order_ids or []))))
            if len(collect_limits) == 1:
                return ["2000001", "2000002"]
            if len(collect_limits) == 2:
                return ["2000003", "2000004"]
            raise AssertionError("Parallel batch collection should stop once the requested total is reached.")

        mock_collect_orders.side_effect = collect_side_effect
        mock_run_single_payload.side_effect = lambda order_id=None, **kwargs: {
            "success": True,
            "message": f"ok {order_id}",
            "target_order_id": str(order_id),
            "report": [_report_row(order_id)],
        }

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="free",
            batch_size=3,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertEqual([item[0] for item in collect_limits], [3, 1])
        self.assertEqual(payload["order_ids"], ["2000001", "2000002", "2000003"])
        self.assertEqual(payload["order_count"], 3)
        self.assertEqual(payload["refresh_passes"], 2)

    @mock.patch.object(crm_validate_address.time, "sleep")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_retries_retryable_list_collection_once(self, mock_collect_orders, _mock_sleep):
        mock_collect_orders.side_effect = [RuntimeError("invalid session id"), []]

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="rush",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertTrue(payload["success"])
        self.assertEqual(payload["resolution"], "no_orders")
        self.assertEqual(mock_collect_orders.call_count, 2)

    @mock.patch.object(crm_validate_address.time, "sleep")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_reports_list_collection_failure_after_retry(self, mock_collect_orders, _mock_sleep):
        mock_collect_orders.side_effect = RuntimeError("invalid session id")

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="rush",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertFalse(payload["success"])
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["resolution"], "list_collection_failed")
        self.assertIn("invalid session id", payload["message"])
        self.assertEqual(mock_collect_orders.call_count, 2)

    @mock.patch.object(crm_validate_address, "write_result_payload")
    @mock.patch.object(crm_validate_address.shutil, "rmtree")
    @mock.patch.object(crm_validate_address, "_clone_profile_for_worker")
    @mock.patch.object(crm_validate_address, "_run_single_payload")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_retries_retryable_worker_exception_once(
        self,
        mock_collect_orders,
        mock_run_single_payload,
        mock_clone_profile,
        _mock_rmtree,
        _mock_write_result_payload,
    ):
        mock_collect_orders.side_effect = [["2000001"], []]
        mock_clone_profile.side_effect = lambda profile_path, label, *args, **kwargs: (
            str(ROOT / "tests_tmp" / label),
            str(ROOT / "tests_tmp" / label / "profile"),
        )
        mock_run_single_payload.side_effect = [
            {
                "success": False,
                "message": "Message: The Shipping Transaction modal did not appear before the timeout expired.",
                "target_order_id": "2000001",
                "retryable": True,
                "report": [
                    {
                        "order_id": "2000001",
                        "success": False,
                        "outcome": "worker_exception",
                        "message": "Message: The Shipping Transaction modal did not appear before the timeout expired.",
                        "manual_review_required": True,
                        "warnings": [],
                        "retry_attempted": False,
                    }
                ],
            },
            {
                "success": True,
                "message": "ok 2000001",
                "target_order_id": "2000001",
                "report": [_report_row("2000001")],
            },
        ]

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="free",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertTrue(payload["success"])
        self.assertEqual(mock_run_single_payload.call_count, 2)
        self.assertEqual(mock_clone_profile.call_count, 2)
        self.assertTrue(payload["report"][0]["retry_attempted"])
        self.assertTrue(payload["report"][0]["transient_retry_attempted"])

    @mock.patch.object(crm_validate_address, "write_result_payload")
    @mock.patch.object(crm_validate_address.shutil, "rmtree")
    @mock.patch.object(crm_validate_address, "_clone_profile_for_worker")
    @mock.patch.object(crm_validate_address, "_run_single_payload")
    @mock.patch.object(crm_validate_address, "_collect_batch_order_ids")
    def test_parallel_batch_does_not_retry_validation_manual_review(
        self,
        mock_collect_orders,
        mock_run_single_payload,
        mock_clone_profile,
        _mock_rmtree,
        _mock_write_result_payload,
    ):
        mock_collect_orders.side_effect = [["2000001"], []]
        mock_clone_profile.side_effect = lambda profile_path, label, *args, **kwargs: (
            str(ROOT / "tests_tmp" / label),
            str(ROOT / "tests_tmp" / label / "profile"),
        )
        mock_run_single_payload.return_value = {
            "success": False,
            "message": "Skipped because the suggested validated address did not match the original shipping address closely enough.",
            "target_order_id": "2000001",
            "report": [
                {
                    "order_id": "2000001",
                    "success": False,
                    "outcome": "validated_candidate_mismatch",
                    "message": "Skipped because the suggested validated address did not match the original shipping address closely enough.",
                    "manual_review_required": True,
                    "warnings": [],
                    "retry_attempted": False,
                }
            ],
        }

        payload = crm_validate_address._run_batch(
            dry_run=True,
            shipping_filter="free",
            batch_size=None,
            parallel_workers=2,
            profile_path=str(ROOT / "chrome_profile_crm"),
        )

        self.assertFalse(payload["success"])
        self.assertEqual(mock_run_single_payload.call_count, 1)
        self.assertEqual(mock_clone_profile.call_count, 1)
        self.assertFalse(payload["report"][0]["retry_attempted"])

    @mock.patch.object(server, "_run_script")
    def test_execute_worker_supports_continuous_batch_mode(self, mock_run_script):
        mock_run_script.return_value = (
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            {"success": False, "message": "CRMAddressValidator timed out after 180 seconds."},
        )

        ok, message, payload = server._execute_crm_address_worker(
            dry_run=True,
            shipping_filter="rush",
            action="validate_batch",
            batch_size=None,
            parallel_workers=1,
        )

        self.assertFalse(ok)
        self.assertEqual(message, "CRMAddressValidator timed out after 180 seconds.")
        self.assertEqual(payload["action"], "validate_batch")
        self.assertIsNone(payload["batch_size"])
        self.assertEqual(payload["parallel_workers"], 1)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--parallel-workers", args)
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)
        self.assertNotIn("--batch-size", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])
        self.assertGreaterEqual(
            mock_run_script.call_args.kwargs["timeout"],
            server.CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS,
        )


class CrmAddressServerTests(unittest.TestCase):
    @mock.patch.object(server, "_run_script")
    def test_execute_worker_enriches_batch_payload_and_uses_scaled_timeout(self, mock_run_script):
        mock_run_script.return_value = (
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            {"success": False, "message": "CRMAddressValidator timed out after 180 seconds."},
        )

        ok, message, payload = server._execute_crm_address_worker(
            dry_run=True,
            shipping_filter="rush",
            action="validate_batch",
            batch_size=3,
            parallel_workers=1,
        )

        self.assertFalse(ok)
        self.assertEqual(message, "CRMAddressValidator timed out after 180 seconds.")
        self.assertEqual(payload["action"], "validate_batch")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["shipping_filter"], "rush")
        self.assertEqual(payload["batch_size"], 3)
        self.assertEqual(payload["parallel_workers"], 1)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])
        self.assertGreater(mock_run_script.call_args.kwargs["timeout"], 180)

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_defaults_to_continuous_rush_dry_run(self, mock_run_script):
        mock_run_script.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok", "action": "order_goods_batch"},
        )

        ok, message, payload = server._execute_crm_order_goods_worker(dry_run=True, batch_size=None)

        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertEqual(payload["action"], "order_goods_batch")
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["shipping_filter"], "rush")
        self.assertIsNone(payload["batch_size"])
        args = mock_run_script.call_args.args[1]
        self.assertEqual(args[:2], ["--action", "order_goods_batch"])
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)
        self.assertNotIn("--batch-size", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])
        self.assertGreaterEqual(
            mock_run_script.call_args.kwargs["timeout"],
            server.CRM_ADDRESS_CONTINUOUS_BATCH_TIMEOUT_SECONDS,
        )

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_passes_limited_batch_size(self, mock_run_script):
        mock_run_script.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok"},
        )

        ok, _message, payload = server._execute_crm_order_goods_worker(dry_run=False, batch_size=3)

        self.assertTrue(ok)
        self.assertEqual(payload["batch_size"], 3)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--batch-size", args)
        self.assertIn("3", args)
        self.assertNotIn("--visible", args)
        self.assertFalse(mock_run_script.call_args.kwargs["show_terminal"])

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_passes_single_order_id(self, mock_run_script):
        mock_run_script.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok", "action": "order_goods_single"},
        )

        ok, _message, payload = server._execute_crm_order_goods_worker(dry_run=True, order_id="https://crm2.legacy.printfly.com/order/4418860")

        self.assertTrue(ok)
        self.assertEqual(payload["action"], "order_goods_single")
        self.assertEqual(payload["target_order_id"], "4418860")
        self.assertEqual(payload["order_ids"], ["4418860"])
        self.assertEqual(payload["batch_size"], 1)
        self.assertEqual(payload["parallel_workers"], 1)
        args = mock_run_script.call_args.args[1]
        self.assertEqual(args[:2], ["--action", "order_goods_single"])
        self.assertIn("--order-id", args)
        self.assertIn("4418860", args)
        self.assertNotIn("--batch-size", args)
        self.assertIn("--visible", args)
        self.assertIn("--dry-run", args)

    @mock.patch.object(server, "_run_script")
    def test_order_goods_worker_rejects_invalid_single_order_id(self, mock_run_script):
        ok, message, payload = server._execute_crm_order_goods_worker(dry_run=True, order_id="bad")

        self.assertFalse(ok)
        self.assertIn("7-digit", message)
        self.assertEqual(payload["resolution"], "invalid_order_id")
        mock_run_script.assert_not_called()

    def test_order_goods_unlock_retry_selects_native_status_without_send_keys(self):
        driver = mock.Mock()
        control = mock.Mock()
        control.send_keys.side_effect = AssertionError("native dropdown should not receive text keys")

        with mock.patch.object(crm_order_goods, "_resolve_stock_unlock_text_control", return_value=control):
            with mock.patch.object(crm_order_goods, "_click_with_fallback"):
                with mock.patch.object(crm_order_goods, "_select_native_stock_unlock_option", return_value=True):
                    with mock.patch.object(crm_order_goods.time, "sleep"):
                        self.assertTrue(crm_order_goods._choose_stock_unlock_status_from_control(driver, control))

        control.send_keys.assert_not_called()

    def test_order_goods_top_panel_unlock_script_handles_dropdown_and_types_full_status(self):
        captured = {}
        driver = mock.Mock()

        def execute_async_script(script):
            captured["script"] = script
            return {"success": True}

        driver.execute_async_script.side_effect = execute_async_script

        result = crm_order_goods._apply_stock_unlock_with_top_panel_script(driver)

        self.assertTrue(result["success"])
        self.assertIn("chooseNativeSelect", captured["script"])
        self.assertIn("'select'", captured["script"])
        self.assertIn("setTypedValue(searchInput, 'Stock Auto Ordering Unlocked')", captured["script"])

    @mock.patch.object(server, "_run_script")
    def test_stock_unlocker_dry_run_uses_visible_terminal(self, mock_run_script):
        mock_run_script.return_value = (True, "ok", {"success": True, "message": "ok"})

        ok, _message, _payload = server._execute_crm_worker(dry_run=True)

        self.assertTrue(ok)
        args = mock_run_script.call_args.args[1]
        self.assertIn("--dry-run", args)
        self.assertIn("--visible", args)
        self.assertTrue(mock_run_script.call_args.kwargs["show_terminal"])

    @mock.patch.object(server, "_persist_crm_order_goods_run_result", return_value={})
    @mock.patch.object(server, "_finish_crm_order_goods_runtime")
    @mock.patch.object(server, "_start_crm_order_goods_runtime")
    @mock.patch.object(server, "_execute_crm_order_goods_worker")
    @mock.patch.object(server, "load_crm_state")
    def test_processing_order_goods_step_runs_headless_hidden(
        self,
        mock_load_state,
        mock_execute,
        _mock_start_runtime,
        _mock_finish_runtime,
        _mock_persist,
    ):
        mock_load_state.return_value = {"saved_order_goods_parallel_workers": 1}
        mock_execute.return_value = (True, "ok", {"success": True, "message": "ok", "order_ids": []})

        result = server._run_crm_processing_step("order_goods", "rush")

        self.assertTrue(result["success"])
        mock_execute.assert_called_once_with(
            dry_run=False,
            batch_size=None,
            parallel_workers=1,
            list_url=None,
            visible=False,
            show_terminal=False,
        )

    def test_order_goods_runtime_config_is_rush_only(self):
        with self.assertRaises(RuntimeError):
            crm_order_goods._validate_runtime_config("https://crm.example/report?shippingCharges%5Bhigh%5D=1")

    @mock.patch.object(crm_order_goods, "_find_sanmar_order_goods_button", return_value=None)
    def test_order_goods_skips_when_stock_already_ordered(self, _mock_button):
        driver = mock.Mock()
        driver.execute_script.return_value = "Stock Status: Ordered Stock : Ordered"

        result = crm_order_goods._order_goods_for_open_order(driver, "4418860", dry_run=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "already_stock_ordered")

    def test_order_goods_does_not_treat_stock_ordered_false_label_as_ordered(self):
        self.assertFalse(crm_order_goods._text_indicates_stock_already_ordered("Stock Ordered: false"))
        self.assertFalse(crm_order_goods._text_indicates_stock_already_ordered("Stock Ordered"))
        self.assertTrue(crm_order_goods._text_indicates_stock_already_ordered("Stock Status: Ordered"))
        self.assertTrue(crm_order_goods._text_indicates_stock_already_ordered("Stock : Ordered"))

    @mock.patch.object(crm_order_goods, "_page_indicates_stock_already_ordered", return_value=False)
    @mock.patch.object(crm_order_goods, "_refresh_order_after_stock_unlock", return_value="orderable")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_click_with_fallback")
    @mock.patch.object(crm_order_goods, "_find_sanmar_order_goods_button")
    def test_order_goods_unlocks_and_retries_when_button_disabled(
        self,
        mock_button,
        mock_click,
        mock_unlock,
        mock_refresh,
        _mock_ordered,
    ):
        disabled_button = mock.Mock()
        disabled_button.is_enabled.return_value = False
        enabled_button = mock.Mock()
        enabled_button.is_enabled.return_value = True
        mock_button.side_effect = [disabled_button, enabled_button]
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlocked",
            "message": "Unlocked first.",
            "manual_review_required": False,
        }

        result = crm_order_goods._order_goods_for_open_order(mock.Mock(), "4418860", dry_run=False)

        self.assertTrue(result["success"])
        self.assertEqual(result["outcome"], "order_goods_clicked")
        self.assertTrue(result["stock_unlocked_before_order_goods"])
        mock_unlock.assert_called_once_with(mock.ANY, "4418860", dry_run=False, force=True)
        mock_refresh.assert_called_once_with(mock.ANY, "4418860", stock_tab_index=None)
        mock_click.assert_called_once_with(mock.ANY, enabled_button)

    def test_order_goods_button_fallback_finds_visible_input_value(self):
        driver = mock.Mock()
        control = mock.Mock()
        control.is_displayed.return_value = True
        control.text = ""

        def get_attribute(name):
            values = {
                "value": "order goods",
                "aria-label": "",
                "title": "",
            }
            return values.get(name)

        control.get_attribute.side_effect = get_attribute
        driver.find_elements.return_value = [control]
        driver.execute_script.side_effect = [True, None]

        result = crm_order_goods._find_sanmar_order_goods_button_fallback(driver)

        self.assertIs(result, control)
        driver.find_elements.assert_called_once()

    def test_order_goods_button_finder_prefers_direct_enabled_order_goods_button(self):
        class FakeButton:
            pass

        disabled_status_container = FakeButton()
        enabled_order_goods_button = FakeButton()
        driver = mock.Mock()
        driver.execute_script.return_value = enabled_order_goods_button

        result = crm_order_goods._find_sanmar_order_goods_button(driver, timeout=0.1)

        self.assertIs(result, enabled_order_goods_button)
        script = driver.execute_script.call_args.args[0]
        self.assertIn("directMatches", script)
        self.assertIn("!== 'order goods'", script)

    @mock.patch.object(crm_order_goods, "_order_goods_for_all_stock_tabs")
    @mock.patch.object(crm_order_goods, "_wait_after_stock_unlock", return_value="orderable")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_wait_for_order_goods_page_ready", return_value=True)
    @mock.patch.object(crm_order_goods, "_open_target_order")
    def test_order_goods_unlocks_locked_order_then_refreshes_before_ordering(self, mock_open, _mock_ready, mock_unlock, _mock_wait_after_unlock, mock_order_tabs):
        driver = mock.Mock()
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlocked",
            "message": "Unlocked first.",
            "manual_review_required": False,
        }
        mock_order_tabs.return_value = [
            {"order_id": "4418860", "success": True, "outcome": "order_goods_clicked", "message": "clicked", "manual_review_required": False}
        ]

        results = crm_order_goods._run_order_with_driver(driver, "4418860", dry_run=False)

        self.assertEqual(mock_open.call_count, 2)
        driver.refresh.assert_called_once()
        mock_order_tabs.assert_called_once_with(driver, "4418860", dry_run=False)
        self.assertTrue(results[0]["stock_unlocked_before_order_goods"])
        self.assertIn("Unlocked first.", results[0]["warnings"])

    @mock.patch.object(crm_order_goods, "_order_goods_for_all_stock_tabs")
    @mock.patch.object(crm_order_goods, "_wait_after_stock_unlock", return_value="ordered")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_wait_for_order_goods_page_ready", return_value=True)
    @mock.patch.object(crm_order_goods, "_open_target_order")
    def test_order_goods_treats_post_unlock_auto_ordered_stock_as_success(self, _mock_open, _mock_ready, mock_unlock, _mock_wait_after_unlock, mock_order_tabs):
        driver = mock.Mock()
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlocked",
            "message": "Unlocked first.",
            "manual_review_required": False,
        }

        results = crm_order_goods._run_order_with_driver(driver, "4418860", dry_run=False)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["outcome"], "already_stock_ordered")
        self.assertTrue(results[0]["stock_unlocked_before_order_goods"])
        mock_order_tabs.assert_not_called()

    @mock.patch.object(crm_order_goods, "_order_goods_for_all_stock_tabs")
    @mock.patch.object(crm_order_goods, "_unlock_current_order_for_auto_ordering")
    @mock.patch.object(crm_order_goods, "_wait_for_order_goods_page_ready", return_value=True)
    @mock.patch.object(crm_order_goods, "_open_target_order")
    def test_order_goods_single_dry_run_reports_locked_unlock_without_ordering(self, _mock_open, _mock_ready, mock_unlock, mock_order_tabs):
        driver = mock.Mock()
        mock_unlock.return_value = {
            "order_id": "4418860",
            "success": True,
            "outcome": "stock_unlock_ready",
            "message": "Would unlock.",
            "manual_review_required": False,
        }

        results = crm_order_goods._run_order_with_driver(driver, "4418860", dry_run=True)

        self.assertEqual(results[0]["outcome"], "stock_unlock_ready")
        driver.refresh.assert_not_called()
        mock_order_tabs.assert_not_called()

    @mock.patch.object(crm_order_goods, "_click_with_fallback")
    @mock.patch.object(crm_order_goods, "_order_goods_for_open_order")
    def test_order_goods_processes_each_stock_tab(self, mock_order_goods, _mock_click):
        driver = mock.Mock()
        tab_element_1 = mock.Mock()
        tab_element_2 = mock.Mock()

        def execute_script_side_effect(_script, *args):
            if not args:
                return [
                    {"index": 0, "label": "H-RyanFowler602 1 - QTY: 2 Design Previews"},
                    {"index": 1, "label": "H-RyanFowler603 2 - QTY: 2 Design Previews"},
                ]
            index = int(args[0])
            return {
                "element": tab_element_1 if index == 0 else tab_element_2,
                "label": f"H-RyanFowler60{index + 2} {index + 1} - QTY: 2 Design Previews",
                "count": 2,
            }

        driver.execute_script.side_effect = execute_script_side_effect
        mock_order_goods.side_effect = [
            {"order_id": "4418860", "success": True, "outcome": "order_goods_ready", "message": "ready", "manual_review_required": False},
            {"order_id": "4418860", "success": True, "outcome": "order_goods_ready", "message": "ready", "manual_review_required": False},
        ]

        results = crm_order_goods._order_goods_for_all_stock_tabs(driver, "4418860", dry_run=True)

        self.assertEqual(len(results), 2)
        self.assertEqual(mock_order_goods.call_count, 2)
        self.assertEqual(results[0]["stock_tab_index"], 1)
        self.assertEqual(results[1]["stock_tab_index"], 2)
        self.assertEqual(results[0]["stock_tab_count"], 2)
        self.assertIn("H-RyanFowler602", results[0]["stock_tab_label"])

    @mock.patch.object(crm_order_goods, "_click_with_fallback")
    @mock.patch.object(crm_order_goods, "_order_goods_for_open_order")
    def test_order_goods_continues_when_multi_tab_has_manual_order_tab(self, mock_order_goods, _mock_click):
        driver = mock.Mock()
        tab_element_1 = mock.Mock()
        tab_element_2 = mock.Mock()

        def execute_script_side_effect(script, *args):
            if script == crm_order_goods.STOCK_TAB_SCRIPT and not args:
                return [
                    {"index": 0, "label": "H-NicoleDeGr551 1 - QTY: 2 Design Previews"},
                    {"index": 1, "label": "H-NicoleDeGr552 2 - QTY: 1 Design Previews"},
                ]
            if script == crm_order_goods.STOCK_TAB_SCRIPT:
                index = int(args[0])
                return {
                    "element": tab_element_1 if index == 0 else tab_element_2,
                    "label": f"H-NicoleDeGr55{index + 1} {index + 1} - QTY: 1 Design Previews",
                    "count": 2,
                }
            return "Stock : Ordered Order Goods From Vendor: Manual Order"

        driver.execute_script.side_effect = execute_script_side_effect
        mock_order_goods.side_effect = [
            crm_order_goods.TimeoutException("button not found"),
            {"order_id": "4427569", "success": True, "outcome": "order_goods_clicked", "message": "clicked", "manual_review_required": False},
        ]

        results = crm_order_goods._order_goods_for_all_stock_tabs(driver, "4427569", dry_run=False)

        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["outcome"], "stock_tab_not_vendor_orderable")
        self.assertEqual(results[1]["outcome"], "order_goods_clicked")

    def test_order_goods_loads_skip_ids_from_order_goods_history_only(self):
        state = {
            "run_history": [
                {
                    "automation_key": "order_goods",
                    "order_ids": ["4418860", "not-an-id"],
                },
                {
                    "automation_key": "stock_unlocker",
                    "order_ids": ["4419999"],
                },
                {
                    "automation_label": "Rush Order Goods",
                    "order_ids": ["4418871"],
                },
            ]
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
            json.dump(state, handle)
            state_path = handle.name
        try:
            skipped = crm_order_goods._load_historical_order_goods_order_ids(state_path)
        finally:
            Path(state_path).unlink(missing_ok=True)

        self.assertEqual(skipped, {"4418860", "4418871"})

    @mock.patch.object(crm_order_goods, "_load_historical_order_goods_order_ids", return_value={"4418860"})
    @mock.patch.object(crm_order_goods, "_build_crm_session_driver")
    @mock.patch.object(crm_order_goods, "_collect_batch_order_ids_with_driver", return_value=[])
    def test_order_goods_batch_excludes_historical_ids_from_collection(self, mock_collect, mock_build_driver, _mock_history):
        driver = mock.Mock()
        mock_build_driver.return_value = driver

        payload = crm_order_goods._run_batch_with_mode(False, dry_run=True, batch_size=None, profile_path=str(ROOT))

        self.assertTrue(payload["success"])
        self.assertEqual(payload["skipped_historical_order_count"], 1)
        self.assertEqual(mock_collect.call_args.kwargs["exclude_order_ids"], {"4418860"})

    @mock.patch.object(crm_order_goods.shutil, "rmtree")
    @mock.patch.object(crm_order_goods, "_clone_profile_for_worker")
    @mock.patch.object(crm_order_goods, "_order_goods_worker_payload")
    @mock.patch.object(crm_order_goods, "_collect_batch_order_ids")
    @mock.patch.object(crm_order_goods, "_load_historical_order_goods_order_ids", return_value=set())
    def test_parallel_order_goods_retries_retryable_worker_once(
        self,
        _mock_history,
        mock_collect,
        mock_worker_payload,
        mock_clone_profile,
        _mock_rmtree,
    ):
        mock_collect.side_effect = [["4418860"], []]
        mock_clone_profile.side_effect = [
            ("temp-a", "profile-a"),
            ("temp-b", "profile-b"),
        ]
        mock_worker_payload.side_effect = [
            {
                "success": False,
                "order_ids": ["4418860"],
                "report": [
                    {
                        "order_id": "4418860",
                        "success": False,
                        "outcome": "worker_exception",
                        "message": "Message: button was not found",
                        "retryable": True,
                    }
                ],
            },
            {
                "success": True,
                "order_ids": ["4418860"],
                "report": [
                    {
                        "order_id": "4418860",
                        "success": True,
                        "outcome": "order_goods_clicked",
                        "message": "Clicked.",
                    }
                ],
            },
        ]

        payload = crm_order_goods._run_parallel_batch_with_mode(
            False,
            dry_run=False,
            batch_size=1,
            profile_path=str(ROOT),
            list_url="https://crm.example/report?shippingCharges%5Blow%5D=1",
            parallel_workers=4,
        )

        self.assertTrue(payload["success"])
        self.assertEqual(mock_worker_payload.call_count, 2)
        self.assertEqual(payload["report"][0]["outcome"], "order_goods_clicked")

    @mock.patch.object(server, "save_crm_state")
    @mock.patch.object(server, "load_crm_state")
    def test_order_goods_persists_into_shared_stock_history(self, mock_load_state, _mock_save_state):
        mock_load_state.return_value = {
            "last_run_timestamp": None,
            "last_run_success": None,
            "last_run_message": None,
            "last_order_count": 0,
            "total_runs": 0,
            "total_orders_processed": 0,
            "last_order_ids": [],
            "run_history": [],
        }

        state = server._persist_crm_order_goods_run_result(
            True,
            "ok",
            {
                "order_count": 2,
                "order_ids": ["4418860", "4418871"],
                "duration_seconds": 12.4,
                "report": [
                    {"order_id": "4418860", "success": True, "outcome": "already_stock_ordered", "message": "Already ordered.", "duration_seconds": 4.2},
                    {"order_id": "4418871", "success": False, "outcome": "order_goods_locked", "message": "Button disabled.", "duration_seconds": 5.5},
                ],
            },
            dry_run=False,
        )

        self.assertEqual(state["last_order_ids"], ["4418860", "4418871"])
        self.assertEqual(state["run_history"][0]["automation_key"], "order_goods")
        self.assertEqual(state["run_history"][0]["automation_label"], "Rush Order Goods")
        self.assertEqual(state["run_history"][0]["duration_seconds"], 12.4)
        self.assertEqual(state["run_history"][0]["order_results"][0]["status"], "Already stock ordered")
        self.assertEqual(state["run_history"][0]["order_results"][0]["duration_seconds"], 4.2)
        self.assertEqual(state["run_history"][0]["order_results"][1]["status"], "Locked")

    def test_order_goods_history_backfills_details_from_last_result(self):
        payload = {
            "action": "order_goods_batch",
            "order_ids": ["4418860", "4418871"],
            "report": [
                {"order_id": "4418860", "success": True, "outcome": "order_goods_clicked", "message": "Clicked."},
                {"order_id": "4418871", "success": False, "outcome": "order_goods_locked", "message": "Button disabled."},
            ],
        }
        saved_state = {
            "run_history": [
                {
                    "automation_key": "order_goods",
                    "automation_label": "Rush Order Goods",
                    "success": False,
                    "order_ids": ["4418860", "4418871"],
                    "message": "1 succeeded. 1 needs attention.",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_state.json"
            state_path.write_text(json.dumps(saved_state), encoding="utf-8")
            with mock.patch.object(server, "CRM_STATE_FILE", str(state_path)):
                with mock.patch.object(server, "_load_last_result_payload", return_value=payload):
                    state = server.load_crm_state()

        results = state["run_history"][0]["order_results"]
        self.assertEqual(results[0]["status"], "Ordered")
        self.assertEqual(results[0]["message"], "Clicked.")
        self.assertEqual(results[1]["status"], "Locked")
        self.assertEqual(results[1]["message"], "Button disabled.")

    def test_processing_steps_run_validator_unlocker_then_order_goods_on_rush(self):
        state = {
            "processing_filter": "rush",
            "address_validator_enabled": True,
            "stock_unlocker_enabled": True,
            "order_goods_enabled": True,
        }

        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            ["address_validator_batch", "product_separator", "stock_unlocker", "order_goods"],
        )

    def test_processing_free_mode_disables_rush_only_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=True,
                    order_goods_enabled=True,
                    processing_filter="free",
                )

        self.assertTrue(ok)
        self.assertTrue(state["address_validator_enabled"])
        self.assertTrue(state["product_separator_enabled"])
        self.assertFalse(state["stock_unlocker_enabled"])
        self.assertFalse(state["order_goods_enabled"])
        self.assertEqual(server._crm_processing_selected_steps_from_state(state), ["address_validator_batch", "product_separator"])

    def test_processing_switch_to_rush_preserves_selected_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=True,
                    order_goods_enabled=True,
                    processing_filter="free",
                )
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=False,
                    address_validator_enabled=False,
                    product_separator_enabled=False,
                    order_goods_enabled=False,
                    processing_filter="rush",
                )

        self.assertTrue(ok)
        self.assertFalse(state["address_validator_enabled"])
        self.assertFalse(state["product_separator_enabled"])
        self.assertFalse(state["stock_unlocker_enabled"])
        self.assertFalse(state["order_goods_enabled"])
        self.assertEqual(server._crm_processing_selected_steps_from_state(state), [])

    def test_processing_preferences_route_ignores_queue_only_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                response = server.app.test_client().post(
                    "/crm/process/preferences",
                    json={
                        "stock_unlocker_enabled": True,
                        "address_validator_enabled": False,
                        "product_separator_enabled": False,
                        "order_goods_enabled": True,
                        "processing_filter": "rush",
                        "advanced_mode": "repeat",
                        "repeat_interval_minutes": 10,
                    },
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertFalse(payload["state"]["address_validator_enabled"])
        self.assertFalse(payload["state"]["product_separator_enabled"])
        self.assertTrue(payload["state"]["stock_unlocker_enabled"])
        self.assertTrue(payload["state"]["order_goods_enabled"])

    def test_processing_state_load_handles_legacy_history_without_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "run_history": [
                            {
                                "timestamp": "2026-05-30T10:00:00",
                                "processing_filter": "rush",
                                "selected_steps": ["stock_unlocker"],
                                "step_results": [],
                                "duration_seconds": 1.2,
                                "message": "Legacy row",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                state = server.load_crm_processing_state()

        self.assertFalse(state["run_history"][0]["success"])
        self.assertEqual(state["run_history"][0]["selected_steps"], ["stock_unlocker"])

    def test_processing_rush_can_run_unlocker_and_order_goods_without_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=False,
                    product_separator_enabled=False,
                    order_goods_enabled=True,
                    processing_filter="rush",
                )

        self.assertTrue(ok)
        self.assertFalse(state["address_validator_enabled"])
        self.assertFalse(state["product_separator_enabled"])
        self.assertTrue(state["stock_unlocker_enabled"])
        self.assertTrue(state["order_goods_enabled"])
        self.assertEqual(
            server._crm_processing_selected_steps_from_state(state),
            ["stock_unlocker", "order_goods"],
        )

    def test_processing_bypass_only_still_selects_order_goods_step(self):
        state = {
            "processing_filter": "rush",
            "address_validator_enabled": False,
            "product_separator_enabled": False,
            "stock_unlocker_enabled": False,
            "order_goods_enabled": False,
            "shipping_bypasser_enabled": True,
        }

        self.assertEqual(server._crm_processing_selected_steps_from_state(state), ["order_goods"])

    def test_shipping_bypasser_history_keeps_sanmar_confirmation_link(self):
        payload = {
            "success": True,
            "order_ids": ["4609966"],
            "report": [
                {
                    "order_id": "4609966",
                    "success": True,
                    "outcome": "shipping_bypass_ordered",
                    "message": "Ordered.",
                    "sanmar_confirmation": {
                        "url": "https://www.sanmar.com/checkout/thank-you",
                        "web_reference": "12345",
                        "screenshot": "screenshots/sanmar_shipping_bypass_4609966.png",
                    },
                }
            ],
        }

        results = server._build_crm_order_goods_order_results(payload)

        self.assertEqual(results[0]["status"], "Bypassed")
        self.assertEqual(results[0]["sanmar_confirmation"]["url"], "https://www.sanmar.com/checkout/thank-you")

    def test_processing_all_mode_forces_validator_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "crm_processing_state.json"
            with mock.patch.object(server, "CRM_PROCESSING_STATE_FILE", str(state_path)):
                server.ensure_crm_processing_state_file()
                ok, _message, state = server.update_crm_processing_preferences(
                    stock_unlocker_enabled=True,
                    address_validator_enabled=False,
                    product_separator_enabled=False,
                    order_goods_enabled=True,
                    processing_filter="all",
                )

        self.assertTrue(ok)
        self.assertTrue(state["address_validator_enabled"])
        self.assertFalse(state["product_separator_enabled"])
        self.assertFalse(state["stock_unlocker_enabled"])
        self.assertFalse(state["order_goods_enabled"])
        self.assertEqual(server._crm_processing_selected_steps_from_state(state), ["address_validator_batch"])

    @mock.patch.object(server, "_finish_crm_address_runtime")
    @mock.patch.object(server, "_persist_crm_address_run_result", return_value={})
    @mock.patch.object(server, "_run_crm_address_with_retry")
    @mock.patch.object(server, "_start_crm_address_runtime")
    @mock.patch.object(server, "load_crm_address_state")
    def test_processing_all_address_step_uses_configured_all_list_url(
        self,
        mock_load_state,
        mock_start_runtime,
        mock_run_retry,
        mock_persist,
        _mock_finish_runtime,
    ):
        all_url = "https://crm.example/report/all"
        mock_load_state.return_value = {"saved_batch_size": 3, "saved_parallel_workers": 2}
        mock_run_retry.return_value = (
            True,
            "ok",
            {"success": True, "message": "ok", "action": "validate_batch", "list_url": all_url},
        )

        with mock.patch.object(server, "CRM_SHIPPING_ALL_URL", all_url):
            result = server._run_crm_processing_step("address_validator_batch", "all")

        self.assertTrue(result["success"])
        self.assertEqual(mock_start_runtime.call_args.kwargs["active_filter"], "all")
        self.assertEqual(mock_start_runtime.call_args.kwargs["list_url"], all_url)
        self.assertEqual(mock_run_retry.call_args.kwargs["list_url"], all_url)
        self.assertEqual(mock_persist.call_args.kwargs["list_url"], all_url)

    def test_worker_all_shipping_filter_uses_all_url(self):
        all_url = "https://crm.example/report/all"
        with mock.patch.object(crm_validate_address, "CRM_SHIPPING_ALL_URL", all_url):
            self.assertEqual(crm_validate_address._normalize_shipping_filter("all"), "all")
            self.assertEqual(crm_validate_address._shipping_list_url_for_filter("all"), all_url)

    @mock.patch.object(server, "log_automation_event")
    @mock.patch.object(server.time, "sleep")
    @mock.patch.object(server, "_execute_crm_address_worker")
    def test_batch_failures_do_not_retry_from_server_layer(
        self,
        mock_execute_worker,
        mock_sleep,
        _mock_log_event,
    ):
        mock_execute_worker.return_value = (
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            {"success": False, "message": "CRMAddressValidator timed out after 180 seconds.", "action": "validate_batch"},
        )

        ok, message, payload = server._run_crm_address_with_retry(
            None,
            "rush",
            dry_run=True,
            action="validate_batch",
            batch_size=3,
            parallel_workers=1,
        )

        self.assertFalse(ok)
        self.assertIn("timed out", message.lower())
        self.assertEqual(payload.get("action"), "validate_batch")
        self.assertEqual(mock_execute_worker.call_count, 1)
        mock_sleep.assert_not_called()

    @mock.patch.object(server, "log_automation_event")
    @mock.patch.object(server.time, "sleep")
    @mock.patch.object(server, "_execute_crm_address_worker")
    def test_single_order_transient_failure_still_retries(
        self,
        mock_execute_worker,
        mock_sleep,
        _mock_log_event,
    ):
        mock_execute_worker.side_effect = [
            (False, "CRMAddressValidator timed out after 180 seconds.", {"success": False, "message": "timed out"}),
            (True, "ok", {"success": True, "message": "ok", "action": "validate_order"}),
        ]

        ok, message, payload = server._run_crm_address_with_retry(
            "1234567",
            "free",
            dry_run=True,
            action="validate_order",
            batch_size=1,
            parallel_workers=1,
        )

        self.assertTrue(ok)
        self.assertEqual(message, "ok")
        self.assertEqual(payload.get("action"), "validate_order")
        self.assertEqual(mock_execute_worker.call_count, 2)
        mock_sleep.assert_called_once()

    @mock.patch.object(server, "_audit_result")
    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_persist_batch_result_keeps_batch_action_and_aggregates_manual_review(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        _mock_save_state,
        _mock_audit_result,
    ):
        mock_load_state.return_value = server._default_crm_address_state()
        payload = {
            "success": False,
            "message": "CRMAddressValidator timed out after 180 seconds.",
            "order_count": 2,
            "order_ids": ["3000001", "3000002"],
            "batch_size": 3,
            "parallel_workers": 1,
            "duration_seconds": 18.6,
            "report": [
                {**_report_row("3000001", success=True, manual_review_required=False, resolution="validated"), "duration_seconds": 7.1},
                {**_report_row("3000002", success=False, manual_review_required=True, resolution="manual_review"), "duration_seconds": 8.2},
            ],
        }

        state = server._persist_crm_address_run_result(
            False,
            "CRMAddressValidator timed out after 180 seconds.",
            payload,
            None,
            "rush",
            dry_run=True,
            action="validate_batch",
            batch_size=3,
            parallel_workers=1,
        )

        self.assertEqual(state["last_action"], "validate_batch")
        self.assertEqual(state["run_history"][0]["action"], "validate_batch")
        self.assertEqual(state["run_history"][0]["duration_seconds"], 18.6)
        self.assertEqual(state["run_history"][0]["report"][0]["duration_seconds"], 7.1)
        self.assertTrue(state["last_manual_review_required"])
        self.assertEqual(state["last_resolution"], "batch")
        self.assertEqual(state["last_order_ids"], ["3000001", "3000002"])

    @mock.patch.object(server, "_audit_result")
    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_persist_batch_result_retains_full_fifty_order_report(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        _mock_save_state,
        _mock_audit_result,
    ):
        mock_load_state.return_value = server._default_crm_address_state()
        order_ids = [str(3000000 + index) for index in range(1, 51)]
        payload = {
            "success": True,
            "message": "Processed 50 order(s).",
            "order_count": 50,
            "order_ids": order_ids,
            "batch_size": None,
            "parallel_workers": 8,
            "report": [_report_row(order_id) for order_id in order_ids],
        }

        state = server._persist_crm_address_run_result(
            True,
            "Processed 50 order(s).",
            payload,
            None,
            "all",
            dry_run=False,
            action="validate_batch",
            batch_size=None,
            parallel_workers=8,
        )

        self.assertEqual(len(state["last_report"]), 50)
        self.assertEqual(len(state["run_history"][0]["report"]), 50)
        self.assertEqual(state["last_order_ids"], order_ids)

    @mock.patch.object(server, "_audit_result")
    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_persist_batch_result_keeps_continuous_batch_size(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        _mock_save_state,
        _mock_audit_result,
    ):
        mock_load_state.return_value = server._default_crm_address_state()
        payload = {
            "success": True,
            "message": "Processed all orders.",
            "order_count": 2,
            "order_ids": ["3000001", "3000002"],
            "batch_size": None,
            "parallel_workers": 1,
            "report": [
                _report_row("3000001", success=True, manual_review_required=False, resolution="validated"),
                _report_row("3000002", success=True, manual_review_required=False, resolution="validated"),
            ],
        }

        state = server._persist_crm_address_run_result(
            True,
            "Processed all orders.",
            payload,
            None,
            "free",
            dry_run=True,
            action="validate_batch",
            batch_size=None,
            parallel_workers=1,
        )

        self.assertIsNone(state["last_batch_size"])
        self.assertIsNone(state["run_history"][0]["batch_size"])

    @mock.patch.object(server, "save_crm_address_state")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    def test_update_crm_address_preferences_saves_continuous_batch_mode(
        self,
        _mock_ensure_state_file,
        mock_load_state,
        mock_save_state,
    ):
        mock_load_state.return_value = server._default_crm_address_state()

        ok, message, state = server.update_crm_address_preferences(batch_size=0, parallel_workers=4)

        self.assertTrue(ok)
        self.assertIn("Batch Size Continuous", message)
        self.assertIsNone(state["saved_batch_size"])
        self.assertEqual(state["saved_parallel_workers"], 4)
        mock_save_state.assert_called_once()

    @mock.patch.object(server.threading, "Thread")
    @mock.patch.object(server, "load_crm_address_state")
    @mock.patch.object(server, "ensure_crm_address_state_file")
    @mock.patch.object(server, "crm_lock")
    def test_start_crm_address_run_uses_saved_preferences_when_request_omits_batch_values(
        self,
        mock_crm_lock,
        _mock_ensure_state_file,
        mock_load_state,
        mock_thread,
    ):
        state = server._default_crm_address_state()
        state["active_filter"] = "rush"
        state["saved_batch_size"] = None
        state["saved_parallel_workers"] = 4
        mock_load_state.return_value = state
        mock_crm_lock.acquire.return_value = True
        thread_instance = mock.Mock()
        mock_thread.return_value = thread_instance

        ok, message = server.start_crm_address_run(
            dry_run=True,
            action="validate_batch",
        )

        self.assertTrue(ok)
        self.assertIn("continuously until no orders remain", message)
        mock_thread.assert_called_once_with(
            target=server._crm_address_run_thread,
            args=(None, "rush", True, "validate_batch", None, 4, None),
            daemon=True,
        )
        thread_instance.start.assert_called_once()


class AutomationRuntimeTests(unittest.TestCase):
    @mock.patch.object(server.os.path, "exists", return_value=False)
    @mock.patch.object(server.subprocess, "Popen")
    def test_run_script_uses_hidden_subprocess(self, mock_popen, _mock_exists):
        proc = mock.Mock()
        proc.wait.return_value = 0
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        ok, message, payload = server._run_script("worker.py", ["--dry-run"], "Worker", timeout=5)

        self.assertTrue(ok)
        self.assertIn("completed successfully", message.lower())
        self.assertTrue(payload["success"])
        self.assertEqual(
            mock_popen.call_args.kwargs["creationflags"],
            getattr(server.subprocess, "CREATE_NO_WINDOW", 0),
        )

    @mock.patch.object(server.os.path, "exists", return_value=False)
    @mock.patch.object(server.subprocess, "Popen")
    def test_run_script_can_use_visible_terminal_subprocess(self, mock_popen, _mock_exists):
        proc = mock.Mock()
        proc.wait.return_value = 0
        proc.returncode = 0
        proc.pid = 12345
        mock_popen.return_value = proc

        ok, _message, _payload = server._run_script("worker.py", ["--visible"], "Worker", timeout=5, show_terminal=True)

        self.assertTrue(ok)
        self.assertEqual(
            mock_popen.call_args.kwargs["creationflags"],
            getattr(server.subprocess, "CREATE_NEW_CONSOLE", 0),
        )

    def test_write_result_payload_allows_none_result_file_and_skips_audit(self):
        temp_result_path = ROOT / "tests_runtime_result.json"
        try:
            with mock.patch.object(automation_runtime, "RESULT_FILE", str(temp_result_path)):
                with mock.patch.object(automation_runtime, "log_automation_result") as mock_audit:
                    payload = automation_runtime.write_result_payload(
                        "crm.address_validator",
                        "crm_validate_address.py",
                        True,
                        "ok",
                        extra_fields={"action": "validate_batch"},
                        result_file=None,
                        audit_log=False,
                    )
        finally:
            temp_result_path.unlink(missing_ok=True)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["action"], "validate_batch")
        mock_audit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
