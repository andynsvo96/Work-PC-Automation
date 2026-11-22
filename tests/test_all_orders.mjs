import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { AllOrdersController } from "../crm-order-dark-mode-extension/all-orders.mjs";
import { CRM_ORIGIN, SESSION_PREFIX, emptyDraft, orderIdFromUrl, editorGroups, normalizeDraft } from "../crm-order-dark-mode-extension/all-orders-model.mjs";

function fixture() {
  const storage = {};
  const events = [];
  const pages = new Map();
  for (const [id, windowId, orderId] of [[1, 11, "1000001"], [2, 11, "1000002"], [3, 22, "1000003"], [4, 11, "1000001"]]) {
    pages.set(id, { id, windowId, orderId, url: `${CRM_ORIGIN}/order/${orderId}`, status: "complete", index: id, active: id === 1,
      editMode: false, doc: `doc-${id}`, saved: 0, reloads: 0, snapshot: { tabCount: id === 2 ? 3 : 1,
        locations: ["Front", "Front-Left Chest", "Back"], methods: ["HD Digital", "Embroidery"],
        groups: [{ sourceKey: "front", label: "Front", tabs: id === 2 ? [1, 2, 3] : [1], methods: ["HD Digital"] }] } });
  }
  pages.set(5, { id: 5, windowId: 11, url: `${CRM_ORIGIN}/reports/1234567`, index: 5, status: "complete" });
  const api = {
    runtime: { id: "fixture", getURL: path => "chrome-extension://fixture/" + path },
    windows: { async get(id) { assert.ok([11, 22].includes(id)); return { id }; } },
    storage: { session: {
      async get(name) { return structuredClone(name === null ? storage : { [name]: storage[name] }); },
      async set(values) { Object.assign(storage, structuredClone(values)); }
    } },
    tabs: {
      async query(options) { events.push(["query", options]); return [...pages.values()].filter(page => page.windowId === options.windowId).map(page => ({ ...page })); },
      async get(id) { if (!pages.has(id)) throw Error("Closed tab"); return { ...pages.get(id) }; },
      async reload(id) {
        const page = pages.get(id);
        assert.equal(page.editMode, false, "never refresh an unsaved editor");
        page.reloads++; page.doc += "-reload"; events.push(["reload", id]);
      }
    },
    scripting: { async executeScript({ target, world, args: [command], func }) {
      assert.equal(world, "MAIN");
      assert.equal(typeof func, "function");
      const page = pages.get(target.tabId);
      if (target.documentIds) assert.deepEqual(target.documentIds, [page.doc]);
      events.push([command.op, page.id]);
      let result;
      if (command.op === "inspect") result = { success: true, orderId: page.orderId, editMode: page.editMode, pending: false, busy: false, snapshot: page.snapshot };
      if (command.op === "begin") {
        if (page.beginWait) await page.beginWait;
        if (page.failBegin) throw Error("Edit unavailable");
        page.editMode = true; result = { success: true, snapshot: page.snapshot };
      }
      if (command.op === "preview") {
        if (page.failPreview) throw Error("Duplicate Front-Left Chest on tabs 2");
        result = { success: true, summary: { changed: 1, added: 0, removed: 0, tabs: [1], notes: Object.keys(command.draft.notes).filter(kind => command.draft.notes[kind]) } };
      }
      if (command.op === "apply") { page.applied = true; result = { success: true, expected: { orderId: page.orderId, notes: command.draft.notes } }; }
      if (command.op === "check") { if (page.changed) throw Error("The order changed after preview"); result = { success: true }; }
      if (command.op === "save") {
        page.saved++;
        if (page.failSave) throw Error("Save rejected; inspect CRM");
        page.editMode = false;
        if (page.lostResponse) throw Error("Response lost");
        result = { success: true };
      }
      if (command.op === "verify") { if (page.failVerify) throw Error("Saved art notes could not be verified"); result = { success: true }; }
      if (command.op === "cancel") { page.editMode = false; result = { success: true }; }
      const reply = { frameId: page.frameId || 0, documentId: page.doc, result };
      return page.ambiguous && target.allFrames ? [reply, { ...reply, frameId: 9 }] : [reply];
    } }
  };
  const sender = { id: "fixture", url: api.runtime.getURL("popup.html") };
  const controller = new AllOrdersController(api, { id: () => "batch-fixture" });
  const request = (action, extra = {}, current = controller) => current.handle({ action, windowId: 11, ...extra }, sender);
  const run = async (action, extra = {}, current = controller) => {
    await request(action, extra, current);
    await current.running.get(extra.windowId || 11);
    return request("get", extra.windowId ? { windowId: extra.windowId } : {}, current);
  };
  const draft = async () => {
    const state = await request("get");
    const value = emptyDraft(); value.changes.front = { location: "Front-Left Chest" }; value.notes.sales = "Shared note";
    return request("draft", { revision: state.revision + 1, draft: value });
  };
  return { api, storage, pages, events, controller, sender, request, run, draft };
}

test("only exact CRM order routes, including embedded routes, are eligible", () => {
  for (const path of ["/order/1234567", "/order/1234567/", "/app#/order/1234567", "/app#!/order/1234567?view=x"]) {
    assert.equal(orderIdFromUrl(CRM_ORIGIN + path), "1234567");
  }
  for (const url of [CRM_ORIGIN + "/reports/1234567", CRM_ORIGIN + "/order/12345678", "https://evil.example/order/1234567", CRM_ORIGIN + ".evil.example/order/1234567"]) {
    assert.equal(orderIdFromUrl(url), "");
  }
});

test("only the extension popup may issue commands, including read commands", async () => {
  const f = fixture();
  for (const sender of [{}, { ...f.sender, tab: { id: 1 } }, { ...f.sender, url: CRM_ORIGIN + "/order/1000001" }, { ...f.sender, id: "other" }]) {
    await assert.rejects(f.controller.handle({ action: "scan", windowId: 11 }, sender), /only.*popup/);
  }
  assert.equal(f.events.length, 0);
});

test("scan is read-only, window-scoped, and de-duplicates orders", async () => {
  const f = fixture();
  const state = await f.run("scan");
  assert.deepEqual(f.events[0], ["query", { windowId: 11 }]);
  assert.deepEqual(state.orders.map(order => order.orderId), ["1000001", "1000002"]);
  assert.deepEqual(state.orders[0].duplicates, [4]);
  assert.ok(f.events.every(([op]) => ["query", "inspect"].includes(op)));
  assert.equal(editorGroups(state.orders)[0].targets[1].tabs.length, 3);
});

test("one action enters the native editors in parallel and waits for every response", async () => {
  const f = fixture(); await f.run("scan");
  let finish;
  f.pages.get(1).beginWait = new Promise(resolve => { finish = resolve; });
  await f.request("prepare");
  for (let i = 0; i < 20 && !f.events.some(([op, id]) => op === "begin" && id === 2); i++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(f.events.some(([op, id]) => op === "begin" && id === 2));
  assert.equal((await f.request("get")).busy, true);
  finish(); await f.controller.running.get(11);
  assert.ok((await f.request("get")).orders.every(order => order.status === "prepared"));
});

test("native save, refresh, and verification retain all per-order results and never edit duplicates", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft();
  await f.run("preview"); await f.run("apply");
  assert.equal(f.events.filter(([op]) => op === "save").length, 0);
  const state = await f.run("save");
  assert.equal(state.phase, "complete");
  assert.ok(state.orders.every(order => order.status === "verified"));
  assert.equal(f.pages.get(4).saved, 0); assert.equal(f.pages.get(4).reloads, 1);
  assert.equal(f.pages.get(3).saved + f.pages.get(3).reloads, 0);
  assert.ok(f.events.findIndex(([op]) => op === "reload") > f.events.findLastIndex(([op]) => op === "save"));
});

test("one conflict blocks the entire batch before mutation", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft();
  f.pages.get(2).failPreview = true;
  const state = await f.run("apply");
  assert.equal(state.phase, "prepared");
  assert.match(state.orders[1].message, /Duplicate/);
  assert.equal(f.events.filter(([op]) => ["apply", "save"].includes(op)).length, 0);
});

test("moved tabs and changed native editors stop saves before the first submission", async () => {
  for (const change of [page => { page.windowId = 22; }, page => { page.changed = true; }]) {
    const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft(); await f.run("apply");
    change(f.pages.get(2));
    assert.equal((await f.run("save")).phase, "review");
    assert.equal(f.events.filter(([op]) => op === "save").length, 0);
  }
});

test("a rejected save preserves its editor, while other saved orders refresh", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft(); await f.run("apply");
  f.pages.get(2).failSave = true;
  const state = await f.run("save");
  assert.equal(state.orders[0].status, "verified"); assert.equal(state.orders[1].status, "review");
  assert.equal(f.pages.get(2).reloads, 0); assert.equal(f.pages.get(2).editMode, true);
  await f.run("verify");
  assert.equal(f.pages.get(2).saved, 1); assert.equal(f.pages.get(2).reloads, 0);
});

test("lost save responses recover through read-only verification without adding notes or saving twice", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft(); await f.run("apply");
  f.pages.get(1).lostResponse = true;
  assert.equal((await f.run("save")).phase, "review");
  assert.equal((await f.run("verify")).phase, "complete");
  assert.equal(f.pages.get(1).saved, 1);
  assert.equal(f.events.filter(([op, id]) => op === "apply" && id === 1).length, 1);
});

test("verification waits for a new document instead of accepting the page before reload", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft(); await f.run("apply");
  const reload = f.api.tabs.reload;
  let waited = 0;
  f.api.tabs.reload = async id => { if (id !== 1) await reload(id); };
  f.controller.sleep = async () => { waited++; await reload(1); };
  assert.equal((await f.run("save")).phase, "complete");
  assert.ok(waited > 0);
});

test("closing/reopening the popup retains literal note drafts and rejects stale revisions", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare");
  const draft = emptyDraft(); draft.notes.production = "Keep trailing space ";
  await f.request("draft", { revision: 20, draft });
  await f.request("draft", { revision: 19, draft: emptyDraft() });
  const restarted = new AllOrdersController(f.api);
  assert.equal((await f.request("get", {}, restarted)).draft.notes.production, draft.notes.production);
  assert.equal(normalizeDraft(draft).notes.production, draft.notes.production);
});

test("an interrupted service worker leaves a review receipt and never resumes mutations", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft(); await f.run("apply");
  const name = SESSION_PREFIX + 11;
  f.storage[name].busy = true; f.storage[name].orders[0].status = "saving";
  f.pages.get(1).editMode = false;
  const restarted = new AllOrdersController(f.api);
  const before = f.events.length;
  const state = await f.request("get", {}, restarted);
  assert.equal(state.phase, "review"); assert.equal(state.busy, false);
  assert.equal(f.events.length, before);
  await f.run("verify", {}, restarted);
  assert.equal(f.pages.get(1).saved, 0);
});

test("an ambiguous frame or sleeping tab is excluded before any native write", async () => {
  const f = fixture(); f.pages.get(1).ambiguous = true; f.pages.get(2).frozen = true;
  const state = await f.run("scan");
  assert.ok(state.orders.every(order => !order.selected && order.status === "error"));
  assert.equal(f.events.filter(([op]) => op === "begin").length, 0);
});

test("duplicate tabs with an open editor block entry instead of overwriting their draft", async () => {
  const f = fixture(); f.pages.get(4).editMode = true;
  await f.run("scan"); const state = await f.run("prepare");
  assert.equal(state.orders[0].status, "error");
  assert.equal(f.pages.get(1).editMode, false);
  assert.match(state.orders[0].message, /duplicate tab/);
});

test("unfinished batches in other windows reserve the same order", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare");
  const page = f.pages.get(3); page.orderId = "1000001"; page.url = CRM_ORIGIN + "/order/1000001";
  await f.run("scan", { windowId: 22 });
  await assert.rejects(f.request("prepare", { windowId: 22 }), /unfinished.*another window/);
});

test("cancel uses only native cancel and protects previously saved work", async () => {
  const f = fixture(); await f.run("scan"); await f.run("prepare"); await f.draft(); await f.run("apply");
  const state = await f.run("cancel");
  assert.equal(state.phase, "complete");
  assert.ok(state.orders.every(order => order.status === "canceled"));
  assert.equal(f.events.filter(([op]) => op === "save").length, 0);
});

test("the new adapter is CRM-scoped and exposes no page-event or local-app command channel", async () => {
  const base = new URL("../crm-order-dark-mode-extension/", import.meta.url);
  const manifest = JSON.parse(await readFile(new URL("manifest.json", base), "utf8"));
  const adapter = manifest.content_scripts.find(entry => entry.js.includes("all-orders-page.js"));
  assert.deepEqual(adapter.matches, [CRM_ORIGIN + "/*"]);
  assert.equal(adapter.world, "MAIN"); assert.equal(adapter.all_frames, true);
  const sources = await Promise.all(["all-orders-page.js", "all-orders.mjs"].map(name => readFile(new URL(name, base), "utf8")));
  assert.doesNotMatch(sources.join("\n"), /(?:fetch\s*\(|XMLHttpRequest|addEventListener\s*\(\s*["']message|127\.0\.0\.1|startLocalManualOrderProcessing)/);
});
