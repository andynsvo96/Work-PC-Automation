import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";
import { openApplicableCRMOrders } from "../crm-order-dark-mode-extension/crm-list-tabs.mjs";

const listUrl = "https://crm2.legacy.printfly.com/report/967";
const sender = { url: listUrl, tab: { id: 42, windowId: 9 } };

function tabAPI(overrides = {}) {
  const created = [];
  return {
    created,
    get: async id => { assert.equal(id, 42); return { id, windowId: 12, index: 3, url: listUrl }; },
    create: async options => { created.push(options); return { id: 100 + created.length }; },
    ...overrides
  };
}

test("opens unique orders in list order in the source tab's current window without activating them", async () => {
  const tabs = tabAPI();
  const result = await openApplicableCRMOrders(["1234601", "1234603", "1234601", "1234605"], sender, tabs);
  assert.deepEqual(result, { success: true, openedOrderIds: ["1234601", "1234603", "1234605"] });
  assert.deepEqual(tabs.created, ["1234601", "1234603", "1234605"].map((id, index) => ({
    url: `https://crm2.legacy.printfly.com/order/${id}`, windowId: 12, openerTabId: 42, index: 4 + index, active: false
  })));
});

test("direct and embedded CRM lists can open applicable orders", async () => {
  for (const path of ["/orders", "/reports/orders", "/app#/report/967", "/app/#!/reports/orders?filter=1"]) {
    const url = `https://crm2.legacy.printfly.com${path}`;
    const tabs = tabAPI({ get: async () => ({ id: 42, windowId: 12, index: 0, url }) });
    assert.equal((await openApplicableCRMOrders(["1234601"], { ...sender, url }, tabs)).success, true);
  }
});

test("rejects foreign, order-page, popup, and missing senders before creating tabs", async () => {
  for (const invalid of [null, {}, { url: listUrl },
    { ...sender, url: "https://crm2.legacy.printfly.com.evil.test/report/967" },
    { ...sender, url: "http://crm2.legacy.printfly.com/report/967" },
    { ...sender, url: "https://crm2.legacy.printfly.com/order/1234601" },
    { ...sender, url: "https://crm2.legacy.printfly.com/app#/order/1234601" },
    { ...sender, url: "chrome-extension://extension/popup.html" }
  ]) {
    const tabs = tabAPI({ get: async () => assert.fail("invalid sender reached tab lookup") });
    assert.equal((await openApplicableCRMOrders(["1234601"], invalid, tabs)).success, false);
    assert.deepEqual(tabs.created, []);
  }
});

test("validates the entire ID list before opening anything", async () => {
  for (const invalid of [[], null, "1234601", [1234601], ["1234601", "123"],
    ["1234601", "https://example.test/"], ["1234601", "1234602\n"], ["1234601", "1234602?x=1"]]) {
    const tabs = tabAPI();
    assert.equal((await openApplicableCRMOrders(invalid, sender, tabs)).success, false);
    assert.deepEqual(tabs.created, []);
  }
});

test("stops if the source closes or navigates away", async () => {
  for (const get of [async () => { throw new Error("closed"); },
    async () => ({ id: 42, windowId: 12, index: 0, url: "https://example.test/" })]) {
    const tabs = tabAPI({ get });
    assert.equal((await openApplicableCRMOrders(["1234601"], sender, tabs)).success, false);
    assert.deepEqual(tabs.created, []);
  }
});

test("reports a partial batch and never automatically retries an opened tab", async () => {
  const tabs = tabAPI();
  let attempts = 0;
  tabs.create = async options => {
    attempts++;
    if (attempts === 2) throw new Error("Chrome rejected the tab");
    tabs.created.push(options);
  };
  const result = await openApplicableCRMOrders(["1234601", "1234603", "1234605"], sender, tabs);
  assert.equal(result.success, false);
  assert.deepEqual(result.openedOrderIds, ["1234601"]);
  assert.match(result.message, /Opened 1 of 3.*Selection remains/);
  assert.equal(attempts, 2);
});

test("background message routes the selected IDs and actual sender to the tab opener", async () => {
  const source = await readFile(new URL("../crm-order-dark-mode-extension/background.js", import.meta.url), "utf8");
  let listener;
  let received;
  const context = vm.createContext({
    chrome: {
      runtime: { onMessage: { addListener: fn => { listener = fn; } } },
      webNavigation: { onCreatedNavigationTarget: { addListener() {} } }
    },
    openApplicableCRMOrders: async (ids, actualSender) => {
      received = { ids, actualSender };
      return { success: true, openedOrderIds: ids };
    }
  });
  vm.runInContext(source.replace(/^import[\s\S]*?from ["'][^"']+["'];\s*/gm, "").replace(/^export /gm, ""), context);
  const ids = ["1234601"];
  const response = new Promise(resolve => {
    assert.equal(listener({ type: "crm-list:open-applicable", orderIds: ids }, sender, resolve), true);
  });
  assert.deepEqual(await response, { success: true, openedOrderIds: ids });
  assert.deepEqual(received, { ids, actualSender: sender });
});
