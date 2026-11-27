"""Salesforce redraws must not skip login checks or repeat email submissions."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from unittest import TestCase, mock

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

from workers.crm_sleeve_prints import shared
from workers import salesforce_activity_confirmation as confirmation


class SalesforcePageReadinessTests(TestCase):
    @staticmethod
    def field(input_type, name):
        field = mock.Mock(size={"width": 320, "height": 48})
        field.is_displayed.return_value = True
        attributes = {"type": input_type, "name": name}
        field.get_attribute.side_effect = attributes.get
        return field

    def test_login_check_reacquires_inputs_when_collection_becomes_stale(self):
        username = self.field("text", "username")
        password = self.field("password", "pw")
        driver = mock.Mock(current_url="https://example.test/customer")
        driver.find_elements.side_effect = [StaleElementReferenceException(), [username, password]]
        with mock.patch.object(shared, "_visible_text", return_value=""), mock.patch.object(shared.time, "sleep"):
            self.assertTrue(shared._is_salesforce_login_page(driver))
        self.assertEqual(driver.find_elements.call_count, 2)
        username.send_keys.assert_not_called()
        password.send_keys.assert_not_called()
        driver.refresh.assert_not_called()

    def test_redraw_during_visibility_or_field_inspection_restarts_the_whole_read(self):
        for during in ("visibility", "hint"):
            with self.subTest(during=during):
                stale = self.field("text", "username")
                if during == "visibility":
                    stale.is_displayed.side_effect = StaleElementReferenceException()
                else:
                    stale.get_attribute.side_effect = ["text", StaleElementReferenceException()]
                username = self.field("text", "username")
                password = self.field("password", "pw")
                driver = mock.Mock()
                driver.find_elements.side_effect = [[stale], [username, password]]
                with mock.patch.object(shared.time, "sleep"):
                    self.assertEqual(shared._salesforce_login_fields(driver), (username, password))
                self.assertEqual(driver.find_elements.call_count, 2)

    def test_settled_customer_inputs_are_not_mistaken_for_a_login_form(self):
        driver = mock.Mock(current_url="https://example.test/customer")
        driver.find_elements.side_effect = [StaleElementReferenceException(), [
            self.field("text", "fromAddress"), self.field("text", "subject"),
        ]]
        with mock.patch.object(shared, "_visible_text", return_value="Customer record"), mock.patch.object(shared.time, "sleep"):
            self.assertFalse(shared._is_salesforce_login_page(driver))
        self.assertEqual(driver.find_elements.call_count, 2)

    def test_repeated_login_redraw_stops_instead_of_assuming_authentication(self):
        driver = mock.Mock(current_url="https://example.test/customer")
        driver.find_elements.side_effect = StaleElementReferenceException()
        with mock.patch.object(shared, "_visible_text", return_value=""), mock.patch.object(shared.time, "sleep"):
            with self.assertRaisesRegex(shared.CopyrightCancelError, "inputs kept changing"):
                shared._is_salesforce_login_page(driver)
        self.assertEqual(driver.find_elements.call_count, 3)
        driver.refresh.assert_not_called()

    def test_email_link_click_avoids_stale_web_element_collection(self):
        driver = mock.Mock()
        driver.find_elements.side_effect = StaleElementReferenceException()
        driver.execute_script.return_value = True
        with mock.patch.object(shared, "_wait_for_salesforce_account_page") as account, mock.patch.object(shared.time, "sleep"):
            shared._click_salesforce_email(driver, "customer@example.test")
        account.assert_called_once_with(driver, "customer@example.test", timeout=45)
        driver.find_elements.assert_not_called()
        driver.execute_script.assert_called_once()
        self.assertEqual(driver.execute_script.call_args.args[1:], ("customer@example.test",))
        driver.refresh.assert_not_called()

    def test_missing_email_link_stops_without_replaying_the_click(self):
        driver = mock.Mock()
        driver.execute_script.return_value = False
        with mock.patch.object(shared, "_wait_for_salesforce_account_page"), mock.patch.object(shared.time, "sleep"):
            with self.assertRaisesRegex(shared.CopyrightCancelError, "Could not click Salesforce email"):
                shared._click_salesforce_email(driver, "customer@example.test")
        driver.find_elements.assert_not_called()
        driver.execute_script.assert_called_once()

    def test_blank_email_stops_before_inspecting_or_clicking_the_page(self):
        driver = mock.Mock()
        with mock.patch.object(shared, "_wait_for_salesforce_account_page") as account:
            with self.assertRaisesRegex(shared.CopyrightCancelError, "customer email is blank"):
                shared._click_salesforce_email(driver, "  ")
        account.assert_not_called()
        driver.find_elements.assert_not_called()
        driver.execute_script.assert_not_called()

    def test_non_stale_browser_failure_is_not_retried(self):
        for action in (shared._salesforce_login_fields, lambda driver: shared._click_salesforce_email(driver, "customer@example.test")):
            with self.subTest(action=action):
                driver = mock.Mock()
                driver.find_elements.side_effect = WebDriverException("disconnected")
                driver.execute_script.side_effect = WebDriverException("disconnected")
                with mock.patch.object(shared, "_wait_for_salesforce_account_page"):
                    with self.assertRaises(WebDriverException):
                        action(driver)
                self.assertEqual(driver.find_elements.call_count + driver.execute_script.call_count, 1)

    def test_send_release_failure_never_replays_an_uncertain_click(self):
        driver = mock.Mock()
        driver.execute_script.return_value = {'x': 100, 'y': 200}
        driver.execute_cdp_cmd.side_effect = [None, WebDriverException('Release outcome uncertain')]
        with mock.patch.object(shared, '_restore_salesforce_email_composer'), \
                mock.patch.object(shared, '_wait_for_email_composer'), mock.patch.object(shared.time, 'sleep'):
            with self.assertRaises(WebDriverException):
                shared._click_salesforce_send_button(driver)
        self.assertEqual([call.args[1]['type'] for call in driver.execute_cdp_cmd.call_args_list],
                         ['mousePressed', 'mouseReleased'])


class SalesforceRefundCaseRecoveryTests(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        state = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(mock.patch.object(shared, "STATE_DIR", state))
        self.stack.enter_context(mock.patch.object(confirmation, "STATE_DIR", state))
        self.state = Path(state)
        self.driver = mock.Mock()

    def test_case_retry_uses_confirmed_email_before_opening_the_composer(self):
        subject = "Order #1234567 - Copyright"
        recipient = "customer@example.test"
        path = confirmation._receipt_path("1234567", subject, recipient, shared.COPYRIGHT_CANCEL_PROCESS.key)
        path.parent.mkdir()
        path.write_text(json.dumps({"sent": True, "activity_verified": True}), encoding="utf-8")
        template_store = mock.Mock()
        template_store.get_template.return_value = {"subject": "Order #[ORDER-NUMBER] - Copyright"}
        with mock.patch("salesforce_templates.default_template_store", return_value=template_store), \
             mock.patch.object(shared, "_open_salesforce_account", return_value="sf"), \
             mock.patch.object(shared, "_verify_salesforce_email") as verify, \
             mock.patch.object(shared, "_click_salesforce_email") as composer:
            result = shared._prepare_and_maybe_send_salesforce_email(
                self.driver, "crm", "1234567", recipient, False, resume_confirmed=True,
            )
        self.assertTrue(result["activity_verified"])
        self.assertEqual(result["salesforce_handle"], "sf")
        verify.assert_called_once_with(self.driver, recipient)
        composer.assert_not_called()

    def case_steps(self):
        open_case = self.stack.enter_context(mock.patch.object(shared, "_click_salesforce_cases_new_button"))
        self.stack.enter_context(mock.patch.object(shared, "_wait_for_salesforce_new_case_form"))
        lookup = self.stack.enter_context(mock.patch.object(shared, "_fill_salesforce_case_order_lookup", return_value="advanced"))
        self.stack.enter_context(mock.patch.object(shared, "_select_salesforce_advanced_search_order",
                                                   return_value={"printfly_order_id": "1234567"}))
        self.stack.enter_context(mock.patch.object(shared, "_set_salesforce_case_status"))
        self.stack.enter_context(mock.patch.object(shared, "_fill_salesforce_case_subject"))
        save = self.stack.enter_context(mock.patch.object(
            shared, "_click_salesforce_case_save", side_effect=lambda _driver, before_click: before_click(),
        ))
        confirm = self.stack.enter_context(mock.patch.object(
            shared, "_wait_for_salesforce_refund_case_saved", return_value={"saved": True, "case_number": "00012345"},
        ))
        return open_case, lookup, save, confirm

    def test_lookup_failure_can_retry_and_confirmed_case_is_reused(self):
        open_case, lookup, save, confirm = self.case_steps()
        lookup.side_effect = [StaleElementReferenceException("lookup redrew"), "advanced"]
        with self.assertRaisesRegex(shared.CopyrightCancelError, "select Order Number"):
            shared._create_salesforce_refund_pending_case(self.driver, "1234567")
        self.assertEqual(list(self.state.rglob("*.json")), [])
        self.assertEqual(shared._create_salesforce_refund_pending_case(self.driver, "1234567")["case_number"], "00012345")
        self.assertTrue(shared._create_salesforce_refund_pending_case(self.driver, "1234567")["already_created"])
        self.assertEqual(open_case.call_count, 2)
        save.assert_called_once()
        confirm.assert_called_once()

    def test_uncertain_save_stops_before_opening_another_form(self):
        open_case, _, save, confirm = self.case_steps()
        confirm.side_effect = RuntimeError("Case acknowledgement lost")
        with self.assertRaisesRegex(shared.CopyrightCancelError, "Case acknowledgement lost"):
            shared._create_salesforce_refund_pending_case(self.driver, "1234567")
        with self.assertRaisesRegex(shared.CopyrightCancelError, "submission for order 1234567 is unconfirmed"):
            shared._create_salesforce_refund_pending_case(self.driver, "1234567")
        open_case.assert_called_once()
        save.assert_called_once()

    def test_case_confirmation_checks_the_created_case_and_exact_order_after_refresh(self):
        pages = [
            'Case "00012345" was created. Status Refund Pending Case Number 00012345',
            'Case Number 00012345 Printfly Order Id 12345678 Status Refund Pending Edit Status Subject Copyright Edit Subject',
            'Case Number 00012345 Printfly Order Id 1234567 Status Refund Pending Edit Status Subject Copyright Edit Subject',
        ]
        with mock.patch.object(shared, "_visible_salesforce_text", side_effect=pages) as read, \
             mock.patch.object(shared.time, "sleep"):
            result = shared._wait_for_salesforce_refund_case_saved(self.driver, "1234567", "Copyright")
        self.assertEqual(result["case_number"], "00012345")
        self.assertEqual(read.call_count, 3)
        self.driver.refresh.assert_called_once()
