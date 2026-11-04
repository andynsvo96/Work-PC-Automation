import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../crm-order-dark-mode-extension/bridge.js", import.meta.url), "utf8");
const bridge = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("item suggestions retain CRM stock and the literal required replacement product", async (t) => {
  const products = [{ style: "DM130", description: "District Tee", color: "Red", design_item_ids: ["design-item-101"], total_quantity: 4 }];
  const request = t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, bridge.LOCAL_MANUAL_ORDER_PROCESS_ENDPOINT);
    assert.deepEqual(JSON.parse(options.body), {
      order_id: "1234567", automation: "stock_issue_item", reason: "", product: "5000 Black, Soft & Light", products
    });
    return { ok: true, json: async () => ({ success: true }) };
  });
  await bridge.startLocalManualOrderProcessing("1234567", "stock_issue_item", "", { product: "5000 Black, Soft & Light", products });
  assert.equal(request.mock.callCount(), 1);
});

test("item suggestions preserve each selected stock item's own replacement", async (t) => {
  const products = [
    { style: "DM130", color: "Red", design_item_ids: ["design-item-101"], replacement_product: "5000 Black, Soft & Light" },
    { style: "18500", color: "Navy", design_item_ids: ["design-item-102"], replacement_product: "Zip Hoodie" }
  ];
  const request = t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, bridge.LOCAL_MANUAL_ORDER_PROCESS_ENDPOINT);
    assert.deepEqual(JSON.parse(options.body), {
      order_id: "1234567", automation: "stock_issue_item", reason: "", products
    });
    return { ok: true, json: async () => ({ success: true }) };
  });
  await bridge.startLocalManualOrderProcessing("1234567", "stock_issue_item", "", { products });
  assert.equal(request.mock.callCount(), 1);
});

test("copyright queue requests retain the complete scan and selected stable IDs", async (t) => {
  const designs = {
    order_id: "5451301",
    all_designs: [
      { tab_number: 1, design_id: "101", design_name: "Halloween Crypt", quantity: 1, subtotal: "43.64" },
      { tab_number: 2, design_id: "202", design_name: "Pokemon Crypt", quantity: 1, subtotal: "43.64" }
    ],
    selected_design_ids: ["202"]
  };
  const request = t.mock.method(globalThis, "fetch", async (_url, options) => {
    assert.equal(_url, bridge.LOCAL_COPYRIGHT_DESIGNS_ENDPOINT);
    assert.deepEqual(JSON.parse(options.body), {
      order_id: "5451301", automation: "copyright_cancel", reason: "Protected character", designs
    });
    return { ok: true, json: async () => ({ success: true }) };
  });
  await bridge.startLocalManualOrderProcessing("5451301", "copyright_cancel", "Protected character", { designs });
  assert.equal(request.mock.callCount(), 1);
});

test("copyright requests preserve separate order tabs that reuse artwork", async (t) => {
  const designs = {
    order_id: "5451301",
    all_designs: [
      { tab_number: 1, order_design_id: "900", design_id: "101", design_name: "Shared artwork", quantity: 1, subtotal: "120.00" },
      { tab_number: 2, order_design_id: "901", design_id: "101", design_name: "Shared artwork", quantity: 1, subtotal: "80.00" }
    ],
    selected_design_ids: ["order-design:901"]
  };
  const request = t.mock.method(globalThis, "fetch", async (url, options) => {
    assert.equal(url, bridge.LOCAL_COPYRIGHT_DESIGNS_ENDPOINT);
    assert.deepEqual(JSON.parse(options.body).designs, designs);
    return { ok: true, json: async () => ({ success: true }) };
  });
  await bridge.startLocalManualOrderProcessing("5451301", "copyright_cancel", "Protected character", { designs });
  assert.equal(request.mock.callCount(), 1);
});

test("an older app cannot turn a partial selection into legacy full cancellation", async (t) => {
  const request = t.mock.method(globalThis, "fetch", async (url) => {
    assert.equal(url, bridge.LOCAL_COPYRIGHT_DESIGNS_ENDPOINT);
    return { ok: false, status: 404, json: async () => null };
  });
  await assert.rejects(bridge.startLocalManualOrderProcessing("5451301", "copyright_cancel", "Reason", {
    designs: { selected_design_ids: ["202"] }
  }), /Restart the local Automation app.*No copyright task was queued/);
  assert.equal(request.mock.callCount(), 1);
});

test("queue requests can receive confirmation after the old timeout", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let signal;
  let finish;
  t.mock.method(globalThis, "fetch", async (_url, options) => {
    signal = options.signal;
    await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => ({ success: true }) };
  });
  const pending = bridge.startLocalManualOrderProcessing("123", "stock_issue_size");
  t.mock.timers.tick(3000);
  assert.equal(signal.aborted, false);
  finish();
  assert.deepEqual(await pending, { success: true });
});

test("queue timeout explains uncertain acceptance without retrying", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const fetchMock = t.mock.method(globalThis, "fetch", (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason));
  }));
  const pending = bridge.startLocalManualOrderProcessing("123", "stock_issue_size");
  const rejected = assert.rejects(pending, /task may already be queued.*Check the Automation queue/);
  t.mock.timers.tick(20000);
  await rejected;
  assert.equal(fetchMock.mock.callCount(), 1);
});

test("status checks retain their short timeout", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.mock.method(globalThis, "fetch", (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(signal.reason));
  }));
  const pending = bridge.getLocalBridgeStatus();
  t.mock.timers.tick(2500);
  const result = await pending;
  assert.equal(result.connected, false);
  assert.match(result.message, /in time/);
});
