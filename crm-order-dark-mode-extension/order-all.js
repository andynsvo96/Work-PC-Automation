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
  let receiptReadError = false;
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

  function hasMultipleDesigns(ctx) {
    return Array.isArray(ctx?.resource?.designs) && ctx.resource.designs.filter(design =>
      design && String(design.crudAction || "").trim().toLowerCase() !== "d").length > 1;
  }

  function targets(ctx, allowSingle = false) {
    if (!ctx?.details || (!allowSingle && !hasMultipleDesigns(ctx)) || ctx.scope.editMode || ctx.scope.transfersOnly || ctx.scope.savingOrder) return [];
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

  function loadReceipt(ctx) {
    if (currentId === ctx.id) return;
    if (inFlight) throw Error("Stock requests for the previous order are still pending.");
    currentId = ctx.id;
    receipt = null;
    message = "";
    receiptReadError = false;
    try {
      const stored = sessionStorage.getItem(receiptKey(ctx.id));
      if (stored) {
        const record = JSON.parse(stored);
        if (!batch.valid(record, ctx.id)) throw new Error();
        receipt = batch.resume(record);
        save(receipt);
      }
    } catch (_) {
      receiptReadError = true;
      message = "Could not read the previous stock request. Check CRM stock before ordering again.";
    }
  }

  function pendingDraftOrDialog(ctx) {
    return ["addSalesNotes", "addProductionNotes", "addArtNotes"].some(name => String(ctx.resource[name] || "").trim()) ||
      Boolean(window.__crmAllTabsHasPendingChanges?.()) ||
      [...document.querySelectorAll('.modal, [role="dialog"], .ngdialog-content')].some(node =>
        node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
  }

  function stockSkipReason(ctx) {
    if (ctx.scope.editMode) return "In Edit Order; stock was skipped.";
    if (ctx.scope.transfersOnly) return "Transfers-only order; stock was skipped.";
    if (ctx.scope.saving || ctx.scope.savingOrder || ctx.scope.fileUploading || pendingDraftOrDialog(ctx)) {
      return "This order has pending edits, a dialog, or a native action in progress. Resolve it in CRM first.";
    }
    if (inFlight || inventoryBusy(ctx) || priorAutomationPending(ctx.id)) return "Another stock request may still be active. Review it in CRM first.";
    if (receiptReadError) return message || "Could not read the previous stock request. Check CRM stock before ordering again.";
    if (receipt?.entries.some(entry => ["submitted", "review"].includes(entry.state))) return "A previous stock request needs review. Check this order's stock in CRM before retrying from its order page.";
    return "";
  }

  function inspectStock(id) {
    const ctx = context();
    if (!ctx || ctx.id !== id) return null;
    loadReceipt(ctx);
    const skipReason = stockSkipReason(ctx);
    return { success: true, adapterVersion: 1, orderId: ctx.id, skipReason,
      reviewRequired: receiptReadError || Boolean(receipt?.entries.some(entry => ["submitted", "review"].includes(entry.state))),
      confirmed: [...(receipt?.history || []), ...(receipt?.entries || []).filter(entry => entry.state === "confirmed")]
        .map(({ id, fingerprint }) => ({ id, fingerprint })),
      designs: (ctx.resource.designs || []).filter(design => design && String(design.crudAction || "").trim().toLowerCase() !== "d")
        .map(design => ({ id: String(design.id), fingerprint: batch.fingerprint(design) })),
      targets: skipReason ? [] : batch.retryable(receipt, targets(ctx, true)).map(({ id, tab, fingerprint }) => ({ id, tab, fingerprint })) };
  }

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
        const design = owner?.design?.();
        if (owner?.order?.()?.getResource?.() === ctx.resource && design?.crudAction !== "d" && owner.placingOrder) {
          // Successful native stock requests normally remain busy until reload.
          // They must not keep failed tabs from being retried in a partial run.
          const confirmed = design && batch.retryable(receipt, [{ id: String(design.id), fingerprint: batch.fingerprint(design) }]).length === 0;
          if (!confirmed) return true;
        }
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

  async function start(command, attempt = null) {
    const fromWindow = command?.op === "order";
    const ctx = context();
    if (fromWindow) {
      if (!ctx || ctx.id !== command.orderId) throw Error("This page no longer contains the expected CRM order.");
      loadReceipt(ctx);
      const reason = stockSkipReason(ctx);
      if (reason) throw Error(reason);
    }
    if (!ctx || inFlight || inventoryBusy(ctx) || priorAutomationPending(ctx.id)) return;
    let selected = batch.retryable(receipt, targets(ctx, fromWindow));
    if (fromWindow) {
      if (!Array.isArray(command.targets) || !command.targets.length || new Set(command.targets.map(target => target.id)).size !== command.targets.length) {
        throw Error("The stock tabs could not be identified uniquely. Scan this window again.");
      }
      selected = command.targets.map(expected => {
        const target = selected.find(target => target.id === expected.id && target.fingerprint === expected.fingerprint);
        if (!target) throw Error("Stock changed after this window was read. Review this order before ordering again.");
        return target;
      });
    }
    if (!selected.length || batch.blocked(receipt, selected)) return;
    let service;
    try {
      const injector = angular.element(ctx.root).injector?.() || angular.element(document.body).injector?.();
      service = injector?.get("orders/inventory/InventorySrv");
      if (typeof service?.orderGoodsSupplynet !== "function") throw new Error();
    } catch (_) {
      message = "CRM's Order Goods action is not ready. Refresh the order and try again.";
      sync();
      if (fromWindow) throw Error(message);
      return;
    }
    const record = receipt = batch.create(ctx.id, selected, receipt);
    inFlight = true;
    message = "";
    try {
      await batch.run(record, selected, {
        save,
        submit(target) {
          if (attempt) attempt.submitted = true;
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
      if (record.phase === "confirmed" && orderId() === ctx.id && !ctx.scope.editMode) {
        // Use one refresh after all native requests finish, rather than the
        // separate five-second reload scheduled by each individual button.
        setTimeout(() => { if (orderId() === ctx.id && !ctx.scope.editMode && !pendingDraftOrDialog(ctx)) window.location.reload(); }, 1500);
      }
    } catch (_) {
      const notSubmitted = record.entries.every(entry => entry.state === "prepared");
      if (notSubmitted) receipt = null;
      message = "Could not record the stock requests. Check CRM stock before trying again.";
      if (fromWindow) return { success: false, orderId: ctx.id, notSubmitted, message };
    } finally { inFlight = false; sync(); }
    if (fromWindow) return { success: true, adapterVersion: 1, orderId: ctx.id, phase: record.phase,
      entries: record.entries.map(({ id, tab, fingerprint, state, message }) => ({ id, tab, fingerprint, state, message })) };
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
    const result = existing || document.createElement("div");
    result.id = "crm-order-all-result";
    result.setAttribute("role", "status");
    result.setAttribute("aria-live", "polite");
    if (result.className !== "crm-order-all-result") result.className = "crm-order-all-result";
    const entries = receipt?.entries || [];
    const confirmed = entries.filter(entry => entry.state === "confirmed");
    const review = entries.filter(entry => entry.state === "review");
    const progress = inFlight ? `Ordering stock in CRM: ${confirmed.length}/${entries.length} tabs confirmed.`
      : confirmed.length ? `CRM confirmed stock ordered on tabs ${confirmed.map(entry => entry.tab).join(", ")}.` : "";
    const notSent = entries.filter(entry => entry.state === "not_sent");
    const content = JSON.stringify([message, receipt?.message, progress, review.map(entry => [entry.tab, entry.message]), notSent.map(entry => entry.tab)]);
    if (result.dataset.content !== content) {
      result.replaceChildren();
      for (const text of [message, receipt?.message, progress].filter(Boolean)) {
        const line = document.createElement("div");
        line.textContent = text;
        result.append(line);
      }
      for (const entry of review) {
        const line = document.createElement("div");
        line.className = "crm-order-all-error";
        const label = document.createElement("strong");
        label.textContent = `Tab ${entry.tab}:`;
        line.append(label, document.createTextNode(` ${entry.message}`));
        result.append(line);
      }
      if (notSent.length) {
        const line = document.createElement("div");
        line.textContent = `Not submitted: tabs ${notSent.map(entry => entry.tab).join(", ")}.`;
        result.append(line);
      }
      result.dataset.content = content;
    }
    if (!existing) ctx.details.querySelector(".nav-tabs")?.after(result);
  }

  function sync() {
    const ctx = context();
    if (!ctx?.details) { clear(); return; }
    try { loadReceipt(ctx); } catch (_) { clear(); return; }
    if (ctx.scope.editMode) { clear(); return; }
    const available = batch.retryable(receipt, targets(ctx));
    if (hasMultipleDesigns(ctx) && (available.length || inFlight)) {
      const header = [...document.querySelectorAll('#main-header-design-tabs button[ng-repeat]')].at(-1);
      const detail = [...ctx.details.querySelectorAll(".nav-tabs .order-form-design-tab")].at(-1);
      for (const [id, native, tag] of [[ids[0], header, "span"], [ids[1], detail, "li"]]) {
        if (!native) continue;
        const wrapper = document.getElementById(id) || control(id, tag);
        if (native.nextElementSibling !== wrapper) native.after(wrapper);
        const button = wrapper.querySelector("button");
        const disabled = inFlight || inventoryBusy(ctx) || priorAutomationPending(ctx.id) || batch.blocked(receipt, available);
        if (button.disabled !== disabled) button.disabled = disabled;
        button.title = priorAutomationPending(ctx.id) ? "An earlier automation request may still be active. Check that queue entry before ordering again."
          : disabled ? "Stock is loading or requests are still pending."
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
  // Only the extension's scripting coordinator calls this adapter. No page-event
  // or local-app write channel is added. The normal per-order button stays scoped
  // to multi-product orders; the window action also supports single-product orders.
  window.CRMOrderAllPage = { async execute(command) {
    const attempt = { submitted: false };
    try {
      if (command.op === "inspect") return inspectStock(command.orderId);
      if (command.op === "order") return await start(command, attempt);
      throw Error("Unknown stock action.");
    } catch (error) { return { success: false, orderId: command.orderId, notSubmitted: !attempt.submitted, message: error.message }; }
  } };
  new MutationObserver(schedule).observe(document, { subtree: true, childList: true,
    attributes: true, attributeFilter: ["class", "disabled", "hidden", "style"] });
  window.addEventListener("hashchange", schedule);
  window.addEventListener("popstate", schedule);
  window.setInterval(sync, 800);
  sync();
})();
