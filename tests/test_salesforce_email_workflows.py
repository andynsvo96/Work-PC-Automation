"""Isolated integration checks for every Salesforce email send path."""

import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from unittest.mock import Mock, patch

import server
from workers import crm_sleeve_prints as sleeves
from workers import crm_stock_issue_color as color
from workers import crm_stock_issue_extension as extension
from workers import crm_stock_issue_size as size
from workers import salesforce_activity_confirmation as confirmation


class SalesforceEmailWorkflowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        state = patch.object(confirmation, 'STATE_DIR', folder.name)
        state.start()
        self.addCleanup(state.stop)

    def confirmation_driver(self, subject, confirmed=True):
        driver = Mock()
        row = {'subject': subject, 'sender': 'Orders', 'recipient': 'Customer'}
        # No Activity on the first send or its refresh. Only the second can confirm.
        snapshots = iter([[], [], [], [], [row]])

        def execute(script, *args):
            if script == confirmation.ACTIVITY_SCRIPT:
                return next(snapshots) if confirmed else []
            return False  # No linked product text in the draft.

        driver.execute_script.side_effect = execute
        return driver

    def setup_confirmation(self, stack, shared):
        actual_send = confirmation.send_and_confirm

        def send(*args, **kwargs):
            return actual_send(*args, timeout=0, **kwargs)

        stack.enter_context(patch.object(confirmation, 'send_and_confirm', side_effect=send))
        for name in ('_open_salesforce_account', '_verify_salesforce_email',
                     '_click_salesforce_email', '_wait_for_email_composer',
                     '_set_salesforce_from_orders', '_replace_subject_order_number'):
            stack.enter_context(patch.object(shared, name))
        stack.enter_context(patch.object(shared.time, 'sleep'))
        return stack.enter_context(patch.object(shared, '_click_salesforce_send_button', return_value=True))

    def test_all_cancellation_reachout_and_embroidery_templates_confirm_second_send(self):
        shared = sleeves.shared
        processes = [process for process in shared.CANCEL_PROCESSES
                     if process.key not in ('auto_splitter', 'manual_stock_order')]
        for process in processes:
            with self.subTest(process=process.key), ExitStack() as stack:
                subject = f'Order #1234567 - {process.key}'
                driver = self.confirmation_driver(subject)
                click = self.setup_confirmation(stack, shared)
                final = {'subject': subject, 'body': 'Verified email body', 'from': 'Orders'}
                fill = stack.enter_context(patch.object(
                    shared, '_fill_salesforce_email_from_salesforce_template', return_value={'state': final}))
                stack.enter_context(patch.object(shared, '_verify_salesforce_email_ready_to_send', return_value=final))
                result = shared._prepare_and_maybe_send_salesforce_email(
                    driver, 'crm', '1234567', 'customer@example.test', False, process=process, reason='Reason')
                self.assertTrue(result['activity_verified'])
                self.assertEqual(result['send_attempts'], 2)
                self.assertEqual(click.call_count, 2)
                self.assertEqual(fill.call_count, 2)
                self.assertEqual(fill.call_args.kwargs['reason'], 'Reason')
                driver.refresh.assert_called_once()

    def test_general_sender_does_not_report_success_after_two_unconfirmed_attempts(self):
        shared = sleeves.shared
        subject = 'Order #1234567 - Copyright'
        driver = self.confirmation_driver(subject, confirmed=False)
        with ExitStack() as stack:
            click = self.setup_confirmation(stack, shared)
            final = {'subject': subject, 'body': 'Verified email body', 'from': 'Orders'}
            stack.enter_context(patch.object(
                shared, '_fill_salesforce_email_from_salesforce_template', return_value={'state': final}))
            stack.enter_context(patch.object(shared, '_verify_salesforce_email_ready_to_send', return_value=final))
            with self.assertRaisesRegex(confirmation.UnconfirmedEmailError, 'after 2 send attempt'):
                shared._prepare_and_maybe_send_salesforce_email(
                    driver, 'crm', '1234567', 'customer@example.test', False, reason='Reason')
            self.assertEqual(click.call_count, 2)

    def test_extra_print_and_comp_sleeves_rebuild_same_draft_and_confirm_activity(self):
        for process in (sleeves.SLEEVE_PRINTS_PROCESS, sleeves.COMP_SLEEVES_PROCESS):
            with self.subTest(process=process.key), ExitStack() as stack:
                subject = f'Order #1234567 - {process.salesforce_template}'
                driver = self.confirmation_driver(subject)
                click = self.setup_confirmation(stack, sleeves.shared)
                template = {'subject': subject, 'body': '[REQUEST] [REQUEST] [COST] [INVOICE_LINK]'}
                body = 'sleeve prints sleeve prints $0.00 https://example.test/invoice'
                final = {'subject': subject, 'body': body}
                stack.enter_context(patch.object(sleeves.shared, '_insert_cancel_template'))
                stack.enter_context(patch.object(
                    sleeves.shared, '_read_salesforce_email_state', side_effect=[template, final, template, final]))
                stack.enter_context(patch.object(sleeves, '_replace_additional_request_placeholders',
                                                 return_value={'[REQUEST]': 2, '[COST]': 1, '[INVOICE_LINK]': 1}))
                recipients = stack.enter_context(patch.object(sleeves, '_verify_final_recipients'))
                result = sleeves._prepare_and_send_salesforce_email(
                    driver, 'crm', '1234567', 'customer@example.test', 'sleeve prints',
                    '$0.00', 'https://example.test/invoice', process=process)
                self.assertTrue(result['activity_verified'])
                self.assertEqual(result['send_attempts'], 2)
                self.assertEqual(click.call_count, 2)
                self.assertEqual(recipients.call_count, 2)

    def test_all_stock_issue_senders_revalidate_and_require_activity(self):
        for workflow in ('extension', 'color', 'size'):
            context = size._size_workflow() if workflow == 'size' else nullcontext()
            with self.subTest(workflow=workflow), context, ExitStack() as stack:
                worker = extension if workflow == 'extension' else color
                subject = f'Order #1234567 - {workflow}'
                driver = self.confirmation_driver(subject)
                click = self.setup_confirmation(stack, worker.shared)
                placeholder = extension.DAYS_PLACEHOLDER if workflow == 'extension' else color.COLOR_PLACEHOLDER
                template = {'subject': subject, 'body': f'[STOCK] {placeholder}'}
                stack.enter_context(patch.object(worker.shared, '_read_salesforce_email_state', return_value=template))
                content = {'subject': subject, 'body': 'Verified stock email body'}
                verify = stack.enter_context(patch.object(worker, '_verify_email_content', return_value=content))
                recipients_owner = extension if workflow == 'extension' else color.extension
                recipients = stack.enter_context(patch.object(recipients_owner, '_verify_final_recipients'))
                if workflow == 'extension':
                    stack.enter_context(patch.object(extension, '_insert_exact_stock_extension_template'))
                    stack.enter_context(patch.object(extension, '_replace_stock_body_placeholders'))
                else:
                    stack.enter_context(patch.object(color, '_insert_exact_stock_color_template'))
                    stack.enter_context(patch.object(color, '_replace_stock_color_placeholders'))
                    stack.enter_context(patch.object(color, '_selected_product_text_is_linked', return_value=False))
                products = [{'style': 'DM130', 'description': 'Tees', 'color': 'Red'}]
                activity = {}
                result = worker._prepare_and_send_salesforce_email(
                    driver, 'crm', '1234567', 'customer@example.test', 'DM130 red Tees',
                    5 if workflow == 'extension' else 'Navy', products, activity)
                self.assertTrue(result['activity_verified'])
                self.assertTrue(activity['email_sent'])
                self.assertEqual(result['send_attempts'], 2)
                self.assertEqual(click.call_count, 2)
                self.assertEqual(verify.call_count, 2)
                self.assertEqual(recipients.call_count, 2)


class SalesforceEmailNotificationTests(unittest.TestCase):
    message = (f'{confirmation.FAILURE_MARKER} 1234567 after 2 send attempt(s): '
               'No new matching Salesforce email Activity appeared.')

    def test_nested_batch_failures_notify_each_order_once(self):
        payload = {'failures': [{'error': self.message}, {'error': self.message},
                               {'error': self.message.replace('1234567', '7654321')}]}
        with patch.object(server, 'notify_user') as notify:
            server._notify_unconfirmed_salesforce_emails(payload, self.message)
        self.assertEqual(notify.call_count, 2)
        self.assertIn('#1234567', notify.call_args_list[0].args[1])
        self.assertIn('#7654321', notify.call_args_list[1].args[1])

    def test_unrelated_failures_do_not_notify_about_email(self):
        with patch.object(server, 'notify_user') as notify:
            server._notify_unconfirmed_salesforce_emails({'message': 'CRM order timed out'})
        notify.assert_not_called()

    def test_email_confirmation_stop_is_not_a_transient_worker_retry(self):
        self.assertFalse(server._is_crm_transient_failure(
            self.message + ' connection timed out', {'retryable': True}))

    def test_all_direct_queued_email_workflows_notify_and_release_crm_lock(self):
        workflows = (
            (server.run_crm_stock_issue_extension_queued, extension, 'run_stock_issue_extension_order', (5, [])),
            (server.run_crm_stock_issue_color_queued, color, 'run_stock_issue_color_order', (['Navy'], [])),
            (server.run_crm_stock_issue_size_queued, size, 'run_stock_issue_size_order', (['M'], [])),
            (server.run_crm_sleeve_prints_queued, sleeves, 'run_sleeve_prints_order', ([],)),
        )
        result = (False, self.message, {'error': self.message})
        for runner, worker, name, args in workflows:
            with self.subTest(runner=runner.__name__), patch.object(worker, name, return_value=result), \
                    patch.object(server, 'notify_user') as notify, patch.object(server, 'crm_lock') as lock:
                lock.acquire.return_value = True
                self.assertEqual(runner('1234567', *args), result)
                notify.assert_called_once()
                lock.release.assert_called_once()

    def test_mass_emailer_failure_notifies_from_worker_payload(self):
        payload = {'success': False, 'message': 'One order failed', 'failures': [{'error': self.message}]}
        with patch.object(server, '_run_script', return_value=(False, 'One order failed', payload)), \
                patch.object(server, 'notify_user') as notify:
            server._execute_crm_mass_emailer_worker(dry_run=False)
        notify.assert_called_once()


class SalesforceRefreshIndicatorTests(unittest.TestCase):
    warning = confirmation.refresh_warning('1234567')

    def email_result(self, **fields):
        return {'order_id': '1234567', 'activity_verified': True, 'refresh_used': True,
                'refreshes': 1, 'refresh_warning': self.warning, **fields}

    def test_success_after_refresh_stays_successful_and_notifies_once(self):
        email = self.email_result()
        payload = {'success': True, 'salesforce': {**email, 'confirmation': dict(email)}}
        with patch.object(server, 'notify_user') as notify:
            ok, message, result = server._report_salesforce_email_result(True, 'Completed.', payload)
        self.assertTrue(ok)
        self.assertTrue(result['success'])
        self.assertIn(self.warning, message)
        self.assertEqual(result['message'], message)
        self.assertEqual(result['salesforce_refresh_warnings'], [self.warning])
        notify.assert_called_once_with('Salesforce Activity Refresh Used', self.warning)

    def test_success_without_refresh_has_no_warning_or_notification(self):
        payload = {'success': True, 'salesforce': {'activity_verified': True, 'refreshes': 0}}
        with patch.object(server, 'notify_user') as notify:
            result = server._report_salesforce_email_result(True, 'Completed.', payload)
        self.assertEqual(result, (True, 'Completed.', payload))
        self.assertNotIn('salesforce_refresh_warnings', payload)
        notify.assert_not_called()

    def test_reused_receipt_does_not_notify_about_historical_refresh(self):
        payload = {'success': True, 'salesforce': self.email_result(skipped=True)}
        with patch.object(server, 'notify_user') as notify:
            result = server._report_salesforce_email_result(True, 'Previously sent.', payload)
        self.assertEqual(result[1], 'Previously sent.')
        self.assertNotIn('salesforce_refresh_warnings', payload)
        notify.assert_not_called()

    def test_refresh_warning_and_stop_notice_survive_wrapped_failure(self):
        message = SalesforceEmailNotificationTests.message + ' ' + self.warning
        payload = {'success': False, 'stages': [{'message': message}], 'error': message}
        with patch.object(server, 'notify_user') as notify:
            ok, reported, result = server._report_salesforce_email_result(False, message, payload)
        self.assertFalse(ok)
        self.assertEqual(reported.count(self.warning), 1)
        self.assertEqual(result['salesforce_refresh_warnings'], [self.warning])
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notify.call_args_list[0].args[0], 'Salesforce Activity Refresh Used')
        self.assertEqual(notify.call_args_list[1].args[0], 'Salesforce Email Needs Review')

    def test_all_direct_queued_email_workflows_surface_successful_refresh(self):
        workflows = (
            (server.run_crm_stock_issue_extension_queued, extension, 'run_stock_issue_extension_order', (5, [])),
            (server.run_crm_stock_issue_color_queued, color, 'run_stock_issue_color_order', (['Navy'], [])),
            (server.run_crm_stock_issue_size_queued, size, 'run_stock_issue_size_order', (['M'], [])),
            (server.run_crm_sleeve_prints_queued, sleeves, 'run_sleeve_prints_order', ([],)),
        )
        for runner, worker, name, args in workflows:
            with self.subTest(runner=runner.__name__), \
                    patch.object(worker, name, return_value=(True, 'Completed.', {'salesforce': self.email_result()})), \
                    patch.object(server, 'notify_user') as notify, patch.object(server, 'crm_lock') as lock:
                lock.acquire.return_value = True
                ok, message, payload = runner('1234567', *args)
                self.assertTrue(ok)
                self.assertIn(self.warning, message)
                self.assertEqual(payload['salesforce_refresh_warnings'], [self.warning])
                notify.assert_called_once_with('Salesforce Activity Refresh Used', self.warning)
                lock.release.assert_called_once()

    def test_batch_refresh_is_visible_in_summary_and_persisted_order_details(self):
        other = confirmation.refresh_warning('7654321')
        payload = {'success': True, 'processed': [
            {'order_id': '1234567', 'process': 'copyright_reachout', 'salesforce': self.email_result()},
            {'order_id': '7654321', 'process': 'complicated_emb_to_hdd',
             'salesforce': self.email_result(order_id='7654321', refresh_warning=other)},
        ]}
        with patch.object(server, '_run_script', return_value=(True, 'Processed 2 orders.', payload)), \
                patch.object(server, 'notify_user') as notify:
            ok, message, result = server._execute_crm_mass_emailer_worker(dry_run=False)
        self.assertTrue(ok)
        self.assertIn(self.warning, message)
        self.assertIn(other, message)
        self.assertEqual(notify.call_count, 2)
        details = server._crm_mass_emailer_order_details_from_payload(result)
        self.assertIn(self.warning, details[0]['message'])
        self.assertIn(other, details[1]['message'])
        history = server._normalize_crm_mass_emailer_history([{
            'success': True, 'action': 'process_queue', 'message': message, 'order_details': details,
        }])
        self.assertEqual(history[0]['message'], message)
        self.assertEqual(history[0]['order_details'][0]['message'].count(self.warning), 1)


if __name__ == '__main__':
    unittest.main()
