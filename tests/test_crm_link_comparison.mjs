import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const context = vm.createContext({ URL, URLSearchParams });
for (const name of ['parseCrmComparisonLink', 'crmLinkFilterLabel', 'crmLinkFilterColumns', 'formatCrmLinkFilterValue']) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing ${name}`);
  vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
}
const { parseCrmComparisonLink: parse, crmLinkFilterColumns: columns, formatCrmLinkFilterValue: value } = context;

test('prioritizes report filters and combines both bounds into one column', () => {
  const rush = parse('https://crm.example/report/967?shippingCharges[low]=1&blank=false&quantity[low]=2&quantity[high]=100');
  const free = parse('https://crm.example/report/967?shippingCharges[high]=1&scheduledPrintDate[low]=11%2F22%2F2022');
  const highValue = parse('https://crm.example/report/966?stockOrdered=false');
  assert.deepEqual(Array.from(columns([rush, free, highValue])), ['shippingCharges', 'scheduledPrintDate', 'blank', 'quantity', 'stockOrdered']);
  assert.equal(rush.report, '967');
  assert.equal(highValue.report, '966');
  assert.equal(value(rush, 'shippingCharges'), '≥ 1');
  assert.equal(value(free, 'shippingCharges'), '≤ 1');
  assert.equal(value(highValue, 'shippingCharges'), '—');
  assert.equal(value(free, 'scheduledPrintDate'), '≥ 11/22/2022');
  assert.equal(value(rush, 'blank'), 'No');
  assert.equal(value(rush, 'quantity'), '2 – 100');
});

test('handles embedded reports, repeated choices and newly added filter names', () => {
  const link = parse('https://crm.example/app#!/report/967?vendorName%5B%5D=B&vendorName%5B%5D=A&vendorName%5B%5D=&newOrderStatus[low]=ready');
  assert.equal(link.report, '967');
  assert.deepEqual(Array.from(columns([link])), ['newOrderStatus', 'vendorName']);
  assert.equal(context.crmLinkFilterLabel('newOrderStatus'), 'New order status');
  assert.equal(value(link, 'vendorName'), 'A, B');
  assert.equal(value(link, 'newOrderStatus'), '≥ ready');
});

test('omits session parameters from the comparison and the new-tab link', () => {
  const link = parse('https://crm.example/app?_token=fixture#/report/967?sessionId=fixture&stockOrdered=false');
  const destination = new URL(link.href);
  assert.equal(destination.search, '');
  assert.equal(destination.hash, '#/report/967?stockOrdered=false');
  assert.deepEqual([...link.filters.entries()].map(([key, values]) => [key, Array.from(values)]), [['stockOrdered', ['false']]]);
  for (const invalid of ['', '/report/967', 'javascript:alert(1)', 'https://user:pass@crm.example/report/967']) {
    assert.equal(parse(invalid), null);
  }
});
