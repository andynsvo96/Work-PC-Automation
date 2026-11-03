"""Loopback extension transport; these endpoints never create Reorder tasks."""
import re
import time
from flask import request
from ssactivewear_bridge import PROTOCOL, SxsBridge, SxsBridgeError

PREFIX = '/api/extension/bridge/ssactivewear'


def register_ssactivewear_bridge_routes(app, *, is_local_extension, response, broker=None):
    store = broker or SxsBridge()

    def identity(data):
        extension_id = str(data.get('extension_id') or '')
        if not re.fullmatch(r'[a-p]{32}', extension_id):
            raise SxsBridgeError('A Chrome extension identity is required.')
        canonical = 'chrome-extension://' + extension_id
        if request.headers.get('Origin') not in {None, '', canonical}:
            raise SxsBridgeError('S&S extension origin does not match its connection.')
        return str(data.get('client_id') or ''), canonical

    def guarded(action):
        if not is_local_extension():
            return response({'success': False, 'message': 'S&S extension requests must use loopback and an extension origin.'}, 403)
        if request.method == 'OPTIONS':
            return response({'success': True})
        if request.content_length and request.content_length > 300000:
            return response({'success': False, 'message': 'S&S bridge request is too large.'}, 413)
        data = request.args if request.method == 'GET' else request.get_json(silent=True)
        if not isinstance(data, dict) and request.method != 'GET':
            return response({'success': False, 'message': 'A JSON object is required.'}, 400)
        try:
            client_id, origin = identity(data)
            return response({'success': True, 'protocol': PROTOCOL, **action(data, client_id, origin)})
        except (SxsBridgeError, ValueError, TypeError) as exc:
            return response({'success': False, 'message': str(exc)}, 409)

    def connect(data, client_id, origin):
        enabled = data.get('enabled')
        if enabled is not None and type(enabled) is not bool:
            raise SxsBridgeError('Invalid S&S browser connection state.')
        store.register(client_id, origin, enabled, data.get('probe'))
        return {'status': store.status()}

    def poll(data, client_id, origin):
        deadline = time.monotonic() + 15
        while True:
            command = store.poll(client_id, origin)
            if command or time.monotonic() >= deadline:
                return {'command': command, 'active_session': store.active_session(client_id)}
            time.sleep(0.25)

    def complete(data, client_id, origin):
        store.complete(client_id, origin, data.get('command_id'), data.get('result'), data.get('error'))
        return {}

    app.add_url_rule(PREFIX + '/connect', 'sxs_extension_connect', lambda: guarded(connect), methods=['POST', 'OPTIONS'])
    app.add_url_rule(PREFIX + '/poll', 'sxs_extension_poll', lambda: guarded(poll), methods=['GET', 'OPTIONS'])
    app.add_url_rule(PREFIX + '/result', 'sxs_extension_result', lambda: guarded(complete), methods=['POST', 'OPTIONS'])
