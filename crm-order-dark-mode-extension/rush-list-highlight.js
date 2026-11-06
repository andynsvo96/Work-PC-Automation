// List tools have their own route gate; order tools and dark mode stay order-only.
const CRM_LIST_RUSH_CLASS = "crm-list-rush";
const CRM_LIST_APPLICABLE_COLORS = new Set([
  "rgb(243, 196, 156)", // Tan.
  "rgb(32, 223, 75)", "rgb(34, 236, 72)", // Native and extension neon green.
  "rgb(134, 32, 159)", "rgb(156, 39, 176)" // Purple.
]);
const CRM_LIST_DATE_PATTERN = /\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\/\d{1,2}\/(?:\d{4}|\d{2}))(?!\d)/g;
const CRM_LIST_DATE_FIELDS = {
  fulfillment: {
    attributes: ["data-fulfillment-date", "data-fulfilment-date", "data-due-date"],
    bindings: ["fulfillmentDate", "fulfilmentDate", "dueDate"],
    label: "(?:fulfill?ment|due)\\s+date"
  },
  production: {
    attributes: ["data-production-date", "data-scheduled-print-date"],
    bindings: ["productionDate", "scheduledPrintDate"],
    label: "(?:production|scheduled\\s+print)\\s+date"
  }
};
let crmRushListRows = new Set();
let crmRushListRefreshQueued = false;
let crmListSelectionBusy = false;

function crmListDateKey(value) {
  const dates = new Set();
  for (const token of String(value || "").match(CRM_LIST_DATE_PATTERN) || []) {
    const parts = token.split(/[-/]/).map(Number);
    let [year, month, day] = token.includes("-") ? parts : [parts[2], parts[0], parts[1]];
    if (year < 100) year += 2000;
    const date = new Date(year, month - 1, day, 12);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return "";
    dates.add(`${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`);
  }
  return dates.size === 1 ? [...dates][0] : "";
}

function crmListCalendarDay(today, offset = 0) {
  // Local calendar arithmetic survives DST and month/year boundaries.
  const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() + offset, 12);
  return crmListDateKey(`${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`);
}

function crmListRushDates(today = new Date()) {
  const day = today.getDay();
  if (day === 5) return { production: crmListCalendarDay(today), fulfillment: crmListCalendarDay(today, 3) };
  if (day === 6 || day === 0) {
    const mondayOffset = day === 6 ? 2 : 1;
    return { production: crmListCalendarDay(today, mondayOffset), fulfillment: crmListCalendarDay(today, mondayOffset + 1) };
  }
  return null;
}

function crmListRoute(location = window.location) {
  const path = location.pathname.replace(/\/$/, "");
  return path === "/app" ? location.hash.replace(/^#!?\/?/, "/").split("?")[0] : path;
}

function crmIsOrderListRoute(location = window.location) {
  return /^\/(?:reports?|orders)(?:\/|$)/i.test(crmListRoute(location));
}

function crmListOrderId(link) {
  try {
    const url = new URL(link.getAttribute("href"), window.location.href);
    if (url.origin !== window.location.origin) return "";
    return crmListRoute(url).match(/^\/order\/(\d{7})\/?$/)?.[1] || "";
  } catch (_error) {
    return "";
  }
}

function crmListDateValues(row, kind) {
  const { attributes, bindings, label } = CRM_LIST_DATE_FIELDS[kind];
  const values = [];
  const selector = [...attributes.map(name => `[${name}]`), ...bindings.map(name => `[ng-bind*='${name}']`)].join(", ");
  for (const field of [row, ...row.querySelectorAll(selector)]) {
    for (const attribute of attributes) {
      if (field.hasAttribute(attribute)) values.push(field.getAttribute(attribute));
    }
    if (bindings.some(name => (field.getAttribute("ng-bind") || "").includes(name))) values.push(field.textContent);
  }

  const text = String(row.innerText || row.textContent || "").replace(/\s+/g, " ");
  // Keep malformed labeled dates as evidence, so a group header cannot mask one.
  for (const match of text.matchAll(new RegExp(`\\b${label}\\s*:?\\s*([^|]*?)(?=\\b(?:production|scheduled|fulfill?ment|due)\\s+(?:print\\s+)?date\\b|$|\\|)`, "gi"))) {
    values.push(match[1]);
  }

  const table = row.closest("table");
  if (table && row.cells) {
    const headers = new Set(Array.from(table.querySelectorAll("th"), cell => cell.parentElement));
    for (const header of headers) {
      if (header.closest("table") !== table || !header.cells) continue;
      let column = 0;
      for (const cell of header.cells) {
        if (new RegExp(`\\b${label}\\b`, "i").test(cell.textContent) && cell.colSpan === 1) {
          let offset = 0;
          for (const dataCell of row.cells) {
            if (offset === column && dataCell.colSpan === 1) values.push(dataCell.textContent);
            offset += dataCell.colSpan;
          }
        }
        column += cell.colSpan;
      }
    }
  }
  return values;
}

function crmListConsistentDate(values) {
  const dates = values.map(crmListDateKey);
  return dates.length && dates.every(date => date && date === dates[0]) ? dates[0] : "";
}

function crmListFulfillmentDate(row) {
  return crmListConsistentDate(crmListDateValues(row, "fulfillment"));
}

function crmListProductionDate(row) {
  const values = crmListDateValues(row, "production");
  // Legacy reports put Production Date in a group header, before several orders
  // and their companion image-preview rows. Never borrow a date across groups.
  if (!row.matches("tr")) return crmListConsistentDate(values);
  for (let previous = row.previousElementSibling; previous; previous = previous.previousElementSibling) {
    if (previous.matches(".image-preview")) continue;
    if (previous.cells?.length === 1 && previous.cells[0].colSpan > 1
        && !Array.from(previous.querySelectorAll("a[href]")).some(crmListOrderId)) {
      return crmListConsistentDate([...values, ...crmListDateValues(previous, "production")]);
    }
  }
  return crmListConsistentDate(values);
}

function crmListRowForOrder(link) {
  for (let row = link.parentElement; row && row !== document.body; row = row.parentElement) {
    const ids = new Set(Array.from(row.querySelectorAll("a[href]"), crmListOrderId).filter(Boolean));
    if (ids.size !== 1) return null;
    if (crmListFulfillmentDate(row)) return row;
  }
  return null;
}

function crmRefreshRushListRows() {
  const nextRows = new Set();
  const dates = crmIsOrderListRoute() ? crmListRushDates() : null;
  if (dates) {
    const checked = new Set();
    for (const link of document.querySelectorAll("a[href]")) {
      if (!crmListOrderId(link)) continue;
      const row = crmListRowForOrder(link);
      if (!row || checked.has(row) || !crmListRowIsApplicable(row)) continue;
      checked.add(row);
      if (crmListFulfillmentDate(row) === dates.fulfillment && crmListProductionDate(row) === dates.production) nextRows.add(row);
    }
  }
  for (const row of crmRushListRows) {
    if (!nextRows.has(row)) row.classList.remove(CRM_LIST_RUSH_CLASS);
  }
  for (const row of nextRows) {
    if (!row.classList.contains(CRM_LIST_RUSH_CLASS)) row.classList.add(CRM_LIST_RUSH_CLASS);
  }
  crmRushListRows = nextRows;
}

function crmListSelectableRows() {
  return Array.from(document.querySelectorAll("#report-table tbody tr.selectable[data-id]")).filter(row => {
    const ids = new Set(Array.from(row.querySelectorAll("a[href]"), crmListOrderId).filter(Boolean));
    return row.getClientRects().length && ids.size === 1 && ids.has(row.getAttribute("data-id"));
  });
}

function crmListRowIsApplicable(row) {
  // Native selection/hover paints rows gray. The inline CRM status color keeps
  // the original eligibility available even while an order is selected. Rush
  // highlighting must never promote another status or a dimmed issue row.
  const style = getComputedStyle(row);
  if (Number(style.opacity) < 1) return false;
  const color = row.style.backgroundColor || style.backgroundColor;
  return CRM_LIST_APPLICABLE_COLORS.has(color);
}

function crmListRowIsSelected(row) {
  return row.classList.contains("previewPaneSelected");
}

async function crmWaitForListState(ready) {
  const deadline = performance.now() + 5000;
  while (!ready()) {
    if (!crmIsOrderListRoute()) throw new Error("The CRM list changed. Return to the list and try again.");
    if (performance.now() >= deadline) throw new Error("CRM selection is busy or could not be verified. Wait for the preview to finish, then try again.");
    await new Promise(resolve => window.setTimeout(resolve, 50));
  }
}

async function crmSelectListOrders(controls, openTabs) {
  if (crmListSelectionBusy) return;
  crmListSelectionBusy = true;
  const buttons = controls.querySelectorAll("button");
  const status = controls.querySelector("[role='status']");
  for (const button of buttons) button.disabled = true;
  status.textContent = "Selecting applicable orders…";
  try {
    crmRefreshRushListRows();
    const rows = crmListSelectableRows();
    const originalIds = rows.map(row => row.getAttribute("data-id"));
    const selectedRows = new Set(rows.filter(crmListRowIsApplicable));
    // Toggle only the rows that need to change. CRM owns selection and preview
    // state; adding a CSS class alone would leave bulk actions out of sync.
    for (const row of rows) {
      const selected = selectedRows.has(row);
      if (crmListRowIsSelected(row) === selected) continue;
      await crmWaitForListState(() => !document.querySelector("#preview-pane.loading"));
      if (!row.isConnected || !crmIsOrderListRoute()) throw new Error("The CRM list changed. Try again on the updated list.");
      row.dispatchEvent(new MouseEvent("click", {
        bubbles: true, cancelable: true, view: window, button: 0,
        ctrlKey: true, metaKey: true, shiftKey: false
      }));
      await crmWaitForListState(() => crmListRowIsSelected(row) === selected);
    }
    await crmWaitForListState(() => !document.querySelector("#preview-pane.loading"));
    crmRefreshRushListRows();
    const currentRows = crmListSelectableRows();
    if (!crmIsOrderListRoute() || currentRows.length !== rows.length
        || rows.some((row, index) => !currentRows.includes(row) || row.getAttribute("data-id") !== originalIds[index]
          || crmListRowIsApplicable(row) !== selectedRows.has(row)
          || crmListRowIsSelected(row) !== selectedRows.has(row))) {
      throw new Error("The CRM list changed or selection could not be verified. Try again on the updated list.");
    }
    const orderIds = [...new Set(Array.from(selectedRows, row => row.getAttribute("data-id")))];
    if (!orderIds.length) {
      status.textContent = "No tan, neon-green, or purple orders on this page.";
      return;
    }
    if (!openTabs) {
      status.textContent = `${orderIds.length} orders selected`;
      return;
    }
    status.textContent = `Opening ${orderIds.length} orders…`;
    const result = await chrome.runtime.sendMessage({ type: "crm-list:open-applicable", orderIds });
    if (!result?.success) throw new Error(result?.message || "Could not confirm which tabs opened. Check the tabs before trying again.");
    status.textContent = `${orderIds.length} selected · ${result.openedOrderIds.length} tabs opened`;
  } catch (error) {
    status.textContent = error.message || "Could not select applicable orders. Refresh CRM and try again.";
  } finally {
    crmListSelectionBusy = false;
    for (const button of buttons) button.disabled = false;
  }
}

function crmRefreshListControls() {
  const existing = document.getElementById("crm-select-applicable-controls");
  const table = crmIsOrderListRoute() && document.getElementById("report-table");
  const heading = table && table.closest(".middle-pane")?.querySelector(".dashhead");
  if (!heading) {
    existing?.remove();
    document.querySelector(".crm-list-tools-heading")?.classList.remove("crm-list-tools-heading");
    return;
  }
  if (existing?.parentElement === heading) return;
  existing?.remove();
  heading.classList.add("crm-list-tools-heading");
  const controls = document.createElement("div");
  controls.id = "crm-select-applicable-controls";
  const actions = document.createElement("div");
  actions.className = "crm-list-action-buttons";
  for (const [id, label, title, openTabs] of [
    ["crm-open-applicable-button", "Open Applicable", "Select the tan, neon-green, and purple orders on this page and open them in background tabs in this window.", true],
    ["crm-select-applicable-button", "Select Applicable", "Select the tan, neon-green, and purple orders on this page without opening tabs.", false]
  ]) {
    const button = document.createElement("button");
    button.id = id;
    button.type = "button";
    button.className = "btn btn-primary";
    button.textContent = label;
    button.title = title;
    button.addEventListener("click", () => { void crmSelectListOrders(controls, openTabs); });
    actions.append(button);
  }
  const status = document.createElement("span");
  status.setAttribute("role", "status");
  controls.append(actions, status);
  heading.append(controls);
}

function crmQueueRushListRefresh() {
  if (crmRushListRefreshQueued) return;
  crmRushListRefreshQueued = true;
  requestAnimationFrame(() => {
    crmRushListRefreshQueued = false;
    crmRefreshRushListRows();
    crmRefreshListControls();
  });
}

function crmStartRushListTools() {
  const observer = new MutationObserver(crmQueueRushListRefresh);
  observer.observe(document.documentElement, {
    childList: true, subtree: true, characterData: true, attributes: true,
    attributeFilter: ["href", "class", "style", "data-id", ...Object.values(CRM_LIST_DATE_FIELDS).flatMap(field => field.attributes)]
  });
  for (const event of ["hashchange", "popstate", "pageshow", "focus"]) {
    window.addEventListener(event, crmQueueRushListRefresh);
  }
  document.addEventListener("visibilitychange", crmQueueRushListRefresh);
  function refreshAtMidnight() {
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    window.setTimeout(() => {
      crmQueueRushListRefresh();
      refreshAtMidnight();
    }, midnight.getTime() - now.getTime() + 50);
  }
  refreshAtMidnight();
  crmQueueRushListRefresh();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", crmStartRushListTools, { once: true });
} else {
  crmStartRushListTools();
}
