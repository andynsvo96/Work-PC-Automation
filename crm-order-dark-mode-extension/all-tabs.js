// Runs in MAIN alongside CRM's Angular app. Only these visible controls can
// apply edits; there is no page-event or local-server write bridge.
(() => {
  "use strict";
  if (window.__crmAllTabsInstalled) return;
  window.__crmAllTabsInstalled = true;
  const model = CRMAllTabsModel;
  let context = null;
  let state = null;
  let queued = false;
  let additionId = 0;

  function routeOrderId() {
    const direct = location.pathname.match(/^\/order\/(\d{7})\/?$/);
    const embedded = /^\/app\/?$/.test(location.pathname) && location.hash.match(/^#!?\/?order\/(\d{7})\/?(?:[?#].*)?$/);
    return (direct || embedded)?.[1] || "";
  }

  function scopeFunction(scope, name) {
    for (let current = scope, hops = 0; current && hops < 12; current = current.$parent, hops++) {
      if (typeof current[name] === "function") return current[name].bind(current);
    }
    return null;
  }

  function statics(scope) {
    for (let current = scope, hops = 0; current && hops < 12; current = current.$parent, hops++) {
      if (current.STATICS) return current.STATICS;
    }
    return window.STATICS || {};
  }

  function findContext() {
    const orderId = routeOrderId();
    if (!orderId || !window.angular?.element) return null;
    const details = document.getElementById("order-design-details");
    if (!details) return null;
    let scope = context?.details === details ? context.scope : null;
    if (!scope || scope.$$destroyed || String(scope.order?.getResource?.()?.id) !== orderId) {
      scope = null;
      for (const element of document.querySelectorAll('#view-orders-index, #order-design-details, #main-header-design-tabs, [ng-click="saveOrder();"]')) {
        let candidate;
        try { candidate = window.angular.element(element).scope(); } catch (_) { continue; }
        for (let hops = 0; candidate && hops < 12; candidate = candidate.$parent, hops++) {
          if (candidate.order?.getResource && typeof candidate.saveOrder === "function" &&
              String(candidate.order.getResource()?.id) === orderId) { scope = candidate; break; }
        }
        if (scope) break;
      }
    }
    if (!scope || !scope.editMode) return null;
    return { orderId, details, scope, resource: scope.order.getResource() };
  }

  function busy() {
    return Boolean(context?.scope.saving || context?.scope.savingOrder || context?.scope.fileUploading ||
      scopeFunction(context?.scope, "isGettingShippingMethods")?.());
  }

  function describeArea(area) {
    return model.clean(area.description || area.printAreaDescription || area.printArea?.description ||
      area.printAreaTemplate?.description || scopeFunction(context.scope, "setPrintAreaTemplateDescription")?.(area));
  }

  function describeMethod(area) {
    return model.clean(area.printMethodDescription || area.methodDescription || area.printMethod?.description ||
      area.printMethodTemplate?.description || scopeFunction(context.scope, "setPrintMethodDescription")?.(area));
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, className, handler) {
    const node = element("button", className, label);
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
  }

  function pending() {
    return Boolean(state && (state.additions.length || Object.values(state.changes).some(change => change.location || change.method)));
  }

  function selectField(card, label, id, values, selected, placeholder, onChange) {
    const wrapper = element("label", "crm-all-tabs-field");
    wrapper.htmlFor = id;
    wrapper.append(element("span", "", label));
    const select = element("select", "form-control");
    select.id = id;
    select.append(new Option(placeholder, ""));
    const seen = new Set();
    for (const value of values) {
      const key = model.key(value.description);
      if (seen.has(key)) continue;
      seen.add(key);
      select.append(new Option(value.description, key));
    }
    select.value = selected;
    select.addEventListener("change", () => { onChange(select.value); preview(); });
    wrapper.append(select);
    card.append(wrapper);
    return select;
  }

  function setStatus(message, error = false) {
    const status = document.getElementById("crm-all-tabs-status");
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("crm-all-tabs-error", error);
  }

  function freshState() {
    state = { session: model.createSession(context.resource, describeArea, describeMethod),
      changes: {}, additions: [], open: state?.open || false, busy: busy() };
  }

  function render() {
    const panel = document.getElementById("crm-all-tabs-panel");
    panel.replaceChildren();
    panel.append(element("h3", "", "All Tabs"));
    panel.append(element("p", "crm-all-tabs-help", `Print areas across ${state.session.designs.length} tabs. Choose changes, apply them, then use Save Order. Notes and artwork stay with their own tabs.`));
    const status = element("p", "crm-all-tabs-status");
    status.id = "crm-all-tabs-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    panel.append(status);
    const cards = element("div", "crm-all-tabs-cards");
    const catalog = statics(context.scope);
    const templates = model.choices(catalog.printAreaTemplates, true);
    const methods = model.choices(catalog.printMethods);
    for (const group of state.session.groups) {
      const card = element("section", "crm-all-tabs-card");
      card.dataset.sourceArea = group.sourceKey;
      card.append(element("h4", "", group.label));
      const tabs = [...new Set(group.members.map(member => member.record.tab))];
      const affected = element("p", "crm-all-tabs-affected", `Affects tabs ${tabs.join(", ")}`);
      affected.title = group.members.map(member => `Tab ${member.record.tab}: ${member.method || "missing method"}`).join("; ");
      card.append(affected);
      const change = state.changes[group.id] ||= {};
      selectField(card, "Print area", `crm-all-tabs-location-${group.id}`, templates, change.location || "",
        `Keep ${group.label}`, value => { change.location = value === group.sourceKey ? "" : value; });
      const existingMethods = [...new Set(group.members.map(member => member.method))];
      selectField(card, "Print method", `crm-all-tabs-method-${group.id}`, methods, change.method || "",
        existingMethods.length === 1 ? `Keep ${existingMethods[0] || "existing method"}` : "Keep each tab's method (mixed)",
        value => { change.method = existingMethods.length === 1 && model.key(existingMethods[0]) === value ? "" : value; });
      cards.append(card);
    }
    if (!state.session.groups.length) cards.append(element("p", "", "These tabs have no print areas yet. Add one below."));
    for (const addition of state.additions) {
      const card = element("section", "crm-all-tabs-card crm-all-tabs-addition");
      card.dataset.addition = addition.id;
      card.append(element("h4", "", "New print area"));
      selectField(card, "Print area", `crm-all-tabs-location-${addition.id}`, templates, addition.location || "",
        "Choose a location", value => { addition.location = value; });
      selectField(card, "Print method", `crm-all-tabs-method-${addition.id}`, methods, addition.method || "",
        "Choose a method", value => { addition.method = value; });
      const affected = element("p", "crm-all-tabs-affected");
      affected.dataset.additionTargets = addition.id;
      card.append(affected);
      card.append(button("Remove new area", "btn btn-default", () => {
        state.additions = state.additions.filter(item => item !== addition);
        render();
      }));
      cards.append(card);
    }
    panel.append(cards);
    const actions = element("div", "crm-all-tabs-actions");
    const add = button("Add print area", "btn btn-default", () => {
      const addition = { id: `a${++additionId}`, location: "", method: "" };
      state.additions.push(addition);
      render();
      document.getElementById(`crm-all-tabs-location-${addition.id}`).focus();
    });
    add.id = "crm-all-tabs-add";
    const apply = button("Apply changes", "btn btn-primary", applyChanges);
    apply.id = "crm-all-tabs-apply";
    const discard = button("Discard pending changes", "btn btn-default", () => {
      try { freshState(); render(); } catch (error) { setStatus(error.message, true); }
    });
    discard.id = "crm-all-tabs-discard";
    actions.append(add, apply, discard);
    panel.append(actions);
    preview();
  }

  function preview() {
    if (!state) return;
    const apply = document.getElementById("crm-all-tabs-apply");
    if (!apply) return;
    let planned;
    let error;
    try { planned = model.plan(state.session, state.changes, state.additions, statics(context.scope), context.resource); }
    catch (failure) { error = failure; }
    // Even an incomplete new row shows its scope once its location is chosen.
    for (const addition of state.additions) {
      const label = document.querySelector(`[data-addition-targets="${addition.id}"]`);
      let tabs = planned?.additionTargets.get(addition.id);
      if (!tabs && addition.location) {
        try {
          const method = model.choices(statics(context.scope).printMethods)[0]?.description;
          const partial = state.additions.slice(0, state.additions.indexOf(addition) + 1)
            .filter(item => item.location).map(item => ({ ...item, method: item.method || method }));
          tabs = model.plan(state.session, state.changes, partial, statics(context.scope), context.resource).additionTargets.get(addition.id);
        } catch (_) { /* The conflict is explained in the main status. */ }
      }
      label.textContent = tabs ? (tabs.length ? `Will add to tabs ${tabs.join(", ")}; existing areas are kept.` : "Already present on every tab; no areas will be added.")
        : "Choose a location to see affected tabs.";
    }
    const hasWork = Boolean(planned && (planned.edits.length || planned.creates.length));
    apply.disabled = Boolean(error || !hasWork || busy());
    document.getElementById("crm-all-tabs-discard").disabled = !pending();
    for (const control of document.querySelectorAll("#crm-all-tabs-panel select, #crm-all-tabs-add")) control.disabled = busy();
    if (error) setStatus(error.message, true);
    else if (hasWork) setStatus(`Ready: ${planned.edits.length} existing areas to change, ${planned.creates.length} areas to add. Affects tabs ${planned.affectedTabs.join(", ")}.`);
    else if (pending()) setStatus("No changes are needed. Discard pending changes to return to the current areas.");
    else setStatus("Choose a location or method to edit matching areas across their listed tabs.");
  }

  function runInAngular(fn) {
    let result;
    let failure;
    const run = () => { try { result = fn(); } catch (error) { failure = error; } };
    if (context.scope.$root?.$$phase) run();
    else context.scope.$apply(run);
    if (failure) throw failure;
    return result;
  }

  function applyChanges() {
    if (!context || !state || busy()) return;
    if (!context.scope.editMode || routeOrderId() !== context.orderId) { sync(); return; }
    try {
      // Re-read immediately before applying, even if the preview was valid.
      context.resource = context.scope.order.getResource();
      const planned = model.plan(state.session, state.changes, state.additions, statics(context.scope), context.resource);
      if (!planned.edits.length && !planned.creates.length) return;
      const result = runInAngular(() => model.apply(planned, {
        updateAreaTemplate: scopeFunction(context.scope, "updateAreaTemplate"),
        updateAreaMethod: scopeFunction(context.scope, "updateAreaMethod"),
        addPrintArea: scopeFunction(context.scope, "addPrintArea"),
        copy: value => window.angular.copy(value),
        equals: window.angular.equals
      }, context.resource));
      freshState();
      render();
      setStatus(`Applied ${result.changedAreas} existing-area changes and added ${result.addedAreas} areas on tabs ${result.affectedTabs.join(", ")}. Review the tabs, then Save Order to keep them.`);
    } catch (error) {
      setStatus(error.message, true);
      document.getElementById("crm-all-tabs-apply").disabled = true;
    }
  }

  function setOpen(open) {
    if (state) state.open = open;
    document.documentElement.classList.toggle("crm-all-tabs-view", open);
    const panel = document.getElementById("crm-all-tabs-panel");
    if (panel) panel.hidden = !open;
    for (const id of ["crm-all-tabs-header", "crm-all-tabs-detail"]) {
      const tab = document.getElementById(id);
      tab?.classList.toggle("crm-all-tabs-selected", open);
      const control = tab?.matches("button") ? tab : tab?.querySelector("button");
      control?.setAttribute("aria-pressed", String(open));
    }
  }

  function open() {
    try {
      sync();
      if (!context) return;
      if (!state || !pending()) freshState();
      render();
      setOpen(true);
    } catch (error) {
      const panel = document.getElementById("crm-all-tabs-panel");
      panel?.replaceChildren(element("p", "crm-all-tabs-error", error.message));
      setOpen(true);
    }
  }

  function clear() {
    setOpen(false);
    for (const id of ["crm-all-tabs-header", "crm-all-tabs-detail", "crm-all-tabs-panel"]) document.getElementById(id)?.remove();
    context = null;
    state = null;
  }

  function sync() {
    let next;
    try { next = findContext(); } catch (_) { return; }
    if (!next) { if (context) clear(); return; }
    if (context && (context.orderId !== next.orderId || context.details !== next.details)) clear();
    context = next;
    const content = context.details.querySelector(".tab-content");
    if (!content) return;
    const nativeHeader = document.querySelector('#main-header-design-tabs button[ng-repeat]');
    if (nativeHeader) {
      let tab = document.getElementById("crm-all-tabs-header");
      if (!tab) {
        tab = button("All Tabs", "btn btn-default crm-all-tabs-tab", open);
        tab.id = "crm-all-tabs-header";
        tab.setAttribute("aria-controls", "crm-all-tabs-panel");
      }
      if (nativeHeader.previousElementSibling !== tab) nativeHeader.before(tab);
    }
    const nativeDetail = context.details.querySelector(".nav-tabs .order-form-design-tab");
    if (nativeDetail) {
      let tab = document.getElementById("crm-all-tabs-detail");
      if (!tab) {
        tab = element("li", "crm-all-tabs-tab");
        tab.id = "crm-all-tabs-detail";
        const link = button("All Tabs", "crm-all-tabs-detail-button", open);
        link.setAttribute("aria-controls", "crm-all-tabs-panel");
        tab.append(link);
      }
      if (nativeDetail.previousElementSibling !== tab) nativeDetail.before(tab);
    }
    if (content && !document.getElementById("crm-all-tabs-panel")) {
      const panel = element("section", "crm-all-tabs-panel");
      panel.id = "crm-all-tabs-panel";
      panel.hidden = true;
      content.prepend(panel);
      if (state) { render(); setOpen(state.open); }
    }
    if (state && state.busy !== busy()) {
      state.busy = busy();
      if (document.getElementById("crm-all-tabs-apply")) preview();
    }
  }

  function scheduleSync() {
    if (queued) return;
    queued = true;
    window.setTimeout(() => { queued = false; sync(); }, 60);
  }

  document.addEventListener("click", event => {
    const target = event.target.closest?.("button, a, li.order-form-design-tab");
    if (!target || !context) return;
    if (/^\s*saveOrder\s*\(/.test(target.getAttribute("ng-click") || "") && pending()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      open();
      setStatus("Apply or discard the pending All Tabs changes before Save Order.", true);
      return;
    }
    if (!target.closest(".crm-all-tabs-tab") &&
        (target.closest("#main-header-design-tabs") || target.closest("#order-design-details .nav-tabs"))) setOpen(false);
    scheduleSync();
  }, true);
  window.addEventListener("hashchange", scheduleSync);
  window.addEventListener("popstate", scheduleSync);
  window.addEventListener("focus", scheduleSync);
  const observer = new MutationObserver(scheduleSync);
  observer.observe(document, { subtree: true, childList: true, attributes: true,
    attributeFilter: ["class", "ng-show", "disabled"] });
  // Also catch Angular-only state changes and programmatic route transitions.
  window.setInterval(sync, 800);
  sync();
})();
