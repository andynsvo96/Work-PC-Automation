import { SESSION_PREFIX, CRM_ORIGIN, noteKinds, emptyDraft, editorGroups, commonChoices, hasChanges, orderIdFromUrl, canChangeOrderSelection } from "./all-orders-model.mjs";

const byId = id => document.getElementById("orders-" + id);
const colors = { "hd digital": "#ff5686", "screen printing": "#5bbad3", embroidery: "#69bd45", "direct to garment": "#7e51a1" };
const expanded = new URLSearchParams(location.search).has("editor");
document.documentElement.classList.toggle("expanded-editor", expanded);
function fitPopup() {
  document.documentElement.style.setProperty("--popup-height", Math.max(200, Math.min(600, screen.availHeight - 140)) + "px");
  document.documentElement.style.setProperty("--popup-width", Math.max(240, Math.min(760, screen.availWidth - 32)) + "px");
}
fitPopup();
window.addEventListener("resize", fitPopup);
let windowId;
let state;
let revision = 0;
let pendingDraft = Promise.resolve();
let localRequest = false;

function node(tag, text, className) {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = text;
  if (className) value.className = className;
  return value;
}
function error(message) {
  byId("status").textContent = message;
  byId("status").classList.add("error");
}
async function send(action, extra = {}) {
  const response = await chrome.runtime.sendMessage({ type: "crm-all-orders", windowId, action, ...extra });
  if (!response?.success) throw Error(response?.message || "The extension could not complete this action.");
  return response.state;
}
function receive(next) {
  if (!next || next.windowId !== windowId) return;
  // An older storage notification must not overwrite a new keystroke.
  if (state && next.revision < revision) next = { ...next, draft: state.draft, revision };
  state = next;
  revision = Math.max(revision, state.revision);
  render();
}
function persistDraft() {
  const draft = structuredClone(state.draft);
  const nextRevision = ++revision;
  state.revision = nextRevision;
  if (state.phase === "previewed") state.phase = "prepared";
  // Send immediately, even on the last keystroke before the popup closes.
  pendingDraft = send("draft", { draft, revision: nextRevision }).then(next => {
    if (next.revision >= revision) receive(next);
  });
  // Keep the rejection for dependent actions, so an undelivered draft cannot
  // silently apply an older set of edits. Also handle it when no action follows.
  void pendingDraft.catch(cause => error(cause.message));
}
async function action(name, extra = {}) {
  if (localRequest) return;
  localRequest = true;
  render();
  let failure;
  try {
    await pendingDraft;
    receive(await send(name, name === "expand" ? { screen: { left: screen.availLeft, top: screen.availTop, width: screen.availWidth, height: screen.availHeight } } : extra));
  } catch (cause) { failure = cause.message; }
  finally {
    localRequest = false;
    render();
    if (failure) error(failure);
  }
}
function field(card, label, id, options, selected, placeholder, change) {
  const row = node("label", label);
  row.htmlFor = id;
  const select = node("select");
  select.id = id;
  select.append(new Option(placeholder, ""));
  options.forEach(value => select.append(new Option(value, value)));
  select.value = selected || "";
  select.addEventListener("change", () => { change(select.value); persistDraft(); renderAreas(); renderButtons(); });
  row.append(select);
  card.append(row);
  return select;
}
function renderAreas() {
  const active = document.activeElement;
  const focusId = active?.id;
  const wrapper = byId("areas");
  wrapper.replaceChildren();
  const editable = ["prepared", "previewed"].includes(state.phase) && !state.busy && !localRequest;
  const locations = commonChoices(state.orders, "locations");
  const methods = commonChoices(state.orders, "methods");
  for (const [i, group] of editorGroups(state.orders).entries()) {
    const change = state.draft.changes[group.sourceKey] || {};
    const card = node("article", undefined, "area-card" + (change.remove ? " removed" : ""));
    const title = node("div", undefined, "area-title");
    title.append(node("span", group.label));
    const chosenMethods = change.method ? [change.method] : group.methods;
    if (chosenMethods.length === 1 && colors[chosenMethods[0].toLowerCase()]) title.style.backgroundColor = colors[chosenMethods[0].toLowerCase()];
    const remove = node("button", change.remove ? "Undo" : "×");
    remove.type = "button";
    remove.title = change.remove ? "Undo removal" : "Remove this location from its listed tabs";
    remove.disabled = !editable;
    remove.addEventListener("click", () => {
      state.draft.changes[group.sourceKey] = { ...change, remove: !change.remove };
      persistDraft(); renderAreas(); renderButtons();
    });
    title.append(remove);
    const body = node("div", undefined, "area-body");
    body.append(node("p", group.targets.map(target => `#${target.orderId}: tab${target.tabs.length === 1 ? "" : "s"} ${target.tabs.join(", ")}`).join(" · ")));
    const update = (name, value) => { state.draft.changes[group.sourceKey] = { ...change, [name]: value }; };
    const location = field(body, "Location", `orders-location-${i}`, locations, change.location, `Keep ${group.label}`, value => update("location", value));
    const method = field(body, "Method", `orders-method-${i}`, methods, change.method,
      group.methods.length === 1 ? `Keep ${group.methods[0]}` : "Keep each method", value => update("method", value));
    location.disabled = method.disabled = !editable || change.remove;
    if (change.remove) body.append(node("p", "This original location will be removed from the listed tabs."));
    card.append(title, body);
    wrapper.append(card);
  }
  state.draft.additions.forEach((addition, i) => {
    const card = node("article", undefined, "area-card");
    const title = node("div", undefined, "area-title");
    title.append(node("span", "New print area"));
    if (colors[addition.method.toLowerCase()]) title.style.backgroundColor = colors[addition.method.toLowerCase()];
    const remove = node("button", "×");
    remove.type = "button"; remove.disabled = !editable; remove.title = "Discard this new area";
    remove.addEventListener("click", () => { state.draft.additions.splice(i, 1); persistDraft(); renderAreas(); renderButtons(); });
    title.append(remove);
    const body = node("div", undefined, "area-body");
    body.append(node("p", "Added only to product tabs missing this location after the planned changes."));
    const targets = state.orders.filter(order => order.selected && order.preview?.additions?.[`a${i}`]?.length)
      .map(order => `#${order.orderId}: tabs ${order.preview.additions[`a${i}`].join(", ")}`);
    if (targets.length) body.append(node("p", targets.join(" · ")));
    field(body, "Location", `orders-add-location-${i}`, locations, addition.location, "Choose location", value => { addition.location = value; }).disabled = !editable;
    field(body, "Method", `orders-add-method-${i}`, methods, addition.method, "Choose method", value => { addition.method = value; }).disabled = !editable;
    card.append(title, body); wrapper.append(card);
  });
  if (focusId?.startsWith("orders-location-") || focusId?.startsWith("orders-method-") || focusId?.startsWith("orders-add-")) {
    document.getElementById(focusId)?.focus({ preventScroll: true });
  }
}
function renderButtons() {
  if (!state) return;
  const busy = state.busy || localRequest;
  const selected = state.orders.filter(order => order.selected);
  const editable = ["prepared", "previewed"].includes(state.phase);
  byId("scan").disabled = busy;
  byId("order-all").disabled = busy;
  byId("edit").disabled = busy || !selected.length || !["scanned", "prepared"].includes(state.phase) || selected.every(order => order.status === "prepared");
  byId("apply").disabled = busy || !selected.length || !editable || !hasChanges(state.draft) || selected.some(order => order.status !== "prepared");
  byId("save").disabled = busy || !selected.length || state.phase !== "applied";
  byId("discard").disabled = busy || !editable || !hasChanges(state.draft);
  byId("cancel").disabled = busy || !selected.length || !["prepared", "previewed", "applied", "review"].includes(state.phase);
  byId("verify").disabled = busy || !selected.some(order => order.expected && order.status !== "verified");
  byId("add").disabled = busy || !selected.length || !editable;
  byId("expand").disabled = localRequest;
  for (const kind of noteKinds) byId("note-" + kind).disabled = busy || !selected.length || !editable;
}
function orderLink(order) {
  const link = node("a", "#" + order.orderId);
  link.href = `${CRM_ORIGIN}/order/${order.orderId}`;
  link.addEventListener("click", async event => {
    event.preventDefault();
    try {
      const tab = await chrome.tabs.get(order.tabId);
      if (tab.windowId !== windowId || orderIdFromUrl(tab.url) !== order.orderId) throw Error("This order tab moved or navigated away. Review the order in its current window.");
      await chrome.tabs.update(order.tabId, { active: true });
      if (expanded) {
        const source = await chrome.windows.get(windowId);
        await chrome.windows.update(windowId, { focused: true, ...(source.state === "minimized" ? { state: "normal" } : {}) });
      }
    } catch (cause) { error(cause.message); }
  });
  return link;
}
function renderStock() {
  byId("stock").hidden = !state.stock;
  if (!state.stock) return;
  byId("stock-status").textContent = state.stock.message;
  byId("stock-status").classList.toggle("error", state.stock.phase === "review");
  const list = byId("stock-list"); list.replaceChildren();
  for (const order of state.stock.orders) {
    const row = node("div", undefined, "stock-row " + order.status);
    const details = node("div", order.message);
    for (const entry of order.entries || []) details.append(node("p", `Tab ${entry.tab}: ${entry.message}`, entry.state === "review" ? "error" : ""));
    row.append(orderLink(order), details);
    list.append(row);
  }
}
function render() {
  if (!state) return;
  const scroll = document.querySelector(".orders-scroll").scrollTop;
  byId("status").textContent = state.message;
  byId("status").classList.toggle("error", state.phase === "review" || state.validationError === true);
  byId("count").textContent = `${state.orders.filter(order => order.selected).length} of ${state.orders.length} orders included`;
  const list = byId("list"); list.replaceChildren();
  for (const order of state.orders) {
    const row = node("div", undefined, "order-row " + order.status + (order.validationError ? " error" : ""));
    const check = node("input"); check.type = "checkbox"; check.checked = order.selected;
    check.setAttribute("aria-label", "Include order " + order.orderId);
    check.disabled = localRequest || !canChangeOrderSelection(state, order, !order.selected);
    check.addEventListener("change", () => action("select", { orderId: order.orderId, selected: check.checked }));
    const link = orderLink(order);
    if (order.duplicates.length) link.append(node("span", `${order.duplicates.length} duplicate tab${order.duplicates.length === 1 ? "" : "s"}`, "duplicate-label"));
    row.append(check, link, node("span", order.message)); list.append(row);
  }
  renderStock();
  byId("editor").hidden = !["prepared", "previewed", "applied", "review"].includes(state.phase) || !state.orders.some(order => order.snapshot && order.selected);
  renderAreas();
  for (const kind of noteKinds) {
    const input = byId("note-" + kind);
    if (input.value !== state.draft.notes[kind]) input.value = state.draft.notes[kind];
  }
  renderButtons();
  document.querySelector(".orders-scroll").scrollTop = scroll;
}

for (const [id, name] of Object.entries({ scan: "scan", edit: "prepare", "order-all": "order-all", expand: "expand", apply: "apply", save: "save", cancel: "cancel", verify: "verify" })) {
  byId(id).addEventListener("click", () => action(name));
}
byId("add").addEventListener("click", () => {
  state.draft.additions.push({ location: "", method: "" }); persistDraft(); renderAreas(); renderButtons();
});
byId("discard").addEventListener("click", () => { state.draft = emptyDraft(); persistDraft(); render(); });
for (const kind of noteKinds) byId("note-" + kind).addEventListener("input", event => {
  state.draft.notes[kind] = event.target.value; persistDraft(); renderButtons();
});
chrome.storage.onChanged.addListener((values, area) => {
  if (area === "session" && windowId !== undefined && values[SESSION_PREFIX + windowId]?.newValue) receive(values[SESSION_PREFIX + windowId].newValue);
});
try {
  if (expanded) {
    const response = await chrome.runtime.sendMessage({ type: "crm-all-orders", action: "editor-source" });
    if (!response?.success) throw Error(response?.message || "The original Chrome window is unavailable.");
    windowId = response.state.windowId;
    receive(response.state);
  } else {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !Number.isInteger(tab.windowId)) throw Error("No Chrome window is available.");
    windowId = tab.windowId;
    receive(await send("get"));
  }
  if (state.phase === "idle") await action("scan");
} catch (cause) { error(cause.message); }
