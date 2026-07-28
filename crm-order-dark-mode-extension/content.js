const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";
const ROOT_CLASS = "crm-order-dark-mode";
const ROOT_ATTRIBUTE = "data-crm-order-dark-mode";

let themeEnabled = false;
let refreshQueued = false;
let orderProcessorPollTimer = null;

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
  { key: "push_back", label: "Push Back" }
];

const CANCEL_ORDER_AUTOMATIONS = [
  { key: "copyright_cancel", label: "Copyright - Cancel", requiresReason: true },
  { key: "content_violation_cancel", label: "Content Violation - Cancel" },
  { key: "existing_designs_cancel", label: "CANCEL - Existing Designs" },
  { key: "outside_limit_cancel", label: "CANCEL - Outside Limit" }
];

const REACHOUT_ORDER_AUTOMATIONS = [
  { key: "complicated_emb_to_hdd", label: "Complicated EMB to HDD" },
  { key: "oversize_emb_to_hdd", label: "Oversize EMB to HDD" },
  { key: "copyright_removal", label: "Copyright Removal", requiresReason: true },
  { key: "copyright_reachout", label: "Copyright - Reachout", requiresReason: true }
];

function currentOrderId() {
  const match = `${window.location.pathname || ""}${window.location.hash || ""}`.match(/\/order\/(\d{7})\b/);
  return match ? match[1] : "";
}

function pageShowsShippingTooExpensive() {
  return /shipping\s+is\s+too\s+expensive/i.test(document.body && document.body.innerText || "");
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

function orderProcessorStageLabel(stage) {
  const labels = {
    queued: "Queued",
    address_validator: "Address",
    product_separator: "Separating",
    auto_splitter: "Splitting",
    order_goods: "Ordering goods",
    shipping_bypasser: "Shipping bypass"
  };
  return labels[String(stage || "")] || "Processing";
}

function setOrderProcessorButtonStyle(button, background, border) {
  button.style.setProperty("background", background, "important");
  button.style.setProperty("border", `1px solid ${border}`, "important");
}

function manualOrderProcessorButton() {
  return document.getElementById("crm-order-manual-process-button");
}

function setOrderProcessorControlsDisabled(disabled) {
  const autoProcessButton = document.getElementById("crm-order-automation-button");
  const manualProcessButton = manualOrderProcessorButton();
  if (autoProcessButton) autoProcessButton.disabled = disabled;
  if (manualProcessButton) manualProcessButton.disabled = disabled;
  document.querySelectorAll("[data-crm-order-process-trigger='true']").forEach((button) => {
    button.disabled = disabled;
  });
  if (disabled) {
    document.querySelectorAll("[data-crm-order-menu-control='true']").forEach(closeOrderProcessMenu);
  }
}

function orderProcessorResultSummary(runtime) {
  const steps = Array.isArray(runtime && runtime.steps) ? runtime.steps : [];
  if (!steps.length) return String((runtime && runtime.lastMessage) || "");
  const used = steps.filter((step) => step && step.success && !step.skipped)
    .map((step) => String(step.label || step.key || "Used"));
  const skipped = steps.filter((step) => step && step.skipped)
    .map((step) => String(step.label || step.key || "Skipped"));
  const parts = [];
  if (used.length) parts.push(`Used: ${used.join(", ")}.`);
  if (skipped.length) parts.push(`Not needed: ${skipped.join(", ")}.`);
  return parts.join(" ") || String((runtime && runtime.lastMessage) || "");
}

function refreshOrderAfterProcessorCompletion(runtime) {
  if (!runtime || runtime.queued || runtime.running || runtime.lastSuccess === null || runtime.lastSuccess === undefined) return;
  const orderId = currentOrderId();
  const completedAt = String(runtime.completedAt || "");
  if (!orderId || !completedAt || String(runtime.orderId || "") !== orderId) return;
  const activeRunKey = `crm-auto-process-active:${orderId}`;
  if (!sessionStorage.getItem(activeRunKey)) return;
  const refreshKey = `crm-auto-process-refresh:${orderId}:${completedAt}`;
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
  // Keep the result after the complete action group, rather than between its
  // Auto-Process and Manual Process buttons.
  const manualControl = document.getElementById("crm-order-manual-process-control");
  const resultAnchor = manualControl || button;
  if (result.previousElementSibling !== resultAnchor) resultAnchor.insertAdjacentElement("afterend", result);
  result.textContent = text;
  result.style.color = tone === "error" ? "#b91c1c" : (tone === "success" ? "#15803d" : "#075985");
}

function renderOrderProcessorStatus(button, response) {
  const runtime = response && response.runtime;
  if (!runtime || (runtime.orderId && runtime.orderId !== currentOrderId())) return false;
  const message = String(runtime.lastMessage || "");
  const manualProcess = runtime.runKind === "manual";
  const manualButton = manualOrderProcessorButton();
  button.title = message || "Validate address, separate products, split over 10 tabs, unlock/order goods, and bypass flagged shipping.";
  if (runtime.queued) {
    setOrderProcessorControlsDisabled(true);
    if (manualProcess && manualButton) {
      manualButton.textContent = "Manual Process: Queued";
      manualButton.title = message || "Queued behind any active CRM automation.";
      setOrderProcessorResult(manualButton, message || "Queued behind any active CRM automation.", "progress");
      return true;
    }
    button.dataset.autoProcessState = "running";
    button.textContent = "Auto-Process: Queued";
    setOrderProcessorButtonStyle(button, "#0369a1", "#075985");
    setOrderProcessorResult(button, "Queued behind any active CRM automation.", "progress");
    return true;
  }
  if (runtime.running) {
    setOrderProcessorControlsDisabled(true);
    if (manualProcess && manualButton) {
      manualButton.textContent = "Manual Process: Working";
      manualButton.title = message || "Working…";
      setOrderProcessorResult(manualButton, message || "Working…", "progress");
      return true;
    }
    button.dataset.autoProcessState = "running";
    button.textContent = `Auto-Process: ${orderProcessorStageLabel(runtime.currentStep)}`;
    setOrderProcessorButtonStyle(button, "#0369a1", "#075985");
    setOrderProcessorResult(button, message || "Working…", "progress");
    return true;
  }
  setOrderProcessorControlsDisabled(false);
  if (manualProcess && manualButton) {
    manualButton.textContent = runtime.lastSuccess === false ? "Manual Process: Review" : "Manual Process";
    manualButton.title = message || "Choose one automation to run for this order only.";
    setOrderProcessorResult(manualButton, message, runtime.lastSuccess === false ? "error" : "success");
    return false;
  }
  if (runtime.lastSuccess === true) {
    button.dataset.autoProcessState = "complete";
    const skipped = Array.isArray(runtime.steps) && runtime.steps.some((step) => step && step.skipped);
    button.textContent = skipped ? "Auto-Process: Complete" : "Auto-Process: Done";
    setOrderProcessorButtonStyle(button, skipped ? "#a16207" : "#15803d", skipped ? "#854d0e" : "#166534");
    setOrderProcessorResult(button, orderProcessorResultSummary(runtime), "success");
  } else if (runtime.lastSuccess === false) {
    button.dataset.autoProcessState = "review";
    button.textContent = "Auto-Process: Review";
    setOrderProcessorButtonStyle(button, "#b91c1c", "#991b1b");
    setOrderProcessorResult(button, message || "Processing stopped and needs review.", "error");
  } else {
    delete button.dataset.autoProcessState;
    button.textContent = "Auto-Process";
    setOrderProcessorButtonStyle(button, "#0369a1", "#075985");
    setOrderProcessorResult(button, "", "progress");
  }
  refreshOrderAfterProcessorCompletion(runtime);
  return false;
}

function beginOrderProcessorPolling(button) {
  stopOrderProcessorPolling();
  const poll = async () => {
    try {
      const response = await chrome.runtime.sendMessage({ type: "crm-order-automation:status" });
      if (!response || !response.success) {
        button.dataset.autoProcessState = "review";
        setOrderProcessorControlsDisabled(false);
        button.textContent = "Auto-Process: Review";
        button.title = (response && response.message) || "Could not read the local Automation app status.";
        setOrderProcessorButtonStyle(button, "#b91c1c", "#991b1b");
        setOrderProcessorResult(button, button.title, "error");
        stopOrderProcessorPolling();
        return;
      }
      if (!renderOrderProcessorStatus(button, response)) stopOrderProcessorPolling();
    } catch (_error) {
      button.dataset.autoProcessState = "review";
      setOrderProcessorControlsDisabled(false);
      button.textContent = "Auto-Process: Review";
      button.title = "Could not read the local Automation app status. Confirm the local app is running, then try again.";
      setOrderProcessorButtonStyle(button, "#b91c1c", "#991b1b");
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
    // The local app may not be running yet; the button remains available to start a run.
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

function queueManualOrderAutomation(automation, triggerButton, autoProcessButton, reason = "") {
  const orderId = currentOrderId();
  if (!orderId) return;
  const control = triggerButton.closest("[data-crm-order-menu-control='true']");
  closeOrderProcessMenu(control);
  setOrderProcessorControlsDisabled(true);
  const isManualButton = triggerButton.id === "crm-order-manual-process-button";
  if (isManualButton) triggerButton.textContent = `Queuing ${automation.label}…`;
  setOrderProcessorResult(triggerButton, `Sending ${automation.label} for order ${orderId} to the CRM automation queue…`, "progress");
  chrome.runtime.sendMessage({
    type: "crm-order-automation:manual-start",
    orderId,
    automation: automation.key,
    reason
  }).then((response) => {
    if (response && response.success) {
      renderOrderProcessorStatus(autoProcessButton, response);
      beginOrderProcessorPolling(autoProcessButton);
      return;
    }
    setOrderProcessorControlsDisabled(false);
    if (isManualButton) triggerButton.textContent = "Manual Process";
    triggerButton.title = (response && response.message) || "Could not queue the selected automation.";
    setOrderProcessorResult(triggerButton, triggerButton.title, "error");
  }).catch(() => {
    setOrderProcessorControlsDisabled(false);
    if (isManualButton) triggerButton.textContent = "Manual Process";
    triggerButton.title = "Could not queue the selected automation. Confirm the local Automation app is running, then try again.";
    setOrderProcessorResult(triggerButton, triggerButton.title, "error");
  });
}

function showOrderAutomationConfirmation(automation, triggerButton, autoProcessButton) {
  document.getElementById("crm-order-automation-confirmation")?.remove();
  const requiresReason = automation.requiresReason === true;
  const overlay = document.createElement("div");
  overlay.id = "crm-order-automation-confirmation";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", `Confirm ${automation.label}`);
  Object.assign(overlay.style, {
    position: "fixed", zIndex: "2147483647", inset: "0", display: "flex", alignItems: "center", justifyContent: "center",
    padding: "20px", background: "rgba(15,23,42,.56)", font: "14px system-ui, sans-serif"
  });
  const dialog = document.createElement("div");
  Object.assign(dialog.style, {
    width: "min(430px, 100%)", padding: "20px", borderRadius: "7px", color: "#0f172a", background: "#fff",
    boxShadow: "0 20px 45px rgba(15,23,42,.34)"
  });
  const title = document.createElement("div");
  title.textContent = `Queue ${automation.label}?`;
  Object.assign(title.style, { font: "700 17px system-ui, sans-serif", marginBottom: "8px" });
  const explanation = document.createElement("p");
  explanation.textContent = `This will queue ${automation.label} for the currently open order.`;
  Object.assign(explanation.style, { margin: "0 0 14px", lineHeight: "1.45" });
  dialog.append(title, explanation);
  let reasonInput = null;
  if (requiresReason) {
    const label = document.createElement("label");
    label.textContent = "Reason (required)";
    label.htmlFor = "crm-order-automation-reason";
    Object.assign(label.style, { display: "block", marginBottom: "5px", fontWeight: "700" });
    reasonInput = document.createElement("textarea");
    reasonInput.id = "crm-order-automation-reason";
    reasonInput.rows = 4;
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
  continueButton.textContent = "Queue task";
  continueButton.disabled = requiresReason;
  Object.assign(cancel.style, { padding: "7px 11px", cursor: "pointer" });
  Object.assign(continueButton.style, {
    padding: "7px 11px", border: "1px solid #166534", borderRadius: "3px", color: "#fff", background: "#15803d", cursor: "pointer"
  });
  cancel.addEventListener("click", () => overlay.remove());
  continueButton.addEventListener("click", () => {
    const reason = String(reasonInput?.value || "").trim();
    if (requiresReason && !reason) return;
    overlay.remove();
    queueManualOrderAutomation(automation, triggerButton, autoProcessButton, reason);
  });
  reasonInput?.addEventListener("input", () => { continueButton.disabled = !reasonInput.value.trim(); });
  overlay.addEventListener("click", (event) => { if (event.target === overlay) overlay.remove(); });
  overlay.addEventListener("keydown", (event) => { if (event.key === "Escape") overlay.remove(); });
  actions.append(cancel, continueButton);
  dialog.append(actions);
  overlay.append(dialog);
  document.body.append(overlay);
  if (reasonInput) reasonInput.focus(); else continueButton.focus();
}

function createOrderProcessMenuControl({ controlId, buttonId, label, title, color, border, automations, needsConfirmation }) {
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
      if (needsConfirmation) showOrderAutomationConfirmation(automation, button, document.getElementById("crm-order-automation-button"));
      else queueManualOrderAutomation(automation, button, document.getElementById("crm-order-automation-button"));
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

function ensureManualOrderProcessorControl(autoProcessButton) {
  let control = document.getElementById("crm-order-manual-process-control");
  if (!control) {
    control = createOrderProcessMenuControl({
      controlId: "crm-order-manual-process-control", buttonId: "crm-order-manual-process-button", label: "Manual Process",
      title: "Choose one automation to run for this order only.", color: "#475569", border: "#334155",
      automations: MANUAL_ORDER_AUTOMATIONS, needsConfirmation: false
    });
    control.style.marginLeft = "4px";
  }
  if (autoProcessButton.nextElementSibling !== control) autoProcessButton.insertAdjacentElement("afterend", control);
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

function ensureConfirmedOrderControl({ controlId, buttonId, label, title, color, border, automations, anchor }) {
  let control = document.getElementById(controlId);
  if (!anchor) {
    control?.remove();
    return null;
  }
  if (!control) {
    control = createOrderProcessMenuControl({ controlId, buttonId, label, title, color, border, automations, needsConfirmation: true });
    control.style.marginRight = "4px";
  }
  if (anchor.previousElementSibling !== control) anchor.insertAdjacentElement("beforebegin", control);
  return control;
}

function ensureSingleOrderSheetScannerControls() {
  ensureConfirmedOrderControl({
    controlId: "crm-order-cancel-control", buttonId: "crm-order-cancel-button", label: "Cancel",
    title: "Choose a cancellation workflow for this order.", color: "#b91c1c", border: "#991b1b",
    automations: CANCEL_ORDER_AUTOMATIONS, anchor: findNativeEditOrderButton()
  });
  ensureConfirmedOrderControl({
    controlId: "crm-order-reachout-control", buttonId: "crm-order-reachout-button", label: "Reachout",
    title: "Choose a customer-reachout workflow for this order.", color: "#15803d", border: "#166534",
    automations: REACHOUT_ORDER_AUTOMATIONS, anchor: findAddAccountButton()
  });
}

function ensureOrderProcessorButton() {
  if (!isOrderDocument() || !document.body) return;
  const sendInvoiceButton = findSendInvoiceButton();
  const existingButton = document.getElementById("crm-order-automation-button");
  if (!sendInvoiceButton) {
    existingButton?.remove();
    document.getElementById("crm-order-manual-process-control")?.remove();
    document.getElementById("crm-order-cancel-control")?.remove();
    document.getElementById("crm-order-reachout-control")?.remove();
    stopOrderProcessorPolling();
    return;
  }

  const button = existingButton || document.createElement("button");
  if (!existingButton) {
    button.id = "crm-order-automation-button";
    button.type = "button";
    button.textContent = "Auto-Process";
    button.title = "Validate address, separate products, split over 10 tabs, unlock/order goods, and bypass flagged shipping.";
    button.addEventListener("click", async () => {
      const orderId = currentOrderId();
      if (!orderId) return;
      setOrderProcessorControlsDisabled(true);
      button.textContent = "Starting…";
      setOrderProcessorResult(button, "Sending the order to the CRM automation queue…", "progress");
      try {
        const response = await chrome.runtime.sendMessage({
          type: "crm-order-automation:start",
          orderId,
          shippingTooExpensive: pageShowsShippingTooExpensive()
        });
        if (response && response.success) {
          sessionStorage.setItem(`crm-auto-process-active:${orderId}`, "1");
          renderOrderProcessorStatus(button, response);
          beginOrderProcessorPolling(button);
        } else {
          button.dataset.autoProcessState = "review";
          setOrderProcessorControlsDisabled(false);
          button.textContent = "Auto-Process: Review";
          button.title = (response && response.message) || "Could not queue the order in the local Automation app.";
          setOrderProcessorButtonStyle(button, "#b91c1c", "#991b1b");
          setOrderProcessorResult(button, button.title, "error");
        }
      } catch (_error) {
        button.dataset.autoProcessState = "review";
        setOrderProcessorControlsDisabled(false);
        button.textContent = "Auto-Process: Review";
        button.title = "Could not queue the order in the local Automation app.";
        setOrderProcessorButtonStyle(button, "#b91c1c", "#991b1b");
        setOrderProcessorResult(button, button.title, "error");
      }
    });
  }

  Object.assign(button.style, {
    position: "static", display: "inline-block", boxSizing: "border-box", height: "32px", marginLeft: "4px",
    padding: "6px 12px", borderRadius: "2px", verticalAlign: "top", font: "600 12px/18px system-ui, sans-serif", cursor: "pointer"
  });
  if (!button.dataset.autoProcessState) setOrderProcessorButtonStyle(button, "#0369a1", "#075985");
  button.style.setProperty("color", "#fff", "important");
  if (sendInvoiceButton.nextElementSibling !== button) sendInvoiceButton.insertAdjacentElement("afterend", button);
  ensureManualOrderProcessorControl(button);
  ensureSingleOrderSheetScannerControls();
  if (!existingButton) void loadOrderProcessorStatus(button);
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
  ensureOrderProcessorButton();
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
  if (message.type === "crm-order-automation:get-order-context") {
    sendResponse({
      isOrderDocument: isOrderDocument(),
      orderId: currentOrderId(),
      shippingTooExpensive: pageShowsShippingTooExpensive()
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
