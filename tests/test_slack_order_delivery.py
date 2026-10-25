"""One-time order notification receipts and cancellation routing, without Slack."""

from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
import crm_copyright_cancel as copyright
import slack_delivery_receipts as receipts
import slack_team


CHANNEL = "https://app.slack.com/client/T123/C456"
MESSAGE = "https://example.test/order/1234567"
KEY = "copyright_cancel_stock_return:1234567"
MESSAGE_ID = "1792920000.123456"


class SlackOrderReceiptTests(unittest.TestCase):
    def setUp(self):
        artifacts = ROOT / "runtime/debug/slack_delivery_tests"
        artifacts.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="receipts-", dir=artifacts)
        self.addCleanup(self.directory.cleanup)
        patch = mock.patch.object(receipts, "RECEIPT_DIR", Path(self.directory.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_reservation_persists_and_another_attempt_cannot_reserve_it(self):
        receipts.reserve(CHANNEL, KEY, MESSAGE)
        self.assertEqual(receipts.load(CHANNEL, KEY, MESSAGE)["status"], "submitting")
        with self.assertRaises(receipts.SlackReceiptError):
            receipts.reserve(CHANNEL, KEY, MESSAGE)
        receipts.confirm(CHANNEL, KEY, MESSAGE, MESSAGE_ID)
        saved = receipts.load(CHANNEL, KEY, MESSAGE)
        self.assertEqual(saved["status"], "confirmed")
        self.assertEqual(saved["message_id"], MESSAGE_ID)
        with mock.patch.object(slack_team, "build_chrome_driver") as launch:
            ok, message, retryable = slack_team._run_once("custom", MESSAGE, CHANNEL, "unused", True, delivery_key=KEY)
        self.assertTrue(ok, message)
        self.assertFalse(retryable)
        launch.assert_not_called()

    def test_receipt_scope_separates_orders_workspaces_and_conversations(self):
        receipts.confirm(CHANNEL, KEY, MESSAGE, MESSAGE_ID)
        for channel, key in ((CHANNEL + "7", KEY), (CHANNEL.replace("T123", "T124"), KEY), (CHANNEL, KEY + "8")):
            with self.subTest(channel=channel, key=key):
                self.assertIsNone(receipts.load(channel, key, MESSAGE))
        self.assertIsNotNone(receipts.load(CHANNEL + "/?message_ts=1792920000.123456", KEY, MESSAGE))

    def test_changed_message_and_corrupt_receipt_never_allow_another_send(self):
        receipts.reserve(CHANNEL, KEY, MESSAGE)
        with self.assertRaises(receipts.SlackReceiptError):
            receipts.load(CHANNEL, KEY, MESSAGE + " cancelled")
        path = next(Path(self.directory.name).glob("*.json"))
        for value in ("{unfinished", json.dumps({"version": 9}), "[]"):
            with self.subTest(value=value):
                path.write_text(value, encoding="utf-8")
                with mock.patch.object(slack_team, "build_chrome_driver") as launch:
                    ok, _, retryable = slack_team._run_once("custom", MESSAGE, CHANNEL, "unused", True, delivery_key=KEY)
                self.assertFalse(ok)
                self.assertFalse(retryable)
                launch.assert_not_called()

    def test_confirmation_requires_a_server_message_timestamp(self):
        for value in (True, None, "pending-client-id", ""):
            with self.subTest(value=value), self.assertRaises(receipts.SlackReceiptError):
                receipts.confirm(CHANNEL, KEY, MESSAGE, value)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_wrong_conversation_cannot_reuse_an_existing_message(self):
        driver = mock.Mock(current_url=CHANNEL + "7")
        with self.assertRaises(slack_team.SlackDuplicateCheckError):
            slack_team._confirm_existing_order_message(driver, MESSAGE, CHANNEL)
        driver.execute_script.assert_not_called()


class CancellationSlackRoutingTests(unittest.TestCase):
    def test_cancellation_supplies_order_scope_and_reconciliation_mode(self):
        with mock.patch.object(copyright, "_run_slack_team", return_value=(True, "Existing message verified")) as run:
            result = copyright._send_post_cancel_slack_message(CHANNEL, MESSAGE, False, order_id="1234567", reconcile_only=True)
        self.assertTrue(result["sent"])
        run.assert_called_once_with("custom", custom_message=MESSAGE, channel_url=CHANNEL, delivery_key=KEY, reconcile_only=True)

    def test_cancellation_dry_run_never_sends_or_creates_a_delivery(self):
        with mock.patch.object(copyright, "_run_slack_team") as run:
            result = copyright._send_post_cancel_slack_message(CHANNEL, MESSAGE, True, order_id="1234567")
        self.assertFalse(result["sent"])
        run.assert_not_called()

    def test_already_cancelled_or_refunded_order_never_blindly_sends_a_missing_legacy_post(self):
        for reason, reconcile_only in (("already_cancelled", True), ("already_refunded", True), (None, False)):
            with self.subTest(reason=reason), ExitStack() as stack:
                stack.enter_context(mock.patch.object(copyright, "_open_driver", return_value=mock.Mock()))
                for name in ("safe_get_with_partial_load", "_login_to_crm_if_needed", "_switch_to_crm_app_frame", "_wait_for_order_scope", "safe_driver_quit"):
                    stack.enter_context(mock.patch.object(copyright, name))
                stack.enter_context(mock.patch.object(copyright, "_wait_for_crm_contact_info", return_value={"email": "customer@example.test"}))
                stack.enter_context(mock.patch.object(copyright, "_read_payment_summary", return_value={}))
                stack.enter_context(mock.patch.object(copyright, "_cancel_and_refund_crm_order", return_value={"cancel": {"reason": reason}}))
                stack.enter_context(mock.patch.object(copyright, "_prepare_and_maybe_send_salesforce_email", return_value={"sent": True}))
                route = stack.enter_context(mock.patch.object(copyright, "_handle_post_cancel_stock_return", return_value={}))
                copyright.process_single_order("1234567", "Reason", dry_run=False, process="content_violation_cancel")
                self.assertEqual(route.call_args.kwargs["reconcile_only"], reconcile_only)


if __name__ == "__main__":
    unittest.main()
