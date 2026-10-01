import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from workers import salesforce_activity_confirmation as confirmation


class ActivityConfirmationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = patch.object(confirmation, 'STATE_DIR', folder.name)
        state.start()
        self.addCleanup(state.stop)
        self.row = {'subject': 'Order #1234567', 'sender': 'Staff', 'recipient': 'Customer'}

    def send(self, driver, click, **kwargs):
        return confirmation.send_and_confirm(driver, '1234567', 'Order #1234567',
                                             'customer@example.test', click, **kwargs)

    def test_new_activity_required_and_confirmed_receipt_prevents_second_send(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [self.row]]
        click = Mock(return_value=True)
        self.assertTrue(self.send(driver, click)['activity_verified'])
        self.assertTrue(self.send(driver, click)['skipped'])
        click.assert_called_once_with(driver)

    def test_old_matching_activity_cannot_confirm_send_and_retry_is_blocked(self):
        driver = Mock()
        driver.execute_script.return_value = [self.row]
        click = Mock(return_value=True)
        with patch.object(confirmation.time, 'monotonic', side_effect=[0, 0, 2]), patch.object(confirmation.time, 'sleep'):
            with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'no new matching'):
                self.send(driver, click, timeout=1)
        with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'previous email send attempt'):
            self.send(driver, click)
        click.assert_called_once_with(driver)

    def test_delayed_activity_is_awaited(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [], [self.row]]
        with patch.object(confirmation.time, 'sleep'):
            self.assertTrue(self.send(driver, Mock(return_value=True))['sent'])

    def test_click_exception_leaves_retry_blocked(self):
        driver = Mock()
        driver.execute_script.return_value = []
        click = Mock(side_effect=RuntimeError('connection lost'))
        with self.assertRaisesRegex(RuntimeError, 'connection lost'):
            self.send(driver, click)
        with self.assertRaises(confirmation.UnconfirmedEmailError):
            self.send(driver, click)
        self.assertEqual(click.call_count, 1)


class ActivityBrowserTests(unittest.TestCase):
    def test_salesforce_row_structure_excludes_composer_inbound_and_hidden_rows(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        root = Path(__file__).resolve().parents[1]
        drivers = list((root / '.wdm/drivers/chromedriver').glob('**/chromedriver.exe'))
        if not drivers:
            self.skipTest('No local ChromeDriver')
        local_driver = max(drivers, key=lambda p: tuple(int(n) for n in next(x for x in p.parts if x.count('.') == 3).split('.')))
        with tempfile.TemporaryDirectory(dir=root / 'runtime/debug') as profile:
            options = webdriver.ChromeOptions()
            for argument in ('--headless=new', '--disable-background-networking', '--disable-gpu', '--no-sandbox', f'--user-data-dir={profile}'):
                options.add_argument(argument)
            driver = webdriver.Chrome(service=Service(str(local_driver)), options=options)
            try:
                driver.get('about:blank')
                row = '''<li class="row SiqEmail noLinkSubject"><lightning-icon icon-name="standard:email"></lightning-icon>
                  <span class="timelineSubject">Order #1234567</span>
                  <span class="fromAddress"><a href="/lightning/r/005TEST/view">Staff</a></span>
                  <span class="toAddress">Customer</span></li>'''
                driver.execute_script('document.body.innerHTML = arguments[0]',
                    '<div role="dialog">Order #1234567</div><ul>' + row +
                    row.replace('005TEST', '003CONTACT') +
                    row.replace('<li ', '<li style="display:none" ') + '</ul>')
                rows = driver.execute_script(confirmation.ACTIVITY_SCRIPT, 'Order #1234567')
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['recipient'], 'Customer')
                self.assertEqual(driver.execute_script(confirmation.ACTIVITY_SCRIPT, 'Order #7654321'), [])
            finally:
                driver.quit()


class ExtraPrintSendIntegrationTests(unittest.TestCase):
    def test_unconfirmed_activity_propagates_instead_of_returning_sent(self):
        from workers import crm_sleeve_prints as sleeve
        driver = Mock()
        with ExitStack() as stack:
            for name in ('_open_salesforce_account', '_verify_salesforce_email',
                         '_click_salesforce_email', '_wait_for_email_composer',
                         '_set_salesforce_from_orders', '_insert_cancel_template'):
                stack.enter_context(patch.object(sleeve.shared, name))
            stack.enter_context(patch.object(sleeve.shared, '_read_salesforce_email_state', side_effect=[
                {'subject': 'Order #1234567', 'body': '[REQUEST] [REQUEST] [COST] [INVOICE_LINK]'},
                {'subject': 'Order #1234567', 'body': 'side embroidery side embroidery $15 https://example.test/invoice'},
            ]))
            stack.enter_context(patch.object(sleeve, '_replace_additional_request_placeholders'))
            stack.enter_context(patch.object(sleeve, '_verify_final_recipients'))
            stack.enter_context(patch.object(sleeve.time, 'sleep'))
            check = stack.enter_context(patch.object(confirmation, 'send_and_confirm',
                side_effect=confirmation.UnconfirmedEmailError('Activity unconfirmed')))
            with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'Activity unconfirmed'):
                sleeve._prepare_and_send_salesforce_email(
                    driver, 'crm', '1234567', 'customer@example.test', 'side embroidery',
                    '$15', 'https://example.test/invoice')
            check.assert_called_once()
