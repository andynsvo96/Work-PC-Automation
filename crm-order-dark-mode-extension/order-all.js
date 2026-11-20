// Isolated world: use the existing single-order queue and its serialization,
// result checks, polling and reload handling. Never submit purchases directly.
(() => {
  "use strict";
  if (window.__crmOrderAllInstalled) return;
  window.__crmOrderAllInstalled = true;
  let queued = false;
  let inFlight = false;
  let currentId = "";
  const ids = ["crm-order-all-header", "crm-order-all-detail"];
  const receiptKey = id => `crm-order-all-request:${id}`;

  function visible(node) {
    return Boolean(node && node.getClientRects().length && getComputedStyle(node).visibility !== "hidden");
  }

  function eligibleTabs() {
    const result = new Set();
    const details = document.getElementById("order-design-details");
    if (!details) return [];
    const panes = [...details.querySelectorAll(":scope > .tab-content > .tab-pane")];
    for (const control of details.querySelectorAll('.order-inventory button[ng-click="orderGoods(provider);"]')) {
      if (control.disabled || control.getAttribute("aria-disabled") === "true") continue;
      const row = control.closest('tr[ng-repeat="provider in providers"]');
      // CRM labels provider rows with <th>; their first <td> holds Order Goods.
      // Keep the data-cell form for older table layouts, scoped to this row.
      const vendorCell = row?.querySelector(":scope > th") || row?.querySelector(":scope > td");
      const vendor = vendorCell?.textContent || "";
      // Manual Order opens a vendor-entry dialog and cannot order stock itself.
      if (!/sanmar\s*\/\s*s\s*&\s*s\s+activewear/i.test(vendor)) continue;
      const pane = control.closest(".tab-pane");
      if (!panes.includes(pane)) continue;
      let hidden = false;
      // Ignore the inactive pane's CSS display:none, but retain every native
      // business-rule hide, including ordered providers and deleted designs.
      for (let node = control; node && node !== details; node = node.parentElement) {
        if (node.hidden || node.classList.contains("ng-hide") || node.style.display === "none" ||
            getComputedStyle(node).visibility === "hidden") { hidden = true; break; }
      }
      if (!hidden) result.add(panes.indexOf(pane) + 1);
    }
    return [...result].sort((a, b) => a - b);
  }

  function cleanup() {
    for (const id of ids) document.getElementById(id)?.remove();
  }

  function blocked(id) {
    return inFlight || Boolean(manualOrderProcessorButton()?.disabled ||
      sessionStorage.getItem(`crm-order-process-active:${id}`) || sessionStorage.getItem(receiptKey(id)));
  }

  async function start(event) {
    const trigger = event.currentTarget;
    const id = currentOrderId();
    if (!id || id !== currentId || blocked(id) || !eligibleTabs().length) return;
    inFlight = true;
    // Reserve before posting. An uncertain acceptance must not create a second
    // purchase task after a second click, a DOM rebuild, or a page refresh.
    sessionStorage.setItem(receiptKey(id), "pending");
    sync();
    try {
      const result = await queueManualOrderAutomation({ key: "order_goods", label: "Order All" }, trigger,
        manualOrderProcessorButton());
      if (result?.success || !/may already be queued|not confirmed|uncertain|check.{0,30}queue/i.test(result?.message || "")) {
        sessionStorage.removeItem(receiptKey(id));
      } else {
        sessionStorage.setItem(receiptKey(id), "uncertain");
      }
    } catch (_) {
      sessionStorage.setItem(receiptKey(id), "uncertain");
      setOrderProcessorResult(trigger, "Order All was not confirmed. Check the Automation queue before trying again.", "error");
    } finally {
      inFlight = false;
      sync();
    }
  }

  function makeControl(id, tag) {
    const wrapper = document.createElement(tag);
    wrapper.id = id;
    wrapper.className = "crm-order-all-control";
    wrapper.dataset.crmOrderMenuControl = "true";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-success crm-order-all-button";
    button.dataset.crmOrderProcessTrigger = "true";
    button.textContent = "Order All";
    button.addEventListener("click", start);
    wrapper.append(button);
    return wrapper;
  }

  function sync() {
    if (typeof isOrderDocument !== "function" || typeof queueManualOrderAutomation !== "function") return;
    const id = currentOrderId();
    if (currentId && currentId !== id) { cleanup(); inFlight = false; }
    currentId = id;
    const edit = document.querySelector('button[ng-click="editModeOn();"]');
    const save = document.querySelector('button[ng-click="saveOrder();"]');
    if (!id || !isOrderDocument() || !visible(edit) || visible(save) || !manualOrderProcessorButton()) { cleanup(); return; }
    const tabs = eligibleTabs();
    if (!tabs.length) { cleanup(); return; }
    const header = [...document.querySelectorAll('#main-header-design-tabs button[ng-repeat]')].at(-1);
    const detail = [...document.querySelectorAll('#order-design-details .nav-tabs .order-form-design-tab')].at(-1);
    for (const [id, native, tag] of [[ids[0], header, "span"], [ids[1], detail, "li"]]) {
      if (!native) continue;
      let control = document.getElementById(id);
      if (!control) control = makeControl(id, tag);
      if (native.nextElementSibling !== control) native.after(control);
      const button = control.querySelector("button");
      const disabled = blocked(currentId);
      if (button.disabled !== disabled) button.disabled = disabled;
      button.title = sessionStorage.getItem(receiptKey(currentId)) === "uncertain"
        ? "Check the Automation queue before trying again; the previous request may already be queued."
        : `Order available stock on tabs ${tabs.join(", ")} through the local Automation queue.`;
    }
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
