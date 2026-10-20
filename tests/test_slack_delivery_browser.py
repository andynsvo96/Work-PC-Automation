"""Real Chrome confirmation against a local fixture; never opens Slack."""

import os
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
