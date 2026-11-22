import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../crm-order-dark-mode-extension/order-all-batch.js", import.meta.url), "utf8");
const context = vm.createContext({ setTimeout, clearTimeout });
vm.runInContext(source, context);
const batch = context.CRMOrderAllBatch;
const plain = value => JSON.parse(JSON.stringify(value));
const targets = () => [{ id: "101", tab: 2, fingerprint: "stock-a" }, { id: "102", tab: 3, fingerprint: "stock-b" }];
const success = { succeeded: true, supplier: "Sanmar" };
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test("starts every native request immediately and saves each intent before submission", async () => {
  const selected = targets();
  const record = batch.create("1234567", selected);
  const events = [];
  const requests = selected.map(deferred);
  const running = batch.run(record, selected, {
    save: value => events.push({ type: "save", states: value.entries.map(entry => entry.state) }),
    submit: target => { events.push({ type: "submit", id: target.id }); return requests[selected.indexOf(target)].promise; },
    changed() {}
  });
  assert.deepEqual(events.filter(event => event.type === "submit").map(event => event.id), ["101", "102"]);
  for (const id of ["101", "102"]) {
    const position = events.findIndex(event => event.type === "submit" && event.id === id);
    assert.equal(events[position - 1].type, "save");
    assert.equal(events[position - 1].states[id === "101" ? 0 : 1], "submitted");
  }
  requests[1].resolve(success);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(record.entries[1].state, "confirmed");
  assert.equal(record.phase, "submitting");
  requests[0].resolve(success);
  await running;
  assert.equal(record.phase, "confirmed");
  assert.equal(batch.blocked(record, selected), true);
});

test("an error or absent supplier allows an explicit retry without repeating confirmed stock", async () => {
  for (const response of [{ succeeded: false, errors: ["No stock"] }, { succeeded: true }]) {
    const selected = targets();
    const record = batch.create("1234567", selected);
    const calls = [];
    await batch.run(record, selected, { save() {}, changed() {}, submit(target) {
      calls.push(target.id);
      return Promise.resolve(target.id === "101" ? success : response);
    } });
    assert.deepEqual(calls, ["101", "102"]);
    assert.equal(record.phase, "review");
    assert.deepEqual(plain(record.entries.map(entry => entry.state)), ["confirmed", "review"]);
    assert.equal(batch.blocked(record, selected), false);
    const retry = batch.retryable(record, selected);
    assert.deepEqual(plain(retry.map(target => target.id)), ["102"]);
    const second = batch.create("1234567", retry, record);
    await batch.run(second, retry, { save() {}, changed() {}, submit(target) {
      calls.push(target.id); return Promise.resolve(success);
    } });
    assert.deepEqual(calls, ["101", "102", "102"]);
    assert.equal(second.phase, "confirmed");
    assert.equal(batch.blocked(second, selected), true);
  }
});

test("a lost or timed-out response can be retried by another explicit click", async () => {
  for (const submit of [() => Promise.reject(new Error("lost")), () => new Promise(() => {})]) {
    const selected = targets().slice(0, 1);
    const record = batch.create("1234567", selected);
    await batch.run(record, selected, { save() {}, changed() {}, submit, timeoutMs: 5 });
    assert.equal(record.entries[0].state, "review");
    assert.equal(batch.blocked(record, selected), false);
  }
});

test("browser storage must record intent before any stock request", async () => {
  let calls = 0;
  const selected = targets();
  const record = batch.create("1234567", selected);
  await assert.rejects(batch.run(record, selected, {
    save() { throw new Error("storage full"); }, changed() {}, submit() { calls++; }
  }), /storage full/);
  assert.equal(calls, 0);
  assert.deepEqual(plain(record.entries.map(entry => entry.state)), ["prepared", "prepared"]);
});

test("a mid-batch storage failure stops remaining submissions without repeating earlier requests", async () => {
  const selected = targets();
  const record = batch.create("1234567", selected);
  const calls = [];
  let writes = 0;
  await batch.run(record, selected, {
    save() { if (++writes === 3) throw new Error("storage full"); }, changed() {},
    submit(target) { calls.push(target.id); return Promise.resolve(success); }
  });
  assert.deepEqual(calls, ["101"]);
  assert.equal(record.phase, "review");
  assert.deepEqual(plain(record.entries.map(entry => entry.state)), ["confirmed", "not_sent"]);
});

test("refresh never resumes a submitted stock request automatically", () => {
  const record = batch.create("1234567", targets());
  record.entries[0].state = "submitted";
  batch.resume(record);
  assert.deepEqual(plain(record.entries.map(entry => entry.state)), ["review", "not_sent"]);
  assert.equal(batch.blocked(record, targets()), false);
  assert.match(record.entries[0].message, /page changed/);
});

test("pending native requests still block a second concurrent submission", () => {
  const selected = targets();
  const record = batch.create("1234567", selected);
  assert.equal(batch.blocked(record, selected), true);
  record.entries[0].state = "submitted";
  record.entries[1].state = "review";
  assert.equal(batch.blocked(record, selected), true);
  record.entries[0].state = "confirmed";
  assert.equal(batch.blocked(record, selected), false);
});

test("preserves confirmed receipts across later batches and permits new stock contents", () => {
  const first = batch.create("1234567", targets().slice(0, 1));
  first.entries[0].state = "confirmed";
  const secondTargets = targets().slice(1);
  const second = batch.create("1234567", secondTargets, first);
  second.entries[0].state = "confirmed";
  assert.equal(batch.blocked(second, targets().slice(0, 1)), true);
  const addedSize = [{ ...targets()[0], fingerprint: "stock-with-new-size" }];
  assert.equal(batch.blocked(second, addedSize), false);
  const third = batch.create("1234567", addedSize, second);
  assert.equal(third.history.length, 2);
  assert.equal(batch.valid(third, "1234567"), true);
  assert.equal(batch.valid(third, "7654321"), false);
});

test("stock fingerprints ignore prices, notes, numeric type changes and row sorting", () => {
  const design = { designItems: [
    {id: 10, productId: 20, colorId: 30, quantity: "2", pricePerPiece: "25.00", sizes: [{id: 1,sizeId: 2,quantity: "1"}, {id: 2,sizeId: 3,quantity: 1}]},
    {id: 11,productId: 21,quantity: 1,sizes: []}
  ] };
  const original = batch.fingerprint(design);
  design.designItems[0].quantity = 2;
  design.designItems[0].pricePerPiece = "30.00";
  design.designItems[0].notes = "Unrelated edit";
  design.designItems[0].sizes.reverse();
  design.designItems.reverse();
  assert.equal(batch.fingerprint(design), original);
  design.designItems[0].quantity++;
  assert.notEqual(batch.fingerprint(design), original);
});

test("duplicate design IDs and invalid order IDs cannot create a batch", () => {
  assert.throws(() => batch.create("1234567", [targets()[0], targets()[0]]), /uniquely/);
  assert.throws(() => batch.create("123", targets()), /uniquely/);
  assert.throws(() => batch.create("1234567", []), /uniquely/);
});

test("view callback failures cannot lose or resend a confirmed CRM result", async () => {
  const selected = targets();
  const record = batch.create("1234567", selected);
  const saved = [];
  await batch.run(record, selected, {
    save: value => saved.push(plain(value)), submit: () => Promise.resolve(success),
    changed() { throw new Error("view removed"); }, finished() { throw new Error("scope removed"); }
  });
  assert.equal(record.phase, "confirmed");
  assert.equal(saved.at(-1).phase, "confirmed");
});

test("native ordering runs in MAIN with existing CRM access and no Automation bridge", async () => {
  const manifest = JSON.parse(await readFile(new URL("../crm-order-dark-mode-extension/manifest.json", import.meta.url), "utf8"));
  const entry = manifest.content_scripts.find(script => script.js.includes("order-all.js"));
  assert.equal(entry.world, "MAIN");
  assert.deepEqual(entry.js, ["order-all-batch.js", "order-all.js"]);
  assert.deepEqual(entry.matches, ["https://crm2.legacy.printfly.com/*"]);
  assert.deepEqual(manifest.permissions, ["storage", "tabs", "webNavigation", "scripting"]);
});
