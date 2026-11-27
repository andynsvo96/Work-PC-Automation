import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const names = [
  'crmProcessingAdvancedMode', 'normalizeCrmProcessingRepeatMinutes', 'formatCrmProcessingLocalDateTime',
  'resolveCrmProcessingScheduledDateTime', 'syncScheduledTimeInput', 'syncCrmProcessingAdvancedControls',
  'buildCrmProcessingAdvancedPayload', 'communicationSchedulePayload', 'syncCommunicationScheduleControls',
  'renderM6PostingToggle', 'loadM6Posting', 'toggleM6Posting',
  'buildCrmMassEmailerPayload',
];
const functions = names.map(name => {
  let start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing function ${name}`);
  if (source.slice(start - 6, start) === 'async ') start -= 6;
  return source.slice(start, source.indexOf('\n}', start) + 2);
}).join('\n');
const globals = ['m6PostingEnabled', 'm6PostingSaving', 'm6PostingRevision']
  .map(name => source.match(new RegExp(`^let ${name} = .*;`, 'm'))[0]).join('\n');

function dashboard({ now = new Date(2026, 10, 16, 14, 0, 20), api } = {}) {
  const elements = Object.fromEntries([
    'crmProcessingRepeatToggle', 'crmProcessingScheduledToggle', 'crmProcessingRepeatMinutesInput',
    'crmProcessingScheduledTimeInput', 'crmProcessingAdvancedSummaryText', 'communicationScheduledToggle',
    'communicationScheduledTimeInput', 'communicationAdvancedSummary', 'm6PostingToggle',
  ].map(id => [id, { checked: false, value: '', disabled: false, textContent: '', attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    setCustomValidity(value) { this.validationMessage = value; },
  }]));
  elements.crmProcessingRepeatMinutesInput.value = '30';
  elements.crmProcessingScheduledTimeInput.value = '15:00';
  const messages = [];
  const context = vm.createContext({
    document: { getElementById: id => elements[id] },
    Date: class extends Date { constructor(...args) { super(...(args.length ? args : [now.getTime()])); } },
    fmtDateTime: value => value,
    getSavedCrmWorkerPreference: () => 4,
    setStatus: message => messages.push(message),
    setErrorStatus: message => messages.push(message),
    api: api || (async () => ({success: true, enabled: true})),
  });
  vm.runInContext(`${globals}\n${functions}`, context);
  return { context, elements, messages };
}

test('Sheets Scanner always includes earlier errors without the removed retry checkbox', () => {
  const {context} = dashboard();
  const payload = JSON.parse(JSON.stringify(context.buildCrmMassEmailerPayload()));
  assert.deepEqual(payload, {retry_errors: true, parallel_workers: 4});
});

test('repeat and scheduled remain enabled together, with both values in the request', () => {
  const {context, elements} = dashboard();
  elements.crmProcessingRepeatToggle.checked = true;
  elements.crmProcessingScheduledToggle.checked = true;
  for (const source of ['repeat', 'scheduled']) {
    context.syncCrmProcessingAdvancedControls(source);
    assert.equal(elements.crmProcessingRepeatToggle.checked, true);
    assert.equal(elements.crmProcessingScheduledToggle.checked, true);
    assert.equal(elements.crmProcessingRepeatMinutesInput.disabled, false);
    assert.equal(elements.crmProcessingScheduledTimeInput.disabled, false);
  }
  const payload = JSON.parse(JSON.stringify(context.buildCrmProcessingAdvancedPayload()));
  assert.deepEqual(payload, {advanced_mode: 'scheduled_repeat', repeat_interval_minutes: 30, scheduled_time: '2026-11-16T15:00:00'});
  assert.match(elements.crmProcessingAdvancedSummaryText.textContent, /Then repeat every 30 minutes/);
});

test('normal, repeat only, and scheduled only preserve their separate behavior', () => {
  const {context, elements} = dashboard();
  assert.equal(JSON.stringify(context.buildCrmProcessingAdvancedPayload()), '{}');
  elements.crmProcessingRepeatToggle.checked = true;
  elements.crmProcessingRepeatMinutesInput.value = '0';
  assert.equal(context.buildCrmProcessingAdvancedPayload().repeat_interval_minutes, 0);
  elements.crmProcessingScheduledToggle.checked = true;
  assert.equal(context.buildCrmProcessingAdvancedPayload().repeat_interval_minutes, 0);
  elements.crmProcessingRepeatToggle.checked = false;
  context.syncCrmProcessingAdvancedControls();
  assert.equal(elements.crmProcessingRepeatMinutesInput.disabled, true);
  assert.equal(context.buildCrmProcessingAdvancedPayload().advanced_mode, 'scheduled');
  assert.equal('repeat_interval_minutes' in context.buildCrmProcessingAdvancedPayload(), false);
});

test('past and current minute times are blocked without rolling into tomorrow', () => {
  const {context, elements} = dashboard();
  elements.crmProcessingScheduledToggle.checked = true;
  for (const value of ['13:59', '14:00', '25:00', 'bad']) {
    elements.crmProcessingScheduledTimeInput.value = value;
    context.syncCrmProcessingAdvancedControls();
    assert.equal(context.resolveCrmProcessingScheduledDateTime(value), null);
    assert.throws(() => context.buildCrmProcessingAdvancedPayload(), /later today/);
    assert.match(elements.crmProcessingScheduledTimeInput.validationMessage, /later today/);
  }
  assert.equal(elements.crmProcessingScheduledTimeInput.min, '14:01');
  assert.equal(elements.crmProcessingScheduledTimeInput.max, '23:59');
});

test('communications uses the same today-only scheduling restriction', () => {
  const {context, elements, messages} = dashboard();
  elements.communicationScheduledToggle.checked = true;
  elements.communicationScheduledTimeInput.value = '13:59';
  context.syncCommunicationScheduleControls();
  assert.equal(elements.communicationScheduledTimeInput.min, '14:01');
  assert.equal(context.communicationSchedulePayload(), null);
  assert.match(messages.at(-1), /later today/);
  elements.communicationScheduledTimeInput.value = '16:00';
  assert.equal(context.communicationSchedulePayload().scheduled_time, '2026-11-16T16:00:00');
});

test('scheduling is unavailable when no later minute remains today', () => {
  const {context, elements} = dashboard({now: new Date(2026, 10, 16, 23, 59, 30)});
  elements.crmProcessingScheduledToggle.checked = true;
  elements.crmProcessingScheduledTimeInput.value = '00:30';
  context.syncCrmProcessingAdvancedControls();
  assert.equal(elements.crmProcessingScheduledTimeInput.disabled, true);
  assert.equal(context.resolveCrmProcessingScheduledDateTime('00:30'), null);
  assert.throws(() => context.buildCrmProcessingAdvancedPayload(), /later today/);
});

test('M6 switch persists the new value with JSON and displays the saved state', async () => {
  let enabled = true;
  const calls = [];
  const {context, elements, messages} = dashboard({api: async (path, options = {}) => {
    calls.push({path, options});
    if (options.method === 'POST') {
      assert.equal(options.headers['Content-Type'], 'application/json');
      enabled = JSON.parse(options.body).enabled;
    }
    return {success: true, enabled};
  }});
  await context.loadM6Posting();
  assert.equal(elements.m6PostingToggle.attributes['aria-pressed'], 'true');
  await context.toggleM6Posting();
  assert.equal(enabled, false);
  assert.equal(elements.m6PostingToggle.attributes['aria-pressed'], 'false');
  assert.equal(elements.m6PostingToggle.textContent, 'Post to M6: Off');
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 1);
  assert.match(messages.at(-1), /Chrome extension/);
});

test('failed M6 save restores the actual setting and reports the error', async () => {
  const {context, elements, messages} = dashboard({api: async (_path, options = {}) => {
    if (options.method === 'POST') throw new Error('Save failed');
    return {success: true, enabled: true};
  }});
  await context.loadM6Posting();
  await context.toggleM6Posting();
  assert.equal(elements.m6PostingToggle.attributes['aria-pressed'], 'true');
  assert.equal(elements.m6PostingToggle.disabled, false);
  assert.match(messages.at(-1), /update failed: Save failed/);
});

test('an older M6 poll cannot replace a more recent saved state', async () => {
  let resolveOldPoll;
  let calls = 0;
  let enabled = true;
  const {context, elements} = dashboard({api: async (_path, options = {}) => {
    calls += 1;
    if (calls === 2) return new Promise(resolve => { resolveOldPoll = resolve; });
    if (options.method === 'POST') enabled = JSON.parse(options.body).enabled;
    return {success: true, enabled};
  }});
  await context.loadM6Posting();
  const oldPoll = context.loadM6Posting();
  await context.toggleM6Posting();
  resolveOldPoll({success: true, enabled: true});
  await oldPoll;
  assert.equal(elements.m6PostingToggle.attributes['aria-pressed'], 'false');
});

function feedbackPanel() {
  const ids = ['actionFeedback', 'actionFeedbackMessage', 'statusBox', 'processingStatusBox',
    'settingsStatusBox', 'powerStatusBox', 'consoleStatusBox', 'metricsStatusBox'];
  const elements = Object.fromEntries(ids.map(id => [id, {textContent: '', classList: {
    values: new Set(),
    add(...names) { names.forEach(name => this.values.add(name)); },
    remove(...names) { names.forEach(name => this.values.delete(name)); },
    contains(name) { return this.values.has(name); },
  }}]));
  const timers = new Map();
  let nextTimer = 0;
  const context = vm.createContext({
    activeTab: 'processing',
    document: {getElementById: id => elements[id]},
    setSettingsSaveState: message => { elements.settingsStatusBox.textContent = message; },
    setTimeout(callback, delay) { timers.set(++nextTimer, {callback, delay}); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
  });
  const globals = ['actionFeedbackTimer', 'dismissedActionFeedbackMessage']
    .map(name => source.match(new RegExp(`^let ${name} = .*;`, 'm'))[0]).join('\n');
  const functions = ['setStatus', 'setErrorStatus', 'showActionFeedback', 'dismissActionFeedback'].map(name => {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `Missing feedback function ${name}`);
    return source.slice(start, source.indexOf('\n}', start) + 2);
  }).join('\n');
  vm.runInContext(`${globals}\n${functions}`, context);
  return {context, elements, timers};
}

test('routine confirmations and automation outcomes stay inline without popups', () => {
  const {context, elements, timers} = feedbackPanel();
  for (const [message, tab, target] of [
    ['Last Automate Processing run completed successfully.', 'processing', 'processingStatusBox'],
    ['Automate Processing stopped: Salesforce email failed.', 'processing', 'processingStatusBox'],
    ['Stock issue needs manual review; required stock is unavailable.', 'processing', 'processingStatusBox'],
    ['Queuing failed Sheets Scanner rows for retry.', 'processing', 'processingStatusBox'],
    ['Work clock-in completed. Slack failed.', 'automation', 'statusBox'],
    ['Settings saved.', 'settings', 'settingsStatusBox'],
    ['Post to M6 is off.', 'automation', 'statusBox'],
    ['No available Salesforce worker profiles can be tested.', 'settings', 'settingsStatusBox'],
  ]) {
    context.setStatus(message, tab);
    assert.equal(elements[target].textContent, message);
    assert.equal(elements.actionFeedback.classList.contains('active'), false);
    assert.equal(timers.size, 0);
  }
});

test('explicit direct-action errors show an error popup even without error keywords', () => {
  const {context, elements, timers} = feedbackPanel();
  context.setErrorStatus('Disk is full.', 'settings');
  assert.equal(elements.settingsStatusBox.textContent, 'Disk is full.');
  assert.equal(elements.actionFeedbackMessage.textContent, 'Disk is full.');
  assert.equal(elements.actionFeedback.classList.contains('active'), true);
  assert.equal(elements.actionFeedback.classList.contains('error'), true);
  assert.equal([...timers.values()][0].delay, 15000);
});

test('a run update cannot replace or reset a direct-action error popup', () => {
  const {context, elements, timers} = feedbackPanel();
  context.setErrorStatus('Could not save settings.', 'settings');
  const timer = [...timers.keys()][0];
  context.setStatus('Last Automate Processing run completed successfully.');
  context.setStatus('Automation run failed; view Queue for details.');
  assert.equal(elements.actionFeedbackMessage.textContent, 'Could not save settings.');
  assert.deepEqual([...timers.keys()], [timer]);
});

test('direct-action errors can be dismissed and are never reopened by run updates', () => {
  const {context, elements, timers} = feedbackPanel();
  context.setErrorStatus('Clipboard send failed.');
  context.dismissActionFeedback();
  assert.equal(elements.actionFeedback.classList.contains('active'), false);
  assert.equal(timers.size, 0);
  context.setStatus('Automation run failed.');
  assert.equal(elements.actionFeedback.classList.contains('active'), false);
  context.setErrorStatus('Could not update Headless mode.');
  assert.equal(elements.actionFeedback.classList.contains('active'), true);
  const timer = [...timers.values()][0];
  timer.callback();
  assert.equal(elements.actionFeedback.classList.contains('active'), false);
  assert.equal(elements.actionFeedback.classList.contains('error'), false);
});
