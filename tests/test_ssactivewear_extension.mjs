import assert from 'node:assert/strict';
import test from 'node:test';
import { createSxsWorker, isSxsUrl, allowedNavigation, installSxsWorker } from '../crm-order-dark-mode-extension/ssactivewear-worker.mjs';

const ORIGIN = 'https://www.ssactivewear.com';
function fixture() {
  const local = { sxsReorderBrowser: { enabled: true, clientId: '1'.repeat(32) } }, session = { sxsReorderOwnedTab: { tabId: 7, boundSession: '2'.repeat(32) } };
  const calls = [], tabs = new Map([[7, { id: 7, windowId: 3, status: 'complete', url: ORIGIN + '/checkout/cart' }]]), listeners = [];
  let nextId = 8;
  const storage = values => ({ get: async key => ({ [key]: structuredClone(values[key]) }), set: async data => Object.assign(values, structuredClone(data)), remove: async key => { delete values[key]; } });
  const event = { addListener: fn => listeners.push(fn) };
  const api = {
    runtime: { id: 'a'.repeat(32), onMessage: event, onStartup: event, onInstalled: event },
    storage: { local: storage(local), session: storage(session) },
    permissions: { contains: async () => true },
    alarms: { create: async () => {}, clear: async () => {}, onAlarm: event },
    tabs: {
      get: async id => { if (!tabs.has(id)) throw Error('No tab'); return { ...tabs.get(id) }; },
      update: async (id, patch) => { calls.push(['update', id, patch]); Object.assign(tabs.get(id), patch); return { ...tabs.get(id) }; },
      create: async options => { calls.push(['create', options]); const tab = { id: nextId++, windowId: 3, status: 'complete', ...options }; tabs.set(tab.id, tab); return tab; },
      sendMessage: async (id, message) => { calls.push(['send', id, message]); return { success: true, result: { url: tabs.get(id).url, hidden: true, signed_in: true, cart_container: true, anchor_id: 'cSwatch16787', candidates: [{ url: ORIGIN + '/p/gildan/5000', title: '5000 Gildan Heavy Cotton T-Shirt' }] } }; }
    },
    scripting: { executeScript: async args => { calls.push(['inject', args]); return args.world === 'MAIN' ? [{ result: { success: true, anchor_id: 'cSwatch16787' } }] : []; } },
    windows: { get: async () => ({ state: 'minimized' }), update: async (...args) => calls.push(['window-update', ...args]), create: async options => { calls.push(['window', options]); const tab = await api.tabs.create({ url: options.url, active: false }); return { id: 3, tabs: [tab] }; } }
  };
  const fetcher = async (url, options) => {
    calls.push(['fetch', url, options]);
    return { ok: true, status: 200, json: async () => ({ success: true, protocol: 'automation.ssactivewear.extension/v1' }) };
  };
  const command = (operation, payload = {}) => ({ id: '3'.repeat(32), session_id: '2'.repeat(32), order_id: '5000001', operation, payload, deadline: Date.now() / 1000 + 60 });
  return { api, calls, tabs, local, session, fetcher, command, listeners };
}

test('S&S URLs and navigation are restricted to the exact HTTPS supplier', () => {
  assert.ok(isSxsUrl(ORIGIN + '/checkout/orderconfirmation?orderguid=test'));
  for (const url of ['https://www.ssactivewear.com.evil.test/', 'http://www.ssactivewear.com/', 'https://user@www.ssactivewear.com/', ORIGIN + '/checkout/submit', ORIGIN + '/?download=anything']) assert.equal(allowedNavigation(url), false);
  for (const url of [ORIGIN + '/', ORIGIN + '/checkout/cart', ORIGIN + '/ps/5000', ORIGIN + '/p/gildan/5000']) assert.equal(allowedNavigation(url), true);
});

test('navigation and page operations do not activate tabs or focus windows', async () => {
  const f = fixture(), worker = createSxsWorker(f.api, f.fetcher);
  await worker.execute(f.command('navigate', { url: ORIGIN + '/ps/5000' }));
  await worker.execute(f.command('health'));
  assert.equal(f.calls.filter(c => c[0] === 'window-update').length, 0);
  assert.ok(f.calls.filter(c => c[0] === 'update').every(c => c[2].active !== true));
  assert.equal(f.calls.find(c => c[0] === 'inject')[1].target.tabId, 7);
});

test('a closed owned tab is not replaced during its active session', async () => {
  const f = fixture(); f.tabs.delete(7);
  await assert.rejects(createSxsWorker(f.api, f.fetcher).execute(f.command('health')), /closed or changed/);
  assert.equal(f.calls.filter(c => c[0] === 'create').length, 0);
});

test('a browser restart never adopts a stale local tab ID', async () => {
  const f = fixture();
  delete f.session.sxsReorderOwnedTab;
  Object.assign(f.local.sxsReorderBrowser, { tabId: 7, boundSession: '2'.repeat(32) });
  await assert.rejects(createSxsWorker(f.api, f.fetcher).execute(f.command('health')), /restarted|checked tab/);
  assert.equal(f.calls.filter(c => ['create', 'send', 'inject'].includes(c[0])).length, 0);
});

test('foreign, frozen, and unpermitted tabs stop before page input', async () => {
  for (const mode of ['foreign', 'frozen', 'permission']) {
    const f = fixture();
    if (mode === 'foreign') f.tabs.get(7).url = 'https://example.test/';
    if (mode === 'frozen') f.tabs.get(7).frozen = true;
    if (mode === 'permission') f.api.permissions.contains = async () => false;
    await assert.rejects(createSxsWorker(f.api, f.fetcher).execute(f.command('confirm_submit', { po: '5000001' })));
    assert.equal(f.calls.filter(c => c[0] === 'send').length, 0);
  }
});

test('a lost final click response is not retried by the controller', async () => {
  const f = fixture(); let sends = 0;
  f.api.tabs.sendMessage = async () => { sends++; throw Error('message port closed during navigation'); };
  await assert.rejects(createSxsWorker(f.api, f.fetcher).execute(f.command('confirm_submit', { po: '5000001' })), /port closed/);
  assert.equal(sends, 1);
});

test('color selection uses one fixed page-context handler and verifies its result', async () => {
  const f = fixture();
  const command = f.command('color', { color: 'Black' });
  await createSxsWorker(f.api, f.fetcher).execute(command);
  assert.deepEqual(f.calls.filter(c => c[0] === 'send').map(c => c[2].operation), ['color_ready', 'color']);
  const main = f.calls.filter(c => c[0] === 'inject' && c[1].world === 'MAIN');
  assert.equal(main.length, 1);
  assert.equal(main[0][1].func.name, 'selectSxsColorInMain');
  assert.deepEqual(main[0][1].args, [{ color: 'Black', anchorId: 'cSwatch16787', orderId: '5000001', deadline: command.deadline }]);
  assert.equal(f.calls.filter(c => c[0] === 'window-update').length, 0);
});

test('minimized read-only check uses an unfocused owned window and never purchases', async () => {
  const f = fixture(); f.local.sxsReorderBrowser.enabled = true;
  // Prevent the optional connection pump from starting after this isolated check.
  f.fetcher = async (url, options) => {
    f.calls.push(['fetch', url, options]);
    if (url.endsWith('/connect')) f.local.sxsReorderBrowser.enabled = false;
    return { ok: true, status: 200, json: async () => ({ success: true, protocol: 'automation.ssactivewear.extension/v1' }) };
  };
  const result = await createSxsWorker(f.api, f.fetcher).probe('minimized');
  assert.equal(result.success, true); assert.equal(result.signed_in, true);
  const create = f.calls.find(c => c[0] === 'window')[1];
  assert.equal(create.state, 'minimized'); assert.equal(create.focused, false);
  assert.ok(f.calls.filter(c => c[0] === 'send').every(c => ['health', 'candidates'].includes(c[2].operation)));
  assert.ok(f.calls.filter(c => c[0] === 'window-update').every(c => c[2].focused === false && c[2].state === 'minimized'));
});

test('minimized check stops if its newly created window does not minimize', async () => {
  const f = fixture(); f.api.windows.get = async () => ({ state: 'normal' });
  f.fetcher = async () => { f.local.sxsReorderBrowser.enabled = false; return { ok: true, status: 200, json: async () => ({ success: true, protocol: 'automation.ssactivewear.extension/v1' }) }; };
  const result = await createSxsWorker(f.api, f.fetcher).probe('minimized');
  assert.equal(result.success, false);
  assert.match(result.message, /did not minimize/);
  assert.equal(f.calls.filter(c => c[0] === 'send').length, 0);
});

test('content scripts cannot enable a connection or start a browser check', async () => {
  const f = fixture(); f.local.sxsReorderBrowser.enabled = false;
  installSxsWorker(f.api);
  for (const listener of f.listeners) {
    assert.notEqual(listener({ type: 'sxs:probe' }, { id: f.api.runtime.id, tab: { id: 7 } }, () => assert.fail('page must not control connection')), true);
  }
});
