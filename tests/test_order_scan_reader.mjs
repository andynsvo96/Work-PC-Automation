import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const folder = new URL('../crm-order-dark-mode-extension/', import.meta.url);
const source = await readFile(new URL('order-scan-reader.js', folder), 'utf8');
const copyright = await readFile(new URL('copyright-design-reader.js', folder), 'utf8');
const embroidery = await readFile(new URL('emb-design-reader.js', folder), 'utf8');
const bridge = await readFile(new URL('order-scan-bridge.js', folder), 'utf8');
const content = await readFile(new URL('content.js', folder), 'utf8');

function fixture() {
  const item = (id, size, count) => ({ id, style: 'ST350', ourLabel: 'Sport-Tek Tee', color: 'Black',
    splitIntoSizes: 1, quantity: count, sizes: [{ sizeCode: 'S', quantity: 0 }, { sizeCode: size, quantity: count }] });
  const resource = { id: '1234567', designs: [
    { id: 901, designId: 101, name: 'Shared artwork', designItems: [item(11, 'L', 1)],
      printAreas: [{ description: 'Back', printMethodDescription: 'HD Digital' }] },
    { id: 902, designId: 101, name: 'Shared artwork', designItems: [item(12, 'XL', 2)],
      printAreas: [{ description: 'Front-left Chest', printMethodDescription: 'Embroidery' }] },
  ] };
  let reads = 0;
  const scope = { copyOrder() {}, order: { getResource() { reads++; return resource; } } };
  const rows = [{ innerText: 'Design 1 Total: 1 pieces $12.00' }, { innerText: 'Design 2 Total: 2 pieces $24.00' }];
  const listeners = new Map();
  const responses = [];
  const location = { pathname: '/order/1234567', hash: '' };
  const context = vm.createContext({ location, setTimeout, clearTimeout, document: {
    querySelectorAll(selector) { return selector === 'tr' ? rows : [{}]; },
    addEventListener(name, listener) { listeners.set(name, listener); },
    removeEventListener(name, listener) { if (listeners.get(name) === listener) listeners.delete(name); },
    dispatchEvent(event) { responses.push(event); listeners.get(event.type)?.(event); },
  }, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } } });
  context.window = { angular: { element: () => ({ scope: () => scope }) } };
  vm.runInContext(source + copyright + embroidery + bridge, context);
  const read = expression => JSON.parse(JSON.stringify(vm.runInContext(expression, context)));
  return { context, resource, scope, rows, location, listeners, responses, read, reads: () => reads };
}

function installPicker(f) {
  vm.runInContext(content.slice(content.indexOf('function stockIssueCleanText'), content.indexOf('function visibleStockIssueElement'))
    + content.slice(content.indexOf('function readStockIssueProductSnapshot'), content.indexOf('const EXTRA_PRINT_AREAS'))
    + content.slice(content.indexOf('function deduplicateStockIssueProducts'), content.indexOf('function createStockIssueDialogShell'))
    + `
      function visibleStockIssueDesignTabs() { return [{tabNumber: 1, quantity: 1}, {tabNumber: 2, quantity: 2}]; }
      function clickStockIssueDesignTab() { throw new Error('Unexpected tab click'); }
      function stockIssueDelay() { throw new Error('Unexpected fixed wait'); }
    `, f.context);
}

test('loading the reader and bridge does no order reads or network work', () => {
  const f = fixture();
  assert.equal(f.reads(), 0);
  assert.deepEqual([...f.listeners.keys()], ['crm-stock-products-request']);
  f.location.pathname = '/report/stock';
  f.listeners.get('crm-stock-products-request')({ detail: 'request-1' });
  assert.equal(f.reads(), 0);
  assert.equal(f.responses.length, 0);
});

test('stock reads every active design and only ordered sizes, without merging identities', () => {
  const f = fixture();
  const result = f.read('CRMOrderScanReader.stockProducts()');
  assert.deepEqual(result.tabs, [{ tab_number: 1, quantity: 1 }, { tab_number: 2, quantity: 2 }]);
  assert.deepEqual(result.products.map(row => [row.design_item_id, row.available_sizes, row.total_quantity]),
    [['design-item-11', ['L'], 1], ['design-item-12', ['XL'], 2]]);
  f.resource.designs[0].designItems[0].sizes[1].quantity = 3;
  assert.equal(f.read('CRMOrderScanReader.stockProducts()').products[0].total_quantity, 3);
});

test('stock picker uses its actual read-only bridge and combines products without tab clicks or waits', async () => {
  const f = fixture();
  installPicker(f);
  const result = JSON.parse(JSON.stringify(await vm.runInContext('scanAllStockIssueProducts()', f.context)));
  assert.equal(result.length, 1);
  assert.deepEqual(result[0].design_item_ids, ['design-item-11', 'design-item-12']);
  assert.deepEqual(result[0].available_sizes, ['L', 'XL']);
  assert.deepEqual(result[0].tab_numbers, [1, 2]);
  assert.equal(result[0].total_quantity, 3);
  assert.equal(f.reads(), 2);
});

test('a stock change between reads stops the picker before it can return a stale selection', async () => {
  const f = fixture();
  installPicker(f);
  const getResource = f.scope.order.getResource;
  let reads = 0;
  f.scope.order.getResource = () => {
    if (++reads === 2) f.resource.designs[0].designItems[0].color = 'Navy';
    return getResource();
  };
  await assert.rejects(vm.runInContext('scanAllStockIssueProducts()', f.context), /products changed during the scan/);
});

test('deleted tabs/items and zero quantities are excluded; tab numbers retain their native positions', () => {
  const f = fixture();
  f.resource.designs[0].crudAction = 'd';
  f.resource.designs[1].designItems.push({ crudAction: 'd' });
  assert.deepEqual(f.read('CRMOrderScanReader.stockProducts()').tabs, [{ tab_number: 2, quantity: 2 }]);
  f.resource.designs[1].designItems[0].sizes[1].quantity = 0;
  f.resource.designs[1].designItems[0].quantity = 0;
  assert.deepEqual(f.read('CRMOrderScanReader.stockProducts()').products, []);
});

test('positive stock with unreadable size allocation cannot disappear from the selection', () => {
  const f = fixture();
  f.resource.designs[1].designItems[0].sizes[1].quantity = 0;
  assert.equal(f.read('CRMOrderScanReader.stockProducts()'), null);
  f.resource.designs[1].designItems[0].splitIntoSizes = 0;
  const row = f.read('CRMOrderScanReader.stockProducts()').products[1];
  assert.equal(row.total_quantity, 2);
  assert.deepEqual(row.available_sizes, []);
});

test('missing fields request the complete legacy scan instead of returning a partial list', () => {
  for (const field of ['style', 'ourLabel', 'color', 'id', 'sizes']) {
    const f = fixture();
    delete f.resource.designs[1].designItems[0][field];
    assert.equal(f.read('CRMOrderScanReader.stockProducts()'), null, field);
  }
  const f = fixture();
  delete f.resource.designs[1].designItems;
  assert.equal(f.read('CRMOrderScanReader.stockProducts()'), null);
});

test('stock retains Style Sub fields and blocks ambiguous substitutions', () => {
  const f = fixture();
  const item = f.resource.designs[0].designItems[0];
  item.isStyleSub = 1;
  item.styleSubs = [{ style: 'Custom', description: 'Custom Tee', color: 'Navy' }];
  const row = f.read('CRMOrderScanReader.stockProducts()').products[0];
  assert.deepEqual([row.style, row.description, row.color], ['Custom', 'Custom Tee', 'Navy']);
  item.styleSubs.push({ style: 'Other' });
  assert.equal(f.read('CRMOrderScanReader.stockProducts()'), null);
});

test('duplicate stock IDs, wrong orders, unsaved edits, and active saves stop the scan', () => {
  const f = fixture();
  f.resource.designs[1].designItems[0].id = 11;
  assert.throws(() => f.read('CRMOrderScanReader.stockProducts()'), /duplicate product IDs/);
  f.resource.id = '7654321';
  assert.throws(() => f.read('CRMOrderScanReader.context()'), /verify the loaded order/);
  f.resource.id = '1234567';
  for (const flag of ['editMode', 'saving', 'savingOrder']) {
    f.scope[flag] = true;
    assert.throws(() => f.read('CRMOrderScanReader.context()'), /Save or leave Edit Order/);
    f.scope[flag] = false;
  }
});

test('copyright reads names/totals while keeping tabs that share artwork individually identifiable', () => {
  const f = fixture();
  const rows = f.read('readCopyrightDesignIds()').loaded_designs;
  assert.deepEqual(rows.map(row => [row.order_design_id, row.design_id, row.design_name, row.quantity, row.subtotal]),
    [['901', '101', 'Shared artwork', 1, '12.00'], ['902', '101', 'Shared artwork', 2, '24.00']]);
  f.resource.designs[0].name = 'Updated';
  assert.equal(f.read('readCopyrightDesignIds()').loaded_designs[0].design_name, 'Updated');
  delete f.resource.designs[0].name;
  assert.equal(f.read('readCopyrightDesignIds()').loaded_designs, null);
});

test('native computed totals are checked against rendered copies, including zeroed purchased stock', () => {
  const f = fixture();
  f.scope.order.getDesignSums = design => ({ quantity: design.id === 901 ? 1 : 2, pricePerPiece: design.id === 901 ? 12 : 24 });
  assert.equal(f.read('readCopyrightDesignIds()').loaded_designs.length, 2);
  f.rows.push({ innerText: 'Design 1 Total: 1 pieces $13.00' });
  assert.throws(() => f.read('readCopyrightDesignIds()'), /Conflicting quantity or total/);
  f.rows.pop();
  f.rows[0].innerText = 'Design 1 Total: 0 pieces $0.00';
  assert.throws(() => f.read('readCopyrightDesignIds()'), /still updating/);
  f.scope.order.getDesignSums = () => ({ quantity: 0, pricePerPiece: 0 });
  f.rows[1].innerText = 'Design 2 Total: 0 pieces $0.00';
  assert.deepEqual(f.read('readCopyrightDesignIds()').loaded_designs.map(row => [row.quantity, row.subtotal]), [[0, '0.00'], [0, '0.00']]);
});

test('embroidery reads names and native print areas together, including ink conversions', () => {
  const f = fixture();
  const rows = f.read('readEmbDesignMethods()');
  assert.deepEqual(rows.map(row => [row.design_name, row.eligible, row.print_areas[0].description]),
    [['Shared artwork', true, 'Back'], ['Shared artwork', true, 'Front-left Chest']]);
});

test('stock bridge handles embedded orders on demand and returns only the stock scan', () => {
  const f = fixture();
  f.location.pathname = '/app';
  f.location.hash = '#/order/1234567';
  f.listeners.get('crm-stock-products-request')({ detail: 'request-2' });
  const response = JSON.parse(f.responses[0].detail);
  assert.equal(response.id, 'request-2');
  assert.equal(response.scan.order_id, '1234567');
  assert.equal(response.scan.products.length, 2);
  assert.deepEqual(Object.keys(response.scan).sort(), ['order_id', 'products', 'tabs']);
});
