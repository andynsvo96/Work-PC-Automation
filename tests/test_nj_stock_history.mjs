import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../ui_panel.html", import.meta.url), "utf8");
const names = [
  "esc", "fmtDuration", "normalizeCrmOrderIds", "normalizeCrmOrderDetails",
  "crmStockTabDetailText", "crmStockOrderedDetailHtml", "crmStockOrderedDetailText",
  "crmSanmarConfirmations", "buildCrmOrderDetailsHtml", "buildCrmOrderDetailsText",
];
const context = vm.createContext({ crmOrderLinkHtml: (id) => id });
vm.runInContext(names.map((name) => {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing dashboard function ${name}`);
  const end = source.indexOf("\n}", start) + 2;
  return source.slice(start, end);
}).join("\n"), context);

const nj = { po: "H-CJVillafra302", url: "https://www.sanmar.com/orders/nj", po_confirmed: true };
const add = { po: "ADD-H-CJVillafra302", url: "https://www.sanmar.com/orders/add", po_confirmed: true };
const row = {
  order_id: "5000001", success: true, status: "Success", sanmar_confirmation: add,
  stock_orders: [{ sanmar_confirmation: nj }, { sanmar_confirmation: add }],
};

test("NJ and ADD confirmations are both visible in history and copied details", () => {
  const html = context.buildCrmOrderDetailsHtml([row]);
  const text = context.buildCrmOrderDetailsText([row]);
  for (const confirmation of [nj, add]) {
    assert.ok(html.includes(`href="${confirmation.url}"`));
    assert.ok(html.includes(`PO: ${confirmation.po}`));
    assert.ok(text.includes(`PO: ${confirmation.po}`));
    assert.ok(text.includes(confirmation.url));
  }
  assert.equal((html.match(/href=/g) || []).length, 2);
});

test("partial purchases show only confirmed receipts and keep the incomplete status", () => {
  const html = context.buildCrmOrderDetailsHtml([{
    ...row, success: false, status: "Partially successful",
    stock_orders: [{ sanmar_confirmation: nj }, { sanmar_confirmation: { ...add, po_confirmed: false } }],
  }]);
  assert.ok(html.includes("Partially successful"));
  assert.ok(html.includes(nj.url));
  assert.ok(!html.includes(add.url));
});

test("existing orders without NJ retain their single confirmation link", () => {
  const html = context.buildCrmOrderDetailsHtml([{ ...row, stock_orders: undefined }]);
  assert.equal((html.match(/href=/g) || []).length, 1);
  assert.ok(html.includes(add.url));
});

test("duplicate receipts are shown once and PO labels are escaped", () => {
  const confirmation = { ...nj, po: '<NJ "example">' };
  const html = context.buildCrmOrderDetailsHtml([{
    ...row, stock_orders: [{ sanmar_confirmation: confirmation }, { sanmar_confirmation: confirmation }],
  }]);
  assert.equal((html.match(/href=/g) || []).length, 1);
  assert.ok(html.includes("&lt;NJ &quot;example&quot;&gt;"));
  assert.ok(!html.includes(confirmation.po));
});
