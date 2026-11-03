import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

const source = await readFile(new URL('../ui_panel.html', import.meta.url), 'utf8');
const functions = [
  'esc', 'parseWorkWeekDate', 'workDateIso', 'workWeekDates', 'workPlanStorageKey',
  'defaultWorkPlanDates', 'loadWorkPlan', 'saveWorkPlan', 'workPlanPrediction',
  'formatWorkTimer', 'formatWorkHoursMinutes', 'formatWorkHoursCompact',
  'updateCommunicationDashboard', 'liveWorkHoursSnapshot', 'ensureWorkPlanDayButtons',
  'toggleWorkPlanDay', 'updateWorkPlanVisual', 'updateWorkHoursVisual', 'setWorkHoursStatusPayload',
].map((name) => {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing dashboard function ${name}`);
  return source.slice(start, source.indexOf('\n}', start) + 2);
}).join('\n');
const globals = ['WORK_PLAN_STORAGE_PREFIX', 'WORK_PLAN_DAY_NAMES', 'workPlanMemory']
  .map((name) => source.match(new RegExp(`^const ${name} = .*;`, 'm'))[0]).join('\n');
const week = '2026-11-01';
const tuesday = '2026-11-03';
const wednesday = '2026-11-04';
const thursday = '2026-11-05';
const key = `automation.workPlan.${week}`;

class Element {
  constructor() {
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.className = '';
    this.textContent = '';
    this.classList = {
      toggle: (name, enabled) => {
        const classes = new Set(this.className.split(' ').filter(Boolean));
        if (enabled) classes.add(name); else classes.delete(name);
        this.className = [...classes].join(' ');
      },
      add: (name) => this.classList.toggle(name, true),
    };
  }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html || ''; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  querySelector(selector) { return this.children.find((child) => child.className === selector.slice(1)); }
}

function dashboard({ stored, storage = new Map(), blocked = false, payload = {} } = {}) {
  if (stored !== undefined) storage.set(key, JSON.stringify(stored));
  const elements = Object.fromEntries([
    'workPredictionDays', 'workPlanSummary', 'workPlanBreakNote', 'communicationWeekDays',
  ].map((id) => [id, new Element()]));
  const now = new Date(2026, 10, 3, 10).getTime();
  const context = vm.createContext({
    Date: class extends Date {
      constructor(...args) { super(...(args.length ? args : [now])); }
      static now() { return now; }
    },
    document: {
      createElement: () => new Element(),
      getElementById: (id) => elements[id] || null,
      querySelectorAll: () => elements.workPredictionDays.children,
    },
    localStorage: {
      getItem(name) { if (blocked) throw new Error('Unavailable'); return storage.get(name) || null; },
      setItem(name, value) { if (blocked) throw new Error('Unavailable'); storage.set(name, value); },
    },
    updateSlackLunchVisual() {},
    fmtTime: (value) => value || '',
    fixturePayload: {
      capped: true, cap_hours: 40, break_minutes: 30,
      state: { week_start: week, total_paid_hours: 20, days: {} },
      ...payload,
    },
  });
  vm.runInContext(`let workHoursStatusPayload = null;\n${globals}\n${functions}\nsetWorkHoursStatusPayload(fixturePayload);`, context);
  const button = (date) => elements.workPredictionDays.children.find((item) => item.dataset.date === date);
  const hours = (date) => button(date).querySelector('.work-day-hours').textContent;
  const mode = (date) => button(date).querySelector('.work-day-state').textContent;
  const click = (date) => button(date).listeners.click();
  return { context, elements, storage, button, hours, mode, click };
}

test('days cycle On to Half to Off to On and retain the choice after refresh', () => {
  const d = dashboard({ stored: { enabled_dates: [tuesday, wednesday] } });
  assert.equal(d.mode(tuesday), 'On');
  assert.equal(d.hours(tuesday), '10h 00m');
  d.click(tuesday);
  assert.equal(d.mode(tuesday), 'Half');
  assert.equal(d.hours(tuesday), '4h 00m');
  assert.equal(d.button(tuesday).attributes['aria-pressed'], 'mixed');
  assert.equal(d.hours(wednesday), '16h 00m');
  const refreshed = dashboard({ storage: d.storage });
  assert.equal(refreshed.mode(tuesday), 'Half');
  assert.equal(refreshed.hours(wednesday), '16h 00m');
  refreshed.click(tuesday);
  assert.equal(refreshed.mode(tuesday), 'Off');
  assert.equal(refreshed.hours(wednesday), '20h 00m');
  refreshed.click(tuesday);
  assert.equal(refreshed.mode(tuesday), 'On');
  assert.equal(refreshed.hours(wednesday), '10h 00m');
  assert.deepEqual(JSON.parse(refreshed.storage.get(key)).half_dates, []);
});

test('Half reserves four hours and both weekly views share the remaining On targets', () => {
  const d = dashboard({ stored: { enabled_dates: [tuesday, wednesday, thursday], half_dates: [wednesday] } });
  assert.equal(d.hours(tuesday), '8h 00m');
  assert.equal(d.hours(wednesday), '4h 00m');
  assert.equal(d.hours(thursday), '8h 00m');
  assert.match(d.elements.workPlanSummary.textContent, /2 On.*8h 00m each.*1 Half.*4h each/);
  assert.match(d.elements.workPlanBreakNote.textContent, /additional 30m unpaid break on On days/);
  assert.match(d.elements.workPlanBreakNote.textContent, /Half days have no lunch added/);
  assert.match(d.elements.communicationWeekDays.innerHTML, /8:00/);
  assert.match(d.elements.communicationWeekDays.innerHTML, /4:00<\/div><div class='rough-day-state'>Half/);
});

test('completed and live paid hours count toward the Half daily target', () => {
  const d = dashboard({
    stored: { enabled_dates: [tuesday, wednesday], half_dates: [tuesday] },
    payload: { state: {
      week_start: week, total_paid_hours: 20,
      days: { [tuesday]: { paid_hours: 1 } },
      active_shift: { date: tuesday, clock_in_at: '2026-11-03T09:00:00' },
    } },
  });
  assert.equal(d.hours(tuesday), '4h 00m');
  assert.match(d.button(tuesday).title, /2h 00m remaining/);
  assert.equal(d.hours(wednesday), '17h 00m');
  assert.match(d.elements.communicationWeekDays.innerHTML, /2:00<\/div><div class='rough-day-state'>In progress/);
});

test('Half targets stay fixed when selected alone and expose unallocated or excess hours', () => {
  const stored = { enabled_dates: [tuesday, wednesday], half_dates: [tuesday, wednesday] };
  const d = dashboard({ stored });
  assert.equal(d.hours(tuesday), '4h 00m');
  assert.equal(d.hours(wednesday), '4h 00m');
  assert.match(d.elements.workPlanBreakNote.textContent, /12h 00m still needs an On day/);
  assert.doesNotMatch(d.elements.workPlanBreakNote.textContent, /additional .* unpaid break/);
  const excess = dashboard({ stored, payload: { cap_hours: 25 } });
  assert.equal(excess.hours(tuesday), '4h 00m');
  assert.match(excess.elements.workPlanBreakNote.textContent, /exceed the remaining weekly cap by 3h 00m/);
  const mixed = dashboard({ stored: { ...stored, half_dates: [tuesday] }, payload: { cap_hours: 22 } });
  assert.equal(mixed.hours(wednesday), '0m');
  assert.match(mixed.elements.workPlanBreakNote.textContent, /exceed the remaining weekly cap by 2h 00m/);
});

test('no cap, a reached cap, and all Off retain clear states without NaN or extra lunch', () => {
  const stored = { enabled_dates: [tuesday, wednesday], half_dates: [tuesday] };
  const uncapped = dashboard({ stored, payload: { capped: false } });
  assert.equal(uncapped.hours(tuesday), '4h 00m');
  assert.equal(uncapped.hours(wednesday), 'No cap');
  assert.equal(uncapped.elements.workPlanSummary.textContent, 'Enable capped mode for predictions');
  const reached = dashboard({ stored, payload: { cap_hours: 20 } });
  assert.equal(reached.hours(wednesday), '0m');
  assert.equal(reached.elements.workPlanSummary.textContent, 'Weekly cap reached');
  const off = dashboard({ stored: { enabled_dates: [] } });
  assert.equal(off.mode(tuesday), 'Off');
  assert.equal(off.elements.workPlanSummary.textContent, 'Select at least one work day');
});

test('legacy and invalid saved dates remain bounded to the selected week', () => {
  const d = dashboard({ stored: {
    enabled_dates: [tuesday, tuesday, wednesday, '2026-11-10'],
    half_dates: [wednesday, thursday, '2026-11-10'],
  } });
  assert.equal(d.mode(tuesday), 'On');
  assert.equal(d.mode(wednesday), 'Half');
  assert.equal(d.mode(thursday), 'Off');
  d.context.toggleWorkPlanDay('2026-11-10');
  assert.equal(d.hours(tuesday), '16h 00m');
  const nextWeek = dashboard({ storage: d.storage, payload: {
    state: { week_start: '2026-11-08', total_paid_hours: 0, days: {} },
  } });
  assert.equal(nextWeek.mode('2026-11-10'), 'On');
});

test('malformed or unavailable browser storage keeps defaults and allows a Half selection', () => {
  for (const options of [
    { storage: new Map([[key, 'broken json']]) },
    { blocked: true },
  ]) {
    const d = dashboard(options);
    assert.equal(d.mode(tuesday), 'On');
    assert.equal(d.mode('2026-11-02'), 'Off');
    d.click(tuesday);
    assert.equal(d.mode(tuesday), 'Half');
    d.context.updateWorkHoursVisual();
    assert.equal(d.mode(tuesday), 'Half');
  }
});
