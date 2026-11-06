"""Payment-text ordering and recovery without contacting CRM, Salesforce, or Twilio."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from selenium.webdriver.common.keys import Keys

from workers import crm_reverse_prints as reverse
from workers import crm_sleeve_prints as extra
from workers import salesforce_text_messaging as texts


ROOT = Path(__file__).resolve().parents[1]
EMAIL = "customer@example.test"
ORDER = "4705293"
INVOICE = "https://example.test/order-invoice/test?a=1&b=2"
MESSAGES = ["Payment request", INVOICE]


class TwilioDriver:
    current_window_handle = "crm"

    def __init__(self):
        self.actions, self.sends, self.rows = [], [], []
        self.record = "Account/001000000000001AAA"
        self.value = ""
        self.missing_confirmation = False
        self.fail_second_fill = False
        self.status = "sent"
        self.before_click = None

    def execute_script(self, script, action, email, message, record):
        self.actions.append(action)
        if record and record != self.record:
            return {"error": "The Salesforce record changed."}
        if action == "fill":
            if self.fail_second_fill and len(self.sends) == 1:
                raise RuntimeError("Second input unavailable")
            driver = self

            class Input:
                def send_keys(self, *values):
                    for value in values:
                        if value == Keys.BACKSPACE:
                            driver.value = driver.value[:-1]
                        elif value not in {Keys.TAB, Keys.END}:
                            driver.value += value

            return {"input": Input(), "record": self.record, "value": self.value, "enabled": bool(self.value)}
        if action == "send":
            if self.before_click:
                self.before_click()
            self.sends.append(message)
            if not self.missing_confirmation:
                self.rows.append({"text": message, "status": self.status})
            self.value = ""
            return {"clicked": True, "record": self.record}
        return {"record": self.record, "rows": self.rows[:], "value": self.value, "enabled": bool(self.value)}


class TextReceiptTests(unittest.TestCase):
    def setUp(self):
        artifacts = ROOT / "runtime/debug/salesforce_text_tests"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "texts.json"
        self.driver = TwilioDriver()
        self.receipt = texts.prepare_receipt(self.path, ORDER, EMAIL, MESSAGES)

    def send(self):
        return texts.send_text_messages(self.driver, self.path, ORDER, EMAIL, timeout=0)

    def confirmed_email(self):
        texts.confirm_email(self.path, self.receipt)

    def test_email_confirmation_is_required_before_any_ui_operation(self):
        with self.assertRaisesRegex(texts.TextMessageError, "email must be confirmed"):
            self.send()
        self.assertEqual(self.driver.actions, [])

    def test_sends_in_order_and_persists_attempt_before_each_click_then_skips_completed_pair(self):
        self.confirmed_email()

        def checkpoint():
            saved = texts.load_receipt(self.path, ORDER, EMAIL)
            index = len(self.driver.sends)
            self.assertEqual(saved["messages"][index]["state"], "attempted")
            if index:
                self.assertEqual(saved["messages"][0]["state"], "confirmed")

        self.driver.before_click = checkpoint
        result = self.send()
        self.assertEqual(self.driver.sends, MESSAGES)
        self.assertEqual(result["confirmed_count"], 2)
        saved = self.path.read_bytes()
        self.driver.actions.clear()
        self.assertTrue(self.send()["skipped"])
        self.assertEqual(self.driver.actions, [])
        self.assertEqual(self.path.read_bytes(), saved)

    def test_retry_after_first_confirmed_text_only_sends_invoice(self):
        self.confirmed_email()
        self.driver.fail_second_fill = True
        with self.assertRaisesRegex(RuntimeError, "Second input"):
            self.send()
        saved = texts.load_receipt(self.path, ORDER, EMAIL)
        self.assertEqual([entry["state"] for entry in saved["messages"]], ["confirmed", "ready"])
        self.driver.fail_second_fill = False
        self.assertEqual(self.send()["sent_count"], 1)
        self.assertEqual(self.driver.sends, MESSAGES)

    def test_old_matching_text_does_not_confirm_a_new_send_and_pending_retry_never_clicks(self):
        self.confirmed_email()
        self.driver.rows = [{"text": MESSAGES[0], "status": "delivered"}]
        self.driver.missing_confirmation = True
        with self.assertRaisesRegex(texts.TextMessageError, "text 1 was not confirmed"):
            self.send()
        self.driver.actions.clear()
        with self.assertRaisesRegex(texts.TextMessageError, "previous.*unconfirmed"):
            self.send()
        self.assertEqual(self.driver.sends, [MESSAGES[0]])
        self.assertNotIn("send", self.driver.actions)

    def test_uncertain_second_send_blocks_both_messages_on_retry(self):
        self.confirmed_email()
        self.driver.before_click = lambda: setattr(self.driver, "missing_confirmation", len(self.driver.sends) == 1)
        with self.assertRaisesRegex(texts.TextMessageError, "text 2 was not confirmed"):
            self.send()
        self.assertEqual([entry["state"] for entry in texts.load_receipt(self.path, ORDER, EMAIL)["messages"]],
                         ["confirmed", "attempted"])
        with self.assertRaisesRegex(texts.TextMessageError, "previous.*unconfirmed"):
            self.send()
        self.assertEqual(self.driver.sends, MESSAGES)

    def test_click_exception_without_a_record_stops_before_invoice_and_blocks_resend(self):
        self.confirmed_email()
        self.driver.before_click = Mock(side_effect=RuntimeError("Click disconnected"))
        with self.assertRaisesRegex(RuntimeError, "Click disconnected"):
            self.send()
        with self.assertRaisesRegex(texts.TextMessageError, "previous.*unconfirmed"):
            self.send()
        self.assertEqual(self.driver.sends, [])

    def test_recorded_delivery_status_does_not_block_invoice_or_repeat_either_text(self):
        self.confirmed_email()
        for status in ("failed", "undelivered", "canceled", "", "carrier diagnostic"):
            with self.subTest(status=status):
                self.path.write_text(json.dumps(self.receipt), encoding="utf-8")
                self.driver = TwilioDriver()
                self.driver.status = status
                result = self.send()
                self.assertEqual(result["confirmed_count"], 2)
                self.assertEqual([entry["status"] for entry in result["messages"]], [status, status])
                self.assertEqual(self.driver.sends, MESSAGES)
                self.assertTrue(self.send()["skipped"])
                self.assertEqual(self.driver.sends, MESSAGES)

    def test_pending_first_text_recovers_its_undelivered_record_and_sends_only_invoice(self):
        self.confirmed_email()
        self.driver.missing_confirmation = True
        with self.assertRaisesRegex(texts.TextMessageError, "text 1 was not confirmed"):
            self.send()
        self.driver.rows.append({"text": MESSAGES[0], "status": "undelivered"})
        self.driver.missing_confirmation = False
        result = self.send()
        self.assertEqual((result["recovered_count"], result["sent_count"]), (1, 1))
        self.assertEqual(self.driver.sends, MESSAGES)
        self.assertEqual(result["messages"][0]["status"], "undelivered")

    def test_pending_second_text_recovers_its_failed_record_without_any_resend(self):
        self.confirmed_email()
        self.driver.before_click = lambda: setattr(self.driver, "missing_confirmation", len(self.driver.sends) == 1)
        with self.assertRaisesRegex(texts.TextMessageError, "text 2 was not confirmed"):
            self.send()
        self.driver.rows.append({"text": MESSAGES[1], "status": "failed"})
        result = self.send()
        self.assertEqual((result["recovered_count"], result["sent_count"]), (1, 0))
        self.assertTrue(result["skipped"])
        self.assertEqual(self.driver.sends, MESSAGES)

    def test_changed_customer_or_body_and_corrupt_receipts_cannot_bypass_duplicate_checks(self):
        with self.assertRaisesRegex(texts.TextMessageError, "texts changed"):
            texts.prepare_receipt(self.path, ORDER, EMAIL, ["Changed request", INVOICE])
        for content in ("{", "[]", "null", '{"version": 1}', json.dumps({**self.receipt, "recipient": "other@example.test"})):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(texts.TextMessageError, "receipt cannot be verified"):
                    self.send()
        self.assertEqual(self.driver.sends, [])

    def test_record_change_after_partial_completion_stops_remaining_text(self):
        self.confirmed_email()
        self.driver.fail_second_fill = True
        with self.assertRaises(RuntimeError):
            self.send()
        self.driver.fail_second_fill = False
        self.driver.record = "Contact/003000000000002AAA"
        with self.assertRaisesRegex(texts.TextMessageError, "record changed"):
            self.send()
        self.assertEqual(self.driver.sends, [MESSAGES[0]])


class ExtraPrintTextWorkflowTests(unittest.TestCase):
    def setUp(self):
        artifacts = ROOT / "runtime/debug/extra_print_text_tests"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=artifacts)
        self.addCleanup(self.directory.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.driver = TwilioDriver()
        for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame",
                     "_wait_for_order_scope", "safe_driver_quit", "safe_take_screenshot", "_save_order_and_wait"):
            self.stack.enter_context(patch.object(extra.shared, name))
        self.stack.enter_context(patch.object(extra.shared, "_open_driver", return_value=self.driver))
        self.stack.enter_context(patch.object(extra.shared, "_wait_for_crm_contact_info", return_value={"email": EMAIL}))
        self.state = {"designs": [{"tab_number": 1, "quantity": 10, "print_areas": [{"description": "Front", "method": "HD Digital"}]}]}
        self.stack.enter_context(patch.object(extra, "_read_crm_sleeve_state", return_value=self.state))
        self.stack.enter_context(patch.object(extra, "_crm_note_exists", return_value=True))
        self.stack.enter_context(patch.object(extra, "_apply_crm_sleeve_changes", return_value={}))
        self.stack.enter_context(patch.object(extra, "_verify_saved_crm_sleeve_changes", return_value={}))
        self.stack.enter_context(patch.object(reverse, "STATE_DIR", self.directory.name))
        self.invoice = self.stack.enter_context(patch.object(extra, "_capture_view_invoice_link", return_value=INVOICE))
        self.events = []

        def email(*args, **kwargs):
            self.events.append("email")
            self.assertEqual(self.driver.sends, [])
            return {"sent": True, "activity_verified": True}

        self.email = self.stack.enter_context(patch.object(extra, "_prepare_and_send_salesforce_email", side_effect=email))

        def text_pair(driver, crm_handle, order_id, recipient, path, **kwargs):
            self.events.append("texts")
            return texts.send_text_messages(driver, path, order_id, recipient, timeout=0)

        self.text_pair = self.stack.enter_context(patch.object(extra, "_send_salesforce_text_messages", side_effect=text_pair))
        self.selection = [{"tab_number": 1, "quantity": 10, "left": "ink"}]

    def process(self, **kwargs):
        return extra.process_sleeve_prints_order(ORDER, self.selection, **kwargs)

    def test_two_texts_follow_email_with_exact_copy_and_separate_invoice(self):
        result = self.process(ink_price="6.25")
        expected = (
            "Hello, this is RushOrderTees. To proceed with your order please make the additional payment of $6.25 "
            "per sleeve prints by clicking the link below. If you prefer to forgo the sleeve prints, "
            "please reply back and let us know ASAP."
        )
        self.assertEqual(self.events, ["email", "texts"])
        self.assertEqual(self.driver.sends, [expected, INVOICE])
        self.assertTrue(result["success"])
        self.assertEqual(result["stages"][-1]["key"], "salesforce_texts")
        second = self.process(ink_price="6.25")
        self.assertTrue(second["salesforce"]["skipped"])
        self.assertTrue(second["text_messages"]["skipped"])
        self.email.assert_called_once()
        self.invoice.assert_called_once()

    def test_failed_email_or_missing_activity_never_sends_texts(self):
        for email_result in (RuntimeError("Email unconfirmed"), {"sent": True, "activity_verified": False}):
            with self.subTest(email_result=email_result):
                self.email.side_effect = email_result if isinstance(email_result, Exception) else None
                self.email.return_value = email_result
                with self.assertRaises(extra.SleevePrintsError):
                    self.process()
                self.text_pair.assert_not_called()
                self.assertEqual(self.driver.sends, [])

    def test_sms_failure_retains_email_and_invoice_then_retry_sends_only_remaining_text(self):
        self.driver.fail_second_fill = True
        with self.assertRaises(extra.SleevePrintsError) as raised:
            self.process()
        self.assertEqual(raised.exception.result["failed_stage"], "salesforce_texts")
        self.assertEqual(raised.exception.result["text_messages"]["confirmed_count"], 1)
        self.assertFalse(raised.exception.result["text_messages"]["manual_review_required"])
        self.driver.fail_second_fill = False
        self.assertTrue(self.process()["success"])
        self.email.assert_called_once()
        self.invoice.assert_called_once()
        self.assertEqual(len(self.driver.sends), 2)

    def test_uncertain_send_is_reported_for_review_and_retry_does_not_repeat_email(self):
        self.driver.missing_confirmation = True
        with self.assertRaises(extra.SleevePrintsError) as raised:
            self.process()
        self.assertTrue(raised.exception.result["text_messages"]["manual_review_required"])
        with self.assertRaisesRegex(extra.SleevePrintsError, "previous.*unconfirmed"):
            self.process()
        self.email.assert_called_once()
        self.assertEqual(len(self.driver.sends), 1)

    def test_dry_run_and_comp_sleeves_do_not_send_texts(self):
        self.email.side_effect = None
        self.email.return_value = {"sent": False, "dry_run": True}
        result = self.process(dry_run=True)
        self.assertTrue(result["text_messages"]["dry_run"])
        self.assertEqual(list(Path(self.directory.name).rglob("*.json")), [])
        self.email.return_value = {"sent": True, "activity_verified": True}
        result = self.process(ink_price="0")
        self.assertEqual(result["text_messages"]["reason"], "no_additional_payment")
        self.assertEqual(result["salesforce_template"], "[AUTO] Comp Sleeves")
        self.text_pair.assert_not_called()
        self.assertEqual(self.driver.sends, [])

    def test_changed_live_quantity_does_not_hide_completed_texts(self):
        self.process(ink_price="6")
        self.state["designs"][0]["quantity"] = 11
        self.assertTrue(self.process(ink_price="6")["text_messages"]["skipped"])
        self.email.assert_called_once()
        self.assertEqual(len(self.driver.sends), 2)

    def test_changed_quantity_pricing_tier_stops_before_repeating_email_or_texts(self):
        self.process()
        self.state["designs"][0]["quantity"] = 9
        with self.assertRaisesRegex(extra.SleevePrintsError, "payment texts changed"):
            self.process()
        self.email.assert_called_once()
        self.assertEqual(len(self.driver.sends), 2)

    def test_legacy_confirmed_embroidery_email_continues_to_new_text_stage(self):
        self.selection = [{"tab_number": 1, "quantity": 10, "extra_emb": "embroidery"}]
        self.state["designs"][0]["print_areas"] = [{"description": "Front", "method": "Embroidery"}]
        self.stack.enter_context(patch.object(reverse, "email_was_sent", return_value=True))
        result = self.process()
        self.assertTrue(result["salesforce"]["skipped"])
        self.assertEqual(self.events, ["texts"])
        self.assertEqual(len(self.driver.sends), 2)

    def test_mixed_requests_use_each_price_once_and_reverse_keeps_per_area_wording(self):
        request = extra.normalize_request([
            {"tab_number": 1, "quantity": 10, "left": "ink", "side_right": "embroidery"},
            {"tab_number": 2, "quantity": 10, "reverse": "ink"},
            {"tab_number": 3, "quantity": 10, "extra_emb": "embroidery"},
        ], ink_price="6", embroidery_price="12.50", reverse_price="7")
        plan = extra._build_live_plan(request, {"designs": [
            {"tab_number": tab, "quantity": 10, "print_areas": [{"description": "Front", "method": method}]}
            for tab, method in ((1, "HD Digital"), (2, "HD Digital"), (3, "Embroidery"))
        ]})
        message, invoice = extra._payment_text_messages(plan, INVOICE)
        self.assertIn("$6.00 per sleeve prints and $12.50 per side embroidery and $7.00 per area for reverse prints and $12.50 per additional embroidery area", message)
        self.assertIn("forgo the sleeve prints and side embroidery and reverse prints and additional embroidery area,", message)
        self.assertNotIn("per per", message)
        self.assertEqual(invoice, INVOICE)
        for invalid in ("[INVOICE]", "javascript:alert(1)", "https://example.test/has space"):
            with self.assertRaises(extra.SleevePrintsError):
                extra._payment_text_messages(plan, invalid)


if __name__ == "__main__":
    unittest.main()
