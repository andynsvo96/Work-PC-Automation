import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const context = vm.createContext({ URL, URLSearchParams });
for (const name of ['parseCrmComparisonLink', 'crmLinkFilterLabel', 'crmLinkDifferences']) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing ${name}`);
  vm.runInContext(source.slice(start, source.indexOf('\n}', start) + 2), context);
}
const { parseCrmComparisonLink: parse, crmLinkDifferences: differences } = context;
const compare = (base, link) => Array.from(differences(parse(base), parse(link)), (change) => Array.from(change));

test('compares only changed report IDs and populated filters', () => {
  const rush = 'https://crm.example/report/967?shippingCharges[low]=1&salesNotes=too+expensive&stockOrdered=false';
  assert.deepEqual(compare(rush, 'https://crm.example/report/967?shippingCharges[high]=1&salesNotes=too%20expensive&stockOrdered=false'), [
    ['Shipping charges (maximum)', 'Any', '1'],
    ['Shipping charges (minimum)', '1', 'Any'],
  ]);
  assert.deepEqual(compare(rush, 'https://crm.example/report/966?stockOrdered=false&salesNotes=too+expensive'), [
    ['Destination', 'Report 967', 'Report 966'],
    ['Shipping charges (minimum)', '1', 'Any'],
  ]);
});

test('handles embedded reports, repeated choices and newly added filter names', () => {
  const first = 'https://crm.example/report/967?vendorName[]=B&vendorName[]=A&empty=';
  const second = 'https://crm.example/app#!/report/967?vendorName%5B%5D=A&vendorName%5B%5D=B&vendorName%5B%5D=';
  assert.deepEqual(compare(first, second), []);
  assert.deepEqual(compare(first, `${second}&newOrderStatus[low]=ready`), [
    ['New order status (minimum)', 'Any', 'ready'],
  ]);
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
