import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../crm-order-dark-mode-extension/rush-list-highlight.js", import.meta.url), "utf8");
const context = vm.createContext({ document: { readyState: "loading", addEventListener() {} } });
vm.runInContext(source, context);
const evaluate = expression => vm.runInContext(expression, context);

test("Friday targets only the immediately following Monday, using local calendar dates", () => {
  assert.equal(evaluate("crmFridayMondayDate(new Date(2026, 9, 30, 23, 59))"), "2026-11-02");
  assert.equal(evaluate("crmFridayMondayDate(new Date(2026, 2, 6, 0, 1))"), "2026-03-09");
  assert.equal(evaluate("crmFridayMondayDate(new Date(2027, 11, 31))"), "2028-01-03");
  assert.equal(evaluate("crmFridayMondayDate(new Date(2036, 1, 29))"), "2036-03-03");
});

test("other weekdays leave CRM's own urgency colors alone", () => {
  for (const day of [25, 26, 27, 28, 29, 31]) {
    assert.equal(evaluate(`crmFridayMondayDate(new Date(2026, 9, ${day}))`), "");
  }
});

test("CRM date formats include two-digit years and ISO timestamps without UTC conversion", () => {
  for (const value of ["11/2/2026", "11/02/26", "2026-11-02", "2026-11-02T00:00:00Z", "11/02/2026 5:00 PM"]) {
    assert.equal(evaluate(`crmListDateKey(${JSON.stringify(value)})`), "2026-11-02");
  }
});

test("missing, impossible, and conflicting dates are not guessed", () => {
  for (const value of ["", "not a date", "02/30/2026", "13/02/2026", "11/02/2026 11/03/2026", "2026-11-020"]) {
    assert.equal(evaluate(`crmListDateKey(${JSON.stringify(value)})`), "");
  }
  assert.equal(evaluate('crmListDateKey("2028-02-29")'), "2028-02-29");
});

test("the highlighter supports direct and embedded lists but excludes order and unrelated routes", () => {
  for (const [pathname, hash] of [
    ["/report/967", ""], ["/reports/orders", ""], ["/orders", ""],
    ["/app", "#/reports/orders?status=locked"], ["/app", "#reports/orders"],
    ["/app/", "#!/report/967"]
  ]) {
    assert.equal(evaluate(`crmIsOrderListRoute(${JSON.stringify({ pathname, hash })})`), true);
  }
  for (const [pathname, hash] of [
    ["/order/1234567", ""], ["/app", "#/order/1234567"], ["/login", ""],
    ["/accounts", ""], ["/reporting-settings", ""]
  ]) {
    assert.equal(evaluate(`crmIsOrderListRoute(${JSON.stringify({ pathname, hash })})`), false);
  }
});
