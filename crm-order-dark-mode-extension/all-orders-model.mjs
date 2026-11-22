// Popup/coordinator data only. CRM mutations live in the native page adapter.
export const CRM_ORIGIN = "https://crm2.legacy.printfly.com";
export const SESSION_PREFIX = "crm-all-orders:";
export const noteKinds = ["sales", "production", "art"];
export const clean = value => String(value ?? "").replace(/\s+/g, " ").trim();
export const key = value => clean(value).toLowerCase();

export function orderIdFromUrl(value) {
  try {
    const url = new URL(value);
    if (url.origin !== CRM_ORIGIN) return "";
    const route = /^\/app\/?$/.test(url.pathname) ? url.hash.replace(/^#!?\/?/, "/").split("?")[0] : url.pathname;
    return route.match(/^\/order\/(\d{7})\/?$/)?.[1] || "";
  } catch (_) { return ""; }
}

export function emptyDraft() {
  return { changes: {}, additions: [], notes: { sales: "", production: "", art: "" } };
}

export function normalizeDraft(value = {}) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw Error("Invalid editor draft.");
  const draft = emptyDraft();
  for (const [source, change] of Object.entries(value.changes || {})) {
    if (!key(source) || ["__proto__", "constructor", "prototype"].includes(key(source)) || !change || typeof change !== "object") {
      throw Error("Invalid print-area change.");
    }
    draft.changes[key(source)] = { location: clean(change.location), method: clean(change.method), remove: change.remove === true };
  }
  if (!Array.isArray(value.additions || [])) throw Error("Invalid new print areas.");
  draft.additions = (value.additions || []).map((area, i) => ({ id: `a${i}`, location: clean(area.location), method: clean(area.method) }));
  for (const kind of noteKinds) {
    if (value.notes?.[kind] !== undefined && typeof value.notes[kind] !== "string") throw Error("Notes must be text.");
    draft.notes[kind] = value.notes?.[kind] || "";
  }
  return draft;
}

export function hasChanges(draft) {
  return Object.values(draft.changes).some(change => change.location || change.method || change.remove) ||
    draft.additions.length > 0 || noteKinds.some(kind => draft.notes[kind].trim());
}

export function editorGroups(orders) {
  const groups = new Map();
  for (const order of orders.filter(order => order.selected && order.snapshot)) {
    for (const group of order.snapshot.groups) {
      if (!groups.has(group.sourceKey)) groups.set(group.sourceKey, { sourceKey: group.sourceKey, label: group.label, targets: [], methods: [] });
      const combined = groups.get(group.sourceKey);
      combined.targets.push({ orderId: order.orderId, tabs: group.tabs });
      combined.methods.push(...group.methods);
    }
  }
  return [...groups.values()].map(group => ({ ...group, methods: [...new Set(group.methods)] }));
}

export function commonChoices(orders, name) {
  const snapshots = orders.filter(order => order.selected && order.snapshot).map(order => order.snapshot);
  return (snapshots[0]?.[name] || []).filter(label => snapshots.every(snapshot => snapshot[name].some(value => key(value) === key(label))));
}
