import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'workers'))
from ssactivewear_bridge import SxsBridge, SxsBridgeError
from routes.ssactivewear_bridge_routes import register_ssactivewear_bridge_routes, PREFIX
from ssactivewear_extension import SxsExtensionBrowser

ORIGIN = 'chrome-extension://' + 'a' * 32
CLIENT = '1' * 32
READY = {'success': True, 'signed_in': True, 'mode': 'background'}


class SxsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.store = SxsBridge(Path(self.temp.name) / 'bridge.sqlite3', clock=lambda: self.now)
        self.store.register(CLIENT, ORIGIN, probe=READY)

    def test_restart_never_redelivers_a_dispatched_purchase(self):
        sid = self.store.open_session('5000001')
        cid = self.store.enqueue(sid, 'confirm_submit', {'po': '5000001'})
        self.assertEqual(self.store.poll(CLIENT, ORIGIN)['id'], cid)
        reopened = SxsBridge(self.store.path, clock=lambda: self.now)
        self.assertIsNone(reopened.poll(CLIENT, ORIGIN))
        reopened.complete(CLIENT, ORIGIN, cid, {'click_issued': True})
        self.assertEqual(reopened.result(cid), (True, {'click_issued': True}))

    def test_expired_command_requires_review_and_late_result_is_rejected(self):
        sid = self.store.open_session('5000001')
        cid = self.store.enqueue(sid, 'add', {'quantity': 2}, timeout=5)
        self.store.poll(CLIENT, ORIGIN)
        self.now += 6
        with self.assertRaisesRegex(SxsBridgeError, 'will not be repeated'):
            self.store.result(cid)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT state FROM commands WHERE id=?', (cid,)).fetchone()['state'], 'uncertain')
        self.assertIsNone(self.store.poll(CLIENT, ORIGIN))
        with self.assertRaisesRegex(SxsBridgeError, 'expired'):
            self.store.complete(CLIENT, ORIGIN, cid, {})

    def test_multiple_browsers_or_active_session_stop_selection(self):
        sid = self.store.open_session('5000001')
        with self.assertRaisesRegex(SxsBridgeError, 'active Reorder'):
            self.store.open_session('5000002')
        self.store.close(sid)
        self.store.register('2' * 32, ORIGIN, probe=READY)
        with self.assertRaisesRegex(SxsBridgeError, 'exactly one'):
            self.store.open_session('5000002')

    def test_manual_probe_is_optional_but_cannot_replace_an_active_run(self):
        sid = self.store.open_session('5000001')
        with self.assertRaisesRegex(SxsBridgeError, 'active'):
            self.store.register(CLIENT, ORIGIN, probe={'success': False})
        self.store.close(sid)
        self.store.register(CLIENT, ORIGIN, probe={'success': True, 'signed_in': False})
        sid = self.store.open_session('5000001')
        self.assertEqual(self.store.poll(CLIENT, ORIGIN), None)
        self.store.close(sid)
        self.store.register(CLIENT, ORIGIN, probe={'success': False, 'signed_in': True})
        self.assertFalse(self.store.status()['ready'])

    def test_prepare_can_only_be_the_first_command_and_is_never_redelivered(self):
        sid = self.store.open_session('5000001')
        cid = self.store.enqueue(sid, 'prepare')
        self.assertEqual(self.store.poll(CLIENT, ORIGIN)['id'], cid)
        self.assertIsNone(self.store.poll(CLIENT, ORIGIN))
        with self.assertRaisesRegex(SxsBridgeError, 'only once'):
            self.store.enqueue(sid, 'prepare')
        self.store.close(sid)
        sid = self.store.open_session('5000002')
        self.store.enqueue(sid, 'navigate', {'url': 'https://www.ssactivewear.com/checkout/cart'})
        with self.assertRaisesRegex(SxsBridgeError, 'only once'):
            self.store.enqueue(sid, 'prepare')

    def test_origin_and_purchase_po_boundaries(self):
        sid = self.store.open_session('5000001')
        with self.assertRaisesRegex(SxsBridgeError, 'PO'):
            self.store.enqueue(sid, 'confirm_submit', {'po': '5000002'})
        for url in ('https://www.ssactivewear.com.evil.test/checkout/cart', 'https://user@www.ssactivewear.com/', 'http://www.ssactivewear.com/', 'https://www.ssactivewear.com/checkout/submit'):
            with self.subTest(url=url), self.assertRaises(SxsBridgeError):
                self.store.enqueue(sid, 'navigate', {'url': url})
        with self.assertRaises(SxsBridgeError):
            self.store.enqueue(sid, 'eval', {'code': 'anything'})
        cid = self.store.enqueue(sid, 'health')
        self.store.poll(CLIENT, ORIGIN)
        with self.assertRaisesRegex(SxsBridgeError, 'another browser'):
            self.store.complete('2' * 32, ORIGIN, cid, {})
        with self.assertRaisesRegex(SxsBridgeError, 'origin changed'):
            self.store.register(CLIENT, 'chrome-extension://' + 'b' * 32)

    def test_disconnect_and_stale_client_cannot_receive_work(self):
        sid = self.store.open_session('5000001')
        self.store.register(CLIENT, ORIGIN, enabled=False)
        with self.assertRaises(SxsBridgeError):
            self.store.enqueue(sid, 'health')
        with self.assertRaises(SxsBridgeError):
            self.store.poll(CLIENT, ORIGIN)
        # Delayed heartbeats/probe results must not undo an explicit Disconnect.
        self.store.register(CLIENT, ORIGIN, enabled=None, probe=READY)
        self.assertEqual(self.store.status()['connected_browsers'], 0)
        self.store.register(CLIENT, ORIGIN, probe=READY)
        self.now += 76
        with self.assertRaisesRegex(SxsBridgeError, 'exactly one'):
            self.store.open_session('5000001')

    def test_result_retries_are_idempotent_but_conflicts_are_rejected(self):
        sid = self.store.open_session('5000001')
        cid = self.store.enqueue(sid, 'health')
        self.store.poll(CLIENT, ORIGIN)
        self.store.complete(CLIENT, ORIGIN, cid, {'signed_in': True})
        self.store.complete(CLIENT, ORIGIN, cid, {'signed_in': True})
        with self.assertRaisesRegex(SxsBridgeError, 'Conflicting'):
            self.store.complete(CLIENT, ORIGIN, cid, {'signed_in': False})

    def test_routes_reject_web_remote_and_mismatched_extension_identity(self):
        app = Flask(__name__)
        def allowed():
            return request.remote_addr in {'127.0.0.1', '::1'} and request.headers.get('Origin') in {None, ORIGIN}
        register_ssactivewear_bridge_routes(app, is_local_extension=allowed, response=lambda payload, status=200: (jsonify(payload), status), broker=self.store)
        client = app.test_client()
        data = {'client_id': CLIENT, 'extension_id': 'a' * 32, 'enabled': True}
        for path in ('/connect', '/poll', '/result'):
            with self.subTest(path=path):
                self.assertEqual(client.open(PREFIX + path, method='OPTIONS', headers={'Origin': 'https://evil.test'}).status_code, 403)
                remote = client.get(PREFIX + path, query_string=data, environ_overrides={'REMOTE_ADDR': '192.0.2.1'}) if path == '/poll' else client.post(PREFIX + path, json=data, environ_overrides={'REMOTE_ADDR': '192.0.2.1'})
                self.assertEqual(remote.status_code, 403)
        self.assertEqual(client.post(PREFIX + '/connect', json={**data, 'extension_id': 'b' * 32}, headers={'Origin': ORIGIN}).status_code, 409)
        self.assertEqual(client.post(PREFIX + '/connect', json=data, headers={'Origin': ORIGIN}).status_code, 200)
        sid = self.store.open_session('5000001')
        cid = self.store.enqueue(sid, 'health')
        response = client.get(PREFIX + '/poll', query_string=data, headers={'Origin': ORIGIN})
        self.assertEqual(response.get_json()['command']['id'], cid)
        result = client.post(PREFIX + '/result', json={**data, 'command_id': cid, 'result': {'url': 'https://www.ssactivewear.com/checkout/cart'}}, headers={'Origin': ORIGIN})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.post(PREFIX + '/enqueue', json=data).status_code, 404)


class SxsExtensionAdapterTests(unittest.TestCase):
    def test_stale_or_unknown_running_extension_stops_before_stock_actions(self):
        for version in (None, '1.7.7', '1.7.12', '1.7.13', '1.7.14', '1.7.14.1', 'invalid'):
            with self.subTest(version=version):
                session = mock.Mock()
                session.call.return_value = {'url': 'https://www.ssactivewear.com/checkout/cart', 'signed_in': True, 'cart_container': True, 'extension_version': version}
                browser = SxsExtensionBrowser('5000001', session=session)
                with self.assertRaisesRegex(RuntimeError, 'Reload CRM Order Assistant 1.7.15'):
                    browser.ensure_login()
                self.assertEqual([c.args[0] for c in session.call.call_args_list], ['prepare'])
        for version in ('1.7.15', '1.7.16', '1.7.15.1'):
            with self.subTest(version=version):
                session = mock.Mock()
                session.call.return_value = {'url': 'https://www.ssactivewear.com/checkout/cart', 'signed_in': True, 'cart_container': True, 'extension_version': version}
                SxsExtensionBrowser('5000001', session=session).ensure_login()

    def test_automatic_setup_checks_signin_before_any_stock_operation(self):
        session = mock.Mock()
        session.call.return_value = {'url': 'https://www.ssactivewear.com/myaccount/login', 'signed_in': False, 'extension_version': '1.7.15'}
        browser = SxsExtensionBrowser('5000001', session=session)
        with self.assertRaisesRegex(RuntimeError, 'sign-in expired or is missing'):
            browser.ensure_login()
        self.assertEqual([c.args[0] for c in session.call.call_args_list], ['prepare'])

    def test_repeated_login_checks_do_not_repeat_window_setup(self):
        session = mock.Mock()
        session.call.return_value = {'url': 'https://www.ssactivewear.com/checkout/cart', 'signed_in': True, 'cart_container': True, 'extension_version': '1.7.15'}
        browser = SxsExtensionBrowser('5000001', session=session)
        browser.ensure_login()
        browser.ensure_login()
        self.assertEqual([c.args[0] for c in session.call.call_args_list], ['prepare', 'navigate', 'health'])

    def test_stock_addition_waits_for_the_expected_cart_quantity_without_readding(self):
        session = mock.Mock()
        session.call.return_value = {'url': 'https://www.ssactivewear.com/checkout/cart', 'rows': [{'quantity': 1}]}
        browser = SxsExtensionBrowser('5000001', session=session)
        browser.fill_product([{'warehouse': 'Reading, PA', 'quantities': {'L': 1}}])
        calls = session.call.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['fill', 'add', 'navigate', 'cart'])
        self.assertEqual(calls[-1].args[1], {'expected_quantity': 1})

    def test_checkout_navigation_port_loss_reads_the_new_page_without_repeating_click(self):
        session = mock.Mock()
        review = {'url': 'https://www.ssactivewear.com/checkout/', 'po': '5000001', 'address': '2727 Commerce Way Philadelphia, PA 19154', 'payment': 'Net 60', 'methods': ['11/04/2026 UPS Ground']}
        calls = []
        def call(op, payload):
            calls.append(op)
            if op == 'verified_cart': return {'url': 'https://www.ssactivewear.com/checkout/cart', 'rows': []}
            if op == 'checkout': raise SxsBridgeError('The page keeping the extension port is moved into back/forward cache, so the message channel is closed.')
            if op == 'health': return {**review, 'signed_in': True, 'checkout_ready': True}
            if op == 'review': return review
            self.fail(op)
        session.call.side_effect = call
        browser = SxsExtensionBrowser('5000001', session=session)
        line = {'selected': {'url': 'https://www.ssactivewear.com/p/gildan/5000'}, 'product': {'color': 'Black'}, 'quantities': {'L': 1}, 'warehouse': 'Reading, PA'}
        with mock.patch('ssactivewear_ordering.validate_cart'):
            self.assertEqual(browser.checkout('5000001', [line])['po'], '5000001')
        self.assertEqual(calls, ['verified_cart', 'checkout', 'health', 'review'])
        self.assertEqual(browser.owned_lines, [line])

    def test_checkout_login_redirect_stops_for_normal_signin_without_retrying_click(self):
        session = mock.Mock()
        session.call.side_effect = [
            {'url': 'https://www.ssactivewear.com/checkout/cart', 'rows': []},
            {'url': 'https://www.ssactivewear.com/checkout/cart'},
            {'url': 'https://www.ssactivewear.com/myaccount/login?Next=/checkout', 'signed_in': True},
        ]
        browser = SxsExtensionBrowser('5000001', session=session)
        line = {'selected': {'url': 'https://www.ssactivewear.com/p/gildan/5000'}, 'product': {'color': 'Black'}, 'quantities': {'L': 1}, 'warehouse': 'Reading, PA'}
        with mock.patch('ssactivewear_ordering.validate_cart'):
            with self.assertRaisesRegex(RuntimeError, 'sign-in again'):
                browser.checkout('5000001', [line])
        self.assertEqual([c.args[0] for c in session.call.call_args_list], ['verified_cart', 'checkout', 'health'])
        self.assertIsNone(browser.owned_lines)

    def test_cart_handoff_excludes_unneeded_crm_and_inventory_data(self):
        from datetime import date
        session = mock.Mock()
        def call(operation, payload):
            json.dumps(payload)  # Native CRM date values must never reach the bridge.
            return {'url': 'https://www.ssactivewear.com/checkout/cart', 'rows': []}
        session.call.side_effect = call
        browser = SxsExtensionBrowser('5000001', session=session)
        line = {'selected': {'url': 'https://www.ssactivewear.com/p/gildan/5000'},
                'product': {'color': 'Black', 'customer_email': 'private@example.test'},
                'quantities': {'L': 1}, 'warehouse': 'Reading, PA',
                'inventory': [{'eta': date.today()}]}
        self.assertEqual(browser.verified_cart_rows([line]), [])
        payload = session.call.call_args.args[1]
        self.assertNotIn('customer_email', json.dumps(payload))
        self.assertNotIn('inventory', json.dumps(payload))

    def test_reorder_uses_extension_and_releases_session_without_closing_chrome(self):
        from contextlib import nullcontext
        import crm_reorder as worker
        browser = mock.Mock(is_sxs_extension=True)
        crm = mock.Mock()
        def process(_crm, vendors, _order_id, **_kwargs):
            self.assertIs(vendors('ssactivewear'), browser)
            self.assertIs(vendors('ssactivewear'), browser)
            return {'message': 'Fixture complete'}
        with mock.patch.object(worker.bypass, '_worker_profile_lock', return_value=nullcontext()), mock.patch.object(
            worker.bypass, '_build_crm_session_driver', return_value=crm
        ), mock.patch.object(worker, 'SxsExtensionBrowser', return_value=browser) as extension, mock.patch.object(
            worker, 'process_order', side_effect=process
        ), mock.patch.object(worker, 'write_status_payload'), mock.patch.object(worker, 'write_result_payload'), mock.patch.object(
            worker, 'safe_driver_quit'
        ) as quit_driver:
            self.assertTrue(worker.run('5000001', dry_run=True))
        extension.assert_called_once_with('5000001')
        browser.close.assert_called_once()
        quit_driver.assert_called_once_with(crm, profile_path=worker.bypass.PROFILE_PATH)

    def test_lost_final_click_response_only_reads_confirmation_and_never_clicks_twice(self):
        session = mock.Mock()
        review = {'url': 'https://www.ssactivewear.com/checkout', 'po': '5000001', 'address': '2727 Commerce Way Philadelphia, PA 19154', 'payment': 'Net 60', 'methods': ['11/03/2026 UPS Ground']}
        calls = []
        def call(op, payload):
            calls.append(op)
            if op == 'review': return review
            if op == 'begin_submit': return {'url': review['url']}
            if op == 'confirm_submit': raise SxsBridgeError('response lost')
            return {'url': 'https://www.ssactivewear.com/checkout/orderconfirmation', 'text': 'Order Confirmation: 98765432 PO Number: 5000001'}
        session.call.side_effect = call
        browser = SxsExtensionBrowser('5000001', session=session)
        result = browser.submit('5000001')
        self.assertEqual(result['order_number'], '98765432')
        self.assertEqual(calls, ['review', 'begin_submit', 'confirm_submit', 'confirmation'])

    def test_owned_cart_is_not_emptied_if_remaining_stock_changes(self):
        browser = SxsExtensionBrowser('5000001', session=mock.Mock())
        browser.owned_lines = [{'fixture': True}]
        row = {'quantity': 1}
        with mock.patch.object(browser, 'get'), mock.patch.object(browser, 'verified_cart_rows', return_value=[row]), mock.patch.object(browser, 'cart_rows', return_value=[row, row]), mock.patch.object(browser, 'call', return_value={'rows': [{'quantity': 2}]}), mock.patch('ssactivewear_ordering.validate_cart'):
            with self.assertRaisesRegex(RuntimeError, 'removal was not confirmed'):
                browser.empty_owned_cart()

    def test_checkout_values_and_session_po_are_checked_in_python(self):
        browser = SxsExtensionBrowser('5000001', session=mock.Mock())
        with self.assertRaisesRegex(RuntimeError, 'PO changed'):
            browser.submit('5000002')
        with self.assertRaisesRegex(RuntimeError, 'did not verify'):
            browser._validate_review({'po': '5000001', 'payment': 'Net 60', 'address': '2727 Commerce Way Philadelphia PA 19154', 'methods': ['UPS Next Day']}, '5000001')


if __name__ == '__main__':
    unittest.main()
