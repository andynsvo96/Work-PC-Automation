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
    setStatus: message => messages.push(message),
    api: api || (async () => ({success: true, enabled: true})),
  });
  vm.runInContext(`${globals}\n${functions}`, context);
  return { context, elements, messages };
}

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
