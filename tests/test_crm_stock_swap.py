"""Stock Swap comparison, single-order execution, and durable email recovery."""

from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from workers import crm_stock_swap as stock


def item(style="5000", color="Black", quantity=3, sizes=None, description="Cotton Tees"):
    return {"style": style, "color": color, "description": description, "quantity": quantity,
            "split": True, "sizes": [{"label": size, "quantity": count}
                                      for size, count in (sizes or {"M": quantity}).items()]}


def design(*items, identity="tab-1", artwork="art-1", name="Team Logo"):
    return {"id": identity, "artwork_id": artwork, "name": name, "items": list(items)}


def snapshot(*designs):
    return {"id": "1234567", "editing": False, "designs": list(designs)}


def swaps():
    return stock.find_swaps(snapshot(design(item())), snapshot(design(item("PC54"))))


class StockSwapComparisonTests(unittest.TestCase):
    def test_original_is_stock_and_current_is_product(self):
        values = stock.placeholder_values("1234567", swaps())
        self.assertEqual(values, {"[ORDER-NUMBER]": "1234567", "[STOCK]": "5000 Black Cotton Tees (M: 3)",
                                  "[PRODUCT]": "PC54 Black Cotton Tees (M: 3)"})

    def test_sales_note_uses_original_replacement_and_a_real_newline(self):
        self.assertEqual(stock.format_sales_note("1234567", swaps()),
                         "Swapped 5000 Black Cotton Tees (M: 3) to PC54 Black Cotton Tees (M: 3)\nEmailed")

    def test_only_changed_sizes_are_included_in_partial_swap(self):
        before = snapshot(design(item(quantity=7, sizes={"M": 5, "L": 2})))
        after = snapshot(design(item(quantity=4, sizes={"M": 2, "L": 2}), item("PC54")))
        result = stock.find_swaps(before, after)
        self.assertEqual(result[0]["stock"]["quantities"], {"M": 3})
        self.assertEqual(result[0]["product"]["quantities"], {"M": 3})

    def test_replacements_can_differ_by_size_or_split_one_original_quantity(self):
        before = snapshot(design(item(quantity=5, sizes={"M": 3, "L": 2})))
        after = snapshot(design(item("PC54", sizes={"M": 3}), item("PC61", quantity=2, sizes={"L": 2})))
        result = stock.find_swaps(before, after)
        self.assertEqual([(p["product"]["style"], p["stock"]["quantities"]) for p in result],
                         [("PC54", {"M": 3}), ("PC61", {"L": 2})])
        after = snapshot(design(item("PC54", quantity=2), item("PC61", quantity=3)))
        result = stock.find_swaps(snapshot(design(item(quantity=5))), after)
        self.assertEqual([p["stock"]["quantities"] for p in result], [{"M": 2}, {"M": 3}])

    def test_saved_item_ids_pair_equal_quantity_swaps_on_one_design(self):
        before = snapshot(design({**item(), "id": "item-1"}, {**item("DM130"), "id": "item-2"}))
        after = snapshot(design({**item("PC61"), "id": "item-2"}, {**item("PC54"), "id": "item-1"}))
        result = stock.find_swaps(before, after)
        self.assertEqual([(p["stock"]["style"], p["product"]["style"]) for p in result],
                         [("5000", "PC54"), ("DM130", "PC61")])

    def test_reassigned_item_ids_do_not_create_swaps_when_products_are_unchanged(self):
        before = snapshot(design({**item(), "id": "item-1"}, {**item("DM130"), "id": "item-2"}))
        after = snapshot(design({**item(), "id": "item-2"}, {**item("DM130"), "id": "item-1"}))
        with self.assertRaisesRegex(stock.StockSwapError, "No saved stock swaps"):
            stock.find_swaps(before, after)

    def test_one_email_covers_all_swaps_with_matching_list_numbers(self):
        before = snapshot(design(item()), design(item("DM130", quantity=2), identity="tab-2", artwork="art-1", name="Team Logo"))
        after = snapshot(design(item("DM130", color="Navy", quantity=2), identity="tab-2", artwork="art-1", name="Team Logo"),
                         design(item("PC54")))
        result = stock.find_swaps(before, after)
        values = stock.placeholder_values("1234567", result)
        self.assertEqual(len(result), 2)
        self.assertTrue(values["[STOCK]"].startswith("1. 5000"))
        self.assertIn("; 2. DM130 Black", values["[STOCK]"])
        self.assertIn("; 2. DM130 Navy", values["[PRODUCT]"])
        self.assertEqual(stock.format_sales_note("1234567", result),
                         f"Swapped {values['[STOCK]']} to {values['[PRODUCT]']}\nEmailed")

    def test_unchanged_products_and_description_edits_do_not_become_swaps(self):
        before = snapshot(design(item(), item("HAT", quantity=2)))
        after = snapshot(design(item("PC54"), item("HAT", quantity=2, description="Updated catalog description")))
        result = stock.find_swaps(before, after)
        self.assertEqual(len(result), 1)
        self.assertNotIn("HAT", stock.placeholder_values("1234567", result)["[STOCK]"])
        with self.assertRaisesRegex(stock.StockSwapError, "No saved stock swaps"):
            stock.find_swaps(before, before)

    def test_deleted_or_zero_products_are_excluded_by_inventory(self):
        before = snapshot(design(item(), item("EMPTY", quantity=0)))
        self.assertEqual(stock.find_swaps(before, snapshot(design(item("PC54"))))[0]["stock"]["style"], "5000")

    def test_designs_can_match_by_unique_artwork_when_quote_tab_ids_differ(self):
        result = stock.find_swaps(snapshot(design(item())), snapshot(design(item("PC54"), identity="quote-tab")))
        self.assertEqual(result[0]["product"]["style"], "PC54")

    def test_quantity_changes_unknown_products_and_ambiguous_pairs_stop(self):
        invalid = [
            (snapshot(design(item())), snapshot(design(item("PC54", quantity=4)))),
            (snapshot(design(item())), snapshot(design(item("PC54", sizes={"L": 3})))),
            (snapshot(design(item())), snapshot(design(item("PC54"), item("EXTRA")))),
            (snapshot(design(item(), item("DM130"))), snapshot(design(item("PC54"), item("PC55")))),
            (snapshot(design(item(quantity=3), item("DM130", quantity=5))),
             snapshot(design(item("PC54", quantity=3), item("PC55", quantity=5)))),
            (snapshot(design(item())), snapshot(design(item("PC54")), design(item(), identity="extra"))),
            (snapshot(design(item())), snapshot(design(item("PC54", description="")))),
            (snapshot(design(item(), identity="", artwork="", name="")), snapshot(design(item("PC54")))),
        ]
        for before, after in invalid:
            with self.subTest(after=after), self.assertRaises(stock.StockSwapError):
                stock.find_swaps(before, after)

    def test_unreadable_quantities_never_turn_into_a_swap(self):
        for value in (None, True, "unknown", -1, 1.5, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(stock.StockSwapError):
                stock._quantity(value)


class StockSwapReceiptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for module in (stock, stock.confirmation):
            patch = mock.patch.object(module, "STATE_DIR", folder.name)
            patch.start()
            self.addCleanup(patch.stop)
        self.driver = mock.Mock()
        self.draft = {"subject": "Order #1234567 - Stock Swap", "body": "5000 was replaced by PC54",
                      "sent": False, "dry_run": True}
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.prepare = self.patches.enter_context(mock.patch.object(stock, "_prepare_email", return_value=self.draft))
        self.click = self.patches.enter_context(mock.patch.object(stock.shared, "_click_salesforce_send_button", return_value=True))
        actual = stock.confirmation.send_and_confirm
        self.patches.enter_context(mock.patch.object(stock.confirmation, "send_and_confirm", side_effect=lambda *a, **k: actual(*a, timeout=0, **k)))

    def send(self, **kwargs):
        return stock._send_email(self.driver, "crm", "1234567", "customer@example.test", swaps(), **kwargs)

    def confirmed_after_second_attempt(self):
        row = {"subject": self.draft["subject"], "sender": "Orders", "recipient": "Customer"}
        self.driver.execute_script.side_effect = iter([[], [], [], [], [row]])

    def test_bounded_resend_and_title_edit_cannot_repeat_confirmed_email(self):
        self.confirmed_after_second_attempt()
        result = self.send()
        self.assertEqual(result["send_attempts"], 2)
        self.assertEqual(self.click.call_count, 2)
        self.assertEqual(self.prepare.call_count, 2)
        self.prepare.reset_mock()
        self.prepare.return_value = {**self.draft, "subject": "New title #1234567"}
        reused = self.send()
        self.assertTrue(reused["skipped"])
        self.assertEqual(self.click.call_count, 2)
        self.prepare.assert_not_called()

    def test_unconfirmed_send_blocks_future_attempts(self):
        self.driver.execute_script.return_value = []
        with self.assertRaises(stock.confirmation.UnconfirmedEmailError):
            self.send()
        self.assertEqual(self.click.call_count, 2)
        with self.assertRaisesRegex(stock.confirmation.UnconfirmedEmailError, "previous email send attempt"):
            self.send()
        self.assertEqual(self.click.call_count, 2)

    def test_changed_retry_draft_or_recipient_stops(self):
        self.driver.execute_script.return_value = []
        with self.assertRaises(stock.confirmation.UnconfirmedEmailError):
            self.send()
        self.prepare.return_value = {**self.draft, "body": "Changed template"}
        with self.assertRaisesRegex(stock.StockSwapError, "draft changed"):
            self.send()
        with self.assertRaisesRegex(stock.StockSwapError, "receipt cannot be reused"):
            stock._send_email(self.driver, "crm", "1234567", "other@example.test", swaps())
        self.assertEqual(self.click.call_count, 2)

    def test_changed_rebuilt_body_prevents_the_second_send(self):
        self.driver.execute_script.return_value = []
        self.prepare.side_effect = [self.draft, {**self.draft, "body": "changed"}]
        with self.assertRaisesRegex(stock.confirmation.UnconfirmedEmailError, "rebuilt email"):
            self.send()
        self.click.assert_called_once()

    def test_confirmed_activity_survives_interrupted_workflow_receipt_save(self):
        self.confirmed_after_second_attempt()
        write = stock.confirmation._write_receipt

        def fail_final(path, receipt):
            if path.parent.name == "stock_swap_emails" and receipt.get("confirmed"):
                raise OSError("interrupted final checkpoint")
            return write(path, receipt)

        with mock.patch.object(stock.confirmation, "_write_receipt", side_effect=fail_final):
            with self.assertRaises(OSError):
                self.send()
        self.assertTrue(self.send()["skipped"])
        self.assertEqual(self.click.call_count, 2)

    def test_dry_run_writes_no_receipts_and_does_not_send(self):
        self.assertTrue(self.send(dry_run=True)["dry_run"])
        self.click.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_receipt_stops_before_composer_or_send(self):
        path = self.root / "stock_swap_emails/1234567.json"
        path.parent.mkdir()
        path.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(stock.StockSwapError, "receipt cannot be reused"):
            self.send()
        self.prepare.assert_not_called()
        self.click.assert_not_called()

    def test_retry_after_note_failure_recovers_note_without_resending_email(self):
        self.confirmed_after_second_attempt()
        before, after = snapshot(design(item())), snapshot(design(item("PC54")))
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(stock.shared, "_open_driver", return_value=self.driver))
            for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "safe_driver_quit", "safe_take_screenshot"):
                stack.enter_context(mock.patch.object(stock.shared, name))
            stack.enter_context(mock.patch.object(stock.shared, "_wait_for_crm_contact_info", return_value={"email": "customer@example.test"}))
            stack.enter_context(mock.patch.object(stock, "_read_snapshot", side_effect=[after, before, after] * 2))
            note = stack.enter_context(mock.patch.object(stock, "_append_sales_note", side_effect=[
                stock.StockSwapError("Sales Note did not persist"), {"confirmed": True}]))
            ok, message, failed = stock.run_stock_swap_order("1234567")
            self.assertFalse(ok)
            self.assertEqual(failed["failed_stage"], "sales_note")
            self.assertTrue(failed["salesforce"]["activity_verified"])
            ok, message, recovered = stock.run_stock_swap_order("1234567")
        self.assertTrue(ok)
        self.assertTrue(recovered["salesforce"]["skipped"])
        self.assertTrue(recovered["sales_note"]["confirmed"])
        self.assertEqual(note.call_count, 2)
        self.assertEqual(self.click.call_count, 2)


class StockSwapExecutionTests(unittest.TestCase):
    def test_one_email_uses_every_swap_then_the_note_is_saved(self):
        before = snapshot(design(item()))
        after = snapshot(design(item("PC54")))
        sequence = []
        with ExitStack() as stack:
            driver = stack.enter_context(mock.patch.object(stock.shared, "_open_driver")).return_value
            navigate = stack.enter_context(mock.patch.object(stock.shared, "safe_get_with_partial_load"))
            stack.enter_context(mock.patch.object(stock.shared, "_login_to_crm_if_needed"))
            stack.enter_context(mock.patch.object(stock.shared, "_wait_for_crm_contact_info", return_value={"email": "customer@example.test"}))
            stack.enter_context(mock.patch.object(stock, "_read_snapshot", side_effect=[after, before, after]))
            def send_email(*args, **kwargs):
                sequence.append("email")
                return {"sent": True, "activity_verified": True}

            def save_note(*args, **kwargs):
                sequence.append("note")
                return {"confirmed": True}

            send = stack.enter_context(mock.patch.object(stock, "_send_email", side_effect=send_email))
            note = stack.enter_context(mock.patch.object(stock, "_append_sales_note", side_effect=save_note))
            quit_browser = stack.enter_context(mock.patch.object(stock.shared, "safe_driver_quit"))
            result = stock.process_stock_swap_order("1234567")
        self.assertTrue(result["success"])
        self.assertEqual([call.args[1] for call in navigate.call_args_list], [
            "https://crm2.legacy.printfly.com/order/1234567", "https://crm2.legacy.printfly.com/app#/quotes/1234567",
            "https://crm2.legacy.printfly.com/order/1234567", "https://crm2.legacy.printfly.com/order/1234567"])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[-1], swaps())
        self.assertEqual(sequence, ["email", "note"])
        note.assert_called_once_with(driver, "1234567", stock.format_sales_note("1234567", swaps()), dry_run=False)
        self.assertTrue(result["sales_note"]["confirmed"])
        self.assertEqual(driver.execute_script.call_count, 0)
        quit_browser.assert_called_once()

    def test_no_emailed_note_is_saved_for_an_unconfirmed_email(self):
        before, after = snapshot(design(item())), snapshot(design(item("PC54")))
        for response in ({"sent": True}, stock.confirmation.UnconfirmedEmailError("email unconfirmed")):
            with self.subTest(response=response), ExitStack() as stack:
                for name in ("_open_driver", "safe_get_with_partial_load", "_login_to_crm_if_needed", "safe_driver_quit", "safe_take_screenshot"):
                    stack.enter_context(mock.patch.object(stock.shared, name))
                stack.enter_context(mock.patch.object(stock.shared, "_wait_for_crm_contact_info", return_value={"email": "customer@example.test"}))
                stack.enter_context(mock.patch.object(stock, "_read_snapshot", side_effect=[after, before, after]))
                stack.enter_context(mock.patch.object(stock, "_send_email",
                    **({"side_effect": response} if isinstance(response, Exception) else {"return_value": response})))
                note = stack.enter_context(mock.patch.object(stock, "_append_sales_note"))
                ok, message, failed = stock.run_stock_swap_order("1234567")
                self.assertFalse(ok)
                self.assertEqual(failed["failed_stage"], "salesforce_email")
                note.assert_not_called()

    def test_changed_order_stops_before_email(self):
        after = snapshot(design(item("PC54")))
        changed = deepcopy(after)
        changed["designs"][0]["items"][0]["color"] = "Navy"
        with ExitStack() as stack:
            for name in ("_open_driver", "safe_get_with_partial_load", "_login_to_crm_if_needed", "safe_driver_quit", "safe_take_screenshot"):
                stack.enter_context(mock.patch.object(stock.shared, name))
            stack.enter_context(mock.patch.object(stock.shared, "_wait_for_crm_contact_info", return_value={"email": "customer@example.test"}))
            stack.enter_context(mock.patch.object(stock, "_read_snapshot", side_effect=[after, snapshot(design(item())), changed]))
            send = stack.enter_context(mock.patch.object(stock, "_send_email"))
            ok, message, result = stock.run_stock_swap_order("1234567")
        self.assertFalse(ok)
        self.assertIn("saved order changed", message)
        self.assertEqual(result["failed_stage"], "stock_comparison")
        send.assert_not_called()

    def test_invalid_order_never_opens_browser(self):
        with mock.patch.object(stock.shared, "_open_driver") as open_browser:
            ok, message, result = stock.run_stock_swap_order("invalid")
        self.assertFalse(ok)
        open_browser.assert_not_called()

    def test_wrong_order_and_quote_option_ambiguity_stop(self):
        for quote, response, option_count in ((False, {"id": "7654321"}, 1),
                                               (True, {"id": "1234567", "order_id": "7654321"}, 1),
                                               (True, {"id": "1234567", "order_id": "1234567"}, 2)):
            with self.subTest(quote=quote, response=response), ExitStack() as stack:
                stack.enter_context(mock.patch.object(stock.shared, "_activate_crm_context"))
                stack.enter_context(mock.patch.object(stock.shared, "_order_scope", return_value=response))
                stack.enter_context(mock.patch.object(stock, "_quote_scope", side_effect=[response, option_count]))
                with self.assertRaises(stock.StockSwapError):
                    stock._read_snapshot(mock.Mock(), "1234567", quote=quote)

    def test_missing_template_placeholders_stop_before_salesforce_navigation(self):
        with mock.patch.object(stock, "default_template_store") as factory, mock.patch.object(stock.shared, "_open_salesforce_account") as open_sf:
            factory.return_value.get_template.return_value = {"subject": "Order #[ORDER-NUMBER]", "body_html": "<p>[PRODUCT]</p>"}
            with self.assertRaisesRegex(stock.StockSwapError, "Keep.*STOCK"):
                stock._prepare_email(mock.Mock(), "crm", "1234567", "customer@example.test", swaps())
        open_sf.assert_not_called()
