/* Restricted S&S page operations. No credentials, eval, arbitrary selectors, or
 * page-to-extension postMessage channel. Runs only in the owned S&S tab. */
(() => {
  if (globalThis.__sxsReorderPageInstalled) return;
  globalThis.__sxsReorderPageInstalled = true;
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const colorKey = value => norm(value).toUpperCase().replace(/GREY/g, 'GRAY').replace(/[^A-Z0-9]/g, '');
  const sizeKey = value => norm(value).toUpperCase().replace(/\s/g, '').replace(/^XXL$/, '2XL').replace(/^XXXL$/, '3XL');
  const visible = element => {
    if (!element || !element.getBoundingClientRect().width || !element.getBoundingClientRect().height) return false;
    for (let e = element; e; e = e.parentElement) {
      const style = getComputedStyle(e);
      if (style.display === 'none' || style.visibility === 'hidden') return false;
    }
    return true;
  };
  const all = selector => [...document.querySelectorAll(selector)];
  function guard() {
    if (location.origin !== 'https://www.ssactivewear.com') throw Error('Unexpected S&S website origin.');
    const text = document.body?.innerText || '';
    if (/looks like we['’]re having some problems/i.test(text)) {
      const ray = (text.match(/Ray ID\s*[-:]\s*([a-f0-9]{16,32})/i) || [])[1];
      throw Error('S&S returned its website error page' + (ray ? ` (Ray ID: ${ray})` : '') + '.');
    }
    if (/verify (?:that )?you are human|checking your browser|performing security verification|just a moment/i.test(text)) {
      throw Error('S&S requires browser verification. Open its tab and complete it manually.');
    }
  }
  function unique(selector) {
    const matches = all(selector).filter(e => visible(e) && !e.disabled);
    if (matches.length !== 1) throw Error('The required S&S control is missing or ambiguous.');
    return matches[0];
  }
  function waitFor(test, message, timeout = 15000) {
    return new Promise((resolve, reject) => {
      let timer, observer, settled = false;
      const deadline = Date.now() + timeout;
      const finish = (error, value) => { if (settled) return; settled = true; clearTimeout(timer); observer?.disconnect(); error ? reject(error) : resolve(value); };
      const check = () => {
        if (settled) return;
        clearTimeout(timer);
        try {
          guard();
          const result = test();
          if (result) return finish(null, result);
          if (Date.now() >= deadline) return finish(Error(message));
          timer = setTimeout(check, 500);
        } catch (error) { finish(error); }
      };
      observer = new MutationObserver(check);
      observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
      check();
    });
  }
  async function consent() {
    guard();
    const buttons = all('#onetrust-banner-sdk #onetrust-accept-btn-handler').filter(visible);
    if (buttons.length > 1) throw Error('S&S cookie banner is ambiguous.');
    if (buttons.length) {
      buttons[0].click();
      await waitFor(() => !all('#onetrust-banner-sdk #onetrust-accept-btn-handler').some(visible), 'S&S cookie banner did not close.', 5000);
    }
  }
  function inputValue(input, value) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, String(value));
    for (const type of ['input', 'change', 'blur']) input.dispatchEvent(new Event(type, { bubbles: true }));
  }
  function cartRows() {
    return all('#M_M_zCart .cR').filter(visible).map(r => ({
      id: r.id, style: norm(r.querySelector('.cDes b')?.textContent), url: r.querySelector('.cDes a')?.href || '',
      color: norm(r.querySelector('.cCol')?.innerText), size: norm(r.querySelector('.cSiz')?.innerText).replace(/^Size:\s*/i, ''),
      method: norm(r.querySelector('.cMet')?.innerText), warehouse: norm(r.querySelector('.cLoc')?.innerText),
      quantity: Number(r.querySelector('.cPie input')?.value || 0)
    }));
  }
  function checkoutState(po, fill = false) {
    if (!/^\d{7}$/.test(String(po))) throw Error('S&S PO must be the seven-digit CRM order number.');
    const address = norm(document.querySelector('#M_M_zShippingContainer')?.innerText).split(/Shipping\s+(?:Options|Method)/i)[0];
    const methods = all('select[id$="zFreightServiceCB"]').map(s => norm(s.selectedOptions[0]?.textContent));
    const payment = norm(document.querySelector('#M_M_zTermsCB')?.selectedOptions[0]?.textContent);
    const input = document.querySelector('#M_M_zPONumberTB');
    if (!visible(input) || !/2727\s+Commerce\s+Way/i.test(address) || !/Philadelphia,?\s+PA\s+19154/i.test(address)) throw Error('S&S shipping address is not Commerce Way.');
    if (!methods.length || methods.some(m => !/\bUPS Ground\b/i.test(m))) throw Error('S&S shipping is not UPS Ground for every warehouse.');
    if (payment !== 'Net 60') throw Error('S&S payment is not Net 60.');
    if (fill) inputValue(input, po);
    if (input.value !== po) throw Error('S&S checkout PO changed.');
    return { po: input.value, address, payment, methods };
  }
  const commands = {
    health: async () => ({
      signed_in: !!document.querySelector('a[href="/myaccount/signout"],a[href="https://www.ssactivewear.com/myaccount/signout"]'),
      cart_container: !!document.querySelector('#M_M_zCart'), hidden: document.hidden,
      visibility: document.visibilityState, title: document.title
    }),
    candidates: async () => {
      const read = () => {
        const rows = all('a[href*="/p/"]').filter(visible).map(a => ({ url: a.href, title: norm(a.innerText) })).filter(x => x.title);
        if (/^\/p\/[^/]+\/[^/]+\/?$/.test(location.pathname)) {
          const title = norm(document.querySelector('#productMainContainer')?.innerText);
          if (title) rows.push({ url: location.origin + location.pathname.replace(/\/$/, ''), title });
        }
        return rows.length && rows;
      };
      return { candidates: await waitFor(read, 'S&S product results did not load.') };
    },
    color: async ({ color }) => {
      await waitFor(() => all('#colorSwatch a').length, 'S&S colors did not load.');
      const matches = all('#colorSwatch a').filter(a => colorKey(a.textContent) === colorKey(color));
      if (matches.length !== 1) throw Error('S&S color is missing or ambiguous.');
      matches[0].click();
      await waitFor(() => colorKey(norm(document.querySelector('#zcolorname b')?.textContent).split(' - PMS')[0]) === colorKey(color), 'S&S selected color did not verify.');
      await waitFor(() => all('.gR[id^="wh_"] input[aria-label="quantity"]').length, 'S&S inventory did not load.');
      return {};
    },
    inventory: async () => ({ inventory: all('.gR[id^="wh_"]').map(r => ({
      warehouse: norm(r.querySelector('.warehouse')?.textContent), eta_text: norm(r.querySelector('.shippingRowLabel')?.textContent),
      stock: Object.fromEntries([...r.querySelectorAll('.i')].filter(c => c.querySelector('input[aria-label="quantity"]')).map(c => [norm(c.querySelector('.sr-only')?.textContent), norm(c.querySelector('.quantity')?.textContent)]))
    })).filter(r => r.warehouse) }),
    cart: async () => { await waitFor(() => document.querySelector('#M_M_zCart'), 'S&S cart did not load.'); return { rows: cartRows() }; },
    verified_cart: async ({ lines }) => {
      if (!Array.isArray(lines) || !lines.length) throw Error('S&S expected allocation is missing.');
      const original = cartRows(), rows = structuredClone(original);
      for (const row of rows) {
        const parsed = new URL(row.url), productUrl = parsed.origin + '/' + parsed.pathname.split('/').filter(Boolean).slice(0, 3).join('/');
        if (parsed.origin !== location.origin) throw Error('Unexpected S&S cart product origin.');
        const expected = [...new Set(lines.filter(x => x.selected.url === productUrl && colorKey(x.product.color) === colorKey(row.color) && Object.hasOwn(x.quantities, sizeKey(row.size))).map(x => x.warehouse))];
        if (!expected.length) throw Error('S&S cart contains unexpected stock.');
        const locations = expected.filter(w => row.warehouse.includes(w));
        const quantity = lines.filter(x => locations.length === 1 && x.warehouse === locations[0] && x.selected.url === productUrl && colorKey(x.product.color) === colorKey(row.color)).reduce((n, x) => n + (x.quantities[sizeKey(row.size)] || 0), 0);
        if (locations.length === 1 && (expected.length === 1 || quantity === row.quantity)) continue;
        if (!/^cartRow_[0-9_-]+$/.test(row.id)) throw Error('S&S grouped cart row identity is unclear.');
        unique('#' + CSS.escape(row.id) + ' .cMet a[aria-haspopup]').click();
        const body = await waitFor(() => { const b = document.querySelector('#popupBody'); return visible(b) && b; }, 'S&S warehouse breakdown did not open.');
        const allocations = {};
        for (const name of expected) {
          const candidates = [...body.querySelectorAll('tr,.row,.gR')].filter(r => visible(r) && norm(r.innerText).includes(name));
          const matches = candidates.filter(r => !candidates.some(other => other !== r && r.contains(other)));
          if (matches.length !== 1) throw Error('S&S warehouse breakdown is ambiguous.');
          const inputs = [...matches[0].querySelectorAll('input[type=number],input[type=text]')].filter(i => visible(i) && !i.disabled && !i.readOnly);
          if (inputs.length !== 1 || !/^\d+$/.test(inputs[0].value)) throw Error('S&S warehouse quantity is ambiguous.');
          allocations[name] = Number(inputs[0].value);
        }
        row.allocations = allocations;
        const root = body.closest('.modal,.popup,dialog') || document.querySelector('#modalPopupBody') || body.parentElement;
        const close = [...root.querySelectorAll('button,a,[role=button]')].filter(visible).find(x => /^(?:close|cancel)$/i.test(norm(x.innerText || x.getAttribute('aria-label'))) || x.classList.contains('popup-close'));
        if (!close) throw Error('S&S warehouse breakdown cannot be closed without applying changes.');
        close.click();
        await waitFor(() => !visible(body), 'S&S warehouse breakdown did not close.');
      }
      if (JSON.stringify(cartRows()) !== JSON.stringify(original)) throw Error('S&S cart changed while verifying warehouse allocations.');
      return { rows };
    },
    fill: async ({ allocations }) => {
      if (!Array.isArray(allocations) || !allocations.length) throw Error('S&S allocation is missing.');
      const rows = all('.gR[id^="wh_"]'), fills = [];
      for (const allocation of allocations) {
        const matches = rows.filter(r => norm(r.querySelector('.warehouse')?.textContent) === allocation.warehouse);
        if (matches.length !== 1) throw Error('S&S warehouse row is ambiguous.');
        for (const [size, quantity] of Object.entries(allocation.quantities)) {
          if (!Number.isInteger(quantity) || quantity <= 0) throw Error('Invalid S&S quantity.');
          const cells = [...matches[0].querySelectorAll('.i')].filter(c => sizeKey(c.querySelector('.sr-only')?.textContent) === size);
          if (cells.length !== 1) throw Error('S&S size cell is ambiguous.');
          const input = cells[0].querySelector('input[aria-label="quantity"]');
          const stock = Number(norm(cells[0].querySelector('.quantity')?.textContent).replace(/\D/g, ''));
          if (!input || input.disabled || stock < quantity) throw Error('S&S stock is no longer available.');
          fills.push([input, quantity]);
        }
      }
      for (const input of all('.gR input[aria-label="quantity"]')) inputValue(input, '');
      for (const [input, quantity] of fills) inputValue(input, quantity);
      if (!fills.every(([input, quantity]) => Number(input.value) === quantity)) throw Error('S&S quantities did not remain entered.');
      return {};
    },
    add: async ({ quantity }) => {
      const count = () => {
        const live = norm(document.querySelector('#qtyMiniCart')?.textContent).match(/^(\d+)(?:\s+items?)?$/i);
        const label = document.querySelector('a[aria-label^="Cart:"]')?.getAttribute('aria-label') || '';
        return live ? Number(live[1]) : Number((label.match(/Cart:\s*(\d+)/i) || [])[1] ?? NaN);
      };
      const before = count();
      if (!Number.isInteger(before) || !Number.isInteger(quantity) || quantity <= 0) throw Error('S&S cart count or added quantity is invalid.');
      unique('#aToCDesk, #aToCMob, #ninjaAddToCart').click();
      await waitFor(() => count() === before + quantity, 'S&S did not confirm adding stock. Review the cart before retrying.');
      return {};
    },
    remove: async ({ rows }) => {
      if (!Array.isArray(rows) || !rows.length || JSON.stringify(rows) !== JSON.stringify(cartRows())) throw Error('S&S cart changed before removal.');
      const first = all('#M_M_zCart .cR').filter(visible)[0];
      const buttons = [...first.querySelectorAll('a[aria-label="remove item"]')].filter(visible);
      if (buttons.length !== 1) throw Error('S&S cart removal control is ambiguous.');
      buttons[0].click();
      await waitFor(() => cartRows().length === rows.length - 1, 'S&S stock removal did not verify.');
      const following = cartRows();
      if (JSON.stringify(following) !== JSON.stringify(rows.slice(1))) throw Error('Other S&S cart stock changed during removal.');
      return { rows: following };
    },
    checkout: async () => { unique('#zCheckoutBTN').click(); await waitFor(() => visible(document.querySelector('#M_M_zPONumberTB')), 'S&S checkout did not load.'); return {}; },
    review: async ({ po }) => checkoutState(po, true),
    valid: async ({ po }) => checkoutState(po),
    begin_submit: async ({ po }) => {
      checkoutState(po);
      unique('#M_M_zCheckoutBTN').click();
      await waitFor(() => all('#M_M_zConfirmOrderPopup_zCompleteBTN, #M_M_zConfirmOrderPopup_zCompleteBTN_I, #M_M_zConfirmOrderPopup_zConfirmOrderYesBTN').some(visible), 'S&S final confirmation did not open.');
      return {};
    },
    confirm_submit: async ({ po }) => {
      checkoutState(po);
      unique('#M_M_zConfirmOrderPopup_zCompleteBTN, #M_M_zConfirmOrderPopup_zCompleteBTN_I, #M_M_zConfirmOrderPopup_zConfirmOrderYesBTN').click();
      return { click_issued: true };
    },
    confirmation: async () => ({ text: (document.body?.innerText || '').slice(0, 50000) })
  };
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type !== 'sxs:page-command') return false;
    if (sender.id !== chrome.runtime.id || sender.tab || !/^[a-f0-9]{32}$/.test(message.commandId || '')) return false;
    (async () => {
      guard();
      if (!Object.hasOwn(commands, message.operation) || !Number.isFinite(message.deadline) || Date.now() >= message.deadline * 1000) throw Error('Unsupported or expired S&S command.');
      if (['fill', 'add', 'remove', 'checkout', 'review', 'begin_submit', 'confirm_submit'].includes(message.operation)) {
        if (!/^\d{7}$/.test(message.orderId || '')) throw Error('S&S changes require an active Reorder.');
        if (!document.querySelector('a[href="/myaccount/signout"],a[href="https://www.ssactivewear.com/myaccount/signout"]')) throw Error('S&S login expired. Sign in normally before retrying.');
      }
      if (['begin_submit', 'confirm_submit'].includes(message.operation) && (!/^\d{7}$/.test(message.orderId || '') || message.payload?.po !== message.orderId)) throw Error('S&S purchase does not match the active Reorder.');
      await consent();
      return { ...(await commands[message.operation](message.payload || {})), url: location.href };
    })().then(result => sendResponse({ success: true, result }), error => sendResponse({ success: false, message: error.message || 'S&S operation failed.' }));
    return true;
  });
})();
