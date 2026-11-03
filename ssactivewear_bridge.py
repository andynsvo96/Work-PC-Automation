"""Local, durable command handoff between a Reorder subprocess and its extension.

Only the local worker creates sessions/commands. HTTP routes allow an opted-in
extension to claim a command once and return its result; they cannot enqueue work.
"""
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from runtime_paths import state_file

PROTOCOL = 'automation.ssactivewear.extension/v1'
OPERATIONS = frozenset({'navigate', 'health', 'candidates', 'color', 'inventory',
                        'cart', 'verified_cart', 'fill', 'add', 'remove', 'checkout',
                        'review', 'valid', 'begin_submit', 'confirm_submit', 'confirmation'})
PURCHASE_OPERATIONS = frozenset({'begin_submit', 'confirm_submit'})


def valid_navigation(url):
    p = urlsplit(str(url or ''))
    return (p.scheme == 'https' and p.netloc == 'www.ssactivewear.com'
            and not p.query and not p.fragment and (
                p.path in {'/', '/myaccount/login', '/checkout/cart'}
                or re.fullmatch(r'/ps/[A-Za-z0-9_.-]+', p.path)
                or re.fullmatch(r'/p/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', p.path)))


class SxsBridgeError(RuntimeError):
    pass


class SxsBridge:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path or state_file('ssactivewear_extension.sqlite3'))
        self.clock = clock

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS clients (
                  id TEXT PRIMARY KEY, origin TEXT NOT NULL, seen REAL NOT NULL,
                  enabled INTEGER NOT NULL, probe TEXT);
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, order_id TEXT NOT NULL,
                  lease REAL NOT NULL, active INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, operation TEXT NOT NULL,
                  payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
                  error TEXT, deadline REAL NOT NULL);
            ''')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def register(self, client_id, origin='', enabled=True, probe=None):
        if not re.fullmatch(r'[a-f0-9]{32}', str(client_id)):
            raise SxsBridgeError('Invalid S&S browser connection identifier.')
        safe_probe = None
        if probe is not None:
            if not isinstance(probe, dict):
                raise SxsBridgeError('Invalid S&S browser check.')
            safe_probe = json.dumps({key: probe.get(key) for key in
                                    ('success', 'signed_in', 'mode', 'message', 'checked_at')})
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM clients WHERE id=?', (client_id,)).fetchone()
            if old and old['origin'] != origin:
                raise SxsBridgeError('S&S browser connection origin changed.')
            if enabled is None:
                if old is None:
                    raise SxsBridgeError('Connect the S&S browser before running a check.')
                enabled = bool(old['enabled'])
            if safe_probe is not None and db.execute('SELECT id FROM sessions WHERE client_id=? AND active=1 AND lease>?', (client_id, self.clock())).fetchone():
                raise SxsBridgeError('An S&S Reorder is active. Wait before opening sign-in or running a browser check.')
            db.execute('''INSERT INTO clients VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET seen=excluded.seen, enabled=excluded.enabled,
                probe=COALESCE(excluded.probe,clients.probe)''',
                       (client_id, origin, self.clock(), int(bool(enabled)), safe_probe))
            if not enabled:
                db.execute('UPDATE sessions SET active=0 WHERE client_id=?', (client_id,))

    def open_session(self, order_id):
        if not re.fullmatch(r'\d{7}', str(order_id)):
            raise SxsBridgeError('S&S Reorder requires one seven-digit order number.')
        now = self.clock()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE sessions SET active=0 WHERE lease<?', (now,))
            clients = db.execute('SELECT * FROM clients WHERE enabled=1 AND seen>?', (now - 75,)).fetchall()
            if len(clients) != 1:
                raise SxsBridgeError('Connect exactly one S&S browser using CRM Order Assistant, then run its read-only check. Sign in to S&S normally.')
            probe = json.loads(clients[0]['probe'] or '{}')
            if probe.get('success') is not True or probe.get('signed_in') is not True:
                raise SxsBridgeError('Run the extension S&S browser check after signing in. A successful signed-in check is required before Reorder.')
            if db.execute('SELECT id FROM sessions WHERE active=1').fetchone():
                raise SxsBridgeError('The S&S extension browser already belongs to an active Reorder.')
            sid = uuid.uuid4().hex
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,1)', (sid, clients[0]['id'], str(order_id), now + 90))
        return sid

    def enqueue(self, session_id, operation, payload=None, timeout=60):
        if operation not in OPERATIONS or not isinstance(payload or {}, dict):
            raise SxsBridgeError('Unsupported S&S browser command.')
        payload = payload or {}
        encoded = json.dumps(payload, separators=(',', ':'))
        if len(encoded) > 150000:
            raise SxsBridgeError('S&S browser command is too large.')
        if operation == 'navigate' and not valid_navigation(payload.get('url')):
            raise SxsBridgeError('Unexpected S&S navigation URL.')
        with self.db() as db:
            session = db.execute('SELECT * FROM sessions WHERE id=? AND active=1 AND lease>?', (session_id, self.clock())).fetchone()
            if session is None:
                raise SxsBridgeError('S&S browser session expired. Review the current cart before retrying.')
            if operation in PURCHASE_OPERATIONS and payload.get('po') != session['order_id']:
                raise SxsBridgeError('S&S purchase PO does not match this Reorder.')
            cid = uuid.uuid4().hex
            db.execute('INSERT INTO commands VALUES (?,?,?,?,\'pending\',NULL,NULL,?)',
                       (cid, session_id, operation, encoded, self.clock() + timeout))
            db.execute('UPDATE sessions SET lease=? WHERE id=?', (self.clock() + timeout + 90, session_id))
        return cid

    def poll(self, client_id, origin=''):
        now = self.clock()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            client = db.execute('SELECT * FROM clients WHERE id=? AND origin=? AND enabled=1', (client_id, origin)).fetchone()
            if client is None:
                raise SxsBridgeError('S&S browser is not connected.')
            if now - client['seen'] >= 15:
                db.execute('UPDATE clients SET seen=? WHERE id=?', (now, client_id))
            row = db.execute('''SELECT c.*, s.order_id FROM commands c JOIN sessions s ON s.id=c.session_id
                WHERE s.client_id=? AND s.active=1 AND s.lease>? AND c.state='pending'
                AND c.deadline>? ORDER BY c.rowid LIMIT 1''', (client_id, now, now)).fetchone()
            if row is None:
                return None
            # Never put a dispatched command back into pending, even after a
            # service-worker crash. Unknown purchase clicks require review.
            db.execute("UPDATE commands SET state='dispatched' WHERE id=?", (row['id'],))
            return {'id': row['id'], 'session_id': row['session_id'], 'order_id': row['order_id'],
                    'operation': row['operation'], 'payload': json.loads(row['payload']), 'deadline': row['deadline']}

    def complete(self, client_id, origin, command_id, result=None, error=None):
        encoded = json.dumps(result, separators=(',', ':'))
        if len(encoded) > 250000 or (error is not None and not isinstance(error, str)):
            raise SxsBridgeError('Invalid S&S browser result.')
        with self.db() as db:
            row = db.execute('''SELECT c.*, s.client_id, x.origin FROM commands c
                JOIN sessions s ON s.id=c.session_id JOIN clients x ON x.id=s.client_id
                WHERE c.id=?''', (str(command_id),)).fetchone()
            if not row or row['client_id'] != client_id or row['origin'] != origin:
                raise SxsBridgeError('S&S result belongs to another browser session.')
            if row['state'] == 'done':
                if row['result'] != encoded or row['error'] != error:
                    raise SxsBridgeError('Conflicting S&S browser result.')
                return
            if row['state'] != 'dispatched' or row['deadline'] < self.clock():
                raise SxsBridgeError('S&S command expired; review its outcome before retrying.')
            db.execute("UPDATE commands SET state='done',result=?,error=? WHERE id=?", (encoded, (error or '')[:1000] or None, command_id))

    def result(self, command_id):
        expired = False
        with self.db() as db:
            row = db.execute('SELECT * FROM commands WHERE id=?', (command_id,)).fetchone()
            if row is None:
                raise SxsBridgeError('S&S command was not found.')
            if row['state'] == 'done':
                if row['error']:
                    raise SxsBridgeError(row['error'])
                return True, json.loads(row['result'])
            if row['deadline'] < self.clock():
                db.execute("UPDATE commands SET state='uncertain' WHERE id=?", (command_id,))
                expired = True
        if expired:
            raise SxsBridgeError('S&S browser did not confirm the command. Check its tab and cart; the command will not be repeated.')
        return False, None

    def close(self, session_id):
        with self.db() as db:
            db.execute('UPDATE sessions SET active=0 WHERE id=?', (session_id,))

    def status(self):
        with self.db() as db:
            rows = db.execute('SELECT id,seen,probe FROM clients WHERE enabled=1 AND seen>?', (self.clock() - 75,)).fetchall()
        probe = json.loads(rows[0]['probe'] or '{}') if len(rows) == 1 else {}
        return {'connected_browsers': len(rows), 'ready': probe.get('success') is True and probe.get('signed_in') is True}

    def active_session(self, client_id):
        with self.db() as db:
            row = db.execute('SELECT id,order_id FROM sessions WHERE client_id=? AND active=1 AND lease>?', (client_id, self.clock())).fetchone()
        return dict(row) if row else None


class SxsSession:
    def __init__(self, order_id, broker=None):
        self.broker = broker or SxsBridge()
        self.id = self.broker.open_session(order_id)
        self.order_id = str(order_id)

    def call(self, operation, payload=None, timeout=60):
        cid = self.broker.enqueue(self.id, operation, payload, timeout)
        while True:
            ready, result = self.broker.result(cid)
            if ready:
                return result
            time.sleep(0.2)

    def close(self):
        self.broker.close(self.id)
