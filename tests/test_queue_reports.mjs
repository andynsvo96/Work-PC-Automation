import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const names = [
  'esc', 'fmtDateTime', 'fmtDuration', 'crmOrderUrl', 'crmOrderLinkHtml', 'crmOrderTextHtml',
  'normalizeCrmProcessingFilter', 'isCrmRushLikeProcessingFilter',
  'canCrmProcessingFilterRunUnlocker', 'canCrmProcessingFilterRunOrderGoods',
  'canCrmProcessingFilterRunOrderSupport', 'normalizeCrmProcessingStepList',
  'formatCrmProcessingStepLabel', 'formatCrmProcessingStepLabelForState',
  'normalizeCrmAddressFilter', 'formatCrmAddressFilterLabel',
  'crmProcessingRunForReport', 'crmProcessingRunTotals', 'crmProcessingRunId',
  'openCrmProcessingRunReport', 'openQueueTaskReport', 'queueProcessingFilterFromTask',
  'sortedQueueTasks', 'queueStatusLabel', 'queueTaskProgressLabel', 'renderQueue',
];
const functions = names.map((name) => {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing dashboard function ${name}`);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}).join('\n');
const constants = [
  source.match(/const CRM_PROCESSING_STEP_LABELS = \{[\s\S]*?\n\};/)[0],
  source.match(/^const CRM_PROCESSING_FILTER_SEQUENCE = .*;/m)[0],
].join('\n');

function dashboard(tasks, state = {}) {
  const elements = Object.fromEntries([
    'queueList', 'crmProcessingErrorsDialog', 'crmProcessingErrorsDialogTitle',
    'crmProcessingErrorsDialogSummary', 'crmProcessingErrorsDialogBody',
    'queueTaskReportDialog', 'queueTaskReportDialogTitle',
    'queueTaskReportDialogSummary', 'queueTaskReportDialogBody',
  ].map((id) => [id, {
    innerHTML: '', textContent: '', dataset: {}, open: false,
    showModal() { this.open = true; }, close() { this.open = false; },
  }]));
  const context = vm.createContext({
    document: { getElementById: (id) => elements[id] || null },
    queuePayload: { tasks }, queueVisible: true, queueSortOrder: 'latest', queueExpandedTaskId: null,
    crmProcessingStatusPayload: { state },
    applyPlatformCapabilities() {}, queueTimeText: () => '',
    queueRunHistoryHtml: () => '', currentCrmProcessingOrderProgressLabel: () => '',
    stripDuplicateCrmProcessingProgress: (message) => message,
    conciseAutomationFailureMessage: (message) => message,
  });
  vm.runInContext(`${constants}\n${functions}`, context);
  return { context, elements };
}

function mainTask(overrides = {}) {
  return {
    id: 'main-run', task_type: 'crm.processing', label: 'Automate Processing - Rush',
    status: 'failed', success: false, completed_at: '2026-11-20T15:17:27', duration_seconds: 743,
    message: 'Short queue summary.',
    result_context: { report: {
      processing_filter: 'rush', success: false,
      message: 'Processing completed with partial success. Needs attention: Auto Splitter, Order Goods.',
      selected_steps: ['address_validator_batch', 'product_separator', 'auto_splitter',
        'stock_unlocker', 'order_goods', 'shipping_bypasser', 'push_back'],
      step_results: [
        { key: 'address_validator_batch', success: true, order_count: 4, error_count: 0,
          duration_seconds: 107, message: 'Processed 4 orders. 4 succeeded.' },
        { key: 'product_separator', success: true, order_count: 1, error_count: 0,
          duration_seconds: 134, message: 'Product Separator completed 1/1 orders.' },
        { key: 'auto_splitter', success: false, order_count: 1, error_count: 1,
          duration_seconds: 83, message: 'Auto Splitter completed 0/1 orders; 1 needs attention.',
          errors: [{ order_id: '5000003', message: 'Original payment transaction ID could not be read from the payment view popup.' }] },
        { key: 'stock_unlocker', success: true, order_count: 22, error_count: 0,
          duration_seconds: 59, message: 'Unlocked 22 orders successfully.' },
        { key: 'order_goods', success: false, order_count: 22, error_count: 3,
          duration_seconds: 159, message: 'Order Goods: 19 succeeded; 3 need attention.',
          errors: ['5000004', '5000005', '5000006'].map((order_id) => ({ order_id, message: 'Saved stock could not be verified.' })) },
        { key: 'shipping_bypasser', success: true, order_count: 1, error_count: 0,
          duration_seconds: 145, message: 'Shipping Bypasser completed.' },
        { key: 'push_back', success: true, order_count: 1, error_count: 0,
          duration_seconds: 56, message: 'Push Back completed.' },
      ],
    } },
    ...overrides,
  };
}

test('finished main runs offer View Report, including successes and idle repeats', () => {
  for (const status of ['completed', 'failed', 'canceled', 'idle']) {
    const task = mainTask({ status });
    const { context, elements } = dashboard([task]);
    context.renderQueue({ tasks: [task] });
    assert.match(elements.queueList.innerHTML, /openQueueTaskReport\('main-run'\).*?>View Report<\/button>/);
    assert.doesNotMatch(elements.queueList.innerHTML, /View Errors|View Retry Report/);
  }
  for (const status of ['queued', 'running', 'idle']) {
    const task = mainTask({ status, result_context: {} });
    const { context, elements } = dashboard([task]);
    context.renderQueue({ tasks: [task] });
    assert.doesNotMatch(elements.queueList.innerHTML, />View Report<\/button>/);
  }
});

test('a queue report shows its saved full run even when another run is latest or running', () => {
  const task = mainTask();
  const latest = { timestamp: '2026-11-20T16:00:00', message: 'Different run.', selected_steps: ['push_back'] };
  const { context, elements } = dashboard([task, mainTask({ id: 'newer-run' })], { run_history: [latest] });
  context.crmProcessingStatusPayload.running = true;
  context.crmProcessingStatusPayload.runtime = { lastMessage: 'Another automation is running.' };
  const before = structuredClone(task);
  context.openQueueTaskReport(task.id);
  assert.equal(elements.crmProcessingErrorsDialog.open, true);
  assert.equal(elements.queueTaskReportDialog.open, false);
  assert.equal(elements.crmProcessingErrorsDialogTitle.textContent, 'Main Automation Run Report • RUN-1120-1517');
  assert.match(elements.crmProcessingErrorsDialogSummary.textContent, /Rush Orders • Needs attention/);
  const html = elements.crmProcessingErrorsDialogBody.innerHTML;
  for (const metric of ["Tools</div><b>7", "Orders</div><b>52", "Errors</div><b class='bad'>4", '12m 23s']) {
    assert.ok(html.includes(metric), metric);
  }
  assert.equal((html.match(/<section /g) || []).length, 7);
  assert.match(html, /Address Validator \(Batch\)/);
  assert.match(html, /1m 47s/);
  assert.match(html, /Original payment transaction ID could not be read/);
  assert.match(html, /href="https:\/\/crm2.legacy.printfly.com\/order\/5000003"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /19 succeeded; 3 need attention/);
  assert.doesNotMatch(html, /Different run|Another automation|Short queue summary|No error message recorded/);
  assert.deepEqual(task, before, 'Opening a report must preserve saved retry data');
});

test('successful main runs show completed tools and zero errors', () => {
  const task = mainTask({ status: 'completed', success: true });
  task.result_context.report.success = true;
  task.result_context.report.selected_steps = ['stock_unlocker'];
  task.result_context.report.step_results = [task.result_context.report.step_results[3]];
  const { context, elements } = dashboard([task]);
  context.openQueueTaskReport(task.id);
  assert.match(elements.crmProcessingErrorsDialogSummary.textContent, /Completed$/);
  assert.match(elements.crmProcessingErrorsDialogBody.innerHTML, /Errors<\/div><b class=''>0<\/b>/);
  assert.match(elements.crmProcessingErrorsDialogBody.innerHTML, /✓ Completed/);
});

test('idle repeats use their finished run time and duration after task times are cleared', () => {
  const task = mainTask({ status: 'idle', queue_mode: 'repeat', started_at: null, completed_at: null,
    duration_seconds: null, run_history: [{ completed_at: '2026-11-20T15:17:27', duration_seconds: 743 }] });
  const { context, elements } = dashboard([task]);
  context.openQueueTaskReport(task.id);
  assert.match(elements.crmProcessingErrorsDialogTitle.textContent, /RUN-1120-1517$/);
  assert.match(elements.crmProcessingErrorsDialogBody.innerHTML, /12m 23s/);
});

test('sparse saved reports use recorded tools without borrowing current settings', () => {
  const task = mainTask();
  delete task.result_context.report.selected_steps;
  task.result_context.report.step_results = [task.result_context.report.step_results[3]];
  const { context, elements } = dashboard([task], { address_validator_enabled: true, order_goods_enabled: true });
  context.openQueueTaskReport(task.id);
  assert.match(elements.crmProcessingErrorsDialogBody.innerHTML, /Tools<\/div><b>1<\/b>/);
  assert.doesNotMatch(elements.crmProcessingErrorsDialogBody.innerHTML, /Address Validator|Order Goods<\/strong>/);
  task.result_context = {};
  context.openQueueTaskReport(task.id);
  assert.match(elements.crmProcessingErrorsDialogBody.innerHTML, /No tool results recorded/);
  assert.match(elements.crmProcessingErrorsDialogBody.innerHTML, /Short queue summary/);
});

test('main retry reports retain per-tool failure details and escape recorded messages', () => {
  const task = mainTask();
  task.result_context.retry_completed = true;
  task.result_context.report.step_results[2].errors[0].message = '<img src=x onerror=alert(1)> could not be read.';
  const { context, elements } = dashboard([task]);
  context.openQueueTaskReport(task.id);
  const html = elements.crmProcessingErrorsDialogBody.innerHTML;
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /Saved stock could not be verified/);
  assert.equal((html.match(/<section /g) || []).length, 7);
});

test('other failed tasks use View Report and keep their order details', () => {
  const task = { id: 'single', task_type: 'crm.shipping_bypasser', label: 'Shipping Bypasser', status: 'failed',
    result_context: { report: { order_details: [{ order_id: '5000001', success: false,
      function_label: 'Stock verification', message: 'Confirmed purchase needs a CRM note.' }] } } };
  const { context, elements } = dashboard([task]);
  context.renderQueue({ tasks: [task] });
  assert.match(elements.queueList.innerHTML, />View Report<\/button>/);
  context.openQueueTaskReport(task.id);
  assert.equal(elements.queueTaskReportDialogTitle.innerHTML, 'Shipping Bypasser Report');
  assert.match(elements.queueTaskReportDialogBody.innerHTML, /5000001/);
  assert.match(elements.queueTaskReportDialogBody.innerHTML, /Confirmed purchase needs a CRM note/);
});

test('latest and indexed Processing reports still select their original history rows', () => {
  const row = mainTask().result_context.report;
  const { context, elements } = dashboard([], { run_history: [
    { ...row, timestamp: '2026-11-20T16:00:00' },
    { ...row, timestamp: '2026-11-20T15:17:27' },
  ] });
  context.openCrmProcessingRunReport();
  assert.match(elements.crmProcessingErrorsDialogTitle.textContent, /RUN-1120-1600$/);
  context.openCrmProcessingRunReport(1);
  assert.match(elements.crmProcessingErrorsDialogTitle.textContent, /RUN-1120-1517$/);
});
