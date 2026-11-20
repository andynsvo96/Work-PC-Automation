// MAIN world: the same InventorySrv request used by CRM's Order Goods button.
// Submit eligible tabs immediately and keep one result receipt per order/tab.
(() => {
  "use strict";
  if (window.__crmOrderAllInstalled) return;
  window.__crmOrderAllInstalled = true;
  const batch = CRMOrderAllBatch;
  const ids = ["crm-order-all-header", "crm-order-all-detail"];
  let currentId = "";
  let receipt = null;
  let inFlight = false;
  let queued = false;
  let message = "";
  const receiptKey = id => `crm-order-all-native:${id}`;

  function orderId() {
    const direct = location.pathname.match(/^\/order\/(\d{7})\/?$/);
    const embedded = /^\/app\/?$/.test(location.pathname) && location.hash.match(/^#!?\/?order\/(\d{7})\/?(?:[?#].*)?$/);
    return (direct || embedded)?.[1] || "";
  }

  function context() {
    const id = orderId();
    const root = document.getElementById("view-orders-index");
    if (!id || !root || !window.angular?.element) return null;
    let scope;
    try { scope = angular.element(root).scope(); } catch (_) { return null; }
    for (let hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
      if (scope.order?.getResource && String(scope.order.getResource()?.id) === id && typeof scope.saveOrder === "function") {
        return { id, root, scope, resource: scope.order.getResource(), details: document.getElementById("order-design-details") };
      }
    }
    return null;
  }

  function targets(ctx) {
    if (!ctx?.details || ctx.scope.editMode || ctx.scope.transfersOnly || ctx.scope.savingOrder) return [];
    const found = new Map();
    for (const button of ctx.details.querySelectorAll('.order-inventory button[ng-click="orderGoods(provider);"]')) {
      if (button.disabled || button.getAttribute("aria-disabled") === "true") continue;
      const row = button.closest('tr[ng-repeat="provider in providers"]');
      const label = row?.querySelector(":scope > th") || row?.querySelector(":scope > td");
      if (!/sanmar\s*\/\s*s\s*&\s*s\s+activewear/i.test(label?.textContent || "")) continue;
      let hidden = false;
      for (let node = button; node && node !== ctx.details; node = node.parentElement) {
        if (node.hidden || node.classList.contains("ng-hide") || node.style.display === "none" ||
            getComputedStyle(node).visibility === "hidden") { hidden = true; break; }
      }
      if (hidden) continue;
      let owner;
      let rowScope;
      try {
        rowScope = angular.element(button).scope();
        owner = angular.element(button.closest("[order-inventory]")).isolateScope?.();
      } catch (_) { continue; }
      if (!owner) {
        for (let scope = rowScope, hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
          if (Object.hasOwn(scope, "orderGoods") && typeof scope.orderGoods === "function") { owner = scope; break; }
        }
      }
      try {
        const provider = rowScope?.provider;
        const design = owner?.design?.();
        const order = owner?.order?.();
        const tab = ctx.resource.designs.indexOf(design) + 1;
        if (!tab || !/^\d+$/.test(String(design.id)) || design.crudAction === "d" ||
            order?.getResource?.() !== ctx.resource || owner.placingOrder || !(Number(owner.itemCount) > 0) ||
            typeof owner.orderGoods !== "function" || !owner.providers?.includes(provider) || provider.id !== null ||
            provider.goodsPreviouslyOrdered || provider.stockOrdered || owner.pullStock ||
            (provider.allStockOrdered && !provider.forceDisplay) || typeof owner.isOrderingLocked !== "function" || owner.isOrderingLocked(provider)) continue;
        found.set(String(design.id), { id: String(design.id), tab, design, owner, fingerprint: batch.fingerprint(design) });
      } catch (_) { /* Initializing or unsupported native rows remain untouched. */ }
    }
    return [...found.values()].sort((a, b) => a.tab - b.tab);
  }

  function save(record) { sessionStorage.setItem(receiptKey(record.orderId), JSON.stringify(record)); }

  function priorAutomationPending(id) {
    // Honor a request already submitted by older extension versions. Reading
    // its receipt does not contact or start the local Automation app.
    try { return Boolean(sessionStorage.getItem(`crm-order-process-active:${id}`) || sessionStorage.getItem(`crm-order-all-request:${id}`)); }
    catch (_) { return true; }
  }

  function inventoryBusy(ctx) {
    for (const node of ctx.details.querySelectorAll("[order-inventory]")) {
      try {
        const owner = angular.element(node).isolateScope?.();
        if (owner?.order?.()?.getResource?.() === ctx.resource && owner.design?.()?.crudAction !== "d" && owner.placingOrder) return true;
      } catch (_) { return true; }
    }
    return false;
  }

  function inAngular(scope, fn) {
    let result;
    let error;
    const run = () => { try { result = fn(); } catch (failure) { error = failure; } };
    if (scope.$root?.$$phase) run();
    else scope.$apply(run);
    if (error) throw error;
    return result;
  }

  async function start() {
    const ctx = context();
    if (!ctx || inFlight || inventoryBusy(ctx) || priorAutomationPending(ctx.id)) return;
    const selected = targets(ctx);
    if (!selected.length || batch.blocked(receipt, selected)) return;
    let service;
    try {
      const injector = angular.element(ctx.root).injector?.() || angular.element(document.body).injector?.();
      service = injector?.get("orders/inventory/InventorySrv");
      if (typeof service?.orderGoodsSupplynet !== "function") throw new Error();
    } catch (_) { message = "CRM's Order Goods action is not ready. Refresh the order and try again."; sync(); return; }
    receipt = batch.create(ctx.id, selected, receipt);
    inFlight = true;
    message = "";
    try {
      await batch.run(receipt, selected, {
        save,
        submit(target) {
          return inAngular(target.owner, () => {
            target.owner.placingOrder = true;
            return service.orderGoodsSupplynet(target.design.id);
          });
        },
        finished(target, entry) {
          if (entry.state !== "confirmed") inAngular(target.owner, () => { target.owner.placingOrder = false; });
        },
        changed() { sync(); }
      });
      if (receipt.phase === "confirmed" && orderId() === ctx.id && !ctx.scope.editMode) {
        // Use one refresh after all native requests finish, rather than the
        // separate five-second reload scheduled by each individual button.
        setTimeout(() => { if (orderId() === ctx.id && !ctx.scope.editMode) window.location.reload(); }, 1500);
      }
    } catch (_) {
      if (receipt.entries.every(entry => entry.state === "prepared")) receipt = null;
      message = "Could not record the stock requests. Check CRM stock before trying again.";
    } finally { inFlight = false; sync(); }
  }

  function control(id, tag) {
    const wrapper = document.createElement(tag);
    wrapper.id = id;
    wrapper.className = "crm-order-all-control";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-success crm-order-all-button";
    button.textContent = "Order All";
    button.addEventListener("click", start);
    wrapper.append(button);
    return wrapper;
  }

  function clear() {
    for (const id of [...ids, "crm-order-all-result"]) document.getElementById(id)?.remove();
  }

  function renderResult(ctx) {
    const existing = document.getElementById("crm-order-all-result");
    if (!receipt && !message) { existing?.remove(); return; }
    const result = existing || document.createElement("p");
    result.id = "crm-order-all-result";
    result.setAttribute("role", "status");
    result.setAttribute("aria-live", "polite");
    if (result.className !== "crm-order-all-result") result.className = "crm-order-all-result";
    const entries = receipt?.entries || [];
    const confirmed = entries.filter(entry => entry.state === "confirmed");
    const review = entries.filter(entry => entry.state === "review");
    const progress = inFlight ? `Ordering stock in CRM: ${confirmed.length}/${entries.length} tabs confirmed.`
      : receipt?.phase === "confirmed" ? `CRM confirmed stock ordered on tabs ${confirmed.map(entry => entry.tab).join(", ")}.`
      : "Check the stock on the listed tabs before ordering again.";
    const text = message || [receipt?.message, progress,
      ...review.map(entry => `Tab ${entry.tab}: ${entry.message}`),
      entries.some(entry => entry.state === "not_sent") ? `Not submitted: tabs ${entries.filter(entry => entry.state === "not_sent").map(entry => entry.tab).join(", ")}.` : ""
    ].filter(Boolean).join(" ");
    if (result.textContent !== text) result.textContent = text;
    if (!existing) ctx.details.querySelector(".nav-tabs")?.after(result);
  }

  function sync() {
    const ctx = context();
    if (!ctx?.details) { clear(); return; }
    if (currentId !== ctx.id) {
      if (inFlight) { clear(); return; }
      currentId = ctx.id;
      receipt = null;
      message = "";
      try {
        const stored = sessionStorage.getItem(receiptKey(ctx.id));
        if (stored) {
          const record = JSON.parse(stored);
          if (!batch.valid(record, ctx.id)) throw new Error();
          receipt = batch.resume(record);
          save(receipt);
        }
      } catch (_) { message = "Could not read the previous stock request. Check CRM stock before ordering again."; }
    }
    if (ctx.scope.editMode) { clear(); return; }
    const available = targets(ctx);
    if (available.length || inFlight) {
      const header = [...document.querySelectorAll('#main-header-design-tabs button[ng-repeat]')].at(-1);
      const detail = [...ctx.details.querySelectorAll(".nav-tabs .order-form-design-tab")].at(-1);
      for (const [id, native, tag] of [[ids[0], header, "span"], [ids[1], detail, "li"]]) {
        if (!native) continue;
        const wrapper = document.getElementById(id) || control(id, tag);
        if (native.nextElementSibling !== wrapper) native.after(wrapper);
        const button = wrapper.querySelector("button");
        const disabled = inFlight || inventoryBusy(ctx) || priorAutomationPending(ctx.id) || Boolean(message) || batch.blocked(receipt, available);
        if (button.disabled !== disabled) button.disabled = disabled;
        button.title = priorAutomationPending(ctx.id) ? "An earlier automation request may still be active. Check that queue entry before ordering again."
          : disabled ? "Stock is loading, requests are pending, or a previous result needs review."
          : `Press CRM Order Goods for tabs ${available.map(target => target.tab).join(", ")}.`;
      }
    } else for (const id of ids) document.getElementById(id)?.remove();
    renderResult(ctx);
  }

  function schedule() {
    if (queued) return;
    queued = true;
    setTimeout(() => { queued = false; sync(); }, 60);
  }
  new MutationObserver(schedule).observe(document, { subtree: true, childList: true,
    attributes: true, attributeFilter: ["class", "disabled", "hidden", "style"] });
  window.addEventListener("hashchange", schedule);
  window.addEventListener("popstate", schedule);
  window.setInterval(sync, 800);
  sync();
})();
