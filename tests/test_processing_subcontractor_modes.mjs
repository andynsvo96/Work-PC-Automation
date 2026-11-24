import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const names = [
  'normalizeCrmProcessingFilter', 'isCrmRushLikeProcessingFilter',
  'canCrmProcessingFilterRunUnlocker', 'canCrmProcessingFilterRunOrderGoods',
  'canCrmProcessingFilterRunOrderSupport', 'normalizeCrmProcessingStepList',
  'crmProcessingAvailableStepsForFilter', 'updateCrmProcessingSubcontractorToolStyles',
  'renderCrmProcessingSelectionButtons',
  'startCrmPushBackRun',
];
const functions = names.map((name) => {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing ${name}`);
  const declaration = source.slice(start - 6, start) === 'async ' ? start - 6 : start;
  return source.slice(declaration, source.indexOf('\n}', start) + 2);
}).join('\n');
const constants = source.match(/^const CRM_PROCESSING_FILTER_SEQUENCE = .*;/m)[0];
const tools = ['Unlock', 'OrderGoods', 'ShippingBypasser', 'PushBack'];
const otherTools = ['Address', 'Separator', 'AutoSplitter'];

function dashboard() {
  const requests = [];
  const elements = Object.fromEntries([...tools, ...otherTools].map((name) => {
    const classes = new Set();
    return [`crmProcessing${name}ToggleBtn`, {
      disabled: false,
      classList: {
        contains: (key) => classes.has(key),
        remove: (...keys) => keys.forEach((key) => classes.delete(key)),
        toggle(key, on) { on ? classes.add(key) : classes.delete(key); },
      },
    }];
  }));
  const context = vm.createContext({
    document: { getElementById: (id) => elements[id] || null },
    formatCrmAddressFilterLabel: (mode) => mode.toUpperCase(),
    crmProcessingStatusPayload: { state: {} },
    getCrmStockAutomationOrderTarget: () => '',
    setStatus() {}, setAutomationButtonRunning() {}, renderCrmPushBackStatus() {},
    async api(path, options) { requests.push({ path, payload: JSON.parse(options.body) }); return {}; },
    async loadCrmPushBackStatus() {}, async loadCrmStatus() {},
    async loadCrmProcessingStatus() {}, async loadQueue() {},
  });
  vm.runInContext(`${constants}\n${functions}`, context);
  return { context, elements, requests };
}

test('FREE and ALL render all four enabled stock tools with the subcontractor style', () => {
  const { context, elements } = dashboard();
  for (const processing_filter of ['free', 'all']) {
    context.renderCrmProcessingSelectionButtons({
      processing_filter, stock_unlocker_enabled: true, order_goods_enabled: true,
      shipping_bypasser_enabled: true, push_back_enabled: true,
    });
    const available = Array.from(context.crmProcessingAvailableStepsForFilter(processing_filter));
    for (const step of ['stock_unlocker', 'order_goods', 'shipping_bypasser', 'push_back']) {
      assert.ok(available.includes(step), `${processing_filter}: ${step}`);
    }
    for (const name of tools) {
      const button = elements[`crmProcessing${name}ToggleBtn`];
      assert.equal(button.disabled, false, name);
      assert.equal(button.classList.contains('active'), true, name);
      assert.equal(button.classList.contains('subcontractor-mode'), true, name);
    }
    for (const name of otherTools) {
      assert.equal(elements[`crmProcessing${name}ToggleBtn`].classList.contains('subcontractor-mode'), false);
    }
  }
});

test('switching away restores ordinary styles and inactive selections stay inactive', () => {
  const { context, elements } = dashboard();
  context.renderCrmProcessingSelectionButtons({ processing_filter: 'free', push_back_enabled: false });
  assert.equal(elements.crmProcessingPushBackToggleBtn.classList.contains('active'), false);
  for (const mode of ['rush', 'high_value', '813', 'custom']) {
    context.renderCrmProcessingSelectionButtons({ processing_filter: mode, shipping_bypasser_enabled: true, push_back_enabled: true });
    for (const name of tools) {
      assert.equal(elements[`crmProcessing${name}ToggleBtn`].classList.contains('subcontractor-mode'), false, mode);
    }
    assert.equal(elements.crmProcessingShippingBypasserToggleBtn.disabled, false, mode);
    assert.equal(elements.crmProcessingPushBackToggleBtn.disabled, false, mode);
  }
  assert.equal(context.crmProcessingAvailableStepsForFilter('813').includes('stock_unlocker'), false);
});

test('purple rules cover selected buttons in light and dark themes and their hover state', () => {
  assert.match(source, /button\.toggle-btn\.processing-tool-toggle\.subcontractor-mode\.active,\s*:root\[data-theme='dark'\] button\.toggle-btn\.processing-tool-toggle\.subcontractor-mode\.active\{background:#7c3aed/);
  assert.match(source, /subcontractor-mode\.active:hover\{background:#6d28d9/);
});

test('standalone Push Back keeps the selected FREE/ALL mode instead of sending Rush', async () => {
  const { context, requests } = dashboard();
  for (const mode of ['free', 'all']) {
    context.crmProcessingStatusPayload.state.processing_filter = mode;
    await context.startCrmPushBackRun(true);
    assert.deepEqual(requests.at(-1), { path: '/crm/push-back/dry-run', payload: { processing_filter: mode } });
  }
});
