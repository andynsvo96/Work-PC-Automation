import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../crm-order-dark-mode-extension/rush-list-highlight.js", import.meta.url), "utf8");
const context = vm.createContext({
  document: { readyState: "loading", addEventListener() {} },
  getComputedStyle: row => row.computedStyle
});
vm.runInContext(source, context);
const evaluate = expression => vm.runInContext(expression, context);

test("rush highlights cannot make stock issues or dimmed status colors applicable", () => {
  for (const [color, opacity, highlighted, expected] of [
    ["rgb(243, 196, 156)", "1", false, true],
    ["rgb(134, 32, 159)", "1", true, true],
    ["rgb(32, 223, 75)", "1", false, true],
    ["rgb(34, 236, 72)", "1", true, true],
    ["rgb(33, 150, 243)", "0.5", true, false],
    ["rgb(33, 150, 243)", "1", true, false],
    ["rgb(32, 223, 75)", "0.5", false, false],
    ["rgb(243, 196, 156)", "0.5", true, false],
    ["rgb(196, 125, 84)", "1", true, false]
  ]) {
    context.candidate = {
      style: { backgroundColor: color },
      computedStyle: { backgroundColor: "rgb(101, 110, 135)", opacity },
      classList: { contains: () => highlighted }
    };
    assert.equal(evaluate("crmListRowIsApplicable(candidate)"), expected, `${color}, opacity ${opacity}, highlighted ${highlighted}`);
  }
});

test("Friday requires Friday production and the immediately following Monday fulfillment", () => {
  for (const [date, production, fulfillment] of [
    ["2026, 9, 30, 23, 59", "2026-10-30", "2026-11-02"],
    ["2026, 2, 6, 0, 1", "2026-03-06", "2026-03-09"],
    ["2027, 11, 31", "2027-12-31", "2028-01-03"],
    ["2036, 1, 29", "2036-02-29", "2036-03-03"]
  ]) {
    assert.equal(evaluate(`JSON.stringify(crmListRushDates(new Date(${date})))`), JSON.stringify({ production, fulfillment }));
  }
});

test("Saturday and Sunday both use Monday production with Tuesday fulfillment", () => {
  for (const date of ["2026, 9, 31, 23, 59", "2026, 10, 1, 0, 1"]) {
    assert.equal(evaluate(`JSON.stringify(crmListRushDates(new Date(${date})))`),
      JSON.stringify({ production: "2026-11-02", fulfillment: "2026-11-03" }));
  }
  for (const date of ["2028, 11, 30", "2028, 11, 31"]) {
    assert.equal(evaluate(`JSON.stringify(crmListRushDates(new Date(${date})))`),
      JSON.stringify({ production: "2029-01-01", fulfillment: "2029-01-02" }));
  }
});

test("Monday through Thursday leave CRM's own urgency colors alone", () => {
  for (const day of [26, 27, 28, 29]) {
    assert.equal(evaluate(`crmListRushDates(new Date(2026, 9, ${day}))`), null);
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
  assert.equal(evaluate('crmListConsistentDate(["11/02/2026", "2026-11-03"])'), "");
  assert.equal(evaluate('crmListConsistentDate(["11/02/2026", "unreadable"])'), "");
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
