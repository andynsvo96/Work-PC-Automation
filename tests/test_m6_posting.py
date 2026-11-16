"""M6 preferences and notification skips, with no live Slack operations."""

from contextlib import ExitStack
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import m6_posting
import server
from runtime_paths import DEBUG_DIR
from workers import crm_stock_issue_item as item
from workers import crm_stock_issue_color as color
from workers import crm_stock_issue_extension as extension
from workers import crm_auto_splitter as splitter
from workers import rush_order_notifications as rush

slack = sys.modules["slack_team"]
copyright = item.shared


class M6PostingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="m6-posting-", dir=DEBUG_DIR)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "m6_posting.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(m6_posting, "SETTINGS_FILE", self.path))
        self.stack.enter_context(mock.patch.object(server, "APP_PIN_REQUIRED", False))
        self.stack.enter_context(mock.patch.object(server, "_automation_version_block_reason", return_value=""))
        self.stack.enter_context(mock.patch.object(server, "log_automation_event"))

    def test_enabled_default_and_changes_survive_another_worker_process(self):
        self.assertTrue(m6_posting.get_m6_posting_enabled())
        for enabled in (False, True):
            m6_posting.set_m6_posting_enabled(enabled)
            result = subprocess.run([
                sys.executable, "-c",
                "import sys, m6_posting; m6_posting.SETTINGS_FILE = sys.argv[1]; "
                "print(m6_posting.get_m6_posting_enabled())", str(self.path),
            ], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), str(enabled))

    def test_only_the_requested_workspace_and_channel_are_disabled(self):
        m6_posting.set_m6_posting_enabled(False)
        for suffix in ("", "/", "?foo=bar", "/?foo=bar#thread"):
            self.assertTrue(m6_posting.skipped_post_result(m6_posting.M6_CHANNEL_URL + suffix)["disabled_by_setting"])
        for url in ("https://app.slack.com/client/T03DK2TN7/COTHER", "https://app.slack.com/client/TOTHER/C04KPACK6VC",
                    "https://example.test/client/T03DK2TN7/C04KPACK6VC"):
            self.assertIsNone(m6_posting.skipped_post_result(url))

    def test_api_persists_a_boolean_and_rejects_invalid_input(self):
        client = server.app.test_client()
        self.assertTrue(client.get("/api/m6-posting").json["enabled"])
        response = client.post("/api/m6-posting", json={"enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["enabled"])
        self.assertFalse(client.get("/api/m6-posting").json["enabled"])
        for data in ({}, [], {"enabled": "false"}, {"enabled": 0}, {"enabled": None}):
            with self.subTest(data=data):
                self.assertEqual(client.post("/api/m6-posting", json=data).status_code, 400)
                self.assertFalse(m6_posting.get_m6_posting_enabled())

    def test_setting_route_retains_pin_and_csrf_protection(self):
        security = mock.Mock()
        security.csrf_token = "expected"
        with mock.patch.dict(server.app.config, {"SECRET_KEY": "m6-test-only"}), \
                mock.patch.object(server, "APP_PIN_REQUIRED", True), \
                mock.patch.object(server, "initialize_app_security"), \
                mock.patch.object(server, "app_security_config", security):
            client = server.app.test_client()
            self.assertEqual(client.get("/api/m6-posting").status_code, 401)
            self.assertEqual(client.post("/api/m6-posting", json={"enabled": False}).status_code, 401)
            with client.session_transaction() as session:
                session["app_authenticated"] = True
            self.assertEqual(client.get("/api/m6-posting").status_code, 200)
            self.assertEqual(client.post("/api/m6-posting", json={"enabled": False}).status_code, 403)
        self.assertFalse(self.path.exists())

    def test_disabled_sender_does_not_start_a_browser_or_record_delivery(self):
        m6_posting.set_m6_posting_enabled(False)
        with mock.patch.object(slack, "_run_once") as run, \
                mock.patch.object(slack, "record_slack_post") as history, \
                mock.patch.object(slack.delivery_receipts, "reserve") as reserve, \
                mock.patch.object(slack, "log_automation_event"):
            ok, message = slack.run("custom", custom_message="Order notification", channel_url=m6_posting.M6_CHANNEL_URL)
        self.assertTrue(ok)
        self.assertEqual(message, m6_posting.SKIPPED_MESSAGE)
        run.assert_not_called()
        history.assert_not_called()
        reserve.assert_not_called()

    def test_unreadable_setting_stops_m6_without_sending(self):
        self.path.write_text("not JSON", encoding="utf-8")
        with mock.patch.object(slack, "_run_once") as run, mock.patch.object(slack, "log_automation_event"):
            ok, message = slack.run("custom", custom_message="Order notification", channel_url=m6_posting.M6_CHANNEL_URL)
        self.assertFalse(ok)
        self.assertIn("setting", message)
        run.assert_not_called()

    def test_switching_off_during_send_preflight_never_submits_or_reserves_a_receipt(self):
        m6_posting.set_m6_posting_enabled(True)
        driver = mock.Mock()
        composer = mock.Mock()
        def preflight(*_args, before_submit, **_kwargs):
            m6_posting.set_m6_posting_enabled(False)
            before_submit()
            self.fail("Send was reached after M6 was disabled")
        with mock.patch.object(slack, "kill_stale_chrome"), \
                mock.patch.object(slack, "build_chrome_driver", return_value=driver), \
                mock.patch.object(slack, "open_channel_and_find_composer", return_value=(composer, "", "fixture")), \
                mock.patch.object(slack, "set_composer_message", return_value=True), \
                mock.patch.object(slack, "_confirm_existing_order_message", return_value=None), \
                mock.patch.object(slack, "submit_message", side_effect=preflight), \
                mock.patch.object(slack, "safe_driver_quit"), \
                mock.patch.object(slack.delivery_receipts, "load", return_value=None), \
                mock.patch.object(slack.delivery_receipts, "reserve") as reserve, \
                mock.patch.object(slack, "record_slack_post") as history:
            ok, message, retryable = slack._run_once("custom", "Order notification", m6_posting.M6_CHANNEL_URL,
                                                     "fixture-profile", True, delivery_key="fixture-key")
        self.assertTrue(ok)
        self.assertEqual(message, m6_posting.SKIPPED_MESSAGE)
        self.assertFalse(retryable)
        reserve.assert_not_called()
        history.assert_not_called()

    def test_off_is_reported_as_an_intentional_skip_by_all_order_workflows(self):
        m6_posting.set_m6_posting_enabled(False)
        for module in (item, color, extension, rush):
            self.stack.enter_context(mock.patch.object(module, "RUSH_ORDER_SLACK_CHANNEL_URL", m6_posting.M6_CHANNEL_URL))
        self.stack.enter_context(mock.patch.object(splitter._config, "COPYRIGHT_CANCEL_MACH6_STOCK_RETURN_SLACK_URL", m6_posting.M6_CHANNEL_URL))
        with mock.patch.object(copyright, "_run_slack_team") as send, \
                mock.patch.object(rush, "_run_slack_team") as send_rush, \
                mock.patch.object(splitter, "_run_slack_team") as send_split:
            activity = {}
            results = [
                item._send_required_slack("1234567"),
                color._send_required_slack("1234567", activity),
                extension._send_required_slack("1234567", activity),
                rush.send_paid_rush_notification("https://crm.example/order/1234567", "reachout", "10.00"),
                copyright._send_post_cancel_slack_message(m6_posting.M6_CHANNEL_URL, "cancelled", False, order_id="1234567"),
                splitter._send_mach6_stock_cancel_slack("https://crm.example/order/1234567"),
            ]
        for result in results:
            self.assertFalse(result["sent"])
            self.assertFalse(result["confirmed"])
            self.assertTrue(result["disabled_by_setting"])
        self.assertEqual(activity, {})
        send.assert_not_called()
        send_rush.assert_not_called()
        send_split.assert_not_called()

    def test_other_channels_keep_the_existing_sender(self):
        m6_posting.set_m6_posting_enabled(False)
        with mock.patch.object(slack, "_run_once", return_value=(True, "Confirmed", False)) as run, \
                mock.patch.object(slack, "log_automation_event"):
            self.assertTrue(slack.run("custom", custom_message="status", channel_url="https://app.slack.com/client/T03DK2TN7/COTHER")[0])
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
