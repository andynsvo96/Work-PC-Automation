const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";
const ROOT_CLASS = "crm-order-dark-mode";
const ROOT_ATTRIBUTE = "data-crm-order-dark-mode";

let themeEnabled = false;
let refreshQueued = false;
let orderProcessorPollTimer = null;

function isSalesforceLink(value) {
  try {
    const url = new URL(String(value || ""), window.location.href);
    const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
    return (url.protocol === "https:" || url.protocol === "http:") && [
      "salesforce.com",
      "force.com",
      "visualforce.com"
    ].some((suffix) => hostname === suffix || hostname.endsWith(`.${suffix}`));
  } catch (_error) {
    return false;
  }
}

function clickedLink(event) {
  const path = typeof event.composedPath === "function" ? event.composedPath() : [];
  return path.find((node) => node && node.nodeType === Node.ELEMENT_NODE && node.matches("a[href]"))
    || (event.target && event.target.closest && event.target.closest("a[href]"));
}

// Route Salesforce links before CRM can create a duplicate tab. The service
// worker searches the entire Chrome profile, so an existing Salesforce tab in
// another browser window is reused and its window is brought forward.
document.addEventListener("click", (event) => {
  if (event.button !== 0) return;
  const anchor = clickedLink(event);
  const url = anchor && anchor.href;
  if (!isSalesforceLink(url)) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  void chrome.runtime.sendMessage({ type: "crm-salesforce:open-link", url });
}, true);

// Keep extension menus from staying open after the user returns to the CRM
// page. Checking each whole control lets its button continue to toggle it.
document.addEventListener("click", (event) => {
  document.querySelectorAll("[data-crm-order-menu-control='true']").forEach((control) => {
    const menu = control.querySelector("[role='menu']");
    if (menu && !menu.hidden && !control.contains(event.target)) closeOrderProcessMenu(control);
  });
});

const MANUAL_ORDER_AUTOMATIONS = [
  { key: "address_validator", label: "Address Validator" },
  { key: "product_separator", label: "Product Separator" },
  { key: "auto_splitter", label: "Auto Splitter" },
  { key: "order_goods", label: "Order Goods" },
  { key: "shipping_bypasser", label: "Shipping Bypasser" },
  { key: "push_back", label: "Push Back" },
  { key: "sleeve_prints", label: "Extra Print Areas" }
];

const CANCEL_ORDER_AUTOMATIONS = [
  { key: "copyright_cancel", label: "Copyright - Cancel", requiresReason: true },
  { key: "content_violation_cancel", label: "Content Violation - Cancel", requiresReason: true },
  { key: "existing_designs_cancel", label: "CANCEL - Existing Designs" },
  { key: "outside_limit_cancel", label: "CANCEL - Outside Limit" },
  { key: "unresponsive_cancel", label: "CANCEL - Unresponsive" }
];

const REACHOUT_ORDER_AUTOMATIONS = [
  { key: "complicated_emb_to_hdd", label: "Complicated EMB" },
  { key: "oversize_emb_to_hdd", label: "Oversize EMB to HDD" },
  { key: "copyright_removal", label: "Copyright Removal", requiresReason: true },
  { key: "copyright_reachout", label: "Copyright - Reachout", requiresReason: true }
];

const STOCK_ISSUE_AUTOMATIONS = [
  { key: "stock_issue_extension", label: "Extension Required" },
  { key: "stock_issue_color", label: "Suggest Different Color" },
  { key: "stock_issue_size", label: "Suggest Different Size" }
];

function currentOrderId() {
  const match = `${window.location.pathname || ""}${window.location.hash || ""}`.match(/\/order\/(\d{7})\b/);
  return match ? match[1] : "";
}

function crmControlLabel(element) {
  return String(
    element && (element.value || element.innerText || element.textContent || element.getAttribute("aria-label") || element.title) || ""
  ).replace(/\s+/g, " ").trim().toLowerCase();
}

function findSendInvoiceButton() {
  return Array.from(document.querySelectorAll("button, input[type='button'], input[type='submit'], a"))
    .find((element) => crmControlLabel(element) === "send invoice");
}

function stopOrderProcessorPolling() {
  if (orderProcessorPollTimer) {
    clearInterval(orderProcessorPollTimer);
    orderProcessorPollTimer = null;
  }
}

function manualOrderProcessorButton() {
  return document.getElementById("crm-order-manual-process-button");
}

function setOrderProcessorControlsDisabled(disabled) {
  document.querySelectorAll("[data-crm-order-process-trigger='true']").forEach((button) => {
    button.disabled = disabled;
  });
  if (disabled) {
    document.querySelectorAll("[data-crm-order-menu-control='true']").forEach(closeOrderProcessMenu);
  }
}

function refreshOrderAfterProcessorCompletion(runtime) {
  if (!runtime || runtime.queued || runtime.running || runtime.lastSuccess === null || runtime.lastSuccess === undefined) return;
  const orderId = currentOrderId();
  const completedAt = String(runtime.completedAt || "");
  if (!orderId || !completedAt || String(runtime.orderId || "") !== orderId) return;
  const activeRunKey = `crm-order-process-active:${orderId}`;
  if (!sessionStorage.getItem(activeRunKey)) return;
  const refreshKey = `crm-order-process-refresh:${orderId}:${completedAt}`;
  if (sessionStorage.getItem(refreshKey)) return;
  sessionStorage.setItem(refreshKey, "1");
  sessionStorage.removeItem(activeRunKey);
  window.setTimeout(() => window.location.reload(), 300);
}

function setOrderProcessorResult(button, text, tone) {
  let result = document.getElementById("crm-order-automation-result");
  if (!text) {
    result?.remove();
    return;
  }
  if (!result) {
    result = document.createElement("span");
    result.id = "crm-order-automation-result";
    result.setAttribute("role", "status");
    result.setAttribute("aria-live", "polite");
    Object.assign(result.style, {
      marginLeft: "8px", font: "600 11px system-ui, sans-serif", verticalAlign: "middle"
    });
  }
  // Keep the result after the Manual Process menu.
  const manualControl = document.getElementById("crm-order-manual-process-control");
  const resultAnchor = manualControl || button;
  if (result.previousElementSibling !== resultAnchor) resultAnchor.insertAdjacentElement("afterend", result);
  result.textContent = text;
  result.style.color = tone === "error" ? "#b91c1c" : (tone === "success" ? "#15803d" : "#075985");
}

function renderOrderProcessorStatus(button, response) {
  const runtime = response && response.runtime;
  if (!button || !runtime || runtime.runKind !== "manual"
      || (runtime.orderId && runtime.orderId !== currentOrderId())) return false;
  const message = String(runtime.lastMessage || "");
  button.title = message || "Choose one automation to run for this order only.";
  if (runtime.queued) {
    setOrderProcessorControlsDisabled(true);
    button.textContent = "Manual Process: Queued";
    setOrderProcessorResult(button, message || "Queued behind any active CRM automation.", "progress");
    return true;
  }
  if (runtime.running) {
    setOrderProcessorControlsDisabled(true);
    button.textContent = "Manual Process: Working";
    setOrderProcessorResult(button, message || "Working…", "progress");
    return true;
  }
  setOrderProcessorControlsDisabled(false);
  button.textContent = runtime.lastSuccess === false ? "Manual Process: Review" : "Manual Process";
  setOrderProcessorResult(button, message, runtime.lastSuccess === false ? "error" : "success");
  refreshOrderAfterProcessorCompletion(runtime);
  return false;
}

function beginOrderProcessorPolling(button) {
  stopOrderProcessorPolling();
  const poll = async () => {
    try {
      const response = await chrome.runtime.sendMessage({ type: "crm-order-automation:status" });
      if (!response || !response.success) {
        setOrderProcessorControlsDisabled(false);
        button.textContent = "Manual Process: Review";
        button.title = (response && response.message) || "Could not read the local Automation app status.";
        setOrderProcessorResult(button, button.title, "error");
        stopOrderProcessorPolling();
        return;
      }
      if (!renderOrderProcessorStatus(button, response)) stopOrderProcessorPolling();
    } catch (_error) {
      setOrderProcessorControlsDisabled(false);
      button.textContent = "Manual Process: Review";
      button.title = "Could not read the local Automation app status. Confirm the local app is running, then try again.";
      setOrderProcessorResult(button, button.title, "error");
      stopOrderProcessorPolling();
    }
  };
  void poll();
  orderProcessorPollTimer = setInterval(() => { void poll(); }, 2000);
}

async function loadOrderProcessorStatus(button) {
  try {
    const response = await chrome.runtime.sendMessage({ type: "crm-order-automation:status" });
    if (response && response.success && renderOrderProcessorStatus(button, response)) {
      beginOrderProcessorPolling(button);
    }
  } catch (_error) {
    // The local app may not be running yet; the menu remains available.
  }
}

function closeOrderProcessMenu(control) {
  if (!control) return;
  const menu = control.querySelector("[role='menu']");
  const button = control.querySelector("[aria-haspopup='menu']");
  if (menu) menu.hidden = true;
  if (button) button.setAttribute("aria-expanded", "false");
}

function closeAllOrderProcessMenus(exceptControl = null) {
  document.querySelectorAll("[data-crm-order-menu-control='true']").forEach((control) => {
    if (control !== exceptControl) closeOrderProcessMenu(control);
  });
}

function queueManualOrderAutomation(
  automation,
  triggerButton,
  statusButton,
  reason = "",
  structuredData = {},
  options = {}
) {
  const orderId = currentOrderId();
  if (!orderId) {
    return Promise.resolve({ success: false, message: "Open a CRM order with a valid 7-digit order number first." });
  }
  const surfacePageErrors = options.surfacePageErrors !== false;
  const control = triggerButton.closest("[data-crm-order-menu-control='true']");
  closeOrderProcessMenu(control);
  setOrderProcessorControlsDisabled(true);
  const isManualButton = triggerButton.id === "crm-order-manual-process-button";
  if (surfacePageErrors) {
    if (isManualButton) triggerButton.textContent = `Queuing ${automation.label}…`;
    setOrderProcessorResult(triggerButton, `Sending ${automation.label} for order ${orderId} to the CRM automation queue…`, "progress");
  }
  return chrome.runtime.sendMessage({
    type: "crm-order-automation:manual-start",
    orderId,
    automation: automation.key,
    reason,
    ...structuredData
  }).then((response) => {
    if (response && response.success) {
      sessionStorage.setItem(`crm-order-process-active:${orderId}`, "1");
      renderOrderProcessorStatus(statusButton, response);
      beginOrderProcessorPolling(statusButton);
      return response;
    }
    setOrderProcessorControlsDisabled(false);
    const message = (response && response.message) || "Could not queue the selected automation.";
    if (surfacePageErrors) {
      if (isManualButton) triggerButton.textContent = "Manual Process";
      triggerButton.title = message;
      setOrderProcessorResult(triggerButton, message, "error");
    }
    return { ...(response || {}), success: false, message };
  }).catch(() => {
    setOrderProcessorControlsDisabled(false);
    const message = "Could not queue the selected automation. Confirm the local Automation app is running, then try again.";
    if (surfacePageErrors) {
      if (isManualButton) triggerButton.textContent = "Manual Process";
      triggerButton.title = message;
      setOrderProcessorResult(triggerButton, message, "error");
    }
    return { success: false, message };
  });
}

function stockIssueCleanText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function visibleStockIssueElement(element) {
  if (!element) return false;
  const rect = element.getBoundingClientRect();
  const style = window.getComputedStyle ? window.getComputedStyle(element) : {};
  return rect.width > 8 && rect.height > 8 && style.display !== "none" && style.visibility !== "hidden";
}

function visibleStockIssueDesignTabs() {
  const bestByNumber = new Map();
  for (const element of Array.from(document.querySelectorAll("div,a,button,li,span")).filter(visibleStockIssueElement)) {
    const text = stockIssueCleanText(element.innerText || element.textContent);
    const match = text.match(/\b(\d+)\s*-\s*QTY\s*:\s*(\d+)/i);
    if (!match || !/Design Previews|View Proofs/i.test(text)) continue;
    const clickTarget = element.closest("#main-header-design-tabs button, button, a, [role='tab'], [ng-click], [onclick], li") || element;
    const rect = clickTarget.getBoundingClientRect();
    let score = 1000 - text.length;
    if (element.querySelector("input")) score += 100;
    if (/^(?:BUTTON|A)$/i.test(clickTarget.tagName || "")) score += 50;
    if (rect.top < 450) score += 100;
    const tabNumber = Number(match[1]);
    const previous = bestByNumber.get(tabNumber);
    if (!previous || score > previous.score) {
      bestByNumber.set(tabNumber, { element: clickTarget, tabNumber, quantity: Number(match[2]), score });
    }
  }
  return Array.from(bestByNumber.values()).sort((left, right) => left.tabNumber - right.tabNumber);
}

function stockIssueTabHasActiveMarker(element) {
  for (let current = element; current && current !== document.body; current = current.parentElement) {
    if (["active", "selected", "current", "btn-inverse"].some((name) => current.classList?.contains(name))) return true;
    if (["aria-selected", "aria-pressed", "data-active", "data-selected"].some((name) => current.getAttribute?.(name) === "true")) return true;
    if (current !== element && current.id === "main-header-design-tabs") break;
  }
  return Boolean(element.querySelector("input:checked, [aria-selected='true'], [aria-pressed='true'], [data-active='true'], [data-selected='true']"));
}

function stockIssueTabBackgroundBrightness(element) {
  for (let current = element, depth = 0; current && current !== document.body && depth < 4; current = current.parentElement, depth += 1) {
    const color = window.getComputedStyle?.(current).backgroundColor || "";
    const match = color.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)$/i);
    if (!match || (match[4] !== undefined && Number(match[4]) < 0.25)) continue;
    return (Number(match[1]) * 299 + Number(match[2]) * 587 + Number(match[3]) * 114) / 1000;
  }
  return null;
}

function activeStockIssueDesignTabNumber(tabs) {
  if (tabs.length === 1) return tabs[0].tabNumber;
  const marked = tabs.filter(({ element }) => stockIssueTabHasActiveMarker(element));
  if (marked.length === 1) return marked[0].tabNumber;

  // Some CRM order pages expose no selected-state attribute. On those pages
  // the active design tab is the one dark button among otherwise light tabs.
  const byBrightness = tabs
    .map((tab) => ({ ...tab, brightness: stockIssueTabBackgroundBrightness(tab.element) }))
    .filter((tab) => Number.isFinite(tab.brightness))
    .sort((left, right) => left.brightness - right.brightness);
  if (byBrightness.length === tabs.length && byBrightness[1].brightness - byBrightness[0].brightness >= 35) {
    return byBrightness[0].tabNumber;
  }
  return null;
}

function clickStockIssueDesignTab(tabNumber) {
  const tab = visibleStockIssueDesignTabs().find((item) => item.tabNumber === Number(tabNumber));
  if (!tab) throw new Error(`Design tab ${tabNumber} is no longer available.`);
  tab.element.scrollIntoView({ block: "center", inline: "center" });
  tab.element.click();
}

function stockIssueDelay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

const EXTRA_PRINT_AREAS = { left: "Sleeve Left", right: "Sleeve Right", side_left: "Side Left", side_right: "Side Right" };

function sleevePrintCurrency(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : "$0.00";
}

function sleevePrintInkPrice(quantity) {
  if (quantity >= 100) return 5;
  if (quantity >= 20) return 6;
  if (quantity >= 10) return 7;
  return 8;
}

function sleevePrintCleanPrice(value) {
  const text = String(value ?? "").trim();
  if (!text) return { valid: true, value: null, message: "" };
  if (!/^\d+(?:\.\d{1,2})?$/.test(text)) {
    return { valid: false, value: null, message: "Enter a dollar amount with no more than two decimals." };
  }
  const valueAsNumber = Number(text);
  if (!Number.isFinite(valueAsNumber) || valueAsNumber < 0 || valueAsNumber > 1000) {
    return { valid: false, value: null, message: "Enter a custom price from $0.00 through $1,000.00." };
  }
  return { valid: true, value: valueAsNumber, message: "" };
}

function sleevePrintSelectionSummary(selections, tabs, customInkPrice, customEmbroideryPrice, customReversePrice = { value: null }) {
  const inkTabs = selections.filter((selection) => Object.keys(EXTRA_PRINT_AREAS).some((area) => selection[area] === "ink"));
  const embroideryTabs = selections.filter((selection) => selection.extra_emb === "embroidery" || Object.keys(EXTRA_PRINT_AREAS).some((area) => selection[area] === "embroidery"));
  const reverseTabs = selections.filter((selection) => selection.reverse === "ink");
  const pricedTabs = selections.filter((selection) => selection.reverse === "ink" || Object.keys(EXTRA_PRINT_AREAS).some((area) => selection[area] === "ink"));
  const inkQuantity = pricedTabs.reduce((total, selection) => total + Number(tabs.get(selection.tab_number)?.quantity || 0), 0);
  const embroideryQuantity = embroideryTabs.reduce((total, selection) => total + Number(tabs.get(selection.tab_number)?.quantity || 0), 0);
  const calculatedInkPrice = inkQuantity ? sleevePrintInkPrice(inkQuantity) : null;
  const inkPrice = calculatedInkPrice === null ? null : (customInkPrice.value ?? calculatedInkPrice);
  const embroideryPrice = embroideryQuantity ? (customEmbroideryPrice.value ?? 15) : null;
  const reversePrice = reverseTabs.length ? (customReversePrice.value ?? calculatedInkPrice) : null;
  return { inkTabs, embroideryTabs, reverseTabs, inkQuantity, embroideryQuantity, calculatedInkPrice, inkPrice, embroideryPrice, reversePrice };
}

function showSleevePrintsDialog(automation, triggerButton, statusButton) {
  const discoveredTabs = visibleStockIssueDesignTabs();
  if (!discoveredTabs.length) {
    setOrderProcessorResult(triggerButton, "Could not detect any design tabs and quantities for Extra Print Areas.", "error");
    return;
  }
  const tabs = new Map(discoveredTabs.map((tab) => [tab.tabNumber, tab]));
  document.getElementById("crm-sleeve-prints-dialog")?.remove();
  const overlay = document.createElement("div");
  overlay.id = "crm-sleeve-prints-dialog";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "Configure Extra Print Areas");
  Object.assign(overlay.style, { position: "fixed", zIndex: "2147483647", inset: "0", display: "flex",
    alignItems: "center", justifyContent: "center", padding: "20px", background: "rgba(15,23,42,.56)", font: "14px system-ui, sans-serif" });
  const dialog = document.createElement("div");
  Object.assign(dialog.style, { width: "min(620px, 100%)", maxHeight: "calc(100vh - 40px)", overflowY: "auto",
    padding: "20px", borderRadius: "7px", color: "#0f172a", background: "#fff", boxShadow: "0 20px 45px rgba(15,23,42,.34)" });
  const title = document.createElement("div");
  title.textContent = "Extra Print Areas";
  title.style.font = "700 17px system-ui, sans-serif";
  const explanation = document.createElement("p");
  explanation.textContent = "Select tabs and one category per tab. Ink pricing uses the combined garment quantity of selected ink and reversible-print tabs.";
  dialog.append(title, explanation);
  const choices = [];
  const priceOverrides = { ink: null, embroidery: null, reverse: null };
  const priceErrors = { ink: "", embroidery: "", reverse: "" };
  let submitting = false;

  function selectControl(label, entries, parent) {
    const wrap = document.createElement("label");
    wrap.textContent = label;
    const control = document.createElement("select");
    control.setAttribute("aria-label", label);
    Object.assign(control.style, { display: "block", width: "100%", padding: "6px", margin: "4px 0 10px" });
    for (const [value, text] of entries) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = text;
      control.append(option);
    }
    wrap.append(control);
    parent.append(wrap);
    control.addEventListener("change", () => refresh());
    return control;
  }
  for (const tab of discoveredTabs) {
    const card = document.createElement("fieldset");
    Object.assign(card.style, { margin: "0 0 10px", padding: "11px", border: "1px solid #cbd5e1", borderRadius: "4px" });
    const heading = document.createElement("label");
    heading.style.fontWeight = "700";
    const include = document.createElement("input");
    include.type = "checkbox";
    include.setAttribute("aria-label", `Include design tab ${tab.tabNumber}`);
    heading.append(include, document.createTextNode(` Tab ${tab.tabNumber} — Qty: ${tab.quantity}`));
    const details = document.createElement("div");
    Object.assign(details.style, { marginTop: "10px", paddingLeft: "22px" });
    const categories = document.createElement("div");
    Object.assign(categories.style, { display: "flex", flexWrap: "wrap", gap: "12px", marginBottom: "12px" });
    const choice = { tab, include, details, category: "", checks: {} };
    for (const [key, label] of [["sleeve", "Sleeve Prints"], ["reverse", "Reversible Prints"], ["side", "Side Prints"], ["extra_emb", "Extra EMB Area"]]) {
      const wrap = document.createElement("label");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.setAttribute("aria-label", `${label} for tab ${tab.tabNumber}`);
      check.addEventListener("change", () => { choice.category = check.checked ? key : ""; refresh(); });
      choice.checks[key] = check;
      wrap.append(check, document.createTextNode(label));
      categories.append(wrap);
    }
    details.append(categories);
    const areaOptions = document.createElement("div");
    Object.assign(areaOptions.style, { display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "12px" });
    choice.method = selectControl(`Print method for tab ${tab.tabNumber}`, [["ink", "Ink print"], ["embroidery", "Embroidery"]], areaOptions);
    choice.location = selectControl(`Print location for tab ${tab.tabNumber}`, [["", "Select left, right, or both"], ["left", "Left"], ["right", "Right"], ["both", "Both"]], areaOptions);
    choice.areaOptions = areaOptions;
    const reverseHint = document.createElement("p");
    reverseHint.textContent = "Ink only. One charge per garment for a front or back area; two charges for both.";
    choice.reverseHint = reverseHint;
    const embroideryHint = document.createElement("p");
    embroideryHint.textContent = "Sets up left and right chest embroidery. One additional-area charge per garment; the existing area is included. Existing chest areas are reused and pricing still applies.";
    choice.embroideryHint = embroideryHint;
    details.append(areaOptions, reverseHint, embroideryHint);
    include.addEventListener("change", () => {
      if (!include.checked) { choice.category = ""; choice.location.value = ""; }
      refresh();
    });
    card.append(heading, details);
    dialog.append(card);
    choices.push(choice);
  }
  const sharedPrices = {};
  for (const [key, label] of [["ink", "Sleeve / side ink"], ["embroidery", "Embroidery"], ["reverse", "Reversible ink"]]) {
    const priceWrap = document.createElement("label");
    Object.assign(priceWrap.style, { marginTop: "12px", padding: "11px", border: "1px solid #cbd5e1", borderRadius: "4px" });
    const priceHeading = document.createElement("strong");
    priceHeading.textContent = `${label} — shared price per area`;
    const priceCaption = document.createElement("span");
    Object.assign(priceCaption.style, { display: "block", fontSize: "12px", color: "#334155" });
    const priceInput = document.createElement("input");
    priceInput.type = "text";
    priceInput.inputMode = "decimal";
    priceInput.setAttribute("aria-label", `${label} price per area`);
    Object.assign(priceInput.style, { display: "block", width: "100%", padding: "6px", boxSizing: "border-box", marginTop: "4px" });
    sharedPrices[key] = { priceWrap, priceCaption, priceInput };
    priceInput.addEventListener("input", () => {
      const parsed = sleevePrintCleanPrice(priceInput.value);
      priceErrors[key] = parsed.message;
      if (parsed.valid) priceOverrides[key] = parsed.value;
      refresh(priceInput);
    });
    priceWrap.append(priceHeading, priceCaption, priceInput);
    dialog.append(priceWrap);
  }
  const validation = document.createElement("div");
  validation.setAttribute("role", "status");
  validation.setAttribute("aria-live", "polite");
  Object.assign(validation.style, { marginTop: "10px", color: "#b91c1c" });
  const actions = document.createElement("div");
  Object.assign(actions.style, { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "12px" });
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Back";
  const queue = document.createElement("button");
  queue.type = "button";
  queue.textContent = "Queue task";
  for (const button of [cancel, queue]) button.style.padding = "7px 11px";
  queue.style.color = "#fff";
  actions.append(cancel, queue);
  dialog.append(validation, actions);
  overlay.append(dialog);
  document.body.append(overlay);

  function refresh(sourcePriceInput = null) {
    const selected = [];
    const errors = [];
    for (const choice of choices) {
      const { tab, include, category, method, location } = choice;
      if (!include.checked) continue;
      if (!category || (!["reverse", "extra_emb"].includes(category) && !location.value)) {
        errors.push(`Complete the category and location for tab ${tab.tabNumber}.`);
        continue;
      }
      const selection = { tab_number: tab.tabNumber, quantity: tab.quantity };
      if (category === "reverse") selection.reverse = "ink";
      else if (category === "extra_emb") selection.extra_emb = "embroidery";
      else for (const side of ["left", "right"]) {
        if (location.value === side || location.value === "both") selection[category === "side" ? `side_${side}` : side] = method.value;
      }
      selected.push(selection);
    }
    const summary = sleevePrintSelectionSummary(selected, tabs, { value: priceOverrides.ink }, { value: priceOverrides.embroidery }, { value: priceOverrides.reverse });
    const used = { ink: summary.inkTabs.length, embroidery: summary.embroideryTabs.length, reverse: summary.reverseTabs.length };
    for (const choice of choices) {
      const { include, category, checks, details, areaOptions, reverseHint, embroideryHint } = choice;
      details.style.setProperty("display", include.checked ? "block" : "none", "important");
      for (const [key, check] of Object.entries(checks)) { check.checked = category === key; check.disabled = submitting; }
      areaOptions.style.setProperty("display", category && !["reverse", "extra_emb"].includes(category) ? "grid" : "none", "important");
      reverseHint.style.setProperty("display", category === "reverse" ? "block" : "none", "important");
      embroideryHint.style.setProperty("display", category === "extra_emb" ? "block" : "none", "important");
      include.disabled = submitting;
      choice.method.disabled = submitting;
      choice.location.disabled = submitting;
    }
    for (const [key, { priceWrap, priceCaption, priceInput }] of Object.entries(sharedPrices)) {
      priceWrap.style.setProperty("display", used[key] ? "block" : "none", "important");
      const applicableTabs = key === "reverse" ? summary.reverseTabs : key === "ink" ? summary.inkTabs : summary.embroideryTabs;
      const tabNumbers = applicableTabs.map((selection) => selection.tab_number).join(", ");
      const price = key === "reverse" ? summary.reversePrice : key === "ink" ? summary.inkPrice : summary.embroideryPrice;
      priceCaption.textContent = key === "embroidery" ? `Price per area — applies to tabs ${tabNumbers}.`
        : `Price per area — calculated from ${summary.inkQuantity} selected ink-print garments; applies to tabs ${tabNumbers}.`;
      if (priceInput !== sourcePriceInput && !priceErrors[key]) priceInput.value = Number(price ?? (key === "embroidery" ? 15 : sleevePrintInkPrice(summary.inkQuantity))).toFixed(2);
      priceInput.disabled = submitting;
    }
    if (!selected.length) errors.push("Choose at least one print-area request.");
    for (const key of Object.keys(used)) if (used[key] && priceErrors[key]) errors.push(priceErrors[key]);
    const valid = !errors.length && !submitting;
    validation.textContent = submitting ? "Sending Extra Print Areas to the Automation queue…" : errors.join(" ");
    queue.disabled = !valid;
    queue.style.setProperty("background", valid ? "#15803d" : "#9ca3af", "important");
    queue.style.cursor = valid ? "pointer" : "not-allowed";
    return { selected, valid, used };
  }
  cancel.addEventListener("click", () => { if (!submitting) overlay.remove(); });
  queue.addEventListener("click", async () => {
    const state = refresh();
    if (!state.valid) return;
    submitting = true;
    refresh();
    try {
      const response = await queueManualOrderAutomation(automation, triggerButton, statusButton, "", {
        sleeves: state.selected,
        ink_price: state.used.ink ? priceOverrides.ink : null,
        embroidery_price: state.used.embroidery ? priceOverrides.embroidery : null,
        reverse_price: state.used.reverse ? priceOverrides.reverse : null
      }, { surfacePageErrors: false });
      if (response && response.success) { overlay.remove(); return; }
      throw new Error(response?.message || "Could not queue Extra Print Areas.");
    } catch (error) {
      submitting = false;
      refresh();
      validation.textContent = error.message;
    }
  });
  refresh();
}

function stockIssueDetectedSizes(block, rawLines, text) {
  const values = [];
  const add = (value) => {
    const size = stockIssueCleanText(value).replace(/^Sizes?\s*:\s*/i, "");
    if (!size || /^(?:size|sizes|quantity|qty|price)$/i.test(size)) return;
    if (!values.some((item) => item.toLowerCase() === size.toLowerCase())) values.push(size);
  };
  const addSizeText = (value) => {
    const cleaned = stockIssueCleanText(value).replace(/\s+(?:Quantity|Qty|Price)\s*:.*$/i, "");
    if (!cleaned) return;
    const named = cleaned.match(/\b(?:one\s+size|osfa|xxs|xs|s|m|l|xl|2xl|3xl|4xl|5xl|6xl|x-small|small|medium|large|x-large|xx-large|xxx-large)\b/gi);
    if (named && named.length > 1) named.forEach(add);
    else if (/[|,/]/.test(cleaned)) cleaned.split(/[|,/]/).forEach(add);
    else add(cleaned);
  };
  // The CRM highlights the chosen size, quantity, and price with the same
  // class. Scope that class to the row whose label is exactly "Size" so a
  // quantity such as "1" or a price such as "10.00" can never be offered as
  // a size.
  const sizeRows = Array.from(block.querySelectorAll("tr")).filter((row) => {
    const cells = Array.from(row.querySelectorAll(":scope > th, :scope > td"));
    return cells.some((cell) => /^Size\s*:?$/i.test(stockIssueCleanText(cell.innerText || cell.textContent)));
  });
  for (const row of sizeRows) {
    for (const element of Array.from(row.querySelectorAll(".design-item-size-active"))) {
      if (visibleStockIssueElement(element)) add(element.innerText || element.textContent);
    }
  }
  for (const element of Array.from(block.querySelectorAll("[data-size],[data-size-name],[ng-model*='size' i]"))) {
    if (!visibleStockIssueElement(element)) continue;
    add(element.getAttribute("data-size") || element.getAttribute("data-size-name") || element.value || element.innerText || element.textContent);
  }
  // Text parsing is a last resort for older CRM markup without a Size row.
  // A rendered Size row may contain every available size, whereas its active
  // cell(s) above identify only the sizes actually on this order.
  if (!sizeRows.length) {
    for (const line of rawLines) {
      const match = line.match(/^Sizes?\s*:\s*(.+)$/i);
      if (match) addSizeText(match[1]);
    }
    for (const match of text.matchAll(/\bSizes?\s*:\s*(.+?)(?=\s+(?:Quantity|Qty|Price|Sizes?)\s*:|$)/gi)) {
      addSizeText(match[1]);
    }
  }
  return values;
}

function parseStockIssueProductBlock(block, tabNumber) {
  const rawLines = String(block.innerText || block.textContent || "").split(/\n+/).map(stockIssueCleanText).filter(Boolean);
  const text = stockIssueCleanText(rawLines.join(" "));
  const quantityMatch = text.match(/\bTotal Quantity\s*:?\s*(\d+)/i);
  const totalQuantity = quantityMatch ? Number(quantityMatch[1]) : null;
  if (totalQuantity !== null && totalQuantity <= 0) return null;

  const supplierPattern = /^(?:-|–|—)?\s*(?:Alpha(?: Stock)?|SanMar(?: Stock)?|S&S(?: Activewear)?(?: Stock)?|Supplier|Stock Source)\s*$/i;
  const cleanProductText = (value) => stockIssueCleanText(value)
    .replace(/\s*-\s*(?:Alpha(?: Stock)?|SanMar(?: Stock)?|S&S(?: Activewear)?(?: Stock)?).*$/i, "")
    .trim();
  const productLinks = Array.from(block.querySelectorAll("a"))
    .filter(visibleStockIssueElement)
    .map((link) => cleanProductText(link.innerText || link.textContent))
    .filter((value) => value && !supplierPattern.test(value) && !/^(?:check stock|edit|remove|view)$/i.test(value));

  let style = "";
  let description = "";
  for (const candidate of productLinks.concat(rawLines.map(cleanProductText))) {
    const match = candidate.match(/^([A-Z0-9][A-Z0-9.-]{1,24})\s*(?:[-–—:]\s*|\s+)(.+)$/i);
    if (!match) continue;
    const possibleDescription = cleanProductText(match[2])
      .replace(/\s+(?:Color|Total Quantity|Size|Quantity|Price)\s*:.*$/i, "")
      .trim();
    if (!possibleDescription || /^(?:stock|qty|quantity|price|size)\b/i.test(possibleDescription)) continue;
    style = match[1].toUpperCase();
    description = possibleDescription;
    break;
  }
  if (!style) {
    const styleMatch = text.match(/\b([A-Z0-9][A-Z0-9.-]{2,20})\b(?=\s+[A-Z][A-Za-z])/);
    if (styleMatch) style = styleMatch[1].toUpperCase();
  }
  if (style && !description) {
    const escapedStyle = style.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const descriptionMatch = text.match(new RegExp(`\\b${escapedStyle}\\b\\s+(.+?)(?=\\s+-?\\s*(?:Alpha|SanMar|S&S|Supplier)|\\s+Color\\s*:|\\s+Total Quantity|\\s+Size\\s*:|\\s+Quantity\\s*:|$)`, "i"));
    if (descriptionMatch) description = cleanProductText(descriptionMatch[1]);
  }

  let color = "";
  const explicitColor = text.match(/\bColor\b\s*:?\s*(.+?)(?=\s+Total Quantity|\s+Size\s*:|\s+Quantity\s*:|\s+Price\s*:|$)/i);
  if (explicitColor) color = stockIssueCleanText(explicitColor[1]);
  if (!color) {
    const colorControl = Array.from(block.querySelectorAll("select,input,[data-color],[ng-model*='color' i]"))
      .find(visibleStockIssueElement);
    if (colorControl) {
      const selected = colorControl.options && colorControl.selectedIndex >= 0 ? colorControl.options[colorControl.selectedIndex] : null;
      color = stockIssueCleanText(
        (selected && (selected.text || selected.label || selected.value))
        || colorControl.value
        || colorControl.getAttribute("data-color")
      );
    }
  }
  if (!color) {
    // CRM renders the product link, supplier annotation, and color as adjacent
    // text. Read only the value between the supplier and Total Quantity instead
    // of treating that whole combined line as the color.
    const supplierColor = text.match(
      /(?:^|\s)-?\s*(?:Alpha(?: Stock)?|SanMar(?: Stock)?|S&S(?: Activewear)?(?: Stock)?|Supplier|Stock Source)\s+(.+?)(?=\s+Total Quantity|\s+Size\s*:|\s+Quantity\s*:|\s+Price\s*:|$)/i
    );
    if (supplierColor) color = stockIssueCleanText(supplierColor[1]);
  }
  if (!color) {
    const quantityLineIndex = rawLines.findIndex((line) => /Total Quantity/i.test(line));
    const preceding = rawLines.slice(Math.max(0, quantityLineIndex - 5), quantityLineIndex < 0 ? rawLines.length : quantityLineIndex).reverse();
    color = preceding.find((line) => (
      line.length <= 80
      && !supplierPattern.test(line)
      && !productLinks.includes(cleanProductText(line))
      && !/^\s*(?:Color|Total Quantity|Size|Quantity|Price|Check Stock)\b/i.test(line)
      && !/\$\s*\d/.test(line)
      && stockIssueCleanText(line).toLowerCase() !== stockIssueCleanText(description).toLowerCase()
      && stockIssueCleanText(line).toLowerCase() !== stockIssueCleanText(style).toLowerCase()
    )) || "";
  }
  color = stockIssueCleanText(color)
    .replace(/^Color\s*:?\s*/i, "")
    .replace(/^\s*-?\s*(?:Alpha(?: Stock)?|SanMar(?: Stock)?|S&S(?: Activewear)?(?: Stock)?|Supplier|Stock Source)\s+/i, "");
  description = cleanProductText(description);
  const availableSizes = stockIssueDetectedSizes(block, rawLines, text);
  if (!style || !description || !color) {
    return { invalid: true, design_item_id: block.id || "", style, description, color, total_quantity: totalQuantity };
  }
  return {
    tab_number: Number(tabNumber),
    design_item_id: block.id || "",
    style,
    description,
    color,
    available_sizes: availableSizes,
    total_quantity: totalQuantity
  };
}

function scanCurrentStockIssueDesignTab(tabNumber) {
  const blocks = Array.from(document.querySelectorAll("#design-items-list [id^='design-item-']"))
    .filter(visibleStockIssueElement);
  const seenIds = new Set();
  const products = [];
  for (const block of blocks) {
    if (seenIds.has(block.id)) continue;
    seenIds.add(block.id);
    const product = parseStockIssueProductBlock(block, tabNumber);
    if (product) products.push(product);
  }
  return products;
}

function deduplicateStockIssueProducts(products) {
  const unique = new Map();
  for (const product of products) {
    const key = [product.style, product.description, product.color]
      .map((value) => stockIssueCleanText(value).toLowerCase()).join("\u0000");
    if (!unique.has(key)) {
      unique.set(key, {
        ...product,
        tab_numbers: [product.tab_number],
        design_item_ids: product.design_item_id ? [product.design_item_id] : []
      });
      continue;
    }
    const existing = unique.get(key);
    if (!existing.tab_numbers.includes(product.tab_number)) existing.tab_numbers.push(product.tab_number);
    if (product.design_item_id && !existing.design_item_ids.includes(product.design_item_id)) existing.design_item_ids.push(product.design_item_id);
    if (Number.isInteger(product.total_quantity)) {
      existing.total_quantity = Number(existing.total_quantity || 0) + product.total_quantity;
    }
    for (const size of product.available_sizes || []) {
      if (!existing.available_sizes.some((item) => item.toLowerCase() === size.toLowerCase())) {
        existing.available_sizes.push(size);
      }
    }
  }
  return Array.from(unique.values());
}

async function scanAllStockIssueProducts(onProgress = () => {}) {
  const initialTabs = visibleStockIssueDesignTabs();
  const originalTabNumber = activeStockIssueDesignTabNumber(initialTabs);
  if (initialTabs.length > 1 && originalTabNumber === null) {
    throw new Error("Could not determine the active design tab, so the scan was stopped before changing tabs.");
  }
  const tabs = initialTabs.length ? initialTabs : [{ tabNumber: 1 }];
  const found = [];
  const invalid = [];
  let scanError = null;
  let restoreError = null;
  try {
    for (let index = 0; index < tabs.length; index += 1) {
      const tabNumber = tabs[index].tabNumber;
      onProgress(`Scanning design tab ${index + 1} of ${tabs.length}…`);
      if (initialTabs.length) {
        clickStockIssueDesignTab(tabNumber);
        await stockIssueDelay(750);
      }
      for (const product of scanCurrentStockIssueDesignTab(tabNumber)) {
        if (product.invalid) invalid.push(product);
        else found.push(product);
      }
    }
  } catch (error) {
    scanError = error;
  } finally {
    if (originalTabNumber !== null && initialTabs.length) {
      try {
        clickStockIssueDesignTab(originalTabNumber);
        await stockIssueDelay(500);
      } catch (error) {
        restoreError = error;
      }
    }
  }
  if (restoreError) {
    throw new Error(`Products were scanned, but the original design tab could not be restored: ${restoreError.message || restoreError}`);
  }
  if (scanError) throw scanError;
  if (invalid.length) {
    const ids = invalid.map((product) => product.design_item_id || "unknown design item").slice(0, 5).join(", ");
    throw new Error(`Could not safely read product ID, description, and color for: ${ids}.`);
  }
  return deduplicateStockIssueProducts(found);
}

function createStockIssueDialogShell(label) {
  document.getElementById("crm-stock-issue-dialog")?.remove();
  const overlay = document.createElement("div");
  overlay.id = "crm-stock-issue-dialog";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", label);
  Object.assign(overlay.style, {
    position: "fixed", zIndex: "2147483647", inset: "0", display: "flex", alignItems: "center", justifyContent: "center",
    padding: "20px", background: "rgba(15,23,42,.56)", font: "14px system-ui, sans-serif"
  });
  const dialog = document.createElement("div");
  Object.assign(dialog.style, {
    width: "min(560px, 100%)", maxHeight: "min(720px, calc(100vh - 40px))", overflowY: "auto", padding: "20px",
    borderRadius: "7px", color: "#0f172a", background: "#fff", boxShadow: "0 20px 45px rgba(15,23,42,.34)"
  });
  overlay.append(dialog);
  document.body.append(overlay);
  return { overlay, dialog };
}

function validateStockIssueExtensionDays(value) {
  const text = String(value ?? "").trim();
  if (!/^[1-9]\d*$/.test(text)) {
    return { valid: false, days: null, message: "Extension days must be a positive whole number." };
  }
  const days = Number(text);
  if (!Number.isSafeInteger(days)) {
    return { valid: false, days: null, message: "Extension days must be a positive whole number." };
  }
  if (days > 365) {
    return { valid: false, days: null, message: "Extension days cannot exceed 365." };
  }
  return { valid: true, days, message: "" };
}

function validateStockIssueSuggestedValues(value, singular) {
  const plural = `${singular}s`;
  const text = String(value ?? "").trim();
  if (!text) {
    return { valid: false, values: [], message: `Enter at least one suggested ${singular}.` };
  }
  if (text.length > 500) {
    return { valid: false, values: [], message: `The suggested ${singular} list is too long.` };
  }
  const rawValues = text.split(",");
  if (rawValues.length > 20) {
    return { valid: false, values: [], message: `Enter no more than 20 suggested ${plural}.` };
  }
  if (rawValues.some((item) => !stockIssueCleanText(item))) {
    return { valid: false, values: [], message: `List ${plural} separated by commas, without empty entries.` };
  }
  const values = [];
  const seen = new Set();
  for (const rawValue of rawValues) {
    const item = stockIssueCleanText(rawValue);
    if (item.length > 80 || /[<>\r\n]/.test(item)) {
      return { valid: false, values: [], message: `Each suggested ${singular} must be plain text under 80 characters.` };
    }
    const key = item.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      values.push(item);
    }
  }
  return { valid: true, values, message: "" };
}

function validateStockIssueSuggestedColors(value) {
  const result = validateStockIssueSuggestedValues(value, "color");
  return { ...result, colors: result.values };
}

function validateStockIssueSuggestedSizes(value) {
  const result = validateStockIssueSuggestedValues(value, "size");
  return { ...result, sizes: result.values };
}

function showStockIssueProductDialog(products, automation, triggerButton, statusButton) {
  const isColorSuggestion = automation.key === "stock_issue_color";
  const isSizeSuggestion = automation.key === "stock_issue_size";
  const isSuggestion = isColorSuggestion || isSizeSuggestion;
  const isExtension = automation.key === "stock_issue_extension";
  const selectsSizes = isSizeSuggestion || isExtension;
  const suggestionLabel = isSizeSuggestion ? "size" : "color";
  const dialogName = isSuggestion ? `Suggest Different ${suggestionLabel[0].toUpperCase()}${suggestionLabel.slice(1)}` : "Extension Required";
  const { overlay, dialog } = createStockIssueDialogShell(`Configure Stock Issue ${dialogName}`);
  const title = document.createElement("div");
  title.textContent = dialogName;
  Object.assign(title.style, { font: "700 17px system-ui, sans-serif", marginBottom: "6px" });
  const explanation = document.createElement("p");
  explanation.textContent = isSuggestion
    ? `Select each out-of-stock product/color, then list the available replacement ${suggestionLabel}s separated by commas.`
    : "Select each product/color that needs an extension, then enter the number of days. All order sizes start selected; use Clear to choose specific sizes.";
  Object.assign(explanation.style, { margin: "0 0 14px", lineHeight: "1.45" });
  dialog.append(title, explanation);

  const table = document.createElement("table");
  Object.assign(table.style, { width: "100%", borderCollapse: "collapse", marginBottom: "16px" });
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["", "Product ID", "Description", "Color"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    Object.assign(cell.style, { padding: "7px", borderBottom: "1px solid #94a3b8", textAlign: "left", fontWeight: "700" });
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);
  const body = document.createElement("tbody");
  const checkboxes = [];
  const sizeCheckboxesByProduct = new Map();
  const clearSizeButtons = [];
  products.forEach((product, index) => {
    const row = document.createElement("tr");
    const checkCell = document.createElement("td");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = String(index);
    checkbox.checked = products.length === 1;
    checkbox.setAttribute("aria-label", `Select ${product.style} in ${product.color}`);
    checkCell.append(checkbox);
    const styleCell = document.createElement("td");
    styleCell.textContent = product.style;
    const descriptionCell = document.createElement("td");
    descriptionCell.textContent = product.description;
    const colorCell = document.createElement("td");
    colorCell.textContent = product.color;
    for (const cell of [checkCell, styleCell, descriptionCell, colorCell]) {
      Object.assign(cell.style, { padding: "8px 7px", borderBottom: "1px solid #e2e8f0" });
    }
    Object.assign(styleCell.style, { whiteSpace: "nowrap" });
    row.append(checkCell, styleCell, descriptionCell, colorCell);
    body.append(row);
    checkboxes.push(checkbox);
    if (selectsSizes) {
      const sizeRow = document.createElement("tr");
      sizeRow.hidden = !checkbox.checked;
      const sizeCell = document.createElement("td");
      sizeCell.colSpan = 4;
      Object.assign(sizeCell.style, { padding: "7px 7px 11px 38px", background: "#f8fafc", borderBottom: "1px solid #e2e8f0" });
      const sizePrompt = document.createElement("div");
      sizePrompt.textContent = "Affected sizes (select at least one)";
      Object.assign(sizePrompt.style, { marginBottom: "6px", fontWeight: "700" });
      const sizes = Array.isArray(product.available_sizes) ? product.available_sizes : [];
      const sizeChoices = [];
      if (sizes.length) {
        const choices = document.createElement("div");
        Object.assign(choices.style, { display: "flex", flexWrap: "wrap", gap: "8px 12px" });
        sizes.forEach((size, sizeIndex) => {
          const label = document.createElement("label");
          const sizeCheckbox = document.createElement("input");
          sizeCheckbox.type = "checkbox";
          sizeCheckbox.value = size;
          sizeCheckbox.checked = isExtension || (checkbox.checked && sizes.length === 1);
          sizeCheckbox.id = `crm-stock-issue-size-${index}-${sizeIndex}`;
          sizeCheckbox.setAttribute("aria-label", `Select ${size} for ${product.style} in ${product.color}`);
          label.htmlFor = sizeCheckbox.id;
          label.append(sizeCheckbox, document.createTextNode(` ${size}`));
          choices.append(label);
          sizeChoices.push(sizeCheckbox);
        });
        sizeCell.append(sizePrompt, choices);
        if (isExtension) {
          const clear = document.createElement("button");
          clear.type = "button";
          clear.textContent = "Clear";
          clear.setAttribute("aria-label", `Clear sizes for ${product.style} in ${product.color}`);
          Object.assign(clear.style, { marginTop: "8px", padding: "4px 9px", cursor: "pointer" });
          clear.addEventListener("click", () => {
            if (submitting) return;
            sizeChoices.forEach((choice) => { choice.checked = false; });
            refreshAfterEdit();
          });
          sizeCell.append(clear);
          clearSizeButtons.push(clear);
        }
      } else {
        const unavailable = document.createElement("div");
        unavailable.textContent = "No order sizes could be detected for this product. It cannot be selected for this workflow.";
        unavailable.style.color = "var(--crm-stock-error-color, #b91c1c)";
        sizeCell.append(sizePrompt, unavailable);
      }
      sizeRow.append(sizeCell);
      body.append(sizeRow);
      sizeCheckboxesByProduct.set(checkbox, sizeChoices);
      checkbox.addEventListener("change", () => {
        sizeRow.hidden = !checkbox.checked;
        if (isSizeSuggestion) {
          if (!checkbox.checked) sizeChoices.forEach((choice) => { choice.checked = false; });
          else if (sizeChoices.length === 1) sizeChoices[0].checked = true;
        }
      });
    }
  });
  table.append(body);
  dialog.append(table);

  const inputLabel = document.createElement("label");
  inputLabel.textContent = isSuggestion ? `Suggested ${suggestionLabel}s (required)` : "Extension days (required)";
  inputLabel.htmlFor = isSuggestion ? `crm-stock-issue-${suggestionLabel}s` : "crm-stock-issue-days";
  Object.assign(inputLabel.style, { display: "block", marginBottom: "5px", fontWeight: "700" });
  const detailInput = document.createElement("input");
  detailInput.id = inputLabel.htmlFor;
  detailInput.type = isSuggestion ? "text" : "number";
  if (isSuggestion) {
    detailInput.placeholder = isSizeSuggestion ? "Example: Small, Medium, Large" : "Example: Navy, Black, White";
    detailInput.autocomplete = "off";
  } else {
    detailInput.min = "1";
    detailInput.max = "365";
    detailInput.step = "1";
    detailInput.inputMode = "numeric";
  }
  detailInput.required = true;
  Object.assign(detailInput.style, {
    width: isSuggestion ? "min(100%, 420px)" : "120px",
    boxSizing: "border-box", padding: "8px", border: "1px solid #64748b", borderRadius: "3px"
  });
  dialog.append(inputLabel, detailInput);

  const validation = document.createElement("div");
  validation.id = "crm-stock-issue-validation";
  validation.setAttribute("role", "status");
  validation.setAttribute("aria-live", "polite");
  detailInput.setAttribute("aria-describedby", validation.id);
  Object.assign(validation.style, { minHeight: "18px", marginTop: "8px", color: "#b91c1c", fontWeight: "600" });
  dialog.append(validation);
  const actions = document.createElement("div");
  Object.assign(actions.style, { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "12px" });
  const back = document.createElement("button");
  back.type = "button";
  back.textContent = "Back";
  const queue = document.createElement("button");
  queue.type = "button";
  queue.textContent = "Queue task";
  Object.assign(back.style, { padding: "7px 11px", cursor: "pointer" });
  Object.assign(queue.style, { padding: "7px 11px", borderRadius: "3px", color: "#fff" });
  let submitting = false;
  let submissionError = "";

  const refresh = () => {
    const selectedCheckboxes = checkboxes.filter((checkbox) => checkbox.checked);
    const selected = selectedCheckboxes.length;
    const inputValidation = isColorSuggestion
      ? validateStockIssueSuggestedColors(detailInput.value)
      : isSizeSuggestion
      ? validateStockIssueSuggestedSizes(detailInput.value)
      : validateStockIssueExtensionDays(detailInput.value);
    const errors = [];
    if (!selected) errors.push("Select at least one product.");
    const productsMissingSizes = selectsSizes && selectedCheckboxes.some(
      (checkbox) => !(sizeCheckboxesByProduct.get(checkbox) || []).some((choice) => choice.checked)
    );
    if (productsMissingSizes) errors.push("Select at least one affected size for every selected product.");
    if (!inputValidation.valid) errors.push(inputValidation.message);
    if (submissionError) errors.push(submissionError);
    const enabled = !submitting && selected > 0 && !productsMissingSizes && inputValidation.valid;
    validation.textContent = submitting ? `Sending ${automation.label} to the Automation queue…` : errors.join(" ");
    validation.style.color = submitting
      ? "var(--crm-stock-progress-color, #334155)"
      : "var(--crm-stock-error-color, #b91c1c)";
    detailInput.setAttribute("aria-invalid", String(!inputValidation.valid));
    detailInput.disabled = submitting;
    checkboxes.forEach((checkbox) => { checkbox.disabled = submitting; });
    sizeCheckboxesByProduct.forEach((choices) => choices.forEach((choice) => { choice.disabled = submitting; }));
    clearSizeButtons.forEach((button) => { button.disabled = submitting; });
    back.disabled = submitting;
    queue.disabled = !enabled;
    queue.setAttribute("aria-disabled", String(!enabled));
    queue.textContent = submitting ? "Queuing…" : "Queue task";
    queue.style.setProperty("background", enabled ? "#15803d" : "#9ca3af", "important");
    queue.style.setProperty("border", `1px solid ${enabled ? "#166534" : "#6b7280"}`, "important");
    queue.style.setProperty("cursor", enabled ? "pointer" : "not-allowed", "important");
  };
  const refreshAfterEdit = () => {
    submissionError = "";
    refresh();
  };
  checkboxes.forEach((checkbox) => checkbox.addEventListener("change", refreshAfterEdit));
  sizeCheckboxesByProduct.forEach((choices) => choices.forEach((choice) => choice.addEventListener("change", refreshAfterEdit)));
  detailInput.addEventListener("input", refreshAfterEdit);
  back.addEventListener("click", () => { if (!submitting) overlay.remove(); });
  queue.addEventListener("click", () => {
    if (submitting) return;
    const selectedProducts = checkboxes
      .filter((checkbox) => checkbox.checked)
      .map((checkbox) => {
        const product = products[Number(checkbox.value)];
        if (!selectsSizes) return product;
        return {
          ...product,
          affected_sizes: (sizeCheckboxesByProduct.get(checkbox) || [])
            .filter((choice) => choice.checked)
            .map((choice) => choice.value)
        };
      });
    const inputValidation = isColorSuggestion
      ? validateStockIssueSuggestedColors(detailInput.value)
      : isSizeSuggestion
      ? validateStockIssueSuggestedSizes(detailInput.value)
      : validateStockIssueExtensionDays(detailInput.value);
    const missingSelectedSizes = selectsSizes && selectedProducts.some((product) => !product.affected_sizes.length);
    if (!selectedProducts.length || missingSelectedSizes || !inputValidation.valid) {
      refresh();
      return;
    }
    const structuredData = isColorSuggestion
      ? { colors: inputValidation.colors, products: selectedProducts }
      : isSizeSuggestion
      ? { sizes: inputValidation.sizes, products: selectedProducts }
      : { days: inputValidation.days, products: selectedProducts };
    submissionError = "";
    submitting = true;
    refresh();
    queueManualOrderAutomation(
      automation,
      triggerButton,
      statusButton,
      "",
      structuredData,
      { surfacePageErrors: false }
    ).then((response) => {
      if (response && response.success) {
        overlay.remove();
        return;
      }
      submitting = false;
      submissionError = (response && response.message) || "Could not queue the selected automation.";
      refresh();
      detailInput.focus();
    });
  });
  // This dialog is intentionally non-dismissible via the backdrop or Escape.
  // Back is the explicit way to abandon the configured request.
  actions.append(back, queue);
  dialog.append(actions);
  refresh();
  detailInput.focus();
}

async function startStockIssueExtensionSelection(automation, triggerButton, statusButton) {
  const { overlay, dialog } = createStockIssueDialogShell("Scanning Stock Issue products");
  const title = document.createElement("div");
  title.textContent = "Scanning products…";
  Object.assign(title.style, { font: "700 17px system-ui, sans-serif", marginBottom: "8px" });
  const progress = document.createElement("p");
  progress.textContent = "Reading the order's design tabs.";
  Object.assign(progress.style, { margin: "0", lineHeight: "1.45" });
  dialog.append(title, progress);
  setOrderProcessorControlsDisabled(true);
  try {
    const products = await scanAllStockIssueProducts((message) => { progress.textContent = message; });
    overlay.remove();
    setOrderProcessorControlsDisabled(false);
    if (!products.length) throw new Error("No positive-quantity products were found on this order.");
    showStockIssueProductDialog(products, automation, triggerButton, statusButton);
  } catch (error) {
    overlay.remove();
    setOrderProcessorControlsDisabled(false);
    const message = error && error.message ? error.message : "The order products could not be scanned.";
    triggerButton.title = message;
    setOrderProcessorResult(triggerButton, message, "error");
  }
}

function readCopyrightDesignMetadata() {
  return new Promise((resolve, reject) => {
    const id = `${Date.now()}-${Math.random()}`;
    const receive = event => {
      let response;
      try { response = JSON.parse(event.detail); } catch (_) { return; }
      if (response.id !== id) return;
      clearTimeout(timer);
      document.removeEventListener('crm-copyright-designs-response', receive);
      if (response.error) reject(new Error(response.error)); else resolve(response.scan);
    };
    const timer = setTimeout(() => {
      document.removeEventListener('crm-copyright-designs-response', receive);
      reject(new Error('Reload CRM Order Assistant and refresh this order to scan copyright designs.'));
    }, 5000);
    document.addEventListener('crm-copyright-designs-response', receive);
    document.dispatchEvent(new CustomEvent('crm-copyright-designs-request', { detail: id }));
  });
}

function copyrightDesignSelectionId(design) {
  return design.order_design_id ? `order-design:${design.order_design_id}` : design.design_id;
}

async function scanCopyrightDesigns(onProgress = () => {}) {
  const metadata = await readCopyrightDesignMetadata();
  const tabs = visibleStockIssueDesignTabs();
  const original = activeStockIssueDesignTabNumber(tabs);
  if (!metadata?.designs?.length || !/^\d{7}$/.test(metadata.order_id) || original === null ||
      tabs.length !== metadata.designs.length ||
      new Set(metadata.designs.map(copyrightDesignSelectionId)).size !== tabs.length ||
      new Set(metadata.designs.map(design => Boolean(design.order_design_id))).size !== 1 ||
      metadata.designs.some(design => !/^[1-9]\d*$/.test(design.design_id) ||
        (design.order_design_id !== undefined && !/^[1-9]\d*$/.test(design.order_design_id)) ||
        !tabs.some(tab => tab.tabNumber === design.tab_number))) {
    throw new Error('Could not identify every design tab. Refresh the order and try again.');
  }
  const allDesigns = [];
  try {
    for (const design of metadata.designs) {
      onProgress(`Reading Design Name for tab ${design.tab_number} of ${tabs.length}…`);
      clickStockIssueDesignTab(design.tab_number);
      await stockIssueDelay(750);
      let previous = '';
      let name = '';
      for (let attempt = 0; attempt < 30; attempt += 1) {
        const text = document.body.innerText || '';
        const candidate = stockIssueCleanText(text.match(/Design Name:\s*([^|\n\r]+)/i)?.[1]);
        const id = stockIssueCleanText(text.match(/Design ID:\s*([^|\n\r]+)/i)?.[1]);
        if (candidate && candidate === previous && id === design.design_id &&
            activeStockIssueDesignTabNumber(visibleStockIssueDesignTabs()) === design.tab_number) {
          name = candidate;
          break;
        }
        previous = candidate;
        await stockIssueDelay(200);
      }
      const total = (document.body.innerText || '').match(new RegExp(
        `\\bDesign\\s+${design.tab_number}\\s+Total\\s*:\\s*(\\d+)\\s+pieces?\\s*\\$?(-?[0-9,]+\\.\\d{2})`, 'i'
      ));
      if (!name || !total) throw new Error(`Could not verify the name, quantity, and total for tab ${design.tab_number}.`);
      allDesigns.push({
        tab_number: design.tab_number, design_id: design.design_id, design_name: name,
        ...(design.order_design_id ? { order_design_id: design.order_design_id } : {}),
        quantity: Number(total[1]), subtotal: Number(total[2].replaceAll(',', '')).toFixed(2)
      });
    }
    const after = await readCopyrightDesignMetadata();
    if (JSON.stringify(after) !== JSON.stringify(metadata)) {
      throw new Error('The order designs changed during the scan. Refresh and select them again.');
    }
  } finally {
    clickStockIssueDesignTab(original);
    await stockIssueDelay(500);
  }
  return { order_id: metadata.order_id, all_designs: allDesigns };
}

function showCopyrightCancelConfirmation(automation, triggerButton, statusButton) {
  document.getElementById('crm-copyright-cancel-dialog')?.remove();
  const { overlay, dialog } = createStockIssueDialogShell('Copyright - Cancel');
  overlay.id = 'crm-copyright-cancel-dialog';
  overlay.style.boxSizing = 'border-box';
  Object.assign(dialog.style, { boxSizing: 'border-box', minWidth: '0', overflowX: 'hidden', overflowWrap: 'anywhere' });
  const title = document.createElement('h3');
  title.textContent = 'Copyright - Cancel';
  title.style.marginTop = '0';
  const picker = document.createElement('div');
  picker.textContent = 'Scanning order designs…';
  const summary = document.createElement('p');
  summary.setAttribute('role', 'status');
  summary.dataset.copyrightSummary = 'true';
  const fullCancellation = document.createElement('span');
  fullCancellation.textContent = 'Full cancellation';
  fullCancellation.style.setProperty('color', 'var(--crm-stock-error-color, #b91c1c)', 'important');
  const label = document.createElement('label');
  label.textContent = 'Reason (required)';
  label.htmlFor = 'crm-copyright-cancel-reason';
  const reason = document.createElement('textarea');
  reason.id = label.htmlFor;
  reason.rows = 3;
  reason.required = true;
  Object.assign(reason.style, { display: 'block', width: '100%', minWidth: '0', maxWidth: '100%', boxSizing: 'border-box', marginTop: '6px', resize: 'vertical' });
  const actions = document.createElement('div');
  Object.assign(actions.style, { display: 'flex', justifyContent: 'flex-end', gap: '8px', marginTop: '16px' });
  const back = document.createElement('button');
  back.type = 'button';
  back.textContent = 'Back';
  back.addEventListener('click', () => overlay.remove());
  const queue = document.createElement('button');
  queue.type = 'button';
  queue.textContent = 'Queue task';
  queue.disabled = true;
  let scan = null;
  const selectedIds = () => Array.from(picker.querySelectorAll('input[data-copyright-design]:checked'))
    .map(input => input.dataset.copyrightDesign);
  const refresh = () => {
    const count = selectedIds().length;
    queue.disabled = !scan || !count || !reason.value.trim();
    if (!scan) return;
    summary.textContent = `${count} of ${scan.all_designs.length} tabs selected.`;
    if (count === scan.all_designs.length) summary.append(' ', fullCancellation);
    else if (count) summary.append(' Partial cancellation: only these designs will be removed and refunded. The remaining order will continue.');
  };
  reason.addEventListener('input', refresh);
  queue.addEventListener('click', () => {
    if (queue.disabled || !scan) return;
    const designs = { ...scan, selected_design_ids: selectedIds() };
    queue.disabled = true;
    overlay.remove();
    queueManualOrderAutomation(automation, triggerButton, statusButton, reason.value.trim(), { designs });
  });
  actions.append(back, queue);
  dialog.append(title, picker, summary, label, reason, actions);
  setOrderProcessorControlsDisabled(true);
  scanCopyrightDesigns(message => { picker.textContent = message; }).then(result => {
    if (!overlay.isConnected) return;
    scan = result;
    picker.replaceChildren();
    const toggles = document.createElement('div');
    for (const [text, checked] of [['Select all', true], ['Clear', false]]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = text;
      button.addEventListener('click', () => {
        for (const input of picker.querySelectorAll('input[data-copyright-design]')) input.checked = checked;
        refresh();
      });
      toggles.append(button);
    }
    picker.append(toggles);
    for (const design of scan.all_designs) {
      const row = document.createElement('label');
      Object.assign(row.style, { display: 'block', marginTop: '10px' });
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.dataset.copyrightDesign = copyrightDesignSelectionId(design);
      input.checked = scan.all_designs.length === 1;
      input.addEventListener('change', refresh);
      row.append(input, document.createTextNode(` Tab ${design.tab_number}: ${design.design_name} (QTY ${design.quantity})`));
      picker.append(row);
    }
    refresh();
  }).catch(error => {
    if (overlay.isConnected) picker.textContent = error.message;
  }).finally(() => setOrderProcessorControlsDisabled(false));
  reason.focus();
}

function readComplicatedEmbMethods() {
  return new Promise((resolve, reject) => {
    const id = `${Date.now()}-${Math.random()}`;
    const finish = (error, designs) => {
      clearTimeout(timer);
      document.removeEventListener('crm-emb-methods-response', receive);
      if (error) reject(new Error(error)); else resolve(designs);
    };
    const receive = event => {
      let result;
      try { result = JSON.parse(event.detail); } catch (_) { return; }
      if (result.id === id) finish(result.error, result.designs);
    };
    const timer = setTimeout(() => finish('Reload the CRM Order Assistant extension and refresh this order to scan designs.'), 5000);
    document.addEventListener('crm-emb-methods-response', receive);
    document.dispatchEvent(new CustomEvent('crm-emb-methods-request', { detail: id }));
  });
}

async function scanComplicatedEmbDesigns(onProgress, includePrintAreas = false) {
  const tabs = visibleStockIssueDesignTabs();
  const original = activeStockIssueDesignTabNumber(tabs);
  if (!tabs.length || original === null) throw new Error('Could not identify the order design tabs. Refresh the order and try again.');
  const methods = await readComplicatedEmbMethods();
  const found = [];
  try {
    for (const tab of tabs) {
      const metadata = methods.find(item => item.tab_number === tab.tabNumber);
      if (metadata?.available === false) continue;
      onProgress(`Reading design tab ${tab.tabNumber} of ${tabs.length}...`);
      clickStockIssueDesignTab(tab.tabNumber);
      await stockIssueDelay(750);
      let previous = '';
      let name = '';
      for (let attempt = 0; attempt < 30; attempt++) {
        const text = document.body.innerText || '';
        const match = text.match(/Design Name:\s*([^|\n\r]+)/i);
        name = stockIssueCleanText(match?.[1]);
        if (name && name === previous && activeStockIssueDesignTabNumber(visibleStockIssueDesignTabs()) === tab.tabNumber) break;
        previous = name;
        name = '';
        await stockIssueDelay(200);
      }
      if (!metadata || !metadata.methods.length || metadata.methods.some(method => !method)) {
        throw new Error(`Could not determine the print method for tab ${tab.tabNumber}.`);
      }
      if (metadata.eligible) {
        if (!name) throw new Error(`Could not read Design Name for design tab ${tab.tabNumber}.`);
        const design = { tab_number: tab.tabNumber, design_name: name };
        if (includePrintAreas) {
          const areas = (metadata.print_areas || []).map(area => area.description);
          if (!areas.length || areas.some(area => !area) || new Set(areas.map(area => area.toLowerCase())).size !== areas.length) {
            throw new Error(`Could not identify distinct print areas for tab ${tab.tabNumber}.`);
          }
          design.print_areas = areas;
        }
        found.push(design);
      }
    }
  } finally {
    clickStockIssueDesignTab(original);
    await stockIssueDelay(750);
  }
  if (!found.length) throw new Error('No available design tabs were found on this order.');
  return found;
}

async function scanOversizeEmbDesigns(onProgress) {
  return scanComplicatedEmbDesigns(onProgress, true);
}

async function scanComplicatedEmbPrintDesigns(onProgress) {
  return scanComplicatedEmbDesigns(onProgress, true);
}

function showOrderAutomationConfirmation(automation, triggerButton, statusButton) {
  if (automation.key === 'copyright_cancel') {
    showCopyrightCancelConfirmation(automation, triggerButton, statusButton);
    return;
  }
  document.getElementById("crm-order-automation-confirmation")?.remove();
  const asksFeedback = automation.key === "complicated_emb_to_hdd";
  const isOversizeEmb = automation.key === "oversize_emb_to_hdd";
  const selectsPrintAreas = asksFeedback || isOversizeEmb;
  const selectsDesigns = asksFeedback || selectsPrintAreas;
  const requiresReason = automation.requiresReason === true;
  const overlay = document.createElement("div");
  overlay.id = "crm-order-automation-confirmation";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", asksFeedback ? "Feedback required?" : `Confirm ${automation.label}`);
  Object.assign(overlay.style, {
    position: "fixed", zIndex: "2147483647", inset: "0", display: "flex", alignItems: "center", justifyContent: "center",
    padding: "20px", background: "rgba(15,23,42,.56)", font: "14px system-ui, sans-serif"
  });
  const dialog = document.createElement("div");
  Object.assign(dialog.style, {
    width: "min(430px, 100%)", padding: "20px", borderRadius: "7px", color: "#0f172a", background: "#fff",
    boxShadow: "0 20px 45px rgba(15,23,42,.34)"
  });
  dialog.style.maxHeight = '85vh';
  dialog.style.overflowY = 'auto';
  const title = document.createElement("div");
  title.textContent = asksFeedback ? "Feedback required?" : `Queue ${automation.label}?`;
  Object.assign(title.style, { font: "700 17px system-ui, sans-serif", marginBottom: "8px" });
  const explanation = document.createElement("p");
  explanation.textContent = asksFeedback
    ? "Yes: request embroidery feedback. No: use the existing EMB to HDD process."
    : `This will queue ${automation.label} for the currently open order.`;
  Object.assign(explanation.style, { margin: "0 0 14px", lineHeight: "1.45" });
  dialog.append(title, explanation);
  let embroideryDesigns = null;
  const selectedDesigns = () => {
    if (!selectsPrintAreas) return Array.from(dialog.querySelectorAll('input[data-emb-tab]:checked'))
      .map(input => embroideryDesigns.find(design => design.tab_number === Number(input.dataset.embTab)));
    return (embroideryDesigns || []).map(design => ({
      ...design,
      print_areas: Array.from(dialog.querySelectorAll(`input[data-emb-tab="${design.tab_number}"][data-emb-area]:checked`))
        .map(input => design.print_areas[Number(input.dataset.embArea)])
    })).filter(design => design.print_areas.length);
  };
  const designPicker = document.createElement('div');
  if (selectsDesigns) {
    title.textContent = automation.label;
    designPicker.textContent = 'Scanning order designs...';
    dialog.append(designPicker);
  }
  let reasonInput = null;
  if (requiresReason) {
    const label = document.createElement("label");
    label.textContent = "Reason (required)";
    label.htmlFor = "crm-order-automation-reason";
    Object.assign(label.style, { display: "block", marginBottom: "5px", fontWeight: "700" });
    reasonInput = document.createElement("textarea");
    reasonInput.id = "crm-order-automation-reason";
    reasonInput.rows = 4;
    reasonInput.required = true;
    reasonInput.setAttribute("aria-required", "true");
    reasonInput.placeholder = "Enter the reason to continue";
    Object.assign(reasonInput.style, {
      width: "100%", boxSizing: "border-box", resize: "vertical", padding: "8px", border: "1px solid #64748b", borderRadius: "3px",
      font: "14px system-ui, sans-serif"
    });
    dialog.append(label, reasonInput);
  }
  const actions = document.createElement("div");
  Object.assign(actions.style, { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "18px" });
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.textContent = "Back";
  const continueButton = document.createElement("button");
  continueButton.type = "button";
  continueButton.textContent = asksFeedback ? "Yes" : "Queue task";
  Object.assign(cancel.style, { padding: "7px 11px", cursor: "pointer" });
  Object.assign(continueButton.style, {
    padding: "7px 11px", borderRadius: "3px", color: "#fff"
  });
  const refreshContinueButtonState = () => {
    const enabled = (!requiresReason || Boolean(String(reasonInput?.value || "").trim())) &&
      (!selectsDesigns || (embroideryDesigns !== null && selectedDesigns().length > 0));
    continueButton.disabled = !enabled;
    const noButton = actions.querySelector('[data-emb-no]');
    if (noButton) noButton.disabled = !enabled;
    continueButton.setAttribute("aria-disabled", String(!enabled));
    continueButton.style.setProperty("background", enabled ? "#15803d" : "#9ca3af", "important");
    continueButton.style.setProperty("border", `1px solid ${enabled ? "#166534" : "#6b7280"}`, "important");
    continueButton.style.setProperty("cursor", enabled ? "pointer" : "not-allowed", "important");
  };
  refreshContinueButtonState();
  cancel.addEventListener("click", () => overlay.remove());
  continueButton.addEventListener("click", () => {
    const reason = String(reasonInput?.value || "").trim();
    if (requiresReason && !reason) return;
    if (selectsDesigns && (!embroideryDesigns || !selectedDesigns().length)) return;
    const designs = selectsDesigns ? selectedDesigns() : null;
    overlay.remove();
    const selectedAutomation = asksFeedback
      ? { key: "complicated_emb_feedback", label: "Complicated EMB (Feedback)" }
      : automation;
    queueManualOrderAutomation(selectedAutomation, triggerButton, statusButton, reason, designs ? { designs } : {});
  });
  reasonInput?.addEventListener("input", refreshContinueButtonState);
  // Keep the confirmation open until the user clicks Back or queues the task.
  actions.append(cancel, continueButton);
  if (asksFeedback) {
    const no = document.createElement("button");
    no.type = "button";
    no.textContent = "No";
    no.dataset.embNo = 'true';
    no.disabled = true;
    Object.assign(no.style, { padding: "7px 11px", cursor: "pointer" });
    no.addEventListener("click", () => {
      if (!embroideryDesigns || !selectedDesigns().length) return;
      const designs = selectedDesigns();
      overlay.remove();
      queueManualOrderAutomation(automation, triggerButton, statusButton, '', { designs });
    });
    actions.append(no);
  }
  dialog.append(actions);
  overlay.append(dialog);
  document.body.append(overlay);
  if (selectsDesigns) {
    const scan = asksFeedback ? scanComplicatedEmbPrintDesigns : scanOversizeEmbDesigns;
    scan(message => { designPicker.textContent = message; }).then(designs => {
      if (!overlay.isConnected) return;
      embroideryDesigns = designs;
      designPicker.textContent = selectsPrintAreas ? 'Select the affected print area(s) for each design:' : 'Select the design(s) for Complicated EMB:';
      for (const design of designs) {
        if (selectsPrintAreas) {
          const heading = document.createElement('div');
          heading.textContent = `Tab ${design.tab_number}: ${design.design_name}`;
          Object.assign(heading.style, { marginTop: '12px', fontWeight: '700' });
          designPicker.append(heading);
        }
        for (const [areaIndex, area] of (selectsPrintAreas ? design.print_areas.entries() : [[null, null]])) {
          const label = document.createElement('label');
          Object.assign(label.style, { display: 'block', marginTop: '8px' });
          const input = document.createElement('input');
          input.type = 'checkbox';
          input.dataset.embTab = String(design.tab_number);
          if (selectsPrintAreas) input.dataset.embArea = String(areaIndex);
          input.checked = isOversizeEmb
            ? /^(front|back)$/i.test(area.trim())
            : designs.length === 1 && (!selectsPrintAreas || design.print_areas.length === 1);
          input.addEventListener('change', refreshContinueButtonState);
          label.append(input, document.createTextNode(selectsPrintAreas ? ` ${area}` : ` Tab ${design.tab_number}: ${design.design_name}`));
          designPicker.append(label);
        }
      }
      refreshContinueButtonState();
    }).catch(error => { designPicker.textContent = error.message; });
  }
  if (reasonInput) reasonInput.focus(); else continueButton.focus();
}

function createOrderProcessMenuControl({ controlId, buttonId, label, title, color, border, automations, needsConfirmation, onSelect }) {
  const control = document.createElement("span");
  control.id = controlId;
  control.dataset.crmOrderMenuControl = "true";
  Object.assign(control.style, { position: "relative", display: "inline-block", height: "32px", verticalAlign: "top" });
  const button = document.createElement("button");
  button.type = "button";
  button.id = buttonId;
  button.dataset.crmOrderProcessTrigger = "true";
  button.textContent = label;
  button.title = title;
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  Object.assign(button.style, {
    display: "block", boxSizing: "border-box", height: "32px", padding: "6px 12px", borderRadius: "2px", cursor: "pointer",
    font: "600 12px/18px system-ui, sans-serif", color: "#fff", background: color, border: `1px solid ${border}`
  });
  const menu = document.createElement("div");
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", `${label} automation choices`);
  Object.assign(menu.style, {
    position: "absolute", zIndex: "2147483647", top: "calc(100% + 4px)", left: "0", minWidth: "205px",
    padding: "4px", borderRadius: "4px", background: "#fff", border: "1px solid #94a3b8", boxShadow: "0 6px 18px rgba(15,23,42,.22)"
  });
  for (const automation of automations) {
    const option = document.createElement("button");
    option.type = "button";
    option.textContent = automation.label;
    option.setAttribute("role", "menuitem");
    Object.assign(option.style, {
      display: "block", width: "100%", padding: "7px 9px", border: "0", borderRadius: "3px", cursor: "pointer",
      textAlign: "left", color: "#0f172a", background: "transparent", font: "600 12px system-ui, sans-serif"
    });
    option.addEventListener("mouseenter", () => { option.style.background = "#e0f2fe"; });
    option.addEventListener("mouseleave", () => { option.style.background = "transparent"; });
    option.addEventListener("click", () => {
      closeOrderProcessMenu(control);
      if (typeof onSelect === "function") onSelect(automation, button, manualOrderProcessorButton());
      else if (needsConfirmation) showOrderAutomationConfirmation(automation, button, manualOrderProcessorButton());
      else queueManualOrderAutomation(automation, button, manualOrderProcessorButton());
    });
    menu.appendChild(option);
  }
  button.addEventListener("click", () => {
    const opening = menu.hidden;
    closeAllOrderProcessMenus(control);
    menu.hidden = !opening;
    button.setAttribute("aria-expanded", String(opening));
  });
  control.append(button, menu);
  return control;
}

function ensureManualOrderProcessorControl(anchor) {
  let control = document.getElementById("crm-order-manual-process-control");
  if (!control) {
    control = createOrderProcessMenuControl({
      controlId: "crm-order-manual-process-control", buttonId: "crm-order-manual-process-button", label: "Manual Process",
      title: "Choose one automation to run for this order only.", color: "#475569", border: "#334155",
      automations: MANUAL_ORDER_AUTOMATIONS, needsConfirmation: false,
      onSelect: (automation, triggerButton, statusButton) => {
        if (automation.key === "sleeve_prints") {
          showSleevePrintsDialog(automation, triggerButton, statusButton);
          return;
        }
        queueManualOrderAutomation(automation, triggerButton, statusButton);
      }
    });
    control.style.marginLeft = "4px";
  }
  if (anchor.nextElementSibling !== control) anchor.insertAdjacentElement("afterend", control);
  return control;
}

function findNativeEditOrderButton() {
  return Array.from(document.querySelectorAll("button.edit-btn[ng-click='editModeOn();']"))
    .find((button) => crmControlLabel(button) === "edit order");
}

function findAddAccountButton() {
  const contactPanel = document.getElementById("contact-panel");
  if (!contactPanel) return null;
  return Array.from(contactPanel.querySelectorAll("button[ng-click='addAccount()']"))
    .find((button) => crmControlLabel(button) === "add account");
}

function ensureConfirmedOrderControl({ controlId, buttonId, label, title, color, border, automations, anchor, onSelect }) {
  let control = document.getElementById(controlId);
  if (!anchor) {
    control?.remove();
    return null;
  }
  if (!control) {
    control = createOrderProcessMenuControl({ controlId, buttonId, label, title, color, border, automations, needsConfirmation: true, onSelect });
    control.style.marginRight = "4px";
  }
  if (anchor.previousElementSibling !== control) anchor.insertAdjacentElement("beforebegin", control);
  return control;
}

function ensureSingleOrderSheetScannerControls() {
  const reachoutControl = ensureConfirmedOrderControl({
    controlId: "crm-order-cancel-control", buttonId: "crm-order-cancel-button", label: "Cancel",
    title: "Choose a cancellation workflow for this order.", color: "#b91c1c", border: "#991b1b",
    automations: CANCEL_ORDER_AUTOMATIONS, anchor: findNativeEditOrderButton()
  });
  ensureConfirmedOrderControl({
    controlId: "crm-order-reachout-control", buttonId: "crm-order-reachout-button", label: "Reachout",
    title: "Choose a customer-reachout workflow for this order.", color: "#15803d", border: "#166534",
    automations: REACHOUT_ORDER_AUTOMATIONS, anchor: findAddAccountButton()
  });
  ensureConfirmedOrderControl({
    controlId: "crm-order-stock-issue-control", buttonId: "crm-order-stock-issue-button", label: "Stock Issue",
    title: "Choose a stock-issue workflow for this order.", color: "#a16207", border: "#854d0e",
    automations: STOCK_ISSUE_AUTOMATIONS, anchor: reachoutControl,
    onSelect: (automation, triggerButton, statusButton) => {
      void startStockIssueExtensionSelection(automation, triggerButton, statusButton);
    }
  });
}

function ensureOrderProcessorControls() {
  if (!document.body) return;
  const sendInvoiceButton = isOrderDocument() ? findSendInvoiceButton() : null;
  if (!sendInvoiceButton) {
    document.getElementById("crm-order-manual-process-control")?.remove();
    document.getElementById("crm-order-cancel-control")?.remove();
    document.getElementById("crm-order-reachout-control")?.remove();
    document.getElementById("crm-order-stock-issue-control")?.remove();
    document.getElementById("crm-order-automation-result")?.remove();
    stopOrderProcessorPolling();
    return;
  }

  const existingControl = document.getElementById("crm-order-manual-process-control");
  ensureManualOrderProcessorControl(sendInvoiceButton);
  ensureSingleOrderSheetScannerControls();
  if (!existingControl) void loadOrderProcessorStatus(manualOrderProcessorButton());
}

function isOrderDocument() {
  const path = window.location.pathname || "";
  const hash = window.location.hash || "";
  if (/^\/order\/[^/?#]+\/?$/.test(path)) return true;
  return path === "/app" && /^#\/?order\/[^/?#]+\/?(?:[?#].*)?$/.test(hash);
}

function applyThemeState() {
  const root = document.documentElement;
  if (!root) return;
  const active = themeEnabled && isOrderDocument();
  root.classList.toggle(ROOT_CLASS, active);
  root.setAttribute(ROOT_ATTRIBUTE, active ? "enabled" : "disabled");
  ensureOrderProcessorControls();
}

function queueThemeRefresh() {
  if (refreshQueued) return;
  refreshQueued = true;
  requestAnimationFrame(() => {
    refreshQueued = false;
    applyThemeState();
  });
}

async function loadThemePreference() {
  try {
    const response = await chrome.runtime.sendMessage({ type: "crm-dark-mode:get-theme" });
    themeEnabled = response && response.enabled === true;
  } catch (_error) {
    const values = await chrome.storage.local.get(THEME_STORAGE_KEY);
    themeEnabled = values[THEME_STORAGE_KEY] === true;
  }
  applyThemeState();
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.type !== "string") return false;
  if (message.type === "crm-dark-mode:get-page-state") {
    sendResponse({
      enabled: themeEnabled,
      isOrderDocument: isOrderDocument(),
      active: themeEnabled && isOrderDocument()
    });
    return false;
  }
  if (message.type === "crm-dark-mode:refresh-page") {
    queueThemeRefresh();
    sendResponse({ success: true });
    return false;
  }
  return false;
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "local" || !changes[THEME_STORAGE_KEY]) return;
  themeEnabled = changes[THEME_STORAGE_KEY].newValue === true;
  applyThemeState();
});

window.addEventListener("hashchange", queueThemeRefresh, true);
window.addEventListener("popstate", queueThemeRefresh, true);

// CSS covers added nodes; this keeps the route gate correct after legacy CRM
// AJAX/Angular updates replace parts of the order UI in place.
const observer = new MutationObserver(queueThemeRefresh);
observer.observe(document.documentElement, { childList: true, subtree: true });

void loadThemePreference();
