"""Real Chrome confirmation against a local fixture; never opens Slack."""

import os
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
import slack_team


class SlackDeliveryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        driver_path = os.environ.get("CHROMEDRIVER")
        if not driver_path:
            candidates = list((ROOT / ".wdm/drivers/chromedriver").glob("**/chromedriver.exe"))
            if candidates:
                driver_path = str(max(candidates, key=lambda path: tuple(
                    int(number) for number in next(part for part in path.parts if part.count(".") == 3).split(".")
                )))
        if not driver_path:
            raise unittest.SkipTest("No local ChromeDriver available for offline Slack tests.")
        artifacts = ROOT / "runtime/debug/slack_delivery_tests"
        artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifacts)
        assert Path(cls.profile.name).resolve().is_relative_to(artifacts.resolve())
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for argument in ("--headless=new", "--disable-background-networking", "--disable-sync", "--disable-gpu", "--no-sandbox", "--no-first-run", "--window-size=1100,900"):
            options.add_argument(argument)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path, log_output=str(artifacts / "chromedriver.log")), options=options)
        cls.addClassCleanup(cls.driver.quit)
        cls.fixture = (ROOT / "tests/fixtures/slack_delivery.html").as_uri()

    def setUp(self):
        self.driver.get(self.fixture)
        self.driver.execute_script("localStorage.clear()")
        self.driver.refresh()
        self.receipts = tempfile.TemporaryDirectory(prefix="receipts-", dir=ROOT / "runtime/debug/slack_delivery_tests")
        self.addCleanup(self.receipts.cleanup)
        receipt_patch = mock.patch.object(slack_team.delivery_receipts, "RECEIPT_DIR", Path(self.receipts.name))
        receipt_patch.start()
        self.addCleanup(receipt_patch.stop)

    order_message = "https://example.test/order/1234567"
    order_channel = "https://app.slack.com/client/T123/C456"
    delivery_key = "copyright_cancel_stock_return:1234567"

    def run_order(self, *, reconcile_only=False, delivery_key=delivery_key):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, {"AUTOMATION_HEADLESS": "1"}))
            for name in ("kill_stale_chrome", "safe_driver_quit", "safe_take_screenshot", "log_automation_event"):
                stack.enter_context(mock.patch.object(slack_team, name))
            stack.enter_context(mock.patch.object(slack_team, "build_chrome_driver", return_value=self.driver))
            stack.enter_context(mock.patch.object(slack_team, "open_channel_and_find_composer", side_effect=lambda *_args, **_kwargs: (
                self.driver.find_element(By.ID, "composer"), "", "fixture",
            )))
            stack.enter_context(mock.patch.object(slack_team, "is_expected_slack_channel", side_effect=lambda current, target: (
                current.startswith(self.fixture) and target == self.order_channel
            )))
            stack.enter_context(mock.patch.object(slack_team, "SEND_CONFIRMATION_TIMEOUT_SECONDS", 0.6))
            stack.enter_context(mock.patch.object(slack_team, "EXISTING_MESSAGE_CHECK_TIMEOUT_SECONDS", 0.6))
            record = stack.enter_context(mock.patch.object(slack_team, "record_slack_post"))
            result = slack_team.run(
                "custom", custom_message=self.order_message, channel_url=self.order_channel,
                delivery_key=delivery_key, reconcile_only=reconcile_only,
            )
        return result, record

    def send_attempts(self):
        return self.driver.execute_script("return JSON.parse(localStorage.getItem('slack-delivery-fixture') || '{}').attempts || 0")

    def test_legacy_retry_reuses_existing_order_link_from_restored_history_without_sending(self):
        self.driver.get(self.fixture + "?mode=restore-history-clear")
        self.driver.execute_script("addOldMessage(arguments[0], 86400, {link: true})", self.order_message)
        for _ in range(2):
            result, record = self.run_order(reconcile_only=True)
            self.assertTrue(result[0], result[1])
            self.assertIn("no new message sent", result[1])
            self.assertEqual(self.send_attempts(), 0)
            record.assert_not_called()
        receipt = slack_team.delivery_receipts.load(self.order_channel, self.delivery_key, self.order_message)
        self.assertEqual(receipt["status"], "confirmed")

    def test_order_notification_is_sent_once_and_saved_receipt_survives_a_retry(self):
        first, record = self.run_order()
        self.assertTrue(first[0], first[1])
        record.assert_called_once()
        second, record = self.run_order(reconcile_only=True)
        self.assertTrue(second[0], second[1])
        self.assertEqual(self.send_attempts(), 1)
        record.assert_not_called()

    def test_uncertain_submission_with_no_visible_post_never_sends_again(self):
        self.driver.get(self.fixture + "?mode=clear")
        for _ in range(2):
            result, record = self.run_order()
            self.assertFalse(result[0])
            self.assertEqual(self.send_attempts(), 1)
            record.assert_not_called()
        receipt = slack_team.delivery_receipts.load(self.order_channel, self.delivery_key, self.order_message)
        self.assertEqual(receipt["status"], "submitting")

    def test_retry_recovers_a_persisted_post_after_browser_failure_without_resending(self):
        with mock.patch.object(self.driver, "refresh", side_effect=RuntimeError("browser disconnected after Send")):
            first, _ = self.run_order()
        self.assertFalse(first[0])
        receipt = slack_team.delivery_receipts.load(self.order_channel, self.delivery_key, self.order_message)
        self.assertEqual(receipt["status"], "submitting")
        second, record = self.run_order()
        self.assertTrue(second[0], second[1])
        self.assertEqual(self.send_attempts(), 1)
        record.assert_not_called()

    def test_legacy_retry_with_unavailable_history_stops_without_sending(self):
        result, record = self.run_order(reconcile_only=True)
        self.assertFalse(result[0])
        self.assertIn("No duplicate was sent", result[1])
        self.assertEqual(self.send_attempts(), 0)
        record.assert_not_called()

    def test_matching_pending_or_unidentified_post_blocks_another_order_notification(self):
        for options in ({"pending": True}, {"missingId": True}, {"failed": True}):
            with self.subTest(options=options):
                self.driver.execute_script("localStorage.clear()")
                self.driver.refresh()
                self.driver.execute_script("addOldMessage(arguments[0], 60, arguments[1])", self.order_message, options)
                result, record = self.run_order()
                self.assertFalse(result[0])
                self.assertEqual(self.send_attempts(), 0)
                record.assert_not_called()

    def test_optimistic_existing_post_that_disappears_after_refresh_is_never_reposted(self):
        self.driver.execute_script("appendMessage({id: (Date.now() / 1000).toFixed(6), text: arguments[0]})", self.order_message)
        result, record = self.run_order()
        self.assertFalse(result[0])
        self.assertEqual(self.send_attempts(), 0)
        record.assert_not_called()

    def test_thread_only_and_neighboring_order_messages_do_not_suppress_a_new_notification(self):
        self.driver.execute_script("addOldMessage(arguments[0], 60, {thread: true})", self.order_message)
        self.driver.execute_script("addOldMessage(arguments[0], 60)", self.order_message + "8")
        result, record = self.run_order()
        self.assertTrue(result[0], result[1])
        self.assertEqual(self.send_attempts(), 1)
        record.assert_called_once()

    def test_unscoped_custom_messages_remain_repeatable(self):
        for _ in range(2):
            result, record = self.run_order(delivery_key=None)
            self.assertTrue(result[0], result[1])
            record.assert_called_once()
        self.assertEqual(self.send_attempts(), 2)

    def test_receipt_write_failure_stops_before_send(self):
        with mock.patch.object(slack_team.delivery_receipts, "reserve", side_effect=slack_team.delivery_receipts.SlackReceiptError("receipt storage unavailable")):
            result, record = self.run_order()
        self.assertFalse(result[0])
        self.assertEqual(self.send_attempts(), 0)
        record.assert_not_called()

    def send(self, mode, message="signing on", old_message=False):
        self.driver.get(self.fixture + "?mode=" + mode)
        self.driver.execute_script("localStorage.clear()")
        self.driver.refresh()
        if old_message:
            # Even an identical message less than five seconds old is not proof.
            self.driver.execute_script("addOldMessage(arguments[0], 1)", message)
        composer, _ = slack_team.wait_for_composer(self.driver, timeout=2)
        self.assertIsNotNone(composer)
        self.assertTrue(slack_team.set_composer_message(self.driver, composer, message))
        with (
            mock.patch.object(slack_team, "is_expected_slack_channel", side_effect=lambda current, target: current == target),
            mock.patch.object(slack_team, "SEND_CONFIRMATION_TIMEOUT_SECONDS", 0.05),
        ):
            ok = slack_team.submit_message(self.driver, composer, message)
        self.assertEqual(self.driver.execute_script("return JSON.parse(localStorage.getItem('slack-delivery-fixture')).attempts"), 1)
        return ok

    def test_new_persisted_message_confirms_with_an_identical_older_message(self):
        self.assertTrue(self.send("deliver", old_message=True))

    def test_cleared_or_detached_composer_never_proves_delivery(self):
        for mode in ("clear", "detach"):
            with self.subTest(mode=mode):
                self.assertFalse(self.send(mode, old_message=True))

    def test_optimistic_message_must_survive_refresh(self):
        self.assertFalse(self.send("optimistic"))

    def test_delivery_discovered_only_after_refresh_is_confirmed_without_resending(self):
        self.assertTrue(self.send("after-refresh"))

    def test_delivery_confirms_when_refresh_restores_older_virtualized_history(self):
        self.assertTrue(self.send("restore-history", old_message=True))

    def test_order_link_confirms_after_returning_to_latest_messages(self):
        self.assertTrue(self.send("restore-history-link", "https://example.test/order/1234567"))

    def test_returning_to_latest_still_requires_persisted_and_settled_delivery(self):
        for mode in ("restore-history-optimistic", "restore-history-pending"):
            with self.subTest(mode=mode):
                self.assertFalse(self.send(mode, old_message=True))

    def test_preexisting_recent_match_hidden_in_history_cannot_confirm_a_failed_send(self):
        self.assertFalse(self.send("restore-history-clear", old_message=True))

    def test_failed_pending_unidentified_or_thread_messages_never_confirm(self):
        for mode in ("failed", "pending", "missing-id", "thread"):
            with self.subTest(mode=mode):
                self.assertFalse(self.send(mode))

    def test_multiline_text_is_composed_without_premature_sends(self):
        self.assertTrue(self.send("deliver", "signing on\nworking on free ship"))

    def test_plain_url_confirms_when_slack_hides_the_url_scheme(self):
        self.assertTrue(self.send("link", "https://example.test/order/1234567"))

    def test_incorrect_draft_stops_before_clicking_send(self):
        composer = self.driver.find_element(By.ID, "composer")
        composer.send_keys("unrelated draft signing on")
        with mock.patch.object(slack_team, "is_expected_slack_channel", return_value=True):
            with self.assertRaises(slack_team.SlackBeforeSubmitError):
                slack_team.submit_message(self.driver, composer, "signing on")
        self.assertIsNone(self.driver.execute_script("return localStorage.getItem('slack-delivery-fixture')"))

    def test_neighboring_channel_id_cannot_confirm_the_target_conversation(self):
        target = "https://app.slack.com/client/T123/C456"
        self.assertTrue(slack_team.is_expected_slack_channel(target, target))
        self.assertFalse(slack_team.is_expected_slack_channel(target + "7", target))
        self.driver.execute_script("addOldMessage('signing on', 0)")
        with mock.patch.object(slack_team, "SEND_CONFIRMATION_TIMEOUT_SECONDS", 0.01):
            self.assertIsNone(slack_team._wait_for_new_message(self.driver, "signing on", target, set(), 0))


if __name__ == "__main__":
    unittest.main()
