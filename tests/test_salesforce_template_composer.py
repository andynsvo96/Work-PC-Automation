"""JSON sending coverage with a real browser and an isolated CKEditor API fixture."""

from contextlib import ExitStack
import html
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest import mock

from flask import Flask, Response
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from werkzeug.serving import make_server

from salesforce_templates import SalesforceTemplateStore
from workers import salesforce_template_composer as composer
from workers import crm_sleeve_prints as sleeves
from workers import crm_stock_issue_extension as extension
from workers import crm_stock_issue_color as color
from workers import crm_stock_issue_size as size


ROOT = Path(__file__).resolve().parents[1]
shared = sleeves.shared


class LocalTemplateBoundaryTests(unittest.TestCase):
    def test_invalid_or_missing_local_data_never_opens_or_mutates_the_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SalesforceTemplateStore(Path(folder) / 'templates.json')
            driver = mock.Mock()
            with self.assertRaisesRegex(composer.TemplateComposerError, 'Settings'):
                composer.fill_template(driver, '[AUTO] Missing', store=store)
            driver.execute_script.assert_not_called()
            store.path.write_text('{broken', encoding='utf-8')
            with self.assertRaisesRegex(composer.TemplateComposerError, 'invalid JSON'):
                composer.fill_template(driver, '[AUTO] Missing', store=store)
            driver.execute_script.assert_not_called()

    def test_active_content_is_rejected_before_any_browser_action(self):
        for source in ('<script>alert(1)</script>', '<img src="http://127.0.0.1/action">',
                       '<img src="https://127.1/action">', '<img src="https://0x7f.1/action">',
                       '<p onclick="alert(1)">Hello</p>', '<a href="javascript:alert(1)">X</a>',
                       '<style>p{background:url(https://example.test/)}</style>', '<iframe src="https://example.test/">'):
            with self.subTest(source=source):
                store, driver = mock.Mock(), mock.Mock()
                store.get_template.return_value = {'body_html':source}
                with self.assertRaises(composer.TemplateComposerError):
                    composer.fill_template(driver, 'example', store=store)
                driver.execute_script.assert_not_called()

    def test_every_sender_entry_point_uses_the_json_loader_and_propagates_errors(self):
        processes = [p for p in shared.CANCEL_PROCESSES if p.salesforce_template]
        processes += [shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS, sleeves.SLEEVE_PRINTS_PROCESS, sleeves.COMP_SLEEVES_PROCESS]
        with mock.patch.object(shared, '_click_template_button') as picker:
            for process in processes:
                with self.subTest(process=process.key,template=process.salesforce_template), mock.patch.object(composer,'fill_template',return_value={'source':'local_json'}) as fill:
                    self.assertEqual(shared._insert_cancel_template(mock.sentinel.driver,process),{'source':'local_json'})
                    fill.assert_called_once_with(mock.sentinel.driver,process.salesforce_template)
            for worker, method, process in ((extension,'_insert_exact_stock_extension_template',extension.STOCK_EXTENSION_PROCESS),
                                           (color,'_insert_exact_stock_color_template',color.STOCK_COLOR_PROCESS)):
                with mock.patch.object(composer,'fill_template',side_effect=composer.TemplateComposerError('Missing local template')):
                    with self.assertRaisesRegex(RuntimeError,'Missing local template'):
                        getattr(worker,method)(mock.sentinel.driver)
            with size._size_workflow(), mock.patch.object(composer,'fill_template',return_value={'source':'local_json'}) as fill:
                color._insert_exact_stock_color_template(mock.sentinel.driver)
                fill.assert_called_once_with(mock.sentinel.driver,size.SALESFORCE_TEMPLATE)
            picker.assert_not_called()


class LocalTemplateBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        paths=list((ROOT/'.wdm/drivers/chromedriver').glob('**/chromedriver.exe'))
        executable=os.environ.get('SALESFORCE_EDITOR_CHROMEDRIVER')
        if not executable and paths:
            executable=str(max(paths,key=lambda p:tuple(map(int,next(v for v in p.parts if re.fullmatch(r'\d+(?:\.\d+){3}',v)).split('.')))))
        if not executable:
            raise unittest.SkipTest('No local ChromeDriver for the isolated composer fixture.')
        artifacts=ROOT/'runtime/debug/salesforce_json_migration_tests'
        artifacts.mkdir(parents=True,exist_ok=True)
        cls.temp=tempfile.TemporaryDirectory(prefix='composer-',dir=artifacts)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.store=SalesforceTemplateStore(Path(cls.temp.name)/'templates.json')
        app=Flask(__name__)

        @app.get('/')
        def index():
            return Response((ROOT/'tests/fixtures/salesforce_email_composer.html').read_text(encoding='utf-8'),mimetype='text/html')

        @app.get('/editor')
        def editor():
            return Response((ROOT/'tests/fixtures/salesforce_ckeditor_contract.html').read_text(encoding='utf-8'),mimetype='text/html')

        cls.http=make_server('127.0.0.1',0,app,threaded=True)
        cls.addClassCleanup(cls.http.server_close)
        cls.addClassCleanup(cls.http.shutdown)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
        cls.url=f'http://127.0.0.1:{cls.http.server_port}'
        options=webdriver.ChromeOptions()
        for arg in ('--headless=new','--disable-gpu','--no-sandbox','--window-size=1600,1100','--disable-background-networking',
                    '--host-resolver-rules=MAP images.example.test 127.0.0.1'):
            options.add_argument(arg)
        options.add_argument('--user-data-dir='+str(Path(cls.temp.name)/'profile'))
        if os.environ.get('SALESFORCE_EDITOR_CHROME'):
            options.binary_location=os.environ['SALESFORCE_EDITOR_CHROME']
        try:
            cls.driver=webdriver.Chrome(service=Service(executable),options=options)
        except OSError as exc:
            if getattr(exc,'winerror',None)==4551:
                raise unittest.SkipTest('Windows blocked this local ChromeDriver.') from exc
            raise
        cls.addClassCleanup(cls.driver.quit)

    def open_fixture(self):
        self.driver.get(self.url)
        WebDriverWait(self.driver,10).until(lambda d:d.execute_script(
            "return !!document.getElementById('shadowEditor').shadowRoot.querySelector('iframe').contentWindow.CKEDITOR?.instances.editor;"))

    def write_library(self, records):
        self.store.path.write_text(json.dumps({'schema_version':1,'templates':records}),encoding='utf-8')

    def test_source_html_model_retains_links_images_unicode_and_saved_edits(self):
        body='<html><body><p><strong>Bold &amp; café</strong> <span style="font-size:24px;color:#123456;text-decoration:underline">[REQUEST]</span></p><ol><li>First</li></ol><a href="https://example.test/?a=1&amp;b=2">Guide</a><img src="https://images.example.test/logo.png" alt="Logo"></body></html>'
        self.write_library([{'id':'example','name':'[AUTO] Example','subject':'Order #[ORDER-NUMBER]','body_html':body}])
        self.open_fixture()
        loaded=composer.fill_template(self.driver,'[AUTO] Example',store=self.store)
        self.assertEqual(loaded['source'],'local_json')
        self.assertIn('Bold & café',loaded['state']['body'])
        self.driver.find_element('id','send').click()
        sent=self.driver.find_element('id','sentHtml').text
        self.assertIn('href="https://example.test/?a=1&amp;b=2"',sent)
        self.assertIn('src="https://images.example.test/logo.png"',sent)
        self.assertIn('<strong>',sent)
        self.assertIn('font-size:24px;color:#123456;text-decoration:underline',sent)
        row=self.store.get_template('example')
        self.store.save_template('example',{**row,'name':'Renamed','subject':'Edited subject','body_html':body.replace('First','Latest edit')})
        self.open_fixture()
        updated=composer.fill_template(self.driver,'[AUTO] Example',store=self.store)
        self.assertEqual(updated['state']['subject'],'Edited subject')
        self.assertIn('Latest edit',updated['state']['body'])
        self.assertEqual(self.driver.execute_script('return window.pickerClicks;'),0)

    def test_contact_reachout_dry_run_loads_local_json_and_checks_the_completed_draft(self):
        process = shared.COPYRIGHT_REACHOUT_PROCESS
        self.write_library([{'id': 'reachout', 'name': process.salesforce_template,
            'subject': 'Order #[ORDER-NUMBER] - License Required',
            'body_html': '<p>While reviewing your order, <strong>[REASON]</strong> may be protected by copyright.</p>'}])
        self.open_fixture()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(composer, 'default_template_store', return_value=self.store))
            for name in ('_open_salesforce_account', '_verify_salesforce_email',
                         '_click_salesforce_email', '_wait_for_email_composer', '_set_salesforce_from_orders'):
                stack.enter_context(mock.patch.object(shared, name))
            send = stack.enter_context(mock.patch.object(shared, '_click_salesforce_send_button'))
            result = shared._prepare_and_maybe_send_salesforce_email(
                self.driver, 'crm', '1234567', 'customer@example.test', True,
                process=process, reason='Test University', contact={'salesforce_label': 'Salesforce Contact'})
        self.assertEqual(result['fill']['source'], 'local_json')
        self.assertEqual(result['from'], 'Orders <orders@rushordertees.com>')
        self.assertEqual(result['recipients']['to'], ['customer@example.test'])
        self.assertEqual(result['recipients']['cc'], [])
        self.assertEqual(result['recipients']['bcc'], [])
        self.assertEqual(result['email_state']['subject'], 'Order #1234567 - License Required')
        self.assertIn('Test University may be protected by copyright', result['email_state']['body'])
        self.assertTrue(result['dry_run'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.driver.execute_script('return window.pickerClicks;'), 0)
        self.assertEqual(self.driver.find_element('id', 'sentHtml').text, '')
        send.assert_not_called()

    def test_contact_draft_with_wrong_or_extra_recipients_or_senders_never_sends(self):
        process = shared.COPYRIGHT_REACHOUT_PROCESS
        self.write_library([{'id': 'reachout', 'name': process.salesforce_template,
            'subject': 'Order #[ORDER-NUMBER]',
            'body_html': '<p>While reviewing your order, [REASON] may be protected by copyright.</p>'}])
        mutations = (
            "document.querySelector('[data-email]').setAttribute('data-email', 'other@example.test'); document.querySelector('[data-email]').title = 'other@example.test';",
            "document.querySelector('[data-email]').setAttribute('data-email', 'customer@example.test, other@example.test');",
            "document.querySelector('select option').textContent = 'Other <other@example.test>';",
            "document.querySelector('select option').textContent += ' Orders <orders@rushordertees.com>';",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), ExitStack() as stack:
                self.open_fixture()
                self.driver.execute_script(mutation)
                stack.enter_context(mock.patch.object(composer, 'default_template_store', return_value=self.store))
                for name in ('_open_salesforce_account', '_verify_salesforce_email',
                             '_click_salesforce_email', '_wait_for_email_composer', '_set_salesforce_from_orders'):
                    stack.enter_context(mock.patch.object(shared, name))
                send = stack.enter_context(mock.patch.object(shared, '_click_salesforce_send_button'))
                with self.assertRaisesRegex((RuntimeError, shared.CopyrightCancelError), 'recipient verification failed|From is not Orders|exactly one Orders sender'):
                    shared._prepare_and_maybe_send_salesforce_email(
                        self.driver, 'crm', '1234567', 'customer@example.test', False,
                        process=process, reason='Test University', contact={'salesforce_label': 'Salesforce Contact'})
                self.assertEqual(self.driver.find_element('id', 'sentHtml').text, '')
                send.assert_not_called()

    def test_invoice_placeholder_can_be_a_link_target_without_showing_the_url_as_text(self):
        self.write_library([{'id':'invoice','name':sleeves.SLEEVE_PRINTS_PROCESS.salesforce_template,
            'subject':'Order #[ORDER_NUMBER]','body_html':'<p>[REQUEST] [REQUEST] [COST]</p><p><a href="[INVOICE_LINK]">Pay invoice</a></p>'}])
        self.open_fixture()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(composer,'default_template_store',return_value=self.store))
            for name in ('_open_salesforce_account','_verify_salesforce_email','_click_salesforce_email','_wait_for_email_composer','_set_salesforce_from_orders'):
                stack.enter_context(mock.patch.object(shared,name))
            stack.enter_context(mock.patch.object(sleeves,'_verify_final_recipients',return_value={}))
            result=sleeves._prepare_and_send_salesforce_email(self.driver,'crm','1234567','customer@example.test','side prints','$0.00','https://example.test/invoice?a=1&b=2',dry_run=True)
        self.assertNotIn('https://',result['body'])
        self.assertEqual(result['replacement']['invoice_link_targets'],1)
        self.driver.find_element('id','send').click()
        self.assertIn('href="https://example.test/invoice?a=1&amp;b=2"',self.driver.find_element('id','sentHtml').text)

    def test_additional_request_replacements_keep_bold_font_styles_and_clickable_invoice(self):
        self.write_library([{'id':'requests','name':'[AUTO] Additional Requests','subject':'Order #[ORDER_NUMBER]',
            'body_html':'<p>Requested <strong>[REQUEST]</strong>. Price <strong>[COST]</strong>.</p><p>Continue with <span style="font-size:24px"><strong>[REQ<i>UEST]</i></strong></span>.</p><p>[INVOICE_LINK]</p>'}])
        self.open_fixture()
        composer.fill_template(self.driver,'[AUTO] Additional Requests',store=self.store)
        counts=sleeves._replace_additional_request_placeholders(self.driver,'sleeve prints','$0.00','https://example.test/invoice')
        self.assertEqual(counts['[REQUEST]'],2)
        self.driver.find_element('id','send').click()
        saved=self.driver.find_element('id','sentHtml').text
        bold=self.driver.execute_script("return Array.from(document.getElementById('shadowEditor').shadowRoot.querySelector('iframe').contentWindow.CKEDITOR.instances.editor.document.$.querySelectorAll('strong')).map(x=>x.textContent);")
        self.assertEqual(bold.count('sleeve prints'),2)
        self.assertIn('<strong>$0.00</strong>',saved)
        self.assertIn('font-size:24px',saved)
        self.assertIn('<a href="https://example.test/invoice">https://example.test/invoice</a>',saved)

    def test_stock_size_and_color_grammar_keeps_formatted_product_and_offer_placeholders(self):
        for kind in ('color','size'):
            token='[COLOR]' if kind=='color' else '[SIZE]'
            with self.subTest(kind=kind), ExitStack() as stack:
                if kind=='size':stack.enter_context(size._size_workflow())
                self.write_library([{'id':'stock','name':'[AUTO] Stock','subject':'Order #[ORDER-NUMBER]',
                    'body_html':'<p>Stock check.</p><p>The <strong>[STOCK]</strong> is currently out of stock.</p><p>Offer <span style="font-size:24px"><strong>'+token+'</strong></span>.</p>'}])
                self.open_fixture()
                composer.fill_template(self.driver,'[AUTO] Stock',store=self.store)
                counts=color._replace_stock_color_placeholders(self.driver,'DM130 red Tees','Navy' if kind=='color' else 'Large')
                self.assertEqual(counts['grammar_adjustments'],1)
                self.driver.find_element('id','send').click()
                saved=self.driver.find_element('id','sentHtml').text
                self.assertIn('<strong>DM130 red Tees</strong> cannot currently be supplied',saved)
                self.assertIn('<strong>'+('Navy' if kind=='color' else 'Large')+'</strong>',saved)
                self.assertIn('font-size:24px',saved)

    def test_all_sixteen_template_routes_replace_placeholders_in_the_saved_html_model(self):
        processes=[p for p in shared.CANCEL_PROCESSES if p.salesforce_template]
        processes += [shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS]
        cases=[('general',p) for p in processes]
        cases += [('sleeves',sleeves.SLEEVE_PRINTS_PROCESS),('sleeves',sleeves.COMP_SLEEVES_PROCESS),
                  ('extension',extension.STOCK_EXTENSION_PROCESS),('color',color.STOCK_COLOR_PROCESS),('size',size.STOCK_SIZE_PROCESS)]
        self.assertEqual(len(cases),16)
        for index,(kind,process) in enumerate(cases):
            with self.subTest(kind=kind,template=process.salesforce_template):
                body='<p>Hello, '+html.escape(' '.join(process.body_markers))+'</p>'
                if process.replace_body_placeholder_with_reason:
                    body+='<p><strong>[REASON]</strong></p>'
                if process.key in (shared.COPYRIGHT_CANCEL_PROCESS.key,shared.COPYRIGHT_CANCEL_PARTIAL_PROCESS.key,shared.COMPLICATED_EMB_FEEDBACK_PROCESS.key):
                    body+='<p><strong>[DESIGN]</strong></p>'
                if process.key in (shared.COMPLICATED_EMB_TO_HDD_PROCESS.key,shared.OVERSIZE_EMB_TO_HDD_PROCESS.key):
                    body+='<p>[PRINT] for &quot;[DESIGN]&quot;</p>'
                if kind=='sleeves' and process==sleeves.SLEEVE_PRINTS_PROCESS:
                    body+='<p>[REQUEST] [REQUEST] [COST] [INVOICE_LINK]</p>'
                if kind=='extension':
                    body+='<p>[STOCK] [DAYS]</p>'
                if kind in ('color','size'):
                    body+='<p>The [STOCK] is currently out of stock. Available '+('[COLOR]' if kind=='color' else '[SIZE]')+'</p>'
                body+='<p><a href="https://example.test/guide">Guide</a></p><img src="https://images.example.test/logo.png" alt="Logo">'
                self.write_library([{'id':'test_'+str(index),'name':process.salesforce_template,
                    'subject':'Order #[ORDER-NUMBER] - '+' '.join(process.subject_markers),'body_html':'<html><body>'+body+'</body></html>'}])
                self.open_fixture()
                designs=[{'tab_number':1,'design_name':'Test Artwork','print_areas':['Back']}]
                products=[{'style':'DM130','description':'Test Tees','color':'Red','affected_sizes':['M'],'available_sizes':['M']}]
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(composer,'default_template_store',return_value=self.store))
                    for name in ('_open_salesforce_account','_verify_salesforce_email','_click_salesforce_email','_wait_for_email_composer','_set_salesforce_from_orders'):
                        stack.enter_context(mock.patch.object(shared,name))
                    stack.enter_context(mock.patch.object(sleeves,'_verify_final_recipients',return_value={}))
                    stack.enter_context(mock.patch.object(extension,'_verify_final_recipients',return_value={}))
                    # Color imports the extension through its existing legacy module alias.
                    stack.enter_context(mock.patch.object(color.extension,'_verify_final_recipients',return_value={}))
                    if kind=='general':
                        result=shared._fill_salesforce_email_from_salesforce_template(self.driver,'1234567',process=process,reason='Test Reason',designs=designs)
                        state=result['state']
                    elif kind=='sleeves':
                        result=sleeves._prepare_and_send_salesforce_email(self.driver,'crm','1234567','customer@example.test','sleeve prints','$0.00','https://example.test/invoice',process=process,dry_run=True)
                        state=result
                    else:
                        worker=extension if kind=='extension' else color
                        if kind=='size':
                            stack.enter_context(size._size_workflow())
                        result=worker._prepare_and_send_salesforce_email(self.driver,'crm','1234567','customer@example.test','DM130 red Test Tees',5 if kind=='extension' else 'Navy' if kind=='color' else 'Large',products,{},dry_run=True)
                        state=result
                self.assertIn('1234567',state['subject'])
                self.assertTrue(state['body'])
                self.assertEqual(result.get('source') or result.get('template_source'),'local_json')
                self.driver.find_element('id','send').click()
                saved=self.driver.find_element('id','sentHtml').text
                self.assertIn('href="https://example.test/guide"',saved)
                self.assertIn('src="https://images.example.test/logo.png"',saved)
                self.assertNotRegex(saved,r'\[(?:REASON|DESIGN|PRINT|REQUEST|COST|INVOICE_LINK|STOCK|DAYS|COLOR|SIZE|ORDER[-_]NUMBER)\]')
                self.assertEqual(self.driver.execute_script('return window.pickerClicks;'),0)


if __name__=='__main__':
    unittest.main()
