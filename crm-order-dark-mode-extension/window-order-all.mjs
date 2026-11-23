import { SESSION_PREFIX, orderIdFromUrl } from "./all-orders-model.mjs";

const sameStock = (a, b) => a.id === b.id && a.fingerprint === b.fingerprint;
const unfinishedEditor = order => Boolean(order.original || order.expected) && !["saved", "verified", "canceled"].includes(order.status);
function stockVersion(read) {
  if (!Array.isArray(read.designs) || read.designs.some(design => !/^\d+$/.test(design.id) || typeof design.fingerprint !== "string") ||
      new Set(read.designs.map(design => design.id)).size !== read.designs.length) throw Error("The saved stock designs could not be read uniquely. Review this order.");
  return JSON.stringify(read.designs.map(({ id, fingerprint }) => [id, fingerprint]).sort((a, b) => a[0].localeCompare(b[0])));
}

export async function stockWindows(controller) {
  const stored = await controller.api.storage.session.get(null);
  return Object.entries(stored).filter(([name]) => name.startsWith(SESSION_PREFIX)).map(([, state]) => state);
}

export async function assertStockIdle(controller, state) {
  for (const other of await stockWindows(controller)) {
    if (other.windowId === state.windowId || !other.busy || other.operation !== "order-all") continue;
    if (controller.running.has(other.windowId)) throw Error("Wait for Order All in the other Chrome window to finish.");
    // A dormant/restarted service worker never resumes purchases. Loading the
    // journal marks an interrupted operation for review and retains its receipts.
    await controller.load(other.windowId);
  }
}

async function execute(controller, state, order, command, discover = false) {
  try { await controller.tab(order, state.windowId); }
  catch (error) { error.notSubmitted = true; throw error; }
  const target = { tabId: order.tabId };
  if (discover) target.allFrames = true;
  else if (order.documentId) target.documentIds = [order.documentId];
  else target.frameIds = [order.frameId];
  const results = await controller.api.scripting.executeScript({ target, world: "MAIN",
    func: command => globalThis.CRMOrderAllPage?.execute(command) ?? null,
    args: [{ ...command, orderId: order.orderId }] });
  const found = results.filter(result => result.result?.orderId === order.orderId);
  if (found.length !== 1) throw Error("The CRM stock frame is missing or ambiguous. Reload the extension, refresh this order page, and try again.");
  const response = found[0].result;
  if (response.success !== true) {
    const error = Error(response.message || "CRM could not confirm this stock action.");
    error.notSubmitted = response.notSubmitted === true;
    throw error;
  }
  if (response.adapterVersion !== 1) throw Error("Refresh this CRM order page to load the updated Order All controls.");
  if (discover) { order.frameId = found[0].frameId; order.documentId = found[0].documentId; }
  return response;
}

async function inspect(controller, state, order) {
  const reads = await Promise.all([execute(controller, state, order, { op: "inspect" }, true),
    ...order.duplicates.map(tabId => execute(controller, state, { ...order, tabId }, { op: "inspect" }, true))]);
  const reason = reads.find(read => read.skipReason)?.skipReason;
  if (!reason) {
    order.stockVersion = stockVersion(reads[0]);
    if (reads.some(read => stockVersion(read) !== order.stockVersion)) throw Error("Stock differs between duplicate tabs. Refresh them after resolving edits, then review this order before ordering.");
  }
  return { ...reads[0], skipReason: reason || reads[0].skipReason, reviewRequired: reads.some(read => read.reviewRequired),
    confirmed: reads.flatMap(read => read.confirmed || []) };
}

export async function runWindowOrderAll(controller, state) {
  const stock = state.stock;
  const tabs = (await controller.api.tabs.query({ windowId: state.windowId }))
    .filter(tab => orderIdFromUrl(tab.url)).sort((a, b) => Number(b.active) - Number(a.active) || a.index - b.index);
  const orders = new Map();
  for (const tab of tabs) {
    const orderId = orderIdFromUrl(tab.url);
    if (orders.has(orderId)) { orders.get(orderId).duplicates.push(tab.id); continue; }
    orders.set(orderId, { orderId, tabId: tab.id, duplicates: [], status: "reading", message: "Reading stock…", targets: [] });
  }
  stock.orders = [...orders.values()];
  await controller.persist(state);
  const previous = await stockWindows(controller);
  const receipts = previous.flatMap(value => value.stockReceipts || []);
  state.stockReceipts ||= [];
  const openWindows = new Set(await Promise.all(previous.map(async other => {
    try { await controller.api.windows.get(other.windowId); return other.windowId; }
    catch (_) { return null; }
  })));
  await Promise.all(stock.orders.map(async order => {
    try {
      const reserved = previous.some(other => other.windowId !== state.windowId && openWindows.has(other.windowId) && other.orders?.some(row => row.orderId === order.orderId &&
        (unfinishedEditor(row) || (row.selected && other.busy))));
      if (reserved) throw Error("This order has an unfinished All Orders editor in another window. Resolve it first.");
      const read = await inspect(controller, state, order);
      if (read.skipReason) { order.status = read.reviewRequired ? "review" : "skipped"; order.message = read.skipReason; return; }
      if (!Array.isArray(read.targets) || read.targets.some(target => !/^\d+$/.test(target.id) || !Number.isInteger(target.tab) || target.tab < 1 || typeof target.fingerprint !== "string") ||
          new Set(read.targets.map(target => target.id)).size !== read.targets.length) throw Error("CRM's stock tabs could not be read uniquely. Review this order.");
      const confirmed = read.confirmed.map(entry => ({ id: entry.id, fingerprint: entry.fingerprint, orderId: order.orderId, state: "confirmed" }));
      if (confirmed.some(entry => !/^\d+$/.test(entry.id) || typeof entry.fingerprint !== "string")) throw Error("A duplicate tab's stock receipt could not be read. Review this order.");
      for (const entry of confirmed) {
        if (!state.stockReceipts.some(saved => saved.orderId === order.orderId && saved.state === "confirmed" && sameStock(saved, entry))) state.stockReceipts.push(entry);
      }
      const history = [...receipts.filter(entry => entry.orderId === order.orderId), ...confirmed];
      if (history.some(entry => ["submitted", "review"].includes(entry.state))) throw Error("A previous window stock request needs review in CRM. It will not be repeated from this window action.");
      order.targets = read.targets.filter(target => !history.some(entry => entry.state === "confirmed" && sameStock(entry, target)));
      order.status = order.targets.length ? "ready" : "skipped";
      order.message = order.targets.length ? `Ready: product tabs ${order.targets.map(target => target.tab).join(", ")}.` : "No eligible stock remaining; locked, manual, and already ordered stock are skipped.";
    } catch (error) { order.status = "review"; order.message = error.message; }
  }));

  const ready = stock.orders.filter(order => order.status === "ready");
  // Journal every intended request before the first native submission. These
  // receipts also protect duplicate tabs and later runs in other Chrome windows.
  for (const order of ready) {
    order.status = "submitting";
    order.message = "Ordering stock through CRM…";
    for (const target of order.targets) state.stockReceipts.push({ ...target, orderId: order.orderId, operationId: stock.id, state: "submitted" });
  }
  stock.message = ready.length ? `Ordering stock on ${ready.length} order${ready.length === 1 ? "" : "s"}…` : "No orders have eligible stock to submit.";
  await controller.persist(state);

  const outcomes = await Promise.allSettled(ready.map(async order => {
    const journal = state.stockReceipts.filter(entry => entry.operationId === stock.id && entry.orderId === order.orderId && entry.state === "submitted");
    try {
      // Recheck every duplicate for editors/dialogs, then use the original pinned
      // primary document. Never silently follow navigation to a new document.
      for (const tabId of order.duplicates) {
        const read = await execute(controller, state, { ...order, tabId }, { op: "inspect" }, true);
        const changed = !read.skipReason && (stockVersion(read) !== order.stockVersion ||
          read.confirmed?.some(entry => order.targets.some(target => sameStock(entry, target))));
        if (read.skipReason || changed) {
          const error = Error(read.skipReason || "A duplicate tab's stock changed or was ordered after the scan. Review this order before ordering again.");
          error.notSubmitted = true;
          throw error;
        }
      }
      const result = await execute(controller, state, order, { op: "order", targets: order.targets });
      if (!Array.isArray(result.entries) || result.entries.length !== journal.length ||
          journal.some(entry => result.entries.filter(outcome => sameStock(entry, outcome) && ["confirmed", "review", "not_sent"].includes(outcome.state)).length !== 1)) {
        throw Error("CRM returned an incomplete stock result. Check this order before ordering again.");
      }
      for (const entry of journal) Object.assign(entry, result.entries.find(outcome => sameStock(entry, outcome)));
      order.entries = result.entries;
      order.status = result.phase === "confirmed" && journal.every(entry => entry.state === "confirmed") ? "confirmed" : "review";
      order.message = order.status === "confirmed" ? "CRM confirmed stock ordered. One order-page refresh is scheduled." : "Some product tabs need stock review in CRM. No requests were retried.";
    } catch (error) {
      for (const entry of journal) { entry.state = error.notSubmitted ? "not_sent" : "review"; entry.message = error.message; }
      order.status = "review";
      order.message = error.message;
    }
    await controller.persist(state);
  }));
  const recordingFailure = outcomes.find(outcome => outcome.status === "rejected");
  if (recordingFailure) throw recordingFailure.reason;
  const count = status => stock.orders.filter(order => order.status === status).length;
  stock.phase = count("review") ? "review" : "complete";
  stock.message = `${count("confirmed")} ordered, ${count("skipped")} skipped, ${count("review")} need review in this window.`;
}
