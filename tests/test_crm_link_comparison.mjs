import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const context = vm.createContext({ URL, URLSearchParams });
for (const name of [
  'crmLinkFragmentRoute', 'parseCrmComparisonLink', 'crmLinkFilterLabel', 'crmLinkFilterColumns',
  'formatCrmLinkFilterValue', 'crmLinkFilterFields', 'updateCrmLinkFilters',
]) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing ${name}`);
  vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
}
const { parseCrmComparisonLink: parse, crmLinkFilterColumns: columns, formatCrmLinkFilterValue: value } = context;
const update = (link, changes) => context.updateCrmLinkFilters(link, new Map(Object.entries(changes)));

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

test('sets an unset boolean criterion while preserving the report and unrelated filters', () => {
  const original = 'https://crm.example/report/122?addressValidated=false&salesNotes=needs%20review&blank=';
  const sample = parse('https://crm.example/report/967?blank=false');
  const [field] = context.crmLinkFilterFields('blank', [parse(original), sample]);
  assert.equal(field.boolean, true);
  const updated = update(original, { blank: ['false'] });
  assert.equal(parse(updated).report, '122');
  assert.equal(value(parse(updated), 'blank'), 'No');
  assert.equal(value(parse(updated), 'addressValidated'), 'No');
  assert.equal(value(parse(updated), 'salesNotes'), 'needs review');
  assert.equal(update(updated, { blank: ['false'] }), updated);
});

test('updates routed and direct criteria without moving unrelated parameters', () => {
  const original = 'https://crm.example/app?locale=en#!/report/967?shippingCharges[low]=1&shippingCharges[high]=50';
  const fields = context.crmLinkFilterFields('shippingCharges', [parse(original)]);
  assert.deepEqual(Array.from(fields, ({ name }) => name), ['shippingCharges[low]', 'shippingCharges[high]']);
  const updated = update(original, { 'shippingCharges[low]': ['5'], blank: ['false'] });
  assert.equal(value(parse(updated), 'shippingCharges'), '5 – 50');
  assert.equal(new URL(updated).search, '?locale=en');
  assert.match(new URL(updated).hash, /[?&]blank=false/);
  const cleared = update(updated, { 'shippingCharges[high]': [] });
  assert.equal(value(parse(cleared), 'shippingCharges'), '≥ 5');
  assert.equal(value(parse(cleared), 'blank'), 'No');
  assert.equal(update('https://crm.example/login', { blank: ['false'] }), 'https://crm.example/login');
  assert.equal(update('', { blank: ['false'] }), '');
});

test('updates repeated filter values and retains their existing empty placeholder', () => {
  const original = 'https://crm.example/report/967?vendorName[]=&vendorName[]=A&vendorName[]=B';
  const [field] = context.crmLinkFilterFields('vendorName', [parse(original)]);
  assert.equal(field.multiple, true);
  const updated = update(original, { 'vendorName[]': ['C', 'D'] });
  assert.deepEqual(new URL(updated).searchParams.getAll('vendorName[]'), ['', 'C', 'D']);
  assert.equal(value(parse(updated), 'vendorName'), 'C, D');
});
