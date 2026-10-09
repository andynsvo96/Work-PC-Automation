import json
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
        self.draft = {'sent': False, 'dry_run': True, 'subject': 'Order #1234567', 'body': 'Email body'}

    def send(self, driver, click, **kwargs):
        return confirmation.send_and_confirm(driver, '1234567', 'Order #1234567',
                                             'customer@example.test', click, **kwargs)

    def test_new_activity_required_and_confirmed_receipt_prevents_second_send(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [self.row]]
        click = Mock(return_value=True)
        result = self.send(driver, click)
        self.assertTrue(result['activity_verified'])
        self.assertFalse(result['refresh_used'])
        self.assertNotIn('refresh_warning', result)
        self.assertEqual(result['timing']['polls'], 1)
        self.assertGreaterEqual(result['timing']['total_seconds'], result['timing']['after_click_seconds'])
        self.assertTrue(self.send(driver, click)['skipped'])
        click.assert_called_once_with(driver)

    def test_old_matching_activity_cannot_confirm_send_and_retry_is_blocked(self):
        driver = Mock()
        driver.execute_script.return_value = [self.row]
        click = Mock(return_value=True)
        with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'No new matching'):
            self.send(driver, click, timeout=0)
        with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'previous email send attempt'):
            self.send(driver, click)
        click.assert_called_once_with(driver)
        driver.refresh.assert_called_once()

    def test_activity_after_refresh_prevents_resend(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [], [self.row]]
        click = Mock(return_value=True)
        prepare = Mock(return_value=self.draft)
        result = self.send(driver, click, timeout=0, prepare_retry=prepare, body='Email body')
        self.assertEqual(result['confirmation_phase'], 'after_refresh')
        self.assertEqual(result['send_attempts'], 1)
        self.assertTrue(result['refresh_used'])
        self.assertEqual(result['refresh_warning'], confirmation.refresh_warning('1234567'))
        receipt = json.loads(next(Path(confirmation.STATE_DIR).rglob('*.json')).read_text())
        self.assertTrue(receipt['refresh_used'])
        self.assertEqual(receipt['refresh_warning'], result['refresh_warning'])
        click.assert_called_once_with(driver)
        prepare.assert_not_called()
        driver.refresh.assert_called_once()

    def test_second_send_only_after_empty_refresh_and_revalidated_draft(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [], [], [], [self.row]]
        attempts = []

        def click_send(_driver):
            receipt = next(Path(confirmation.STATE_DIR).rglob('*.json'))
            attempts.append(json.loads(receipt.read_text())['send_attempts'])
            return True

        prepare = Mock(return_value=self.draft)
        result = self.send(driver, click_send, timeout=0, prepare_retry=prepare, body='Email body')
        self.assertTrue(result['activity_verified'])
        self.assertEqual(result['send_attempts'], 2)
        self.assertEqual(attempts, [1, 2])
        driver.refresh.assert_called_once()
        prepare.assert_called_once_with()
        # A later queue retry cannot send again, even after a successful resend.
        self.assertTrue(self.send(driver, Mock(), prepare_retry=prepare)['skipped'])
        prepare.assert_called_once_with()

    def test_second_send_can_confirm_after_its_refresh(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [], [], [], [], [self.row]]
        click = Mock(return_value=True)
        result = self.send(driver, click, timeout=0, prepare_retry=Mock(return_value=self.draft))
        self.assertEqual(result['send_attempts'], 2)
        self.assertEqual(result['confirmation_phase'], 'after_refresh')
        self.assertEqual(driver.refresh.call_count, 2)
        self.assertTrue(result['refresh_used'])
        self.assertEqual(result['refreshes'], 2)

    def test_two_unconfirmed_sends_stop_and_block_any_further_queue_retry(self):
        driver = Mock()
        driver.execute_script.return_value = []
        click = Mock(return_value=True)
        prepare = Mock(return_value=self.draft)
        with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'after 2 send attempt'):
            self.send(driver, click, timeout=0, prepare_retry=prepare)
        with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'previous email send attempt'):
            self.send(driver, click, timeout=0, prepare_retry=prepare)
        self.assertEqual(click.call_count, 2)
        self.assertEqual(driver.refresh.call_count, 2)
        prepare.assert_called_once_with()
        receipt = json.loads(next(Path(confirmation.STATE_DIR).rglob('*.json')).read_text())
        self.assertEqual(receipt['send_attempts'], 2)
        self.assertTrue(receipt['manual_review_required'])
        self.assertFalse(receipt['retryable'])
        self.assertFalse(receipt['activity_verified'])
        self.assertTrue(receipt['refresh_used'])
        self.assertIn(confirmation.refresh_warning('1234567'), receipt['message'])

    def test_delayed_first_email_during_preparation_does_not_trigger_second_send(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [], [], [self.row]]
        click = Mock(return_value=True)
        result = self.send(driver, click, timeout=0, prepare_retry=Mock(return_value=self.draft))
        self.assertEqual(result['confirmation_phase'], 'before_resend')
        self.assertEqual(result['send_attempts'], 1)
        click.assert_called_once_with(driver)

    def test_changed_subject_or_body_stops_before_second_send(self):
        for replacement in ({'subject': 'Other order #1234567'}, {'body': 'Changed email'}):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as folder, \
                    patch.object(confirmation, 'STATE_DIR', folder):
                driver = Mock()
                driver.execute_script.return_value = []
                click = Mock(return_value=True)
                prepare = Mock(return_value={**self.draft, **replacement})
                with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'rebuilt email'):
                    self.send(driver, click, timeout=0, prepare_retry=prepare, body='Email body')
                click.assert_called_once_with(driver)

    def test_refresh_failure_stops_without_resend(self):
        driver = Mock()
        driver.execute_script.return_value = []
        driver.refresh.side_effect = RuntimeError('navigation timed out')
        click = Mock(return_value=True)
        prepare = Mock(return_value=self.draft)
        with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'navigation timed out'):
            self.send(driver, click, timeout=0, prepare_retry=prepare)
        click.assert_called_once_with(driver)
        prepare.assert_not_called()
        receipt = json.loads(next(Path(confirmation.STATE_DIR).rglob('*.json')).read_text())
        self.assertTrue(receipt['refresh_used'])
        self.assertEqual(receipt['refreshes'], 1)
        self.assertIn(confirmation.refresh_warning('1234567'), receipt['message'])

    def test_changed_labels_of_old_activity_do_not_confirm_after_refresh(self):
        driver = Mock()
        changed = {**self.row, 'sender': 'Staff Name', 'recipient': 'Customer Name'}
        driver.execute_script.side_effect = [[self.row], [changed], [changed], [changed], [changed, self.row]]
        click = Mock(return_value=True)
        result = self.send(driver, click, timeout=0, prepare_retry=Mock(return_value=self.draft))
        self.assertEqual(result['send_attempts'], 2)
        self.assertEqual(click.call_count, 2)

    def test_distinct_workflows_with_same_subject_use_separate_receipts(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [self.row], [self.row], [self.row, self.row]]
        click = Mock(return_value=True)
        self.send(driver, click, receipt_scope='stock_issue_color')
        self.send(driver, click, receipt_scope='stock_issue_size')
        self.assertEqual(click.call_count, 2)

    def test_malformed_receipt_blocks_resend(self):
        driver = Mock()
        driver.execute_script.side_effect = [[], [self.row]]
        click = Mock(return_value=True)
        self.send(driver, click)
        next(Path(confirmation.STATE_DIR).rglob('*.json')).write_text('{')
        with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'receipt cannot be read'):
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
                # Live Lightning components place lookup links across nested shadow roots.
                driver.execute_script('''
                  document.body.innerHTML = '<ul><li class="row"><x-email></x-email></li></ul>';
                  const root = document.querySelector('x-email').attachShadow({mode:'open'});
                  root.innerHTML = '<lightning-icon icon-name="standard:email"></lightning-icon><span class="timelineSubject">Order #1234567</span><x-from class="fromAddress"></x-from><x-to class="toAddress"></x-to>';
                  root.querySelector('x-from').attachShadow({mode:'open'}).innerHTML = '<x-link></x-link>';
                  root.querySelector('x-from').shadowRoot.querySelector('x-link').attachShadow({mode:'open'}).innerHTML = '<a href="/lightning/r/005STAFF/view">Staff</a>';
                  root.querySelector('x-to').attachShadow({mode:'open'}).innerHTML = '<a>Customer</a>';
                ''')
                rows = driver.execute_script(confirmation.ACTIVITY_SCRIPT, 'Order #1234567')
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['recipient'], 'Customer')
                driver.execute_script('''document.body.innerHTML = '<ul><li class="row"><a class="subjectLink">Order #1234567</a><div class="summary"><a href="mailto:orders@example.test">orders@example.test</a> sent an email to <a class="outputLookupLink">Customer</a></div></li></ul>';''')
                self.assertEqual(len(driver.execute_script(confirmation.ACTIVITY_SCRIPT, 'Order #1234567')), 1)
                driver.execute_script("document.querySelector('.summary').innerHTML = 'Customer received an email';")
                self.assertEqual(driver.execute_script(confirmation.ACTIVITY_SCRIPT, 'Order #1234567'), [])
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
