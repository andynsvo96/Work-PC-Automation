// Selenium adapter for the same native service and batch receipts as Order All.
// CRMOrderAllBatch is loaded immediately before this script by the worker.
const [action, expectedOrderId, options = {}] = arguments;
return (() => {
  const batch = CRMOrderAllBatch;
  const key = `crm-order-all-native:${expectedOrderId}`;
  const text = value => String(value || '').replace(/\s+/g, ' ').trim();
  const removed = design => String(design?.crudAction || '').toLowerCase() === 'd';
  const fail = message => ({ ready: false, error: message });
  const root = document.getElementById('view-orders-index');
  if (!root || !window.angular?.element) return fail('CRM order inventory is not ready.');
  let scope = angular.element(root).scope();
  for (let hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
    if (String(scope.order?.getResource?.()?.id) === String(expectedOrderId) && typeof scope.saveOrder === 'function') break;
  }
  if (!scope || String(scope.order?.getResource?.()?.id) !== String(expectedOrderId) || typeof scope.saveOrder !== 'function') return fail('The expected CRM order is not loaded.');
  if (scope.editMode || scope.transfersOnly || scope.savingOrder) return fail('CRM must finish editing or saving before ordering stock.');
  const resource = scope.order.getResource();
  const details = document.getElementById('order-design-details');
  if (!details || !Array.isArray(resource.designs)) return fail('CRM design inventory is not ready.');

  let prior;
  try {
    const saved = sessionStorage.getItem(key);
    prior = saved ? JSON.parse(saved) : null;
    if (prior && !batch.valid(prior, expectedOrderId)) return fail('The existing Order All receipt needs manual stock review.');
  } catch (_) { return fail('The existing Order All receipt cannot be read. Check CRM stock before retrying.'); }
  if (action === 'status') {
    return prior?.automationId === options.automationId ? { ready: true, record: prior }
      : fail('The native stock result receipt was lost. Check CRM stock before retrying.');
  }
  if (action !== 'verify' && (sessionStorage.getItem(`crm-order-process-active:${expectedOrderId}`) ||
      sessionStorage.getItem(`crm-order-all-request:${expectedOrderId}`) ||
      prior?.entries.some(entry => ['prepared', 'submitted'].includes(entry.state) ||
        (entry.state === 'review' && !entry.failureConfirmed)))) {
    return fail('An earlier stock request is pending or uncertain. Check its results before ordering again.');
  }

  // Inactive tab panes are intentionally included. Hidden native controls and
  // hidden provider rows inside those panes retain their normal exclusions.
  function hidden(button) {
    for (let node = button; node && node !== details; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.hidden || node.classList.contains('ng-hide') || style.visibility === 'hidden' ||
          style.visibility === 'collapse' || (style.display === 'none' && !node.classList.contains('tab-pane'))) return true;
    }
    return false;
  }
  function savedStock(node) {
    return [...node.querySelectorAll('tr,[role="row"],.row')].some(row => {
      if (row.closest('.modal,[role="dialog"]') || hidden(row)) return false;
      const value = text([...row.children].map(cell => cell.textContent).join(' ') || row.textContent);
      return /^(sanmar(?:\s*\(bulk\))?|s\s*&\s*s(?:\s+activewear)?|s\s+and\s+s(?:\s+activewear)?|ssactivewear|local\s+inventory)\s*:?\s+[a-z0-9][a-z0-9._/-]{1,89}(?:\s|$)/i.test(value) &&
        /\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/.test(value) &&
        /vendor\s+order\s*#/i.test(node.textContent) && /order\s+date/i.test(node.textContent);
    });
  }
  const designs = resource.designs.filter(design => design && !removed(design));
  if (!designs.length || designs.some(design => !/^\d+$/.test(String(design.id))) ||
      new Set(designs.map(design => String(design.id))).size !== designs.length) return fail('CRM stock tab IDs are missing or ambiguous.');
  const targets = new Map();
  const states = designs.map((design, index) => {
    const owners = [...details.querySelectorAll('[order-inventory]')].map(node => {
      try {
        let owner = angular.element(node).isolateScope?.();
        if (!owner) {
          let candidate = angular.element(node.querySelector('button[ng-click="orderGoods(provider);"]') || node).scope();
          for (let hops = 0; candidate && hops < 12; candidate = candidate.$parent, hops++) {
            if (Object.hasOwn(candidate, 'orderGoods') && typeof candidate.orderGoods === 'function') { owner = candidate; break; }
          }
        }
        return { node, owner };
      } catch (_) { return {}; }
    }).filter(({ owner }) => owner?.design?.() === design && owner.order?.()?.getResource?.() === resource);
    const state = { id: String(design.id), tab: index + 1, fingerprint: batch.fingerprint(design), eligible: false, stockRecorded: false, kind: 'unavailable' };
    if (owners.length !== 1) { state.kind = 'initializing'; return state; }
    const { node, owner } = owners[0];
    if (!Array.isArray(owner.providers) || (!owner.providers.length && Number(owner.itemCount) > 0) ||
        typeof owner.isOrderingLocked !== 'function' || typeof owner.orderGoods !== 'function') {
      state.kind = 'initializing'; return state;
    }
    const automatic = owner.providers.filter(provider => provider.id === null);
    state.stockRecorded = automatic.some(provider => provider.goodsPreviouslyOrdered || provider.stockOrdered || provider.allStockOrdered) || savedStock(node);
    state.kind = state.stockRecorded ? 'ordered' : automatic.length ? 'unavailable' : 'manual';
    if (owner.placingOrder) { state.kind = 'busy'; return state; }
    for (const button of node.querySelectorAll('button[ng-click="orderGoods(provider);"]')) {
      const row = button.closest('tr[ng-repeat="provider in providers"]');
      const label = row?.querySelector(':scope > th') || row?.querySelector(':scope > td');
      if (!/sanmar\s*\/\s*s\s*&\s*s\s+activewear/i.test(label?.textContent || '')) continue;
      const provider = angular.element(button).scope()?.provider;
      if (!provider || !owner.providers.includes(provider) || provider.id !== null || hidden(button) ||
          provider.goodsPreviouslyOrdered || provider.stockOrdered || owner.pullStock ||
          (provider.allStockOrdered && !provider.forceDisplay) || !(Number(owner.itemCount) > 0)) continue;
      if (button.disabled || button.getAttribute('aria-disabled') === 'true' || owner.isOrderingLocked(provider)) {
        state.kind = 'locked'; continue;
      }
      state.kind = 'eligible';
      state.eligible = true;
      targets.set(state.id, { ...state, design, owner });
    }
    return state;
  });
  if (states.some(state => state.kind === 'initializing')) return fail('CRM design inventory is not ready.');
  const history = [...(prior?.history || []), ...(prior?.entries || []).filter(entry => entry.state === 'confirmed')]
    .map(({ id, fingerprint }) => ({ id, fingerprint }));
  if (action === 'inspect' || action === 'verify') return { ready: true, designs: states, history };
  if (action !== 'start') return fail('Unknown native stock action.');

  // Revalidate all IDs and quantities immediately before the first request.
  const selected = options.entries?.map(entry => targets.get(entry.id));
  if (!selected?.length || selected.some((target, index) => !target || target.fingerprint !== options.entries[index].fingerprint) ||
      JSON.stringify(states.map(({ id, fingerprint }) => [id, fingerprint])) !== JSON.stringify(options.designs.map(({ id, fingerprint }) => [id, fingerprint]))) {
    return { ...fail('CRM stock changed before submission. Recheck the order.'), notSubmitted: true };
  }
  let service;
  try {
    service = (angular.element(root).injector?.() || angular.element(document.body).injector?.())?.get('orders/inventory/InventorySrv');
    if (typeof service?.orderGoodsSupplynet !== 'function') throw new Error();
  } catch (_) { return { ...fail('CRM native Order Goods service is not ready.'), notSubmitted: true }; }
  const record = batch.create(String(expectedOrderId), selected, options.previous);
  record.automationId = options.automationId;
  function inAngular(owner, fn) {
    let result;
    const run = () => { result = fn(); };
    if (owner.$root?.$$phase) run();
    else owner.$apply(run);
    return result;
  }
  batch.run(record, selected, {
    timeoutMs: options.timeoutMs,
    save: value => sessionStorage.setItem(key, JSON.stringify(value)),
    changed() {},
    submit(target) {
      const request = inAngular(target.owner, () => {
        target.owner.placingOrder = true;
        return service.orderGoodsSupplynet(target.design.id);
      });
      return Promise.resolve(request).then(response => {
        const entry = record.entries.find(entry => entry.id === target.id);
        entry.failureConfirmed = response?.succeeded === false && !response.supplier;
        return response;
      });
    },
    finished(target, entry) {
      if (entry.state !== 'confirmed') inAngular(target.owner, () => { target.owner.placingOrder = false; });
    }
  }).catch(() => {
    record.phase = 'review';
    record.message = 'Could not save the native stock receipt. Check CRM stock before retrying.';
    try { sessionStorage.setItem(key, JSON.stringify(record)); } catch (_) { /* The durable worker reservation still blocks resubmission. */ }
  });
  return { ready: true, record };
})();
