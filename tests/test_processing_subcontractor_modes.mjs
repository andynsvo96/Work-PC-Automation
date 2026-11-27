import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const names = [
  'normalizeCrmProcessingFilter', 'isCrmRushLikeProcessingFilter',
  'canCrmProcessingFilterRunUnlocker', 'canCrmProcessingFilterRunOrderGoods',
  'canCrmProcessingFilterRunOrderSupport', 'normalizeCrmProcessingStepList',
  'crmProcessingAvailableStepsForFilter', 'updateCrmProcessingStockToolStyles',
  'renderCrmProcessingSelectionButtons',
  'formatCrmProcessingStepLabel', 'formatCrmProcessingStepShortLabel', 'formatCrmProcessingStepLabelForState',
  'queueCrmProcessingSelectionUpdate', 'toggleCrmProcessingSelection',
  'toggleCrmProcessingBulkSelection', 'crmProcessingBulkPayload',
  'startCrmProcessingRun', 'startCrmPushBackRun',
];
const functions = names.map((name) => {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing ${name}`);
  const declaration = source.slice(start - 6, start) === 'async ' ? start - 6 : start;
  return source.slice(declaration, source.indexOf('\n}', start) + 2);
}).join('\n');
const constants = [
  source.match(/^const CRM_PROCESSING_FILTER_SEQUENCE = .*;/m)[0],
  source.match(/^const CRM_PROCESSING_RUSH_TOOL_BUTTON_IDS = \{[\s\S]*?^\};/m)[0],
  source.match(/^const CRM_PROCESSING_STEP_LABELS = \{[\s\S]*?^\};/m)[0],
  source.match(/^const CRM_LINK_SETTING_GROUPS = \[[\s\S]*?^\];/m)[0],
].join('\n');
const steps = ['stock_unlocker', 'order_goods', 'shipping_bypasser', 'push_back'];
const tools = ['Unlock', 'OrderGoods', 'ShippingBypasser', 'PushBack'];
const otherTools = ['Address', 'Separator', 'AutoSplitter'];

function dashboard() {
  const requests = [];
  const selections = [];
  const errors = [];
  const confirmations = [];
  const elements = Object.fromEntries([...tools, ...otherTools].map((name) => {
    const classes = new Set();
    return [`crmProcessing${name}ToggleBtn`, {
      disabled: false,
      setAttribute(key, value) { this[key] = value; },
      removeAttribute(key) { delete this[key]; },
      classList: {
        contains: (key) => classes.has(key),
        remove: (...keys) => keys.forEach((key) => classes.delete(key)),
        toggle(key, on) { on ? classes.add(key) : classes.delete(key); },
      },
    }];
  }));
  const context = vm.createContext({
    document: { getElementById: (id) => elements[id] || null },
    window: { confirm(message) { confirmations.push(message); return true; } },
    formatCrmAddressFilterLabel: (mode) => mode.toUpperCase(),
    crmProcessingStatusPayload: { state: {} },
    crmProcessingSelectionPromise: null,
    withButtonBusy: (_button, _label, task) => task(),
    buildCrmProcessingAdvancedPayload: () => ({}),
    getCrmStockAutomationOrderTarget: () => '',
    setStatus() {}, setAutomationButtonRunning() {}, renderCrmPushBackStatus() {},
    setErrorStatus(message) { errors.push(message); },
    async persistCrmProcessingSelection(payload) {
      selections.push(JSON.parse(JSON.stringify(payload)));
      Object.assign(context.crmProcessingStatusPayload.state, payload);
      context.renderCrmProcessingSelectionButtons(context.crmProcessingStatusPayload.state);
    },
    async api(path, options) { requests.push({ path, payload: JSON.parse(options.body) }); return {}; },
    async loadCrmPushBackStatus() {}, async loadCrmStatus() {},
    async loadCrmProcessingStatus() {}, async loadQueue() {},
    async loadCrmProductSeparatorStatus() {}, async loadCrmOrderGoodsStatus() {},
    async loadCrmShippingBypasserStatus() {}, async loadCrmMassEmailerStatus() {},
    async loadCrmAutoSplitterStatus() {}, async loadCrmAddressStatus() {},
    renderCrmProcessingStatus() {},
  });
  vm.runInContext(`${constants}\n${functions}`, context);
  return { context, elements, requests, selections, errors, confirmations };
}

test('each saved list mode has a settings link for every supported Run tool', () => {
  const { context } = dashboard();
  const groups = vm.runInContext('CRM_LINK_SETTING_GROUPS', context);
  for (const [title, filter] of Object.entries({ Free: 'free', Rush: 'rush', 'High Value': 'high_value', All: 'all', 813: '813' })) {
    const group = groups.find((group) => group.title === title);
    assert.equal(group.keys.length, context.crmProcessingAvailableStepsForFilter(filter).length, title);
  }
});

test('ALL confirmation names the selected Bypasser and Push Back tools', async () => {
  const { context, confirmations, requests } = dashboard();
  Object.assign(context.crmProcessingStatusPayload.state, {
    processing_filter: 'all', product_separator_enabled: false, auto_splitter_enabled: false,
    shipping_bypasser_enabled: true, push_back_enabled: true,
  });
  await context.startCrmProcessingRun();
  assert.deepEqual(confirmations, ['ALL mode will run Bypasser, Push Back against their configured lists. Continue?']);
  assert.equal(requests[0].payload.shipping_bypasser_enabled, true);
  assert.equal(requests[0].payload.push_back_enabled, true);
});

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

test('each Rush tool cycles regular, contracted, and off even with rapid clicks', async () => {
  const { context, elements, selections } = dashboard();
  const state = context.crmProcessingStatusPayload.state;
  state.processing_filter = 'rush';
  for (const [index, step] of steps.entries()) {
    const button = elements[`crmProcessing${tools[index]}ToggleBtn`];
    await context.toggleCrmProcessingSelection(step);
    assert.equal(button.classList.contains('active'), true);
    assert.equal(button.classList.contains('rush-contracted-mode'), false);
    assert.match(button['aria-label'], /Regular Orders/);
    await context.toggleCrmProcessingSelection(step);
    assert.equal(button.classList.contains('active'), true);
    assert.equal(button.classList.contains('rush-contracted-mode'), true);
    assert.match(button['aria-label'], /Contracted Orders/);
    await context.toggleCrmProcessingSelection(step);
    assert.equal(button.classList.contains('active'), false);
    assert.equal(button.classList.contains('rush-contracted-mode'), false);
    assert.match(button['aria-label'], /Off/);
    await Promise.all([1, 2, 3].map(() => context.toggleCrmProcessingSelection(step)));
    assert.deepEqual(selections.slice(-3).map((payload) => [payload[`${step}_enabled`], payload.rush_contracted_steps]), [
      [true, []], [true, [step]], [false, []],
    ]);
  }
});

test('Rush contracted tools are independent and bulk enable leaves their report choices intact', async () => {
  const { context, elements } = dashboard();
  const state = context.crmProcessingStatusPayload.state;
  Object.assign(state, { processing_filter: 'rush', stock_unlocker_enabled: true, push_back_enabled: true });
  await Promise.all([context.toggleCrmProcessingSelection('stock_unlocker'), context.toggleCrmProcessingSelection('push_back')]);
  await context.toggleCrmProcessingBulkSelection();
  assert.deepEqual(Array.from(state.rush_contracted_steps), ['stock_unlocker', 'push_back']);
  assert.equal(state.order_goods_enabled, true);
  assert.equal(elements.crmProcessingOrderGoodsToggleBtn.classList.contains('rush-contracted-mode'), false);
  context.renderCrmProcessingSelectionButtons({ ...state, processing_filter: 'high_value' });
  assert.equal(elements.crmProcessingPushBackToggleBtn.classList.contains('rush-contracted-mode'), false);
});

test('Add to Queue uses the newly saved report choice and stops if that save fails', async () => {
  const { context, requests, errors } = dashboard();
  const state = context.crmProcessingStatusPayload.state;
  Object.assign(state, { processing_filter: 'rush', push_back_enabled: true });
  await Promise.all([context.toggleCrmProcessingSelection('push_back'), context.startCrmProcessingRun()]);
  assert.equal(requests[0].path, '/crm/process');
  assert.deepEqual(requests[0].payload.rush_contracted_steps, ['push_back']);
  assert.equal(requests[0].payload.push_back_enabled, true);

  context.persistCrmProcessingSelection = async () => { throw new Error('Save failed'); };
  await Promise.all([context.toggleCrmProcessingSelection('push_back'), context.startCrmProcessingRun()]);
  assert.equal(requests.length, 1);
  assert.match(errors[0], /Save failed/);
});

test('standalone Push Back keeps the selected FREE/ALL mode instead of sending Rush', async () => {
  const { context, requests } = dashboard();
  for (const mode of ['free', 'all']) {
    context.crmProcessingStatusPayload.state.processing_filter = mode;
    await context.startCrmPushBackRun(true);
    assert.deepEqual(requests.at(-1), { path: '/crm/push-back/dry-run', payload: { processing_filter: mode } });
  }
});
