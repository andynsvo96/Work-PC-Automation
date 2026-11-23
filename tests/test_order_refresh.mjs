import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";
import { canRefreshOrderPage } from "../crm-order-dark-mode-extension/order-refresh.mjs";

function page(overrides = {}) {
  const resource = { id: "1000001" };
  const scope = { order: { getResource: () => resource }, saveOrder() {}, ...overrides };
  const context = vm.createContext({
    location: { pathname: "/order/1000001", hash: "" },
    window: { angular: { element: () => ({ scope: () => scope }) } },
    document: { querySelectorAll: selector => selector.startsWith(".modal") ? [] : [{}] },
    getComputedStyle: () => ({ visibility: "visible" }),
  });
  vm.runInContext(`var canRefresh = ${canRefreshOrderPage.toString()}`, context);
  return { context, resource, safe: () => context.canRefresh("1000001") };
}

test("refresh guard requires the exact ready order and preserves drafts", () => {
  assert.equal(page().safe(), true);
  for (const field of ["editMode", "saving", "savingOrder", "fileUploading"]) {
    assert.equal(page({ [field]: true }).safe(), false, field);
  }
  for (const field of ["addSalesNotes", "addProductionNotes", "addArtNotes"]) {
    const fixture = page(); fixture.resource[field] = "Unsaved note";
    assert.equal(fixture.safe(), false, field);
  }
  const fixture = page(); fixture.context.location.pathname = "/order/1000002";
  assert.equal(fixture.safe(), false);
  fixture.context.location.pathname = "/reports/1000001";
  assert.equal(fixture.safe(), false);
});

test("refresh guard defers for native dialogs and staged All Tabs edits", () => {
  const fixture = page();
  fixture.context.window.__crmAllTabsHasPendingChanges = () => true;
  assert.equal(fixture.safe(), false);
  fixture.context.window.__crmAllTabsHasPendingChanges = () => false;
  fixture.context.document.querySelectorAll = selector => selector.startsWith(".modal")
    ? [{ getClientRects: () => [{}] }] : [{}];
  assert.equal(fixture.safe(), false);
  fixture.context.window.angular = null;
  assert.equal(fixture.safe(), false);
});

const source = await readFile(new URL("../crm-order-dark-mode-extension/content.js", import.meta.url), "utf8");
const refreshFunction = source.slice(source.indexOf("function refreshOrderAfterProcessorCompletion("),
  source.indexOf("\nfunction setOrderProcessorResult("));

function completionFixture({ safe = true, active = true, openedAt = 0 } = {}) {
  const stored = new Map(active ? [["crm-order-process-active:1000001", "1"]] : []);
  const reloads = [], notices = [];
  const context = vm.createContext({
    currentOrderId: () => "1000001", orderProcessorDocumentOpenedAt: openedAt, orderProcessorRefreshPending: false,
    sessionStorage: { getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key) },
    window: { location: { reload: () => reloads.push(true) } },
    chrome: { runtime: { sendMessage: async () => ({ success: true, safe }) } },
    manualOrderProcessorButton: () => ({}), setOrderProcessorResult: (...args) => notices.push(args),
  });
  vm.runInContext(refreshFunction, context);
  const runtime = { orderId: "1000001", taskId: "task-a", attempt: 1, lastSuccess: true,
    queued: false, running: false, completedAt: "2026-11-23T10:00:00" };
  return { context, reloads, notices, runtime, run: value => context.refreshOrderAfterProcessorCompletion(value || runtime) };
}
const settle = () => new Promise(resolve => setImmediate(resolve));

test("completion refreshes once per attempt and supports duplicate tabs without submission markers", async () => {
  const fixture = completionFixture({ active: false });
  fixture.run(); await settle();
  fixture.run(); await settle();
  assert.equal(fixture.reloads.length, 1);
  fixture.runtime.attempt = 2;
  fixture.run(); await settle();
  assert.equal(fixture.reloads.length, 2);
});

test("completion leaves edits intact and retries the refresh after they close", async () => {
  const fixture = completionFixture({ safe: false });
  fixture.run(); await settle();
  assert.equal(fixture.reloads.length, 0);
  assert.match(fixture.notices[0][1], /Refresh pending/);
  fixture.context.chrome.runtime.sendMessage = async () => ({ safe: true });
  fixture.run(); await settle();
  assert.equal(fixture.reloads.length, 1);
});

test("completion ignores other orders, active work, and history predating a newly opened tab", async () => {
  const fixture = completionFixture({ active: false, openedAt: Date.parse("2026-11-23T11:00:00") });
  fixture.run();
  fixture.run({ ...fixture.runtime, orderId: "1000002" });
  fixture.run({ ...fixture.runtime, running: true });
  await settle();
  assert.equal(fixture.reloads.length, 0);
});

test("failed attempts refresh persisted partial changes and reconnection keeps polling", async () => {
  const fixture = completionFixture();
  fixture.run({ ...fixture.runtime, lastSuccess: false }); await settle();
  assert.equal(fixture.reloads.length, 1);
  const polling = source.slice(source.indexOf("function beginOrderProcessorPolling("), source.indexOf("\nfunction closeOrderProcessMenu("));
  assert.match(polling, /orderId: currentOrderId\(\)/);
  assert.doesNotMatch(polling, /if \(!renderOrderProcessorStatus.*stopOrderProcessorPolling/);
});
