import { SESSION_PREFIX, emptyDraft, normalizeDraft, hasChanges, orderIdFromUrl } from "./all-orders-model.mjs";

export const EDITOR_PREFIX = "crm-all-orders-editor:";

export function editorBounds(screen = {}, source = {}) {
  const number = (value, fallback) => Number.isFinite(value) ? Math.round(value) : fallback;
  const availableWidth = Math.max(240, Math.min(16384, number(screen.width, source.width || 960)));
  const availableHeight = Math.max(240, Math.min(16384, number(screen.height, source.height || 900)));
  const width = Math.min(920, availableWidth - 32), height = availableHeight - 32;
  return { width, height, left: number(screen.left, source.left || 0) + Math.round((availableWidth - width) / 2),
    top: number(screen.top, source.top || 0) + 16 };
}

// A popup action owns a bounded, journaled operation in the extension worker.
// There is no worker subprocess, Flask queue, CRM HTTP write, or automatic retry.
export class AllOrdersController {
  constructor(api, { sleep = ms => new Promise(resolve => setTimeout(resolve, ms)), id = () => crypto.randomUUID() } = {}) {
    this.api = api;
    this.sleep = sleep;
    this.id = id;
    this.states = new Map();
    this.running = new Map();
    this.commands = Promise.resolve();
    this.writes = Promise.resolve();
  }

  authorized(sender) {
    return sender?.id === this.api.runtime.id && sender.url === this.api.runtime.getURL("popup.html") && !sender.tab;
  }

  async sourceWindow(message, sender) {
    if (this.authorized(sender)) return message.windowId;
    const denied = () => Error("All Orders actions are available only from this extension's popup or its bound expanded editor.");
    if (sender?.id !== this.api.runtime.id) throw denied();
    let url;
    try { url = new URL(sender.url); } catch (_) { throw denied(); }
    const editor = url.searchParams.get("editor");
    if (!editor || !/^[a-zA-Z0-9-]+$/.test(editor)) throw denied();
    const values = await this.api.storage.session.get(EDITOR_PREFIX + editor);
    const binding = values[EDITOR_PREFIX + editor];
    if (!binding || binding.url !== sender.url || (message.action !== "editor-source" && message.windowId !== binding.sourceWindowId) ||
        (sender.tab && sender.tab.id !== binding.tabId)) throw denied();
    const tab = await this.api.tabs.get(binding.tabId);
    if (tab.windowId !== binding.windowId || tab.url !== binding.url) throw denied();
    return binding.sourceWindowId;
  }

  async expand(state, screen) {
    const stored = await this.api.storage.session.get(null);
    for (const [key, binding] of Object.entries(stored)) {
      if (!key.startsWith(EDITOR_PREFIX) || binding.sourceWindowId !== state.windowId) continue;
      try {
        const tab = await this.api.tabs.get(binding.tabId);
        if (tab.windowId !== binding.windowId || tab.url !== binding.url) continue;
        const window = await this.api.windows.get(binding.windowId);
        await this.api.windows.update(binding.windowId, { focused: true, ...(window.state === "minimized" ? { state: "normal" } : {}) });
        return structuredClone(state);
      } catch (_) { /* A closed expanded editor can be replaced. */ }
    }
    const source = await this.api.windows.get(state.windowId);
    const editor = `${state.windowId}-${this.id()}`;
    const url = this.api.runtime.getURL(`popup.html?editor=${editor}&sourceWindow=${state.windowId}`);
    const window = await this.api.windows.create({ url: "about:blank", type: "popup", focused: true,
      incognito: source.incognito === true, ...editorBounds(screen, source) });
    try {
      const [tab] = window.tabs?.length ? window.tabs : await this.api.tabs.query({ windowId: window.id });
      if (!Number.isInteger(tab?.id)) throw Error("Chrome did not create an editor tab.");
      // Bind the new window before loading its UI, so it cannot accidentally
      // scan its own window or accept a different source-window parameter.
      await this.api.storage.session.set({ [EDITOR_PREFIX + editor]: { sourceWindowId: state.windowId, windowId: window.id, tabId: tab.id, url } });
      await this.api.tabs.update(tab.id, { url });
      return structuredClone(state);
    } catch (error) {
      await this.api.storage.session.remove(EDITOR_PREFIX + editor).catch(() => {});
      await this.api.windows.remove(window.id).catch(() => {});
      throw error;
    }
  }

  async persist(state) {
    const saved = structuredClone(state);
    const write = this.writes.then(() => this.api.storage.session.set({ [SESSION_PREFIX + state.windowId]: saved }));
    this.writes = write.catch(() => {});
    await write;
  }

  async load(windowId) {
    if (!this.states.has(windowId)) {
      const name = SESSION_PREFIX + windowId;
      const values = await this.api.storage.session.get(name);
      const state = values[name] || { windowId, phase: "idle", busy: false, orders: [], draft: emptyDraft(), revision: 0, message: "Scan this window to choose orders." };
      // A restarted service worker must never resume a write whose response was lost.
      if (state.busy) {
        state.busy = false;
        state.phase = "review";
        state.message = "The batch was interrupted. Review the order pages or use Check saved orders. No edits or saves will be repeated automatically.";
        for (const order of state.orders.filter(order => order.selected && !["verified", "canceled"].includes(order.status))) {
          order.status = "review";
          order.message = "Interrupted; review this order before starting another batch.";
        }
        await this.persist(state);
      }
      this.states.set(windowId, state);
    }
    return this.states.get(windowId);
  }

  handle(message, sender) {
    const pending = this.commands.then(() => this.dispatch(message, sender));
    this.commands = pending.catch(() => {});
    return pending;
  }

  async dispatch(message, sender) {
    const windowId = await this.sourceWindow(message, sender);
    if (!Number.isInteger(windowId) || windowId < 0) throw Error("The source Chrome window is unavailable.");
    await this.api.windows.get(windowId);
    const state = await this.load(windowId);
    const action = message.action;
    if (action === "get" || action === "editor-source") return structuredClone(state);
    if (action === "expand") return this.expand(state, message.screen);
    if (state.busy) throw Error("Wait for the current All Orders action to finish.");
    if (action === "draft") {
      if (!["scanned", "prepared", "previewed"].includes(state.phase)) throw Error("Read the orders before editing the draft.");
      if (!Number.isSafeInteger(message.revision) || message.revision <= state.revision) return structuredClone(state);
      state.draft = normalizeDraft(message.draft);
      state.revision = message.revision;
      if (state.phase === "previewed") state.phase = "prepared";
      delete state.preview;
      await this.persist(state);
      return structuredClone(state);
    }
    if (action === "select") {
      const order = state.orders.find(order => order.orderId === message.orderId);
      if (!order || !(state.phase === "scanned" || (state.phase === "prepared" && order.status === "error" && message.selected === false))) {
        throw Error("Cancel unsaved edits before changing the batch's orders.");
      }
      order.selected = message.selected === true;
      await this.persist(state);
      return structuredClone(state);
    }
    if (!["scan", "prepare", "preview", "apply", "save", "verify", "cancel"].includes(action)) throw Error("Unknown All Orders action.");
    if (action === "prepare") {
      if (!["scanned", "prepared"].includes(state.phase)) throw Error("Scan this window before entering Edit Order.");
      await this.checkOtherBatches(state);
      state.batchId ||= this.id();
    }
    if (["preview", "apply"].includes(action) && !["prepared", "previewed"].includes(state.phase)) throw Error("Enter Edit Order on every included order first.");
    if (["preview", "apply"].includes(action) && !hasChanges(state.draft)) throw Error("Choose a print-area change or enter a new note.");
    if (action === "save" && state.phase !== "applied") throw Error("Apply and review the changes before Save All.");
    if (action !== "scan" && !state.orders.some(order => order.selected)) throw Error("Select at least one CRM order.");
    state.busy = true;
    state.message = ({ scan: "Reading orders in this window…", prepare: "Opening native Edit Order on the included orders…",
      preview: "Validating every order…", apply: "Applying changes to the native editors…", save: "Saving orders through CRM…",
      verify: "Checking saved orders without submitting any changes…", cancel: "Canceling unsaved edits through CRM…" })[action];
    await this.persist(state);
    const task = Promise.resolve().then(() => this.run(state, action)).catch(error => {
      state.phase = "review";
      state.message = error.message;
    }).finally(async () => {
      state.busy = false;
      try { await this.persist(state); } finally { this.running.delete(state.windowId); }
    }).catch(error => {
      state.phase = "review";
      state.message = `The batch needs review because its status could not be retained: ${error.message}`;
    });
    this.running.set(state.windowId, task);
    // Reply now: the operation belongs to the service worker, not the popup.
    return structuredClone(state);
  }

  selected(state) { return state.orders.filter(order => order.selected); }

  async checkOtherBatches(state) {
    const stored = await this.api.storage.session.get(null);
    for (const [name, other] of Object.entries(stored)) {
      if (!name.startsWith(SESSION_PREFIX) || other.windowId === state.windowId || other.phase === "complete") continue;
      try { await this.api.windows.get(other.windowId); } catch (_) { continue; }
      const overlap = this.selected(state).find(order => other.orders.some(row => row.selected && row.orderId === order.orderId &&
        (other.busy || ["entering", "prepared", "applying", "applied", "saving", "saved", "refreshing", "review"].includes(row.status))));
      if (overlap) throw Error(`Order ${overlap.orderId} has an unfinished All Orders batch in another window. Resolve that batch first.`);
    }
  }

  async tab(order, windowId, allowLoading = false) {
    const tab = await this.api.tabs.get(order.tabId);
    if (tab.windowId !== windowId || orderIdFromUrl(tab.url) !== order.orderId ||
        (tab.pendingUrl && orderIdFromUrl(tab.pendingUrl) !== order.orderId)) {
      throw Error("This tab was closed, moved to another window, or navigated away from its order.");
    }
    if (tab.discarded || tab.frozen || (!allowLoading && tab.status === "loading")) {
      throw Error("Activate this order tab and let CRM finish loading, then scan again.");
    }
    return tab;
  }

  async execute(order, state, command, discover = false) {
    await this.tab(order, state.windowId);
    const target = { tabId: order.tabId };
    if (discover) target.allFrames = true;
    else if (order.documentId) target.documentIds = [order.documentId];
    else target.frameIds = [order.frameId];
    let results;
    try {
      results = await this.api.scripting.executeScript({ target, world: "MAIN",
        func: command => globalThis.CRMAllOrdersPage?.execute(command) ?? null,
        args: [{ ...command, orderId: order.orderId, batchId: state.batchId }] });
    } catch (error) { error.transportLost = true; throw error; }
    const matches = results.filter(result => result.result && result.result.orderId === order.orderId);
    // begin/apply results may omit orderId; the document has already been pinned.
    const found = discover ? matches : results.filter(result => result.result);
    if (found.length !== 1) throw Error("The CRM order frame is missing or ambiguous. Reload the extension, refresh the order page, and scan again.");
    const response = found[0].result;
    if (response.success !== true) throw Error(response.message || "CRM rejected the action. Review this order.");
    if (discover && response.adapterVersion !== 2) throw Error("Refresh this CRM order page to load the updated All Orders controls, then scan again.");
    if (discover) { order.frameId = found[0].frameId; order.documentId = found[0].documentId; }
    return response;
  }

  async inspect(order, state) { return this.execute(order, state, { op: "inspect" }, true); }

  async assertClosedEditors(state) {
    for (const order of state.orders.filter(order => order.selected)) {
      let tab;
      try { tab = await this.api.tabs.get(order.tabId); } catch (_) { continue; }
      if (tab.windowId !== state.windowId || orderIdFromUrl(tab.url) !== order.orderId) continue;
      const read = await this.inspect(order, state);
      if (read.editMode || read.busy || read.pending) throw Error(`Order ${order.orderId} has unsaved edits. Use Cancel unsaved edits or resolve its native editor before scanning again.`);
    }
  }

  async scan(state) {
    await this.assertClosedEditors(state);
    const tabs = (await this.api.tabs.query({ windowId: state.windowId }))
      .filter(tab => orderIdFromUrl(tab.url)).sort((a, b) => Number(b.active) - Number(a.active) || a.index - b.index);
    const orders = new Map();
    for (const tab of tabs) {
      const orderId = orderIdFromUrl(tab.url);
      if (orders.has(orderId)) { orders.get(orderId).duplicates.push(tab.id); continue; }
      orders.set(orderId, { orderId, tabId: tab.id, selected: true, status: "scanned", duplicates: [], message: "" });
    }
    state.orders = [...orders.values()];
    state.draft = emptyDraft();
    state.revision++;
    delete state.validationError;
    delete state.batchId;
    await Promise.all(state.orders.map(async order => {
      try {
        const read = await this.inspect(order, state);
        order.snapshot = read.snapshot;
        if (read.editMode || read.busy || read.pending) throw Error("Already editing or busy. Resolve this order's native editor before including it.");
        order.message = `${read.snapshot.tabCount} product tab${read.snapshot.tabCount === 1 ? "" : "s"}`;
      } catch (error) { order.status = "error"; order.selected = false; order.message = error.message; }
    }));
    state.phase = "scanned";
    state.message = `${state.orders.length} unique CRM order${state.orders.length === 1 ? "" : "s"} found in this window. Choose orders, then Edit All Orders.`;
  }

  async validate(state) {
    let valid = true;
    await Promise.all(this.selected(state).map(async order => {
      try {
        if (order.status !== "prepared") throw Error("This order has not entered Edit Order successfully.");
        const result = await this.execute(order, state, { op: "preview", draft: state.draft });
        order.preview = result.summary;
        delete order.validationError;
        order.message = `${result.summary.changed} area change${result.summary.changed === 1 ? "" : "s"}, ${result.summary.added} additions, ${result.summary.removed} removals; tabs ${result.summary.tabs.join(", ") || "unchanged"}; ${result.summary.notes.length} new note categories.`;
      } catch (error) { valid = false; order.validationError = true; order.message = error.message; delete order.preview; }
    }));
    state.validationError = !valid;
    return valid;
  }

  async refreshAndVerify(order, state) {
    const read = await this.inspect(order, state);
    if (read.editMode || read.busy || read.pending) throw Error("The native editor is still open or busy. Its unsaved work was not refreshed.");
    const previousDocumentId = order.documentId;
    order.status = "refreshing";
    await this.persist(state);
    await this.tab(order, state.windowId);
    await this.api.tabs.reload(order.tabId);
    const until = Date.now() + 20000;
    let lastError;
    while (Date.now() < until) {
      try {
        await this.inspect(order, state);
        if (previousDocumentId && order.documentId === previousDocumentId) {
          await this.sleep(200);
          continue;
        }
        await this.execute(order, state, { op: "verify", expected: order.expected });
        order.status = "verified";
        order.message = "Saved, refreshed, and verified. Ready for manual inspection.";
        // Duplicate browser tabs are never edited or saved by the batch.
        for (const tabId of order.duplicates) {
          const duplicate = { ...order, tabId };
          try {
            const duplicateRead = await this.inspect(duplicate, state);
            if (duplicateRead.editMode || duplicateRead.busy || duplicateRead.pending) throw Error("unsaved edits");
            await this.tab(duplicate, state.windowId);
            await this.api.tabs.reload(tabId);
          } catch (_) { order.message += " A duplicate tab needs manual review and was left untouched."; }
        }
        return;
      } catch (error) {
        lastError = error;
        // A ready page with mismatching persisted data is a definitive review.
        if (/Saved |saved .*notes|moved to another window|navigated away/.test(error.message)) throw error;
        await this.sleep(200);
      }
    }
    throw lastError || Error("The refreshed order could not be verified.");
  }

  async confirmCanceled(order, state) {
    const until = Date.now() + 15000;
    let lastError;
    while (Date.now() < until) {
      try {
        const read = await this.inspect(order, state);
        if (read.editMode || read.busy || read.pending) throw Error("CRM has not finished discarding this order's edits.");
        if (!order.original) throw Error("This older batch has no discard baseline. Check the order manually before starting a new batch.");
        await this.execute(order, state, { op: "verify", expected: order.original });
        return;
      } catch (error) {
        if (/moved to another window|navigated away|older batch/.test(error.message)) throw error;
        lastError = error;
        await this.sleep(150);
      }
    }
    throw lastError || Error("CRM could not confirm discarding the unsaved edits.");
  }

  async run(state, action) {
    if (action === "scan") return this.scan(state);
    const orders = this.selected(state);
    if (action === "prepare") {
      await Promise.all(orders.map(async order => {
        if (order.status === "prepared") return;
        try {
          const read = await this.inspect(order, state);
          if (read.editMode || read.busy || read.pending) throw Error("This order is already editing or busy.");
          for (const tabId of order.duplicates) {
            const duplicate = await this.inspect({ ...order, tabId }, state);
            if (duplicate.editMode || duplicate.busy || duplicate.pending) throw Error("A duplicate tab for this order has unsaved edits. Resolve it first.");
          }
          order.status = "entering";
          await this.persist(state);
          const result = await this.execute(order, state, { op: "begin" });
          order.snapshot = result.snapshot;
          order.original = result.original;
          order.status = "prepared";
          order.message = "Native editor ready.";
        } catch (error) { order.status = "error"; order.message = error.message; }
      }));
      state.phase = "prepared";
      state.message = orders.every(order => order.status === "prepared") ? "All included orders are in Edit Order. Choose your shared edits." :
        "Some orders could not enter Edit Order. Resolve them or exclude the failed orders before previewing.";
      return;
    }
    if (action === "preview" || action === "apply") {
      if (!await this.validate(state)) {
        state.phase = "prepared";
        state.message = "The batch is blocked. Resolve the listed conflicts; no changes were applied.";
        return;
      }
      if (action === "preview") {
        state.phase = "previewed";
        state.message = "Every included order validates. Review the affected orders and tabs, then Apply changes.";
        return;
      }
      for (const order of orders) {
        order.status = "applying";
        await this.persist(state);
        try {
          const result = await this.execute(order, state, { op: "apply", draft: state.draft });
          order.expected = result.expected;
          order.status = "applied";
          order.message = "Applied to the native editor; not saved yet.";
          await this.persist(state);
        } catch (error) {
          order.status = "review";
          order.message = error.message;
          state.phase = "review";
          state.message = "Apply stopped. No orders were saved. Review the pages or cancel the unsaved batch.";
          return;
        }
      }
      state.phase = "applied";
      state.message = "Changes are applied to all included native editors. Save All & Refresh to keep them.";
      return;
    }
    if (action === "save") {
      // Check every page again before the first save, including window membership.
      for (const order of orders) await this.execute(order, state, { op: "check" });
      for (const order of orders) {
        order.status = "saving";
        await this.persist(state);
        try {
          await this.execute(order, state, { op: "save" });
          order.status = "saved";
          order.message = "Native save completed; waiting to refresh.";
        } catch (error) { order.status = "review"; order.message = error.message; }
        await this.persist(state);
      }
    }
    if (action === "save" || action === "verify") {
      for (const order of orders.filter(order => order.expected && order.status !== "verified")) {
        if (action === "save" && order.status !== "saved") continue;
        try { await this.refreshAndVerify(order, state); }
        catch (error) { order.status = "review"; order.message = error.message; }
        await this.persist(state);
      }
      const success = orders.every(order => order.status === "verified");
      state.phase = success ? "complete" : "review";
      state.message = success ? "All included orders were saved, refreshed, and verified. Their tabs remain open for manual inspection." :
        "Some orders need review. Successful orders were refreshed; unsaved editors were preserved. Check saved orders never repeats a save or adds notes.";
      return;
    }
    if (action === "cancel") {
      for (const order of orders.filter(order => !["verified", "canceled"].includes(order.status))) {
        try {
          const read = await this.inspect(order, state);
          if (read.busy) throw Error("CRM is still saving. Wait and review this order.");
          if (read.editMode) {
            order.status = "canceling";
            await this.persist(state);
            try { await this.execute(order, state, { op: "cancel" }); }
            catch (error) { if (!error.transportLost) throw error; }
            await this.confirmCanceled(order, state);
          }
          order.status = "canceled";
          order.message = "Native editor closed. Previously saved changes are retained.";
        } catch (error) { order.status = "review"; order.message = error.message; }
      }
      state.phase = orders.every(order => ["verified", "canceled"].includes(order.status)) ? "complete" : "review";
      state.message = "Unsaved batch edits were canceled where CRM could confirm ownership. Review any remaining order errors.";
    }
  }
}
