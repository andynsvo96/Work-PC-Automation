"""Real Chrome verification using an offline order, including red-X and refunds."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers"))
import crm_copyright_cancel as shared
from workers import copyright_cancel_designs as copyright
from workers.complicated_emb import replace_design_placeholder


class CopyrightCancelBrowserTests(unittest.TestCase):
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
            raise unittest.SkipTest("No local ChromeDriver available for offline copyright tests.")
        artifacts = ROOT / "runtime/debug/copyright_cancel_tests"
        artifacts.mkdir(parents=True, exist_ok=True)
        cls.profile = tempfile.TemporaryDirectory(prefix="chrome-", dir=artifacts)
        cls.addClassCleanup(cls.profile.cleanup)
        options = webdriver.ChromeOptions()
        for argument in ("--headless=new", "--disable-background-networking", "--disable-gpu", "--no-sandbox", "--window-size=1100,1000"):
            options.add_argument(argument)
        options.add_argument(f"--user-data-dir={cls.profile.name}")
        cls.driver = webdriver.Chrome(service=Service(driver_path), options=options)
        cls.addClassCleanup(cls.driver.quit)
        source = (ROOT / "crm-order-dark-mode-extension/content.js").read_text(encoding="utf-8")
        cls.ui = (
            source[source.index("function stockIssueCleanText"):source.index("const EXTRA_PRINT_AREAS")]
            + source[source.index("function createStockIssueDialogShell"):source.index("function showStockIssueProductDialog")]
            + source[source.index("function readCopyrightDesignMetadata"):source.index("function readComplicatedEmbMethods")]
        )

    def setUp(self):
        self.driver.get((ROOT / "tests/fixtures/copyright_cancel_crm.html").as_uri())
        self.driver.execute_script("resetFixture(false);")

    def show_picker(self):
        self.driver.execute_script(copyright.READER + self.ui + """
          window.queued = [];
          window.setOrderProcessorControlsDisabled = () => {};
          window.queueManualOrderAutomation = (...args) => queued.push({reason: args[3], data: args[4]});
          // Exercise the actual identity reader while the local fixture has a file URL.
          document.addEventListener('crm-copyright-designs-request', event => {
            document.dispatchEvent(new CustomEvent('crm-copyright-designs-response', {
              detail: JSON.stringify({id: event.detail, scan: readCopyrightDesignIds()})
            }));
          }, {once: false});
          showCopyrightCancelConfirmation({key: 'copyright_cancel', label: 'Copyright - Cancel'}, null, null);
        """)
        WebDriverWait(self.driver, 10).until(lambda driver: len(driver.find_elements(By.CSS_SELECTOR, 'input[data-copyright-design]')) == 2)

    def test_picker_scans_names_restores_tab_and_queues_partial_snapshot(self):
        self.driver.execute_script("document.querySelector('[data-design-id=\"202\"]').click();")
        self.show_picker()
        self.assertEqual(self.driver.execute_script("return active"), 202)
        queue = self.driver.find_element(By.XPATH, '//button[text()="Queue task"]')
        self.assertFalse(queue.is_enabled())
        self.driver.find_element(By.ID, 'crm-copyright-cancel-reason').send_keys('Protected character')
        self.assertFalse(queue.is_enabled())
        self.driver.find_element(By.CSS_SELECTOR, 'input[data-copyright-design="202"]').click()
        self.assertIn('Partial cancellation', self.driver.find_element(By.CSS_SELECTOR, '[data-copyright-summary]').text)
        self.assertTrue(queue.is_enabled())
        queue.click()
        queued = self.driver.execute_script("return queued")
        self.assertEqual(queued[0]['data']['designs']['selected_design_ids'], ['202'])
        self.assertEqual([row['design_name'] for row in queued[0]['data']['designs']['all_designs']], ['Unaffected', 'Pokemon Crypt'])

    def test_select_all_and_clear_show_correct_scope_and_back_does_not_queue(self):
        self.show_picker()
        self.driver.find_element(By.ID, 'crm-copyright-cancel-reason').send_keys('Reason')
        self.driver.find_element(By.XPATH, '//button[text()="Select all"]').click()
        self.assertIn('Full cancellation', self.driver.find_element(By.CSS_SELECTOR, '[data-copyright-summary]').text)
        self.driver.find_element(By.XPATH, '//button[text()="Clear"]').click()
        self.assertFalse(self.driver.find_element(By.XPATH, '//button[text()="Queue task"]').is_enabled())
        self.driver.find_element(By.XPATH, '//button[text()="Back"]').click()
        self.assertEqual(self.driver.execute_script("return queued"), [])

    def use_shared_artwork(self):
        self.driver.execute_script("""
          resource.designs.forEach((design, index) => {
            design.id = 900 + index; design.designId = 101; design.name = 'Shared artwork';
          });
          active = 901; persist(); render();
        """)

    def test_shared_artwork_tabs_scan_restore_and_queue_individually(self):
        self.use_shared_artwork()
        self.show_picker()
        self.assertEqual(self.driver.execute_script('return active'), 901)
        self.driver.find_element(By.ID, 'crm-copyright-cancel-reason').send_keys('Protected character')
        self.driver.find_element(By.CSS_SELECTOR, 'input[data-copyright-design="order-design:900"]').click()
        summary = self.driver.find_element(By.CSS_SELECTOR, '[data-copyright-summary]').text
        self.assertIn('1 of 2 tabs selected', summary)
        self.assertIn('Partial cancellation', summary)
        self.driver.find_element(By.XPATH, '//button[text()="Queue task"]').click()
        selection = self.driver.execute_script('return queued[0].data.designs')
        self.assertEqual(selection['selected_design_ids'], ['order-design:900'])
        self.assertEqual([row['design_id'] for row in selection['all_designs']], ['101', '101'])
        self.assertEqual([row['order_design_id'] for row in selection['all_designs']], ['900', '901'])
        self.assertEqual(selection['all_designs'], copyright.scan_designs(self.driver, '5451301'))

    def test_shared_artwork_removes_or_zeroes_only_the_selected_tab_and_retry_does_not_refund_twice(self):
        for ordered, selected_index in ((False, 0), (False, 1), (True, 0)):
            with self.subTest(ordered=ordered, selected_index=selected_index), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                self.driver.execute_script('resetFixture(arguments[0]);', ordered)
                self.use_shared_artwork()
                key = f'order-design:{900 + selected_index}'
                selection = {'order_id': '5451301', 'all_designs': copyright.scan_designs(self.driver, '5451301'),
                             'selected_design_ids': [key]}
                stack.enter_context(mock.patch.object(copyright, 'STATE_DIR', folder))
                email = stack.enter_context(mock.patch.object(shared, '_prepare_and_maybe_send_salesforce_email',
                    side_effect=lambda *_args, **_kwargs: {'sent': not _args[4], 'dry_run': _args[4]}))
                result = copyright.process_partial(shared, self.driver, self.driver.current_window_handle, selection,
                    'Protected character', {'email': 'customer@example.test'}, dry_run=False,
                    process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS)
                self.assertTrue(result['refund']['verified'])
                self.assertEqual(result['refund']['amount'], '120.00' if selected_index == 0 else '80.00')
                self.assertEqual(result['removal']['zeroed_design_ids' if ordered else 'removed_design_ids'], [key])
                retained = next(row for row in copyright.scan_designs(self.driver, '5451301')
                                if row['order_design_id'] != str(900 + selected_index))
                self.assertEqual(retained['quantity'], 1)
                self.assertEqual(retained['subtotal'], '80.00' if selected_index == 0 else '120.00')
                repeated = copyright.process_partial(shared, self.driver, self.driver.current_window_handle, selection,
                    'Protected character', {'email': 'customer@example.test'}, dry_run=False,
                    process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS)
                self.assertTrue(repeated['already_completed'])
                self.assertEqual(self.driver.execute_script("return resource.transactions.filter(row => row.tag === 'Refund').length"), 1)
                self.assertEqual([call.args[4] for call in email.call_args_list], [True, False])

    def test_ambiguous_legacy_artwork_identity_stops_before_removing_any_tab(self):
        self.use_shared_artwork()
        with self.assertRaisesRegex(Exception, 'ambiguous'):
            copyright.remove_design(shared, self.driver, '101')
        self.assertEqual(self.driver.execute_script('return fixtureCounters.removals'), [])
        self.assertFalse(self.driver.execute_script('return scope.editMode'))

    def test_worker_red_x_save_reload_and_partial_refund_for_removed_and_stock_ordered_tabs(self):
        for ordered, multiple in ((False, False), (True, False), (False, True)):
            with self.subTest(ordered=ordered, multiple=multiple), tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                self.driver.execute_script("resetFixture(arguments[0]);", ordered)
                if multiple:
                    self.driver.execute_script("""
                      resource.designs.push({designId: 303, name: 'Other affected design', quantity: 1, subtotal: '80.00'});
                      resource.transactions[0].amount = '280.00'; persist(); render();
                    """)
                selected = ['202', '303'] if multiple else ['202']
                selection = {"order_id": "5451301", "all_designs": copyright.scan_designs(self.driver, "5451301"),
                             "selected_design_ids": selected}
                stack.enter_context(mock.patch.object(copyright, 'STATE_DIR', folder))
                email = stack.enter_context(mock.patch.object(shared, '_prepare_and_maybe_send_salesforce_email',
                    side_effect=lambda *_args, **_kwargs: {'sent': not _args[4], 'dry_run': _args[4]}))
                for name in ('_cancel_original_order', '_cancel_and_refund_crm_order', '_add_refund_fee_to_original', '_handle_post_cancel_stock_return'):
                    stack.enter_context(mock.patch.object(shared, name, side_effect=AssertionError(name)))
                result = copyright.process_partial(shared, self.driver, self.driver.current_window_handle,
                    selection, 'Protected character', {'email': 'customer@example.test'}, dry_run=False,
                    process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS)
                self.assertFalse(result['cancel']['cancelled'])
                self.assertTrue(result['refund']['verified'])
                self.assertEqual(result['refund']['amount'], '160.00' if multiple else '80.00')
                self.assertEqual(self.driver.execute_script('return scope.order.getAmountDue()'), 0)
                self.assertEqual(self.driver.execute_script('return scope.order.getAmountPaid()'), 120)
                self.assertEqual(self.driver.find_element(By.CSS_SELECTOR, '#order-info-panel h3').text, 'Approved')
                self.assertEqual(result['removal']['zeroed_design_ids' if ordered else 'removed_design_ids'], selected)
                self.assertEqual([call.args[4] for call in email.call_args_list], [True, False])

    def test_sales_note_waits_for_editor_initialization_and_survives_reload(self):
        self.driver.execute_script("localStorage.setItem('copyright-reset-note-on-edit', 'true');")
        shared._append_copyright_cancel_sales_note(self.driver, 'Protected character')
        self.driver.refresh()
        state = shared._get_order_live_state(self.driver)
        self.assertIn('Protected character copyright', state['sales_notes'])
        self.assertEqual(self.driver.execute_script("return localStorage.getItem('copyright-note-save-attempts');"), '1')

    def test_unsaved_note_draft_is_saved_and_other_draft_text_is_preserved(self):
        note = shared._copyright_cancel_sales_note('Protected character')
        for draft in (note, 'Another pending note'):
            with self.subTest(draft=draft):
                self.driver.execute_script("resetFixture(false); resource.addSalesNotes = arguments[0];", draft)
                shared._append_copyright_cancel_sales_note(self.driver, 'Protected character')
                self.driver.refresh()
                saved = shared._get_order_live_state(self.driver)['sales_notes']
                self.assertEqual(saved.count(note), 1)
                self.assertIn(draft, saved)

    def test_worker_retries_a_note_lost_on_save_after_reload_without_repeating_refund(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            self.driver.execute_script("localStorage.setItem('copyright-note-save-failures', '1');")
            selection = {'order_id': '5451301', 'all_designs': copyright.scan_designs(self.driver, '5451301'),
                         'selected_design_ids': ['202']}
            stack.enter_context(mock.patch.object(copyright, 'STATE_DIR', folder))
            email = stack.enter_context(mock.patch.object(shared, '_prepare_and_maybe_send_salesforce_email',
                side_effect=lambda *_args, **_kwargs: {'sent': not _args[4], 'dry_run': _args[4]}))
            result = copyright.process_partial(shared, self.driver, self.driver.current_window_handle, selection,
                'Protected character', {'email': 'customer@example.test'}, dry_run=False,
                process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS)
            self.assertTrue(result['refund']['verified'])
            self.assertEqual(self.driver.execute_script("return localStorage.getItem('copyright-note-save-attempts');"), '2')
            self.assertEqual(self.driver.execute_script("return resource.transactions.filter(row => row.tag === 'Refund').length;"), 1)
            self.assertEqual(shared._get_order_live_state(self.driver)['sales_notes'].count('Partial copyright cancellation -'), 1)
            self.assertEqual([call.args[4] for call in email.call_args_list], [True, False])

    def test_worker_recovers_removed_receipt_after_existing_refund_and_missing_note(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            self.driver.execute_script("localStorage.setItem('copyright-note-save-failures', '2');")
            selection = {'order_id': '5451301', 'all_designs': copyright.scan_designs(self.driver, '5451301'),
                         'selected_design_ids': ['202']}
            stack.enter_context(mock.patch.object(copyright, 'STATE_DIR', folder))
            email = stack.enter_context(mock.patch.object(shared, '_prepare_and_maybe_send_salesforce_email',
                side_effect=lambda *_args, **_kwargs: {'sent': not _args[4], 'dry_run': _args[4]}))
            with self.assertRaisesRegex(ValueError, 'Sales Note did not persist after reload and one save retry'):
                copyright.process_partial(shared, self.driver, self.driver.current_window_handle, selection,
                    'Protected character', {'email': 'customer@example.test'}, dry_run=False,
                    process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS)
            path = copyright.receipt_path(selection, 'Protected character')
            self.assertEqual(copyright.load_receipt(path)['stage'], 'removed')
            self.assertEqual(self.driver.execute_script("return resource.transactions.filter(row => row.tag === 'Refund').length;"), 0)
            # A refund completed separately while the task was stopped.
            self.driver.execute_script("""
              resource.transactions.push({amount: '-80.00', tag: 'Refund', type: 'Refund'});
              persist(); render();
            """)
            result = copyright.process_partial(shared, self.driver, self.driver.current_window_handle, selection,
                'Protected character', {'email': 'customer@example.test'}, dry_run=False,
                process=shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS)
            self.assertTrue(result['refund']['reconciled'])
            self.assertTrue(result['salesforce']['sent'])
            self.assertEqual(self.driver.execute_script("return resource.transactions.filter(row => row.tag === 'Refund').length;"), 1)
            self.assertEqual(shared._get_order_live_state(self.driver)['sales_notes'].count('Partial copyright cancellation -'), 1)
            self.assertEqual([call.args[4] for call in email.call_args_list], [True, False])

    def test_design_replacement_preserves_template_formatting_links_and_literal_names(self):
        self.driver.get('about:blank')
        self.driver.execute_script("""
          document.body.innerHTML = '<div contenteditable="true"><b>[DES</b><i>IGN]</i> contains <b>[REASON]</b>.<a href="https://example.test/help">Help</a></div>';
        """)
        self.assertEqual(replace_design_placeholder(self.driver, 'A & <Hat> $& and Pokemon Crypt'), 1)
        body = self.driver.find_element(By.CSS_SELECTOR, '[contenteditable]').text
        self.assertIn('A & <Hat> $& and Pokemon Crypt', body)
        self.assertIn('[REASON]', body)
        self.assertEqual(self.driver.find_element(By.TAG_NAME, 'a').get_attribute('href'), 'https://example.test/help')


if __name__ == '__main__':
    unittest.main()
