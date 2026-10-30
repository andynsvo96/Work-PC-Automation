// List-only display enhancement. Order tools and dark mode retain their own route gate.
const CRM_FRIDAY_RUSH_CLASS = "crm-friday-rush";
const CRM_LIST_DATE_PATTERN = /\b(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\/\d{1,2}\/(?:\d{4}|\d{2}))(?!\d)/g;
const CRM_FULFILLMENT_LABEL = /\b(?:fulfill?ment|due)\s+date\b/i;
let crmFridayRushRows = new Set();
let crmFridayRushRefreshQueued = false;

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

function crmFridayMondayDate(today = new Date()) {
  if (today.getDay() !== 5) return "";
  // Calendar arithmetic survives daylight-saving changes and month/year boundaries.
  const monday = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 3, 12);
  return crmListDateKey(`${monday.getFullYear()}-${monday.getMonth() + 1}-${monday.getDate()}`);
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

function crmListFulfillmentDate(row) {
  const values = [];
  const fields = [row, ...row.querySelectorAll(
    "[data-fulfillment-date], [data-fulfilment-date], [data-due-date], "
    + "[ng-bind*='fulfillmentDate'], [ng-bind*='fulfilmentDate'], [ng-bind*='dueDate']"
  )];
  for (const field of fields) {
    for (const attribute of ["data-fulfillment-date", "data-fulfilment-date", "data-due-date"]) {
      if (field.hasAttribute(attribute)) values.push(field.getAttribute(attribute));
    }
    if (field !== row && /(?:fulfill?ment|due)Date/.test(field.getAttribute("ng-bind") || "")) {
      values.push(field.textContent);
    }
  }

  // Legacy report cards label the date inside each order row.
  const text = String(row.innerText || row.textContent || "").replace(/\s+/g, " ");
  const labeled = new RegExp(
    `\\b(?:fulfill?ment|due)\\s+date\\s*:?\\s*(${CRM_LIST_DATE_PATTERN.source})`, "gi"
  );
  for (const match of text.matchAll(labeled)) values.push(match[1]);

  // Tabular lists label the column rather than repeating the label in each row.
  const table = row.closest("table");
  if (table && row.cells) {
    const headers = new Set(Array.from(table.querySelectorAll("th"), cell => cell.parentElement));
    for (const header of headers) {
      if (header.closest("table") !== table || !header.cells) continue;
      let column = 0;
      for (const cell of header.cells) {
        if (CRM_FULFILLMENT_LABEL.test(cell.textContent) && cell.colSpan === 1) {
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

  // An absent, malformed, or conflicting fulfillment date is left to CRM.
  const dates = values.map(crmListDateKey);
  return dates.length && dates.every(date => date && date === dates[0]) ? dates[0] : "";
}

function crmListRowForOrder(link) {
  for (let row = link.parentElement; row && row !== document.body; row = row.parentElement) {
    const ids = new Set(Array.from(row.querySelectorAll("a[href]"), crmListOrderId).filter(Boolean));
    if (ids.size !== 1) return null;
    if (crmListFulfillmentDate(row)) return row;
  }
  return null;
}

function crmRefreshFridayRushRows() {
  const nextRows = new Set();
  const monday = crmIsOrderListRoute() ? crmFridayMondayDate() : "";
  if (monday) {
    for (const link of document.querySelectorAll("a[href]")) {
      if (!crmListOrderId(link)) continue;
      const row = crmListRowForOrder(link);
      if (row && crmListFulfillmentDate(row) === monday) nextRows.add(row);
    }
  }
  for (const row of crmFridayRushRows) {
    if (!nextRows.has(row)) row.classList.remove(CRM_FRIDAY_RUSH_CLASS);
  }
  for (const row of nextRows) row.classList.add(CRM_FRIDAY_RUSH_CLASS);
  crmFridayRushRows = nextRows;
}

function crmQueueFridayRushRefresh() {
  if (crmFridayRushRefreshQueued) return;
  crmFridayRushRefreshQueued = true;
  requestAnimationFrame(() => {
    crmFridayRushRefreshQueued = false;
    crmRefreshFridayRushRows();
  });
}

function crmStartFridayRushHighlighter() {
  const observer = new MutationObserver(crmQueueFridayRushRefresh);
  observer.observe(document.documentElement, {
    childList: true, subtree: true, characterData: true, attributes: true,
    // Do not observe our class changes: that would create a refresh loop.
    attributeFilter: ["href", "data-fulfillment-date", "data-fulfilment-date", "data-due-date"]
  });
  for (const event of ["hashchange", "popstate", "pageshow", "focus"]) {
    window.addEventListener(event, crmQueueFridayRushRefresh);
  }
  document.addEventListener("visibilitychange", crmQueueFridayRushRefresh);
  function refreshAtMidnight() {
    const now = new Date();
    const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    window.setTimeout(() => {
      crmQueueFridayRushRefresh();
      refreshAtMidnight();
    }, midnight.getTime() - now.getTime() + 50);
  }
  refreshAtMidnight();
  crmQueueFridayRushRefresh();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", crmStartFridayRushHighlighter, { once: true });
} else {
  crmStartFridayRushHighlighter();
}
