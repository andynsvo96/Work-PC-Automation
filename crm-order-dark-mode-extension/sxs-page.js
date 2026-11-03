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
  const selectedColor = () => norm(document.querySelector('#zcolorname')?.textContent)
    .replace(/^.*?Selected Color:\s*/i, '').replace(/\s*[-\u2013\u2014]\s*PMS:.*$/i, '');
  const deliveryText = row => norm(row.querySelector('.shippingRowLabel')?.textContent)
    || norm(row.querySelector('.shipby')?.textContent);
  function stockCells(row) {
    const cells = [...row.children].filter(e => e.classList.contains('i'));
    const headerRows = [...(row.closest('.oGrid')?.querySelectorAll(':scope > .gH') || [])];
    if (headerRows.length > 1) throw Error('S&S inventory size headers are ambiguous.');
    const headers = headerRows.length ? [...headerRows[0].children].filter(e => !e.classList.contains('color')).map(e => sizeKey(e.textContent)) : [];
    if (headers.length && headers.length !== cells.length) throw Error('S&S inventory size columns do not match their header.');
    const seen = new Set();
    return cells.map((cell, index) => {
      const inline = sizeKey(cell.querySelector('.sr-only')?.textContent), header = headers[index] || '';
      if (inline && header && inline !== header) throw Error('S&S inventory size labels disagree with their header.');
      const size = inline || header;
      if (!size || seen.has(size)) throw Error('S&S inventory size labels are missing or duplicated.');
      seen.add(size);
      return { cell, size, input: cell.querySelector('input[aria-label="quantity"]') };
    });
  }
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
  function signedIn() {
    if (/^\/myaccount\/login\/?$/.test(location.pathname)) return false;
    if (document.querySelector('a[href="/myaccount/signout"],a[href="https://www.ssactivewear.com/myaccount/signout"]')) return true;
    // The authenticated checkout uses a reduced header without a sign-out link.
    return /^\/checkout\/?$/.test(location.pathname)
      && !!document.querySelector('#M_M_zPONumberTB') && !!document.querySelector('#M_M_zTermsCB')
      && !!document.querySelector('#M_M_zShippingContainer') && !all('input[type="password"]').some(visible);
  }
  function cartRows() {
    return all('#M_M_zCart .cR').filter(visible).filter(r => {
      if (r.querySelector('.cPie input, .cDes a[href]')) return true;
      if (/\bwas removed\b/i.test(norm(r.innerText))) return false;
      throw Error('S&S cart contains an unreadable stock row.');
    }).map(r => ({
      id: r.id, style: norm(r.querySelector('.cDes b')?.textContent), url: r.querySelector('.cDes a')?.href || '',
      color: norm(r.querySelector('.cCol')?.innerText), size: norm(r.querySelector('.cSiz')?.innerText).replace(/^Size:\s*/i, ''),
      method: norm(r.querySelector('.cMet')?.innerText), warehouse: norm(r.querySelector('.cLoc')?.innerText),
      quantity: Number(r.querySelector('.cPie input')?.value || 0)
    }));
  }
  function cartCount() {
    const live = norm(document.querySelector('#qtyMiniCart')?.textContent).match(/^(\d+)(?:\s+items?)?$/i);
    const label = document.querySelector('a[aria-label^="Cart:"]')?.getAttribute('aria-label') || '';
    return live ? Number(live[1]) : Number((label.match(/Cart:\s*(\d+)/i) || [])[1] ?? NaN);
  }
  const cartSignature = rows => JSON.stringify(rows.map(r => [r.id, r.style, r.url, r.color, r.size, r.method, r.warehouse, r.quantity]));
  async function loadedCart(expectedQuantity) {
    if (expectedQuantity !== undefined && (!Number.isInteger(expectedQuantity) || expectedQuantity < 0)) throw Error('Invalid S&S expected cart quantity.');
    return waitFor(() => {
      if (!document.querySelector('#M_M_zCart')) return false;
      const rows = cartRows(), count = cartCount();
      if (!Number.isInteger(count) || count < 0 || rows.some(r => !r.url || !r.style || !r.color || !r.size || !r.warehouse || !Number.isInteger(r.quantity) || r.quantity <= 0)) return false;
      const total = rows.reduce((sum, row) => sum + row.quantity, 0);
      return total === count && (expectedQuantity === undefined || total === expectedQuantity) && rows;
    }, 'S&S cart quantities did not finish loading. Review the cart before retrying.', 25000);
  }
  function confirmationControls() {
    const root = document.getElementById('M_M_zConfirmOrderPopup_PW-1')
      || document.getElementById('M_M_zConfirmOrderPopup_PWC-1');
    if (!root || !/Are you sure you want to submit your order\?/i.test(norm(root.textContent))) return [];
    const controls = [...root.querySelectorAll('button,input[type="submit"],input[type="button"],[role="button"],.dxbButton')]
      .filter(e => visible(e) && !e.disabled && /^(?:confirm|yes)$/i.test(norm(e.value || e.innerText || e.getAttribute('aria-label'))));
    // DevExpress can expose one button as a wrapper and a nested submit input.
    return controls.filter(e => !controls.some(other => other !== e && other.contains(e)));
  }
  function confirmationDiagnostics() {
    const root = document.getElementById('M_M_zConfirmOrderPopup_PW-1')
      || document.getElementById('M_M_zConfirmOrderPopup_PWC-1');
    return root ? { id: root.id, display: getComputedStyle(root).display, visibility: getComputedStyle(root).visibility,
      controls: [...root.querySelectorAll('button,input[type="submit"],input[type="button"],[role="button"],.dxbButton')].map(e => ({
        id: e.id, tag: e.tagName, label: norm(e.value || e.innerText || e.getAttribute('aria-label')).slice(0, 80), visible: visible(e), disabled: !!e.disabled })) } : null;
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
      signed_in: signedIn(),
      cart_container: !!document.querySelector('#M_M_zCart'), hidden: document.hidden,
      cart_count: Number.isInteger(cartCount()) ? cartCount() : null,
      checkout_ready: /^\/checkout\/?$/.test(location.pathname) && signedIn() && visible(document.querySelector('#M_M_zPONumberTB')),
      confirmation_controls: confirmationControls().map(e => ({ id: e.id, tag: e.tagName, label: norm(e.value || e.innerText || e.getAttribute('aria-label')) })),
      confirmation_popup: confirmationDiagnostics(),
      visibility: document.visibilityState, title: document.title,
      control_state: all('#aToCDesk, #aToCMob, #ninjaAddToCart, #zCheckoutBTN, #M_M_zCheckoutBTN, #M_M_zConfirmOrderPopup_zCompleteBTN, #M_M_zCart .cR a[aria-label="remove item"]').map(e => ({
        id: e.id, tag: e.tagName, visible: visible(e), handler: ((e.getAttribute('href') || '').match(/^javascript:\s*([A-Za-z_]\w*)\(/) || [])[1] || '' })),
      product_state: {
        color_text: norm(document.querySelector('#zcolorname')?.textContent),
        colors: all('#colorSwatch a').slice(0, 150).map(a => ({ id: a.id, color: norm(a.textContent),
          visible: visible(a), active: a.classList.contains('active'), href: a.getAttribute('href') })),
        warehouse_headers: all('.gR[id^="wh_"]').slice(0, 30).map(r => ({
          warehouse: norm(r.querySelector('.warehouse')?.textContent),
          shipping: norm(r.querySelector('.shipby')?.textContent) }))
      }
    }),
    candidates: async () => {
      const read = () => {
        // Product links can use display:contents in S&S's accessibility layout.
        // Read the visible card's labelled fields rather than the link's box or
        // its separate "+72 more colors" link.
        const rows = all('#M_M_zProducts .gProduct').filter(visible).flatMap(card => {
          const urls = [...new Set([...card.querySelectorAll('a[href*="/p/"]')].map(a => {
            try {
              const url = new URL(a.href);
              return url.origin === location.origin && /^\/p\/[^/]+\/[^/]+\/?$/.test(url.pathname)
                ? url.origin + url.pathname.replace(/\/$/, '') : '';
            } catch { return ''; }
          }).filter(Boolean))];
          const fields = ['.gName', '.gBrand', '.gTitle'].map(selector => norm(card.querySelector(selector)?.textContent));
          return urls.length === 1 && fields.every(Boolean) ? [{ url: urls[0], title: fields.join(' ') }] : [];
        });
        if (!rows.length) rows.push(...all('a[href*="/p/"]').filter(visible)
          .filter(a => !a.closest('.gProduct')).map(a => ({ url: a.href, title: norm(a.innerText) })).filter(x => x.title));
        if (/^\/p\/[^/]+\/[^/]+\/?$/.test(location.pathname)) {
          const title = norm(document.querySelector('#productMainContainer')?.innerText);
          if (title) rows.push({ url: location.origin + location.pathname.replace(/\/$/, ''), title });
        }
        return rows.length && rows;
      };
      return { candidates: await waitFor(read, 'S&S product results did not load.') };
    },
    color_ready: async ({ color }) => {
      await waitFor(() => all('#colorSwatch a.active').length === 1
        && /Selected Color:/i.test(norm(document.querySelector('#zcolorname')?.textContent))
        && all('.gR[id^="wh_"] input[aria-label="quantity"]').length,
      'S&S initial product selection did not finish loading.', 25000);
      const matches = all('#colorSwatch a').filter(a => colorKey(a.textContent) === colorKey(color));
      if (matches.length !== 1 || !visible(matches[0]) || !/^cSwatch\d+$/.test(matches[0].id)) throw Error('S&S color is missing or ambiguous.');
      return { anchor_id: matches[0].id };
    },
    color: async ({ color }) => {
      await waitFor(() => {
        if (colorKey(selectedColor()) !== colorKey(color)) return false;
        const active = all('#colorSwatch a.active').filter(a => colorKey(a.textContent) === colorKey(color));
        if (active.length !== 1 || !/^cSwatch\d+$/.test(active[0].id)) return false;
        const inputs = all('.gR[id^="wh_"] input[aria-label="quantity"]');
        return inputs.length && inputs.every(input => input.getAttribute('csid') === active[0].id.slice('cSwatch'.length));
      }, 'S&S selected color and inventory did not verify.', 25000);
      return {};
    },
    inventory: async () => {
      await waitFor(() => all('.gR[id^="wh_"]').some(row => /\b\d{1,2}\/\d{1,2}\b/.test(deliveryText(row))), 'S&S warehouse delivery estimates did not load.', 25000);
      return { inventory: all('.gR[id^="wh_"]').map(r => ({
      warehouse: norm(r.querySelector('.warehouse')?.textContent), eta_text: deliveryText(r),
      stock: Object.fromEntries(stockCells(r).filter(c => c.input).map(c => [c.size, norm(c.cell.querySelector('.quantity')?.textContent)]))
    })).filter(r => r.warehouse) };
    },
    cart: async ({ expected_quantity }) => ({ rows: await loadedCart(expected_quantity) }),
    verified_cart: async ({ lines }) => {
      if (!Array.isArray(lines) || !lines.length) throw Error('S&S expected allocation is missing.');
      const original = await loadedCart(), rows = structuredClone(original);
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
          const cells = stockCells(matches[0]).filter(c => c.size === sizeKey(size));
          if (cells.length !== 1) throw Error('S&S size cell is ambiguous.');
          const input = cells[0].input;
          const stock = Number(norm(cells[0].cell.querySelector('.quantity')?.textContent).replace(/\D/g, ''));
          if (!input || input.disabled || stock < quantity) throw Error('S&S stock is no longer available.');
          fills.push([input, quantity]);
        }
      }
      for (const input of all('.gR input[aria-label="quantity"]')) inputValue(input, '');
      for (const [input, quantity] of fills) inputValue(input, quantity);
      if (!fills.every(([input, quantity]) => Number(input.value) === quantity)) throw Error('S&S quantities did not remain entered.');
      return {};
    },
    add_ready: async ({ quantity }) => {
      const before = cartCount();
      if (!Number.isInteger(before) || !Number.isInteger(quantity) || quantity <= 0) throw Error('S&S cart count or added quantity is invalid.');
      unique('#aToCDesk, #aToCMob, #ninjaAddToCart');
      return { before };
    },
    add: async ({ quantity, before }) => {
      if (!Number.isInteger(before) || !Number.isInteger(quantity) || quantity <= 0) throw Error('S&S stock addition baseline is missing.');
      await waitFor(() => cartCount() === before + quantity, 'S&S did not confirm adding stock. Review the cart before retrying.');
      return {};
    },
    remove_ready: async ({ rows }) => {
      if (!Array.isArray(rows) || !rows.length || cartSignature(rows) !== cartSignature(cartRows())) throw Error('S&S cart changed before removal.');
      const first = all('#M_M_zCart .cR').filter(visible)[0];
      const buttons = [...first.querySelectorAll('a[aria-label="remove item"]')].filter(visible);
      if (buttons.length !== 1) throw Error('S&S cart removal control is ambiguous.');
      return {};
    },
    remove: async ({ rows }) => {
      if (!Array.isArray(rows) || !rows.length) throw Error('S&S expected cart removal is missing.');
      await waitFor(() => cartRows().length === rows.length - 1, 'S&S stock removal did not verify.');
      const following = cartRows();
      if (cartSignature(following) !== cartSignature(rows.slice(1))) throw Error('Other S&S cart stock changed during removal.');
      return { rows: following };
    },
    checkout: async () => { unique('#zCheckoutBTN').click(); await waitFor(() => visible(document.querySelector('#M_M_zPONumberTB')), 'S&S checkout did not load.'); return {}; },
    review: async ({ po }) => {
      await waitFor(() => visible(document.querySelector('#M_M_zPONumberTB'))
        && document.querySelector('#M_M_zTermsCB')?.selectedOptions.length
        && all('select[id$="zFreightServiceCB"]').length
        && all('select[id$="zFreightServiceCB"]').every(s => s.selectedOptions.length), 'S&S checkout details did not finish loading.');
      return checkoutState(po, true);
    },
    valid: async ({ po }) => checkoutState(po),
    begin_submit_ready: async ({ po }) => {
      checkoutState(po);
      unique('#M_M_zCheckoutBTN');
      return {};
    },
    begin_submit: async ({ po }) => {
      checkoutState(po);
      await waitFor(() => confirmationControls().length, 'S&S final confirmation did not open.');
      return {};
    },
    confirm_submit_ready: async ({ po }) => {
      checkoutState(po);
      const controls = confirmationControls();
      if (controls.length !== 1) throw Error('S&S final confirmation control is missing or ambiguous.');
      return {};
    },
    confirm_submit: async () => ({ click_issued: true }),
    confirmation: async () => ({ text: (document.body?.innerText || '').slice(0, 50000) })
  };
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type !== 'sxs:page-command') return false;
    if (sender.id !== chrome.runtime.id || sender.tab || !/^[a-f0-9]{32}$/.test(message.commandId || '')) return false;
    (async () => {
      guard();
      if (!Object.hasOwn(commands, message.operation) || !Number.isFinite(message.deadline) || Date.now() >= message.deadline * 1000) throw Error('Unsupported or expired S&S command.');
      if (['fill', 'add_ready', 'add', 'remove_ready', 'remove', 'checkout', 'review', 'begin_submit_ready', 'begin_submit', 'confirm_submit_ready', 'confirm_submit'].includes(message.operation)) {
        if (!/^\d{7}$/.test(message.orderId || '')) throw Error('S&S changes require an active Reorder.');
        if (!signedIn()) throw Error('S&S login expired. Sign in normally before retrying.');
      }
      if (['begin_submit_ready', 'begin_submit', 'confirm_submit_ready', 'confirm_submit'].includes(message.operation) && (!/^\d{7}$/.test(message.orderId || '') || message.payload?.po !== message.orderId)) throw Error('S&S purchase does not match the active Reorder.');
      await consent();
      return { ...(await commands[message.operation](message.payload || {})), url: location.href };
    })().then(result => sendResponse({ success: true, result }), error => sendResponse({ success: false, message: error.message || 'S&S operation failed.' }));
    return true;
  });
})();
