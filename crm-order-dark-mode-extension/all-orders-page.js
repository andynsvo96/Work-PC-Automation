// MAIN world, CRM only. Invoked exclusively by chrome.scripting from the popup's
// coordinator; deliberately has no DOM-event, postMessage, or local-app bridge.
var CRMAllOrdersPage = (() => {
  "use strict";
  if (window.CRMAllOrdersPage) return window.CRMAllOrdersPage;
  const model = CRMAllTabsModel;
  const kinds = ["sales", "production", "art"];
  const field = kind => "add" + kind[0].toUpperCase() + kind.slice(1) + "Notes";
  const active = values => (values || []).filter(value => value && model.key(value.crudAction) !== "d");
  const serialize = value => JSON.stringify(value, (name, item) => {
    if (name.startsWith("$$")) return undefined;
    if (item && typeof item === "object" && !Array.isArray(item)) {
      return Object.fromEntries(Object.keys(item).filter(key => !key.startsWith("$$")).sort().map(key => [key, item[key]]));
    }
    return item;
  });
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  let session = null;
  let writing = false;

  function routeId() {
    const route = /^\/app\/?$/.test(location.pathname) ? location.hash.replace(/^#!?\/?/, "/").split("?")[0] : location.pathname;
    return route.match(/^\/order\/(\d{7})\/?$/)?.[1] || "";
  }
  function helper(scope, name) {
    for (let current = scope, hops = 0; current && hops < 12; current = current.$parent, hops++) {
      if (typeof current[name] === "function") return current[name].bind(current);
    }
    return null;
  }
  function context(orderId = routeId()) {
    if (!orderId || routeId() !== orderId || !window.angular?.element || !document.getElementById("order-design-details")) return null;
    for (const element of document.querySelectorAll('#view-orders-index, #order-design-details, [ng-click="saveOrder();"]')) {
      let scope = window.angular.element(element).scope();
      for (let hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
        if (scope.order?.getResource && typeof scope.saveOrder === "function" && String(scope.order.getResource()?.id) === orderId) {
          const resource = scope.order.getResource();
          if (!Array.isArray(resource.designs) || !active(resource.designs).length) return null;
          let statics = scope;
          while (statics && !statics.STATICS) statics = statics.$parent;
          return { orderId, scope, resource, statics: statics?.STATICS || window.STATICS || {} };
        }
      }
    }
    return null;
  }
  const busy = ctx => Boolean(ctx.scope.saving || ctx.scope.savingOrder || ctx.scope.fileUploading || helper(ctx.scope, "isGettingShippingMethods")?.());
  const visible = node => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
  function nativeButton(name) {
    const found = [...document.querySelectorAll("button, a, input[type=button], input[type=submit]")]
      .filter(node => visible(node) && new RegExp(`^\\s*${name}\\s*\\(\\s*\\)\\s*;?\\s*$`).test(node.getAttribute("ng-click") || ""));
    if (found.length !== 1 || found[0].disabled || found[0].getAttribute("aria-disabled") === "true") throw Error("CRM's native " + name + " control is unavailable or disabled.");
    return found[0];
  }
  function discardWarning() {
    const dialogs = [...document.querySelectorAll('.modal, [role="dialog"], .modal-dialog, .modal-content, .ngdialog-content')].filter(visible);
    const roots = dialogs.filter(dialog => !dialogs.some(other => other !== dialog && other.contains(dialog)));
    if (!roots.length) return null;
    if (roots.length !== 1) throw Error("Multiple CRM dialogs are open. Review them before discarding this order's edits.");
    const dialog = roots[0];
    const message = model.clean((dialog.querySelector(".modal-body") || dialog).textContent);
    if (!/some changes were made to the form,\s*do you want to review and save them\?/i.test(message)) {
      throw Error("An unrelated CRM dialog is open. It was left unchanged for review.");
    }
    const buttons = [...dialog.querySelectorAll('button, a, input[type="button"], input[type="submit"]')].filter(visible);
    const named = name => buttons.filter(button => model.key(button.textContent || button.value) === name);
    const no = named("no"), yes = named("yes");
    if (no.length !== 1 || yes.length !== 1 || no[0].disabled || no[0].hasAttribute("disabled") ||
        no[0].classList.contains("disabled") || no[0].getAttribute("aria-disabled") === "true") {
      throw Error("CRM's discard warning is ambiguous or its No button is disabled. Review this order.");
    }
    return no[0];
  }
  function noteInput(kind) {
    const inputs = [...document.querySelectorAll(`#order-notes-${kind}-notes textarea`)]
      .filter(node => node.getAttribute("ng-model") === `order.getResource().${field(kind)}` && visible(node) && !node.disabled && !node.readOnly);
    if (inputs.length !== 1) throw Error(`CRM's native Add ${kind[0].toUpperCase() + kind.slice(1)} Note input is unavailable. Existing notes cannot be edited here.`);
    return inputs[0];
  }
  function text(value) {
    if (typeof value !== "string") throw Error("The saved note history could not be read.");
    const template = document.createElement("template");
    template.innerHTML = value.replace(/<[^>]*>/g, " ");
    return model.clean(template.content.textContent);
  }
  function histories(resource) {
    return Object.fromEntries(kinds.map(kind => [kind, text(resource[kind + "Notes"] || resource["filtered" + kind[0].toUpperCase() + kind.slice(1) + "Notes"] || "")]));
  }
  function savedNoteFields(resource) {
    return Object.fromEntries(kinds.flatMap(kind => [kind + "Notes", "filtered" + kind[0].toUpperCase() + kind.slice(1) + "Notes"])
      .map(name => [name, resource[name]]));
  }
  const count = (value, fragment) => fragment ? value.split(fragment).length - 1 : 0;
  function assertNoDrafts(ctx) {
    if (kinds.some(kind => String(ctx.resource[field(kind)] || "").trim()) || window.__crmAllTabsHasPendingChanges?.()) {
      throw Error("This order has pending notes or All Tabs edits. Resolve them on the order page first.");
    }
  }
  function angularRun(ctx, fn) {
    let result;
    let failure;
    const run = () => { try { result = fn(); } catch (error) { failure = error; } };
    if (ctx.scope.$root?.$$phase) run(); else ctx.scope.$apply(run);
    if (failure) throw failure;
    return result;
  }
  function describe(ctx, area) {
    return model.clean(area.description || area.printAreaDescription || area.printArea?.description || area.printAreaTemplate?.description || helper(ctx.scope, "setPrintAreaTemplateDescription")?.(area));
  }
  function method(ctx, area) {
    return model.clean(area.printMethodDescription || area.methodDescription || area.printMethod?.description || area.printMethodTemplate?.description || helper(ctx.scope, "setPrintMethodDescription")?.(area));
  }
  function createModel(ctx) { return model.createSession(ctx.resource, area => describe(ctx, area), area => method(ctx, area)); }
  function snapshot(ctx) {
    const value = createModel(ctx);
    return { tabCount: value.designs.length, groups: value.groups.map(group => ({ sourceKey: group.sourceKey, label: group.label,
      tabs: [...new Set(group.members.map(member => member.record.tab))], methods: [...new Set(group.members.map(member => member.method))] })),
      locations: model.choices(ctx.statics.printAreaTemplates, true).map(value => value.description),
      methods: model.choices(ctx.statics.printMethods).map(value => value.description) };
  }
  function inspect(orderId) {
    const ctx = context(orderId);
    if (!ctx) return null;
    return { success: true, adapterVersion: 2, orderId: ctx.orderId, editMode: Boolean(ctx.scope.editMode), busy: busy(ctx),
      pending: kinds.some(kind => String(ctx.resource[field(kind)] || "").trim()) || Boolean(window.__crmAllTabsHasPendingChanges?.()), snapshot: snapshot(ctx) };
  }
  async function stable(orderId, predicate, timeout = 15000) {
    const until = Date.now() + timeout;
    let last = null;
    while (Date.now() < until) {
      const ctx = context(orderId);
      if (ctx && predicate(ctx) && ctx.resource === last) return ctx;
      last = ctx && predicate(ctx) ? ctx.resource : null;
      await wait(150);
    }
    throw Error("CRM did not become ready. Check this order's validation messages or dialogs; no action is retried automatically.");
  }
  function owned(command, phase) {
    const ctx = context(command.orderId);
    if (!ctx || !session || session.batchId !== command.batchId || !ctx.scope.editMode || busy(ctx) || ctx.resource !== session.model.resource) {
      throw Error("The order editor changed or is busy. Review this order before continuing.");
    }
    if (phase && session.phase !== phase) throw Error("This order is not ready for that action.");
    if (serialize(ctx.resource) !== session.version) throw Error("The order changed after it was read. Review its native editor before continuing.");
    if (window.__crmAllTabsHasPendingChanges?.()) throw Error("Apply or discard the order page's pending All Tabs edits first.");
    return ctx;
  }
  function planned(ctx, draft) {
    const changes = Object.fromEntries(session.model.groups.map(group => [group.id, draft.changes[group.sourceKey] || {}]));
    const plan = model.plan(session.model, changes, draft.additions, ctx.statics, ctx.resource);
    for (const kind of kinds) {
      const note = draft.notes[kind];
      if (!note.trim()) continue;
      const input = noteInput(kind);
      if (input.maxLength >= 0 && note.length > input.maxLength) throw Error(`The ${kind} note exceeds CRM's native length limit.`);
    }
    return plan;
  }
  function summary(plan, draft) {
    return { changed: plan.edits.length, removed: plan.removes.length, added: plan.creates.length, tabs: plan.affectedTabs,
      notes: kinds.filter(kind => draft.notes[kind].trim()), additions: Object.fromEntries(plan.additionTargets) };
  }
  function checkpoint(root) {
    const saved = new Map();
    function visit(value) {
      if (!value || typeof value !== "object" || saved.has(value)) return;
      const entries = Object.entries(value);
      saved.set(value, { entries, length: Array.isArray(value) ? value.length : null });
      entries.forEach(([, child]) => visit(child));
    }
    visit(root);
    return () => { for (const [value, savedValue] of saved) {
      for (const name of Object.keys(value)) delete value[name];
      if (savedValue.length !== null) value.length = savedValue.length;
      for (const [name, item] of savedValue.entries) value[name] = item;
    } };
  }
  function protectedArea(area) {
    return Object.fromEntries(Object.entries(area).filter(([name]) => /note|file|upload|artwork|width|height/i.test(name)));
  }
  function expected(ctx, notes, before) {
    return { orderId: ctx.orderId, notes, before, designs: active(ctx.resource.designs).map(design => ({ id: String(design.id),
      items: serialize(design.designItems), areas: active(design.printAreas).map(area => ({ location: describe(ctx, area), method: method(ctx, area),
        protected: serialize(protectedArea(area)) })) })) };
  }
  function verify(ctx, target) {
    if (ctx.scope.editMode || busy(ctx)) throw Error("The order still has an open editor or save in progress. Its page has not been refreshed.");
    const actual = expected(ctx, target.notes, target.before);
    if (serialize(actual.designs) !== serialize(target.designs)) throw Error("Saved print areas, artwork, products, or quantities do not match the applied changes. Review this order.");
    const saved = histories(ctx.resource);
    for (const kind of kinds) {
      const before = target.before[kind], after = saved[kind], note = text(target.notes[kind]);
      if ((before && !after.includes(before)) || (note ? count(after, note) !== count(before, note) + 1 : after !== before)) {
        throw Error(`Saved ${kind} notes could not be verified. Review this order; no note was submitted again.`);
      }
    }
    return { success: true, orderId: ctx.orderId };
  }
  async function execute(command) {
    if (command.op === "inspect") return inspect(command.orderId);
    if (writing) throw Error("A native order action is already in progress.");
    writing = true;
    try {
      if (command.op === "begin") {
        let ctx = context(command.orderId);
        if (!ctx || ctx.scope.editMode || busy(ctx)) throw Error("This order is already editing, busy, or not ready. Resolve it on the order page first.");
        assertNoDrafts(ctx);
        nativeButton("editModeOn").click();
        ctx = await stable(command.orderId, value => value.scope.editMode && !busy(value));
        assertNoDrafts(ctx);
        session = { batchId: command.batchId, model: createModel(ctx), phase: "prepared", version: serialize(ctx.resource),
          before: histories(ctx.resource), savedNotes: serialize(savedNoteFields(ctx.resource)) };
        return { success: true, snapshot: snapshot(ctx), original: expected(ctx, { sales: "", production: "", art: "" }, session.before) };
      }
      if (command.op === "preview" || command.op === "apply") {
        const ctx = owned(command, "prepared");
        const plan = planned(ctx, command.draft);
        if (command.op === "preview") return { success: true, summary: summary(plan, command.draft) };
        const restore = checkpoint(ctx.resource);
        try {
          angularRun(ctx, () => model.apply(plan, { updateAreaTemplate: helper(ctx.scope, "updateAreaTemplate"),
            updateAreaMethod: helper(ctx.scope, "updateAreaMethod"), addPrintArea: helper(ctx.scope, "addPrintArea"),
            removePrintArea: helper(ctx.scope, "removePrintArea"), copy: value => window.angular.copy(value), equals: window.angular.equals }, ctx.resource));
          for (const kind of kinds) {
            const note = command.draft.notes[kind];
            if (!note.trim()) continue;
            const input = noteInput(kind);
            input.value = note;
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
            if (ctx.resource[field(kind)] !== note) throw Error(`CRM did not accept the new ${kind} note.`);
          }
          if (serialize(savedNoteFields(ctx.resource)) !== session.savedNotes) throw Error("CRM changed existing notes while adding a draft.");
          session.phase = "applied";
          session.version = serialize(ctx.resource);
          session.expected = expected(ctx, command.draft.notes, session.before);
          return { success: true, expected: session.expected, summary: summary(plan, command.draft) };
        } catch (error) {
          angularRun(ctx, restore);
          for (const kind of kinds) { try { noteInput(kind).value = ctx.resource[field(kind)] || ""; } catch (_) {} }
          throw Error(`${error.message} This order's unsaved batch changes were rolled back.`);
        }
      }
      if (command.op === "save") {
        owned(command, "applied");
        // Mark before clicking: a lost response can never trigger a second save.
        session.phase = "saving";
        nativeButton("saveOrder").click();
        await stable(command.orderId, value => !value.scope.editMode && !busy(value), 25000);
        session.phase = "saved";
        return { success: true };
      }
      if (command.op === "check") {
        owned(command, "applied");
        return { success: true };
      }
      if (command.op === "verify") {
        const ctx = context(command.orderId);
        if (!ctx) throw Error("The saved CRM order is not ready.");
        return verify(ctx, command.expected);
      }
      if (command.op === "cancel") {
        owned(command);
        // Only the explicit Cancel action may answer this exact warning. Never
        // choose Yes, dismiss another dialog, or bypass a disabled native control.
        const existing = discardWarning();
        session.phase = "canceling";
        if (existing) { existing.click(); return { success: true }; }
        nativeButton("editModeOff").click();
        const until = Date.now() + 15000;
        while (Date.now() < until) {
          const ctx = context(command.orderId);
          if (ctx && !ctx.scope.editMode && !busy(ctx)) return { success: true };
          owned(command);
          const no = discardWarning();
          if (no) { no.click(); return { success: true }; }
          await wait(100);
        }
        throw Error("CRM did not confirm discarding the edits. Review this order; no save was submitted.");
      }
      throw Error("Unknown All Orders action.");
    } finally { writing = false; }
  }
  return { async execute(command) {
    try { return await execute(command); }
    catch (error) { return { success: false, orderId: command.orderId, message: error.message }; }
  } };
})();
