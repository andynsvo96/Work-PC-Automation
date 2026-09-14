import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../crm-order-dark-mode-extension/bridge.js", import.meta.url), "utf8");
const bridge = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("queue requests can receive confirmation after the old timeout", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  for (const submit of [
    () => bridge.startLocalManualOrderProcessing("123", "stock_issue_size"),
    () => bridge.startLocalOrderProcessing("123", false)
  ]) {
    let signal;
    let finish;
    t.mock.method(globalThis, "fetch", async (_url, options) => {
      signal = options.signal;
      await new Promise((resolve) => { finish = resolve; });
      return { ok: true, json: async () => ({ success: true }) };
    });
    const pending = submit();
    t.mock.timers.tick(3000);
    assert.equal(signal.aborted, false);
    finish();
    assert.deepEqual(await pending, { success: true });
    t.mock.restoreAll();
  }
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
