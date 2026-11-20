// Native CRM stock requests with confirmation receipts. Errors may be retried
// by another explicit click; confirmed stock is excluded from later batches.
var CRMOrderAllBatch = (() => {
  "use strict";
  function fingerprint(design) {
    const text = value => String(value ?? "");
    const quantity = value => Number.isFinite(Number(value)) ? String(Number(value)) : text(value);
    return JSON.stringify((design.designItems || []).filter(item => item && item.crudAction !== "d").map(item => [
      text(item.id), text(item.productId), text(item.productColorId), text(item.colorId), quantity(item.quantity),
      (item.sizes || []).filter(size => size && size.crudAction !== "d").map(size => [
        text(size.id), text(size.sizeId), text(size.size?.id), quantity(size.quantity)
      ]).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)))
    ]).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))));
  }

  function create(orderId, targets, previous = null) {
    if (!/^\d{7}$/.test(String(orderId)) || !targets.length ||
        new Set(targets.map(target => String(target.id))).size !== targets.length ||
        targets.some(target => !/^\d+$/.test(String(target.id)) || !Number.isInteger(target.tab) || target.tab < 1)) {
      throw new Error("Could not identify the stock tabs uniquely. Refresh the order and try again.");
    }
    const history = new Map([...(previous?.history || []), ...(previous?.entries || []).filter(entry => entry.state === "confirmed")]
      .map(entry => [JSON.stringify([entry.id, entry.fingerprint]), { id: entry.id, fingerprint: entry.fingerprint }]));
    return { version: 1, orderId: String(orderId), phase: "prepared", history: [...history.values()], entries: targets.map(target => ({
      id: String(target.id), tab: target.tab, fingerprint: target.fingerprint, state: "prepared", message: ""
    })) };
  }

  function valid(record, orderId) {
    return record?.version === 1 && record.orderId === String(orderId) && Array.isArray(record.entries) &&
      record.entries.length > 0 && (!record.history || (Array.isArray(record.history) && record.history.every(entry =>
        /^\d+$/.test(entry.id) && typeof entry.fingerprint === "string"))) &&
      new Set(record.entries.map(entry => entry.id)).size === record.entries.length &&
      record.entries.every(entry => /^\d+$/.test(entry.id) && Number.isInteger(entry.tab) && entry.tab > 0 &&
        typeof entry.fingerprint === "string" && ["prepared", "submitted", "confirmed", "review", "not_sent"].includes(entry.state));
  }

  function resume(record) {
    for (const entry of record.entries) {
      if (entry.state === "submitted") {
        entry.state = "review";
        entry.message = "The page changed before CRM confirmed this request.";
      } else if (entry.state === "prepared") entry.state = "not_sent";
    }
    record.phase = record.entries.every(entry => entry.state === "confirmed") ? "confirmed" : "review";
    return record;
  }

  function retryable(record, targets) {
    if (!record) return targets;
    const confirmed = [...record.entries.filter(entry => entry.state === "confirmed"), ...(record.history || [])];
    return targets.filter(target => !confirmed.some(entry => entry.id === String(target.id) && entry.fingerprint === target.fingerprint));
  }

  function blocked(record, targets) {
    if (record?.entries.some(entry => entry.state === "prepared" || entry.state === "submitted")) return true;
    return retryable(record, targets).length === 0;
  }

  async function run(record, targets, adapter) {
    adapter.save(record); // Must succeed before sending the first stock request.
    record.phase = "submitting";
    const pending = [];
    let storageFailed = false;
    const update = () => {
      try { adapter.save(record); }
      catch (_) { storageFailed = true; record.message = "Could not record a stock result. Check CRM stock before ordering again."; }
      try { adapter.changed(record); } catch (_) { /* A detached view cannot discard the result receipt. */ }
    };
    for (let index = 0; index < targets.length; index++) {
      const entry = record.entries[index];
      entry.state = "submitted";
      try { adapter.save(record); }
      catch (_) {
        entry.state = "not_sent";
        for (const remaining of record.entries.slice(index + 1)) remaining.state = "not_sent";
        storageFailed = true;
        record.message = "Could not record the next stock request. Remaining tabs were not submitted.";
        break;
      }
      // Start every request in this click, before waiting for any result.
      let request;
      try { request = adapter.submit(targets[index]); }
      catch (_) { request = Promise.reject(new Error("No confirmed CRM response.")); }
      pending.push(new Promise(resolve => {
        let finished = false;
        const timer = setTimeout(() => settle(null, true), adapter.timeoutMs || 45000);
        const settle = (response, uncertain) => {
          if (finished) return;
          finished = true;
          clearTimeout(timer);
          if (!uncertain && response?.succeeded && response.supplier) {
            entry.state = "confirmed";
            entry.message = "CRM confirmed stock ordered.";
          } else {
            entry.state = "review";
            entry.message = !uncertain && Array.isArray(response?.errors) && response.errors.length
              ? response.errors.map(String).join(", ")
              : "No confirmed CRM response.";
          }
          try { adapter.finished?.(targets[index], entry); } catch (_) { /* Keep the confirmed server result. */ }
          update();
          resolve();
        };
        Promise.resolve(request).then(response => settle(response, false), () => settle(null, true));
      }));
    }
    try { adapter.changed(record); } catch (_) { /* Requests and receipts remain active after route changes. */ }
    await Promise.all(pending);
    record.phase = !storageFailed && record.entries.every(entry => entry.state === "confirmed") ? "confirmed" : "review";
    update();
    if (storageFailed) record.phase = "review";
    return record;
  }

  return { fingerprint, create, valid, resume, retryable, blocked, run };
})();
