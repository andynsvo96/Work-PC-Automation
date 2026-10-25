import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../ui_panel.html", import.meta.url), "utf8");
const names = [
  "esc", "queueStatusLabel", "queueTaskProgressLabel", "setQueueSortOrder",
  "sortedQueueTasks", "renderQueue", "toggleQueueRunHistory", "handleQueueDrop",
  "crmOrderUrl", "crmOrderLinkHtml", "crmOrderTextHtml",
];
const functions = names.map((name) => {
  let start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `Missing dashboard function ${name}`);
  if (source.slice(start - 6, start) === "async ") start -= 6;
  const end = source.indexOf("\n}", start) + 2;
  return source.slice(start, end);
}).join("\n");
const globals = [
  "queueVisible", "queuePayload", "queueSortOrder", "queueExpandedTaskId", "queueDragTaskId",
].map((name) => source.match(new RegExp(`^let ${name} = .*;`, "m"))[0]).join("\n");

function dashboard() {
  const elements = { queueList: { innerHTML: "" }, queueSortSelect: { value: "" } };
  const context = vm.createContext({
    document: { getElementById: (id) => elements[id] || null },
    applyPlatformCapabilities() {},
    queueTimeText: () => "",
    conciseAutomationFailureMessage: (message) => message,
    queueRunHistoryHtml: () => "Repeat run details",
    api: () => { throw new Error("Sorting must not call the server"); },
  });
  vm.runInContext(`${globals}\n${functions}`, context);
  return { context, elements };
}

function task(id, status, minute, extra = {}) {
  return {
    id, label: id, status, activated_at: `2026-10-23T10:${minute}:00-04:00`, ...extra,
  };
}

function renderedIds(elements) {
  return Array.from(elements.queueList.innerHTML.matchAll(/data-task-id='([^']+)'/g), (match) => match[1]);
}

const tasks = [
  task("running", "running", "10"),
  task("waiting-old", "queued", "20", { position: 1 }),
  task("waiting-new", "queued", "40", { position: 2 }),
  task("scheduled", "idle", "30"),
  task("error-old", "failed", "05"),
  task("error-new", "failed", "15"),
  task("done", "completed", "00", { completed_at: "2026-10-23T12:00:00-04:00" }),
  task("canceled", "canceled", "25"),
];
const latest = ["waiting-new", "scheduled", "canceled", "waiting-old", "error-new", "running", "error-old", "done"];

test("the Queue sort control defaults to latest first and sorts by queued time", () => {
  const select = source.match(/<select\b[^>]*\bid='queueSortSelect'[\s\S]*?<\/select>/)[0];
  assert.match(source, /<label\b[^>]*for='queueSortSelect'>Sort<\/label>/);
  assert.match(select, /onchange='setQueueSortOrder\(this.value\)'/);
  assert.match(select, /<option value='latest' selected>Latest first<\/option>/);
  assert.match(select, /<option value='oldest'>Oldest first<\/option>/);
  assert.match(select, /<option value='errors'>Errors first<\/option>/);
  const { context, elements } = dashboard();
  context.renderQueue({ tasks });
  assert.equal(elements.queueSortSelect.value, "latest");
  assert.deepEqual(renderedIds(elements), latest);
});

test("changing the control immediately sorts oldest or errors first and survives refresh", () => {
  const { context, elements } = dashboard();
  const payload = { tasks, queued: tasks.filter((row) => row.status === "queued") };
  const original = structuredClone(payload);
  context.renderQueue(payload);
  context.setQueueSortOrder("oldest");
  assert.equal(elements.queueSortSelect.value, "oldest");
  assert.deepEqual(renderedIds(elements), latest.toReversed());
  context.setQueueSortOrder("errors");
  const expected = ["error-new", "error-old", ...latest.filter((id) => !id.startsWith("error-"))];
  assert.equal(elements.queueSortSelect.value, "errors");
  assert.deepEqual(renderedIds(elements), expected);
  context.renderQueue({ ...payload, tasks: tasks.toReversed() });
  assert.deepEqual(renderedIds(elements), expected);
  assert.deepEqual(payload, original, "Display sorting must preserve the server's queue order");
  context.setQueueSortOrder("unknown");
  assert.equal(elements.queueSortSelect.value, "latest");
  assert.deepEqual(renderedIds(elements), latest);
});

test("equal or unavailable timestamps keep a stable order and timezone offsets are respected", () => {
  const { context } = dashboard();
  const rows = [
    task("missing", "completed", "00", { activated_at: null }),
    task("first-tie", "completed", "00"),
    task("invalid", "completed", "00", { activated_at: "unavailable" }),
    task("second-tie", "completed", "00"),
    task("newest", "completed", "00", { activated_at: "2026-10-23T14:01:00Z" }),
  ];
  for (const mode of ["latest", "oldest", "errors"]) {
    const sorted = Array.from(context.sortedQueueTasks(rows, mode), (row) => row.id);
    const dated = mode === "oldest" ? ["first-tie", "second-tie", "newest"] : ["newest", "first-tie", "second-tie"];
    assert.deepEqual(sorted, [...dated, "missing", "invalid"]);
  }
});

test("sorting preserves task actions, queue positions, and expanded repeat history", () => {
  const { context, elements } = dashboard();
  context.renderQueue({ tasks: [
    ...tasks,
    task("repeat", "idle", "35", { queue_mode: "repeat" }),
    task("retry", "failed", "45", { retryable: true }),
  ] });
  context.toggleQueueRunHistory("repeat");
  for (const mode of ["oldest", "errors", "latest"]) {
    context.setQueueSortOrder(mode);
    const html = elements.queueList.innerHTML;
    assert.ok(html.includes("Repeat run details"));
    assert.ok(html.includes("retryAutomationQueueTask('retry', this)"));
    assert.ok(html.includes("deleteQueueTask('retry')"));
    assert.ok(html.includes("cancelQueueTask('waiting-new')"));
    assert.match(html, /#1 waiting-old/);
    assert.match(html, /#2 waiting-new/);
  }
});

test("drag reordering uses execution order regardless of the selected display sort", async () => {
  for (const mode of ["latest", "oldest", "errors"]) {
    const { context } = dashboard();
    const queued = [task("a", "queued", "10"), task("b", "queued", "20"), task("c", "queued", "30")];
    const payload = { tasks: [...queued], queued };
    context.renderQueue(payload);
    context.setQueueSortOrder(mode);
    const calls = [];
    context.api = async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return payload;
    };
    vm.runInContext("queueDragTaskId = 'c';", context);
    await context.handleQueueDrop({
      preventDefault() {}, currentTarget: { getAttribute: () => "a" },
    });
    assert.deepEqual(calls, [{ url: "/api/queue/reorder", body: { task_ids: ["c", "a", "b"] } }]);
    assert.deepEqual(queued.map((row) => row.id), ["a", "b", "c"]);
  }
});

test("an empty queue stays empty in every sort mode", () => {
  const { context, elements } = dashboard();
  for (const mode of ["latest", "oldest", "errors"]) {
    context.setQueueSortOrder(mode);
    assert.match(elements.queueList.innerHTML, /No queued automations yet/);
  }
});
