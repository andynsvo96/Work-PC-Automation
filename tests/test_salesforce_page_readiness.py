"""Salesforce redraws must not skip login checks or repeat email submissions."""

from unittest import TestCase, mock

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException

from workers.crm_sleeve_prints import shared


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

    def test_email_link_collection_can_redraw_before_a_single_composer_open(self):
        driver = mock.Mock()
        link = mock.Mock()
        driver.find_elements.side_effect = [StaleElementReferenceException(), [link]]
        with mock.patch.object(shared, "_wait_for_salesforce_account_page") as account, mock.patch.object(shared.time, "sleep"):
            shared._click_salesforce_email(driver, "customer@example.test")
        account.assert_called_once_with(driver, "customer@example.test", timeout=45)
        self.assertEqual(driver.find_elements.call_count, 2)
        link.click.assert_called_once_with()
        driver.refresh.assert_not_called()

    def test_repeated_email_link_redraw_stops_before_clicking(self):
        driver = mock.Mock()
        driver.find_elements.side_effect = StaleElementReferenceException()
        with mock.patch.object(shared, "_wait_for_salesforce_account_page"), mock.patch.object(shared.time, "sleep"):
            with self.assertRaisesRegex(shared.CopyrightCancelError, "email links kept changing"):
                shared._click_salesforce_email(driver, "customer@example.test")
        self.assertEqual(driver.find_elements.call_count, 3)
        driver.execute_script.assert_not_called()

    def test_non_stale_browser_failure_is_not_retried(self):
        for action in (shared._salesforce_login_fields, lambda driver: shared._click_salesforce_email(driver, "customer@example.test")):
            with self.subTest(action=action):
                driver = mock.Mock()
                driver.find_elements.side_effect = WebDriverException("disconnected")
                with mock.patch.object(shared, "_wait_for_salesforce_account_page"):
                    with self.assertRaises(WebDriverException):
                        action(driver)
                driver.find_elements.assert_called_once()
