const BASE = 'https://www.ssactivewear.com';
const ENDPOINT = 'http://127.0.0.1:5123/api/extension/bridge/ssactivewear';
const PROTOCOL = 'automation.ssactivewear.extension/v1';
const STATE = 'sxsReorderBrowser';
const OWNED = 'sxsReorderOwnedTab';
const OUTBOX = 'sxsReorderOutbox';
const ALARM = 'sxs-reorder-connection';

export function isSxsUrl(value) {
  try { const u = new URL(value); return u.origin === BASE && !u.username && !u.password; } catch { return false; }
}
export function allowedNavigation(value) {
  try {
    const u = new URL(value);
    return u.origin === BASE && !u.username && !u.password && !u.search && !u.hash &&
      (['/', '/myaccount/login', '/checkout/cart'].includes(u.pathname) || /^\/ps\/[A-Za-z0-9_.-]+$/.test(u.pathname) || /^\/p\/[A-Za-z0-9_-]+\/[A-Za-z0-9_.-]+$/.test(u.pathname));
  } catch { return false; }
}

// Fixed page-context operation for S&S's JavaScript color links. No supplied
// script, selector, function name, credentials, or checkout action is accepted.
export function selectSxsColorInMain({ color, anchorId, orderId, deadline }) {
  if (location.origin !== 'https://www.ssactivewear.com' || !/^\/p\/[^/]+\/[^/]+\/?$/.test(location.pathname)
      || !/^\d{7}$/.test(orderId || '') || !Number.isFinite(deadline) || Date.now() >= deadline * 1000
      || !document.querySelector('a[href="/myaccount/signout"],a[href="https://www.ssactivewear.com/myaccount/signout"]')) throw Error('S&S color selection requires the signed-in Reorder product page.');
  const key = value => String(value || '').toUpperCase().replace(/GREY/g, 'GRAY').replace(/[^A-Z0-9]/g, '');
  const matches = [...document.querySelectorAll('#colorSwatch a')].filter(a => key(a.textContent) === key(color));
  if (matches.length !== 1 || matches[0].id !== anchorId || !/^cSwatch\d+$/.test(anchorId || '')) throw Error('S&S color selector changed before selection.');
  const anchor = matches[0], href = anchor.getAttribute('href') || '';
  const args = href.match(/^javascript:\s*iColor\(\s*'(\d+)'\s*,\s*'(false|true)'\s*,\s*'(false|true)'\s*,\s*'(false|true)'\s*,\s*'([^']*)'\s*\);?\s*$/i);
  if (!args || anchorId !== 'cSwatch' + args[1] || typeof globalThis.iColor !== 'function') throw Error('S&S color selector handler is unavailable or changed.');
  if (args[5]) {
    const video = new URL(args[5]);
    if (video.protocol !== 'https:' || !['sportswearcollection-productvideos.s3.us-east-2.amazonaws.com', 'cdn.ssactivewear.com'].includes(video.hostname)
        || video.username || video.password || args[5].length > 1200) throw Error('Unexpected S&S color video resource.');
  }
  globalThis.iColor(...args.slice(1));
  return { success: true, anchor_id: anchorId };
}

export function clickSxsStockControlInMain({ operation, orderId, deadline, quantity, before, rows }) {
  if (location.origin !== 'https://www.ssactivewear.com' || !/^\d{7}$/.test(orderId || '')
      || !Number.isFinite(deadline) || Date.now() >= deadline * 1000
      || !document.querySelector('a[href="/myaccount/signout"],a[href="https://www.ssactivewear.com/myaccount/signout"]')) throw Error('S&S stock changes require the signed-in Reorder page.');
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = e => e && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0
    && getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden';
  let buttons;
  if (operation === 'add') {
    if (!/^\/p\/[^/]+\/[^/]+\/?$/.test(location.pathname) || !Number.isInteger(quantity) || quantity <= 0) throw Error('Invalid S&S stock addition.');
    const live = norm(document.querySelector('#qtyMiniCart')?.textContent).match(/^(\d+)(?:\s+items?)?$/i);
    const label = document.querySelector('a[aria-label^="Cart:"]')?.getAttribute('aria-label') || '';
    const count = live ? Number(live[1]) : Number((label.match(/Cart:\s*(\d+)/i) || [])[1] ?? NaN);
    const quantities = [...document.querySelectorAll('.gR input[aria-label="quantity"]')].map(i => Number(i.value || 0));
    if (count !== before || !quantities.length || quantities.some(q => !Number.isInteger(q) || q < 0)
        || quantities.reduce((sum, q) => sum + q, 0) !== quantity) throw Error('S&S cart count or entered quantities changed before addition.');
    buttons = [...document.querySelectorAll('#aToCDesk, #aToCMob, #ninjaAddToCart')].filter(visible);
  } else if (operation === 'remove') {
    if (location.pathname !== '/checkout/cart' || !Array.isArray(rows) || !rows.length) throw Error('Invalid S&S stock removal.');
    const current = [...document.querySelectorAll('#M_M_zCart .cR')].filter(visible).filter(r => {
      if (r.querySelector('.cPie input, .cDes a[href]')) return true;
      if (/\bwas removed\b/i.test(norm(r.innerText))) return false;
      throw Error('S&S cart contains an unreadable stock row.');
    });
    const actual = current.map(r => ({ id: r.id, style: norm(r.querySelector('.cDes b')?.textContent), url: r.querySelector('.cDes a')?.href || '',
      color: norm(r.querySelector('.cCol')?.innerText), size: norm(r.querySelector('.cSiz')?.innerText).replace(/^Size:\s*/i, ''),
      method: norm(r.querySelector('.cMet')?.innerText), warehouse: norm(r.querySelector('.cLoc')?.innerText), quantity: Number(r.querySelector('.cPie input')?.value || 0) }));
    const signature = list => JSON.stringify(list.map(r => [r.id, r.style, r.url, r.color, r.size, r.method, r.warehouse, r.quantity]));
    if (signature(actual) !== signature(rows)) throw Error('S&S cart changed before removal.');
    buttons = [...current[0].querySelectorAll('a[aria-label],a[href]')].filter(e => {
      if (!visible(e) || e.disabled) return false;
      const href = e.getAttribute('href') || '';
      if (/^javascript:\s*removeCartItem\(/i.test(href)) {
        const ids = href.match(/,\s*(\d+)\s*,\s*(-?\d+)\s*\)\s*;?\s*$/);
        return !!ids && current[0].id === `cartRow_${ids[1]}_${ids[2]}`;
      }
      return /^remove item$/i.test(norm(e.getAttribute('aria-label'))) && !/^javascript:/i.test(href);
    });
  } else { throw Error('Unsupported S&S stock control.'); }
  if (buttons.length !== 1 || buttons[0].disabled) throw Error('S&S stock control is missing or ambiguous.');
  buttons[0].click();
  return { click_issued: true };
}

export function clickSxsCheckoutControlInMain({ operation, orderId, deadline, po }) {
  if (location.origin !== 'https://www.ssactivewear.com' || !/^\/checkout\/?$/.test(location.pathname)
      || !/^\d{7}$/.test(orderId || '') || po !== orderId || !Number.isFinite(deadline) || Date.now() >= deadline * 1000) throw Error('S&S checkout action does not match the active Reorder.');
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = e => {
    if (!e || !e.getBoundingClientRect().width || !e.getBoundingClientRect().height) return false;
    for (let parent = e; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
    }
    return true;
  };
  const address = norm(document.querySelector('#M_M_zShippingContainer')?.innerText).split(/Shipping\s+(?:Options|Method)/i)[0];
  const methods = [...document.querySelectorAll('select[id$="zFreightServiceCB"]')].map(s => norm(s.selectedOptions[0]?.textContent));
  if (document.querySelector('#M_M_zPONumberTB')?.value !== po || !/2727\s+Commerce\s+Way/i.test(address)
      || !/Philadelphia,?\s+PA\s+19154/i.test(address) || norm(document.querySelector('#M_M_zTermsCB')?.selectedOptions[0]?.textContent) !== 'Net 60'
      || !methods.length || methods.some(m => !/\bUPS Ground\b/i.test(m)) || [...document.querySelectorAll('input[type="password"]')].some(visible)) throw Error('S&S checkout PO, address, shipping, or payment changed before the click.');
  let controls;
  if (operation === 'begin_submit') {
    controls = [...document.querySelectorAll('#M_M_zCheckoutBTN')].filter(e => visible(e) && !e.disabled);
  } else if (operation === 'confirm_submit') {
    const roots = [...document.querySelectorAll('#M_M_zConfirmOrderPopup_PW-1,#M_M_zConfirmOrderPopup_PWC-1,.messagebox_overlay .messagebox,[role="dialog"],[role="alertdialog"]')]
      .filter(root => /Are you sure you want to submit your order\?/i.test(norm(root.textContent)));
    if (!roots.length) throw Error('S&S order confirmation popup changed.');
    controls = [...new Set(roots.flatMap(root => [...root.querySelectorAll('button,input[type="submit"],input[type="button"],[role="button"],.dxbButton')]))]
      .filter(e => visible(e) && !e.disabled && /^(?:confirm|yes)$/i.test(norm(e.value || e.innerText || e.getAttribute('aria-label'))));
    controls = controls.filter(e => !controls.some(other => other !== e && other.contains(e)));
  } else { throw Error('Unsupported S&S checkout control.'); }
  if (controls.length !== 1) throw Error('S&S checkout control is missing or ambiguous.');
  controls[0].click();
  return { click_issued: true };
}

export function createSxsWorker(api, fetcher = fetch) {
  let pumping = false, probing = false, writes = Promise.resolve();
  const read = async () => {
    const settings = { ...((await api.storage.local.get(STATE))[STATE] || {}) };
    // Tab IDs survive a service-worker restart, not a browser restart. Never
    // recover a different user tab from an old numeric ID in local storage.
    delete settings.tabId; delete settings.boundSession; delete settings.minimizedWindowId;
    return { ...settings, ...((await api.storage.session.get(OWNED))[OWNED] || {}) };
  };
  const update = patch => {
    const task = writes.then(async () => {
      const state = { ...(await read()), ...patch };
      const settings = { ...state };
      delete settings.tabId; delete settings.boundSession; delete settings.minimizedWindowId;
      await api.storage.local.set({ [STATE]: settings });
      if (Object.hasOwn(patch, 'tabId') || Object.hasOwn(patch, 'boundSession')) {
        await api.storage.session.set({ [OWNED]: { tabId: state.tabId, boundSession: state.boundSession, minimizedWindowId: state.minimizedWindowId } });
      }
      return state;
    });
    writes = task.catch(() => {});
    return task;
  };
  async function request(path, data, get = false) {
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 23000);
    try {
      const url = ENDPOINT + path + (get ? '?' + new URLSearchParams(data) : '');
      const response = await fetcher(url, { method: get ? 'GET' : 'POST', cache: 'no-store', credentials: 'omit', signal: controller.signal,
        ...(!get ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) } : {}) });
      const payload = await response.json().catch(() => null);
      if (!response.ok || payload?.success !== true || payload.protocol !== PROTOCOL) {
        const error = Error(payload?.message || 'Restart the local Automation app to load the S&S browser connection.');
        error.status = response.status;
        throw error;
      }
      return payload;
    } finally { clearTimeout(timeout); }
  }
  const identity = state => ({ client_id: state.clientId, extension_id: api.runtime.id });
  async function requirePermissions() {
    if (!await api.permissions.contains({ permissions: ['scripting'], origins: [BASE + '/*'] })) throw Error('Click Connect this browser and allow S&S access first.');
  }
  async function connect(enabled) {
    if (enabled) await requirePermissions();
    const old = await read();
    const state = await update({ enabled: enabled === true, clientId: old.clientId || crypto.randomUUID().replaceAll('-', '') });
    if (!enabled) await api.alarms.clear(ALARM);
    else await api.alarms.create(ALARM, { periodInMinutes: 0.5 });
    try {
      await request('/connect', { ...identity(state), enabled: state.enabled });
      await update({ message: enabled ? 'Connected. Reorder opens and checks a minimized S&S window automatically. Sign in once if needed.' : 'S&S browser disconnected.' });
      if (enabled) {
        await api.alarms.create(ALARM, { periodInMinutes: 0.5 });
        void pump();
      } else { await api.alarms.clear(ALARM); }
    } catch (error) { await update({ message: enabled ? 'S&S connection will retry automatically: ' + error.message : 'S&S browser disconnected. ' + error.message }); throw error; }
    return status();
  }
  async function waitTab(tabId) {
    const deadline = Date.now() + 25000;
    while (true) {
      const tab = await api.tabs.get(tabId);
      if (tab.discarded || tab.frozen) throw Error('S&S tab was suspended. Restore its window and review before retrying.');
      if (tab.status === 'complete' && isSxsUrl(tab.url)) return tab;
      if (tab.status === 'complete' && tab.url && !isSxsUrl(tab.url)) throw Error('The S&S worker tab navigated to another website.');
      if (Date.now() >= deadline) throw Error('S&S tab did not finish loading.');
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
  async function ownedTab(sessionId) {
    let state = await read();
    const reorder = /^[a-f0-9]{32}$/.test(sessionId || '');
    if (state.tabId) {
      try {
        const tab = await api.tabs.get(state.tabId);
        if (!isSxsUrl(tab.url || tab.pendingUrl)) throw Error('The dedicated S&S tab was changed to another website.');
        if (state.boundSession !== sessionId) await update({ boundSession: sessionId });
        return tab;
      } catch (error) {
        if (reorder || state.boundSession === sessionId || /another website/.test(error.message)) throw Error('S&S worker tab was closed or changed during the run. Review its cart before retrying.');
      }
    }
    if (reorder) throw Error('S&S browser restarted during this order or its worker tab is missing. Review its cart and any existing purchase before Retry; setup cannot repeat during an active order.');
    const tab = await api.tabs.create({ url: BASE + '/checkout/cart', active: false });
    await api.tabs.update(tab.id, { autoDiscardable: false });
    await update({ tabId: tab.id, boundSession: sessionId });
    return tab;
  }
  async function prepareTab(sessionId) {
    const state = await read();
    // Never recover an interrupted order by opening a different tab. This
    // operation is also restricted to the first command by the local broker.
    if (state.boundSession === sessionId) throw Error('S&S setup was already attempted for this order. Review its tab before retrying.');
    let tab = null;
    if (state.tabId && state.minimizedWindowId) {
      try { tab = await api.tabs.get(state.tabId); } catch { /* Closed between orders: create our own window below. */ }
      if (tab && (!isSxsUrl(tab.url || tab.pendingUrl) || tab.windowId !== state.minimizedWindowId)) {
        throw Error('The dedicated S&S tab was changed. Review its cart before retrying.');
      }
      if (tab?.discarded || tab?.frozen) throw Error('S&S tab was suspended. Review its cart before retrying.');
    }
    await update({ boundSession: sessionId });
    if (!tab) {
      const window = await api.windows.create({ url: BASE + '/checkout/cart', state: 'minimized', focused: false });
      const tabs = window.tabs || await api.tabs.query({ windowId: window.id });
      if (tabs.length !== 1) throw Error('The minimized S&S worker window is ambiguous.');
      tab = tabs[0];
      await update({ tabId: tab.id, boundSession: sessionId, minimizedWindowId: window.id });
    }
    // This window belongs to the worker; never minimize the user's main window.
    await api.windows.update(tab.windowId, { state: 'minimized', focused: false });
    if ((await api.windows.get(tab.windowId)).state !== 'minimized') throw Error('The S&S worker window did not minimize.');
    await api.tabs.update(tab.id, { url: BASE + '/checkout/cart', autoDiscardable: false });
    return tab;
  }
  async function pageCommand(tabId, command) {
    const tab = await waitTab(tabId);
    if (!isSxsUrl(tab.url)) throw Error('Unexpected S&S tab origin.');
    await api.scripting.executeScript({ target: { tabId }, files: ['sxs-page.js'] });
    // Sending an action is attempted once. A lost response never repeats it.
    const result = await api.tabs.sendMessage(tabId, { type: 'sxs:page-command', commandId: command.id,
      orderId: command.order_id || '', operation: command.operation, payload: command.payload || {}, deadline: command.deadline });
    if (!result || result.success !== true || !isSxsUrl(result.result?.url)) throw Error(result?.message || 'S&S did not confirm this operation. Review the tab before retrying.');
    if (command.operation === 'health') {
      const window = await api.windows.get(tab.windowId);
      return { ...result.result, window_state: window.state, tab_active: tab.active === true };
    }
    return result.result;
  }
  async function execute(command) {
    if (!/^[a-f0-9]{32}$/.test(command.id || '') || !/^[a-f0-9]{32}$/.test(command.session_id || '') || !/^\d{7}$/.test(command.order_id || '') || Date.now() >= command.deadline * 1000) throw Error('Invalid or expired S&S Reorder command.');
    await requirePermissions();
    const state = await read();
    if (!state.enabled || probing) throw Error('S&S browser is disconnected or running a browser check.');
    if (command.operation === 'prepare') {
      const tab = await prepareTab(command.session_id);
      const result = await pageCommand(tab.id, { ...command, operation: 'health' });
      if (!result.hidden || result.window_state !== 'minimized') throw Error('The S&S worker window became visible during setup. Review before retrying.');
      await update({ message: result.signed_in && result.cart_container ? 'Minimized S&S browser ready for Reorder.' : 'S&S needs sign-in. Use Open S&S sign-in, then Retry the batch.' });
      return result;
    }
    const tab = await ownedTab(command.session_id);
    if (command.operation === 'navigate') {
      if (!allowedNavigation(command.payload?.url)) throw Error('Unexpected S&S navigation URL.');
      await api.tabs.update(tab.id, { url: command.payload.url, autoDiscardable: false });
      const loaded = await waitTab(tab.id);
      return { url: loaded.url };
    }
    if (command.operation === 'color') {
      const ready = await pageCommand(tab.id, { ...command, operation: 'color_ready' });
      const result = await api.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
        func: selectSxsColorInMain, args: [{ color: command.payload.color, anchorId: ready.anchor_id, orderId: command.order_id, deadline: command.deadline }] });
      if (result?.length !== 1 || result[0].result?.success !== true || result[0].result.anchor_id !== ready.anchor_id) throw Error('S&S color selector did not confirm its invocation.');
      return pageCommand(tab.id, command);
    }
    if (['add', 'remove'].includes(command.operation)) {
      const ready = await pageCommand(tab.id, { ...command, operation: command.operation + '_ready' });
      const payload = { ...command.payload, ...(command.operation === 'add' ? { before: ready.before } : {}) };
      const result = await api.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
        func: clickSxsStockControlInMain, args: [{ ...payload, operation: command.operation, orderId: command.order_id, deadline: command.deadline }] });
      if (result?.length !== 1 || result[0].result?.click_issued !== true) throw Error('S&S stock control did not confirm its invocation. Review the cart before retrying.');
      return pageCommand(tab.id, { ...command, payload });
    }
    if (['begin_submit', 'confirm_submit'].includes(command.operation)) {
      await pageCommand(tab.id, { ...command, operation: command.operation + '_ready' });
      const result = await api.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN', func: clickSxsCheckoutControlInMain,
        args: [{ operation: command.operation, orderId: command.order_id, deadline: command.deadline, po: command.payload.po }] });
      if (result?.length !== 1 || result[0].result?.click_issued !== true) throw Error('S&S checkout click was not confirmed. Review the supplier page before retrying.');
      return pageCommand(tab.id, command);
    }
    return pageCommand(tab.id, command);
  }
  async function flushOutbox(state) {
    const values = await api.storage.session.get(OUTBOX);
    if (!values[OUTBOX]) return;
    try { await request('/result', { ...identity(state), ...values[OUTBOX] }); }
    catch (error) { if (error.status !== 409) throw error; }
    await api.storage.session.remove(OUTBOX);
  }
  async function pump() {
    if (pumping) return;
    pumping = true;
    try {
      let state = await read();
      if (!state.enabled) return;
      await requirePermissions();
      await api.alarms.create(ALARM, { periodInMinutes: 0.5 });
      await request('/connect', identity(state));
      while ((state = await read()).enabled) {
        await flushOutbox(state);
        const response = await request('/poll', identity(state), true);
        await api.storage.session.set({ sxsActiveSession: response.active_session });
        if (!response.command) continue;
        let result = null, error = null;
        await update({ message: 'Working on S&S Reorder ' + response.command.order_id + '.' });
        try { result = await execute(response.command); }
        catch (failure) { error = failure.message || 'S&S browser operation failed.'; }
        await api.storage.session.set({ [OUTBOX]: { command_id: response.command.id, result, error } });
        await flushOutbox(state);
        if (error) await update({ message: error });
      }
    } catch (error) { await update({ message: 'S&S connection paused: ' + error.message }); }
    finally { pumping = false; }
  }
  async function assertIdle() {
    const active = (await api.storage.session.get('sxsActiveSession')).sxsActiveSession;
    if (active) throw Error('S&S is handling Reorder ' + active.order_id + '. Wait for it to finish.');
  }
  async function open() {
    await requirePermissions();
    await assertIdle();
    const state = await read();
    if (!state.enabled) throw Error('Connect this browser first.');
    await request('/connect', { ...identity(state), probe: { success: false, signed_in: false, message: 'Manual S&S sign-in opened.' } });
    const tab = await ownedTab('manual-sign-in');
    await api.tabs.update(tab.id, { url: BASE + '/myaccount/login', active: true });
    const window = await api.windows.get(tab.windowId);
    await api.windows.update(tab.windowId, { focused: true, ...(window.state === 'minimized' ? { state: 'normal' } : {}) });
    return { success: true };
  }
  async function probe(mode = 'background') {
    await requirePermissions(); await assertIdle();
    if (probing || !['background', 'minimized'].includes(mode)) throw Error('A browser check is already running or its mode is invalid.');
    const state = await read();
    if (!state.enabled) throw Error('Connect this browser before its read-only check.');
    probing = true;
    let started = false;
    const check = { success: false, signed_in: false, mode, checked_at: new Date().toISOString(), message: '' };
    try {
      await request('/connect', { ...identity(state), probe: check });
      started = true;
      let tab;
      if (mode === 'minimized') {
        const window = await api.windows.create({ url: BASE + '/', state: 'minimized', focused: false });
        // Confirm the created window's requested state before its first read.
        // Only this newly owned check window is changed.
        await api.windows.update(window.id, { state: 'minimized', focused: false });
        if ((await api.windows.get(window.id)).state !== 'minimized') throw Error('The new S&S check window did not minimize.');
        const tabs = window.tabs || await api.tabs.query({ windowId: window.id });
        if (tabs.length !== 1) throw Error('The minimized S&S check window is ambiguous.');
        tab = tabs[0];
      } else { tab = await api.tabs.create({ url: BASE + '/', active: false }); }
      await api.tabs.update(tab.id, { autoDiscardable: false });
      await update({ tabId: tab.id, boundSession: null, minimizedWindowId: mode === 'minimized' ? tab.windowId : null, message: 'Checking S&S in a ' + mode + ' browser.' });
      const pages = [];
      for (const path of ['/', '/myaccount/login', '/checkout/cart', '/ps/5000']) {
        await api.tabs.update(tab.id, { url: BASE + path });
        await waitTab(tab.id);
        const result = await pageCommand(tab.id, { id: crypto.randomUUID().replaceAll('-', ''), operation: 'health', deadline: Date.now() / 1000 + 50 });
        if (!result.hidden) throw Error(`S&S ${mode} check became visible at ${path} (window: ${result.window_state}, active tab: ${result.tab_active}). Leave its test tab/window in the background and repeat the check.`);
        if (path === '/checkout/cart') check.signed_in = result.signed_in === true && result.cart_container === true;
        if (path === '/ps/5000') await pageCommand(tab.id, { id: crypto.randomUUID().replaceAll('-', ''), operation: 'candidates', deadline: Date.now() / 1000 + 50 });
        pages.push({ path, hidden: result.hidden });
      }
      if (mode === 'minimized' && (await api.windows.get(tab.windowId)).state !== 'minimized') throw Error('The S&S test window was restored; repeat the minimized check.');
      check.success = true;
      check.message = check.signed_in ? 'S&S ' + mode + ' access and signed-in cart verified. Ready for Reorder.' : 'S&S ' + mode + ' access works. Sign in normally, then repeat this check.';
      check.pages = pages;
    } catch (error) { check.message = error.message || 'S&S browser check failed.'; }
    finally {
      probing = false;
      if (started) {
        await request('/connect', { ...identity(await read()), probe: check });
        await update({ probe: check, message: check.message });
      }
      void pump();
    }
    return check;
  }
  async function status() { const state = await read(); return { success: true, enabled: state.enabled === true, message: state.message || 'S&S browser is not connected.', probe: state.probe || null }; }
  return { connect, open, probe, status, pump, execute };
}

export function installSxsWorker(api) {
  const worker = createSxsWorker(api);
  api.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message?.type?.startsWith('sxs:') || sender.id !== api.runtime.id || sender.tab) return false;
    const calls = { 'sxs:connect': () => worker.connect(message.enabled === true), 'sxs:open': () => worker.open(),
      'sxs:probe': () => worker.probe(message.mode), 'sxs:status': () => worker.status() };
    if (!calls[message.type]) return false;
    calls[message.type]().then(sendResponse, error => sendResponse({ success: false, message: error.message }));
    return true;
  });
  api.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM) void worker.pump(); });
  api.runtime.onStartup.addListener(() => { void worker.pump(); });
  api.runtime.onInstalled.addListener(() => { void worker.pump(); });
  void worker.pump();
}
