const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";
const ROOT_CLASS = "crm-order-dark-mode";
const ROOT_ATTRIBUTE = "data-crm-order-dark-mode";

let themeEnabled = false;
let refreshQueued = false;
let orderProcessorPollTimer = null;

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
    button.insertAdjacentElement("afterend", result);
  }
  result.textContent = text;
  result.style.color = tone === "error" ? "#b91c1c" : (tone === "success" ? "#15803d" : "#075985");
}

function renderOrderProcessorStatus(button, response) {
  const runtime = response && response.runtime;
  if (!runtime || (runtime.orderId && runtime.orderId !== currentOrderId())) return false;
  const message = String(runtime.lastMessage || "");
  button.title = message || "Validate address, separate products, split over 10 tabs, unlock/order goods, and bypass flagged shipping.";
  if (runtime.queued) {
    button.dataset.autoProcessState = "running";
    button.disabled = true;
    button.textContent = "Auto-Process: Queued";
    setOrderProcessorButtonStyle(button, "#0369a1", "#075985");
    setOrderProcessorResult(button, "Queued behind any active CRM automation.", "progress");
    return true;
  }
  if (runtime.running) {
    button.dataset.autoProcessState = "running";
    button.disabled = true;
    button.textContent = `Auto-Process: ${orderProcessorStageLabel(runtime.currentStep)}`;
    setOrderProcessorButtonStyle(button, "#0369a1", "#075985");
    setOrderProcessorResult(button, message || "Working…", "progress");
    return true;
  }
  button.disabled = false;
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
        button.disabled = false;
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
      button.disabled = false;
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

function ensureOrderProcessorButton() {
  if (!isOrderDocument() || !document.body) return;
  const sendInvoiceButton = findSendInvoiceButton();
  const existingButton = document.getElementById("crm-order-automation-button");
  if (!sendInvoiceButton) {
    existingButton?.remove();
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
      button.disabled = true;
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
          button.disabled = false;
          button.textContent = "Auto-Process: Review";
          button.title = (response && response.message) || "Could not queue the order in the local Automation app.";
          setOrderProcessorButtonStyle(button, "#b91c1c", "#991b1b");
          setOrderProcessorResult(button, button.title, "error");
        }
      } catch (_error) {
        button.dataset.autoProcessState = "review";
        button.disabled = false;
        button.textContent = "Auto-Process: Review";
        button.title = "Could not queue the order in the local Automation app.";
        setOrderProcessorButtonStyle(button, "#b91c1c", "#991b1b");
        setOrderProcessorResult(button, button.title, "error");
      }
    });
  }

  Object.assign(button.style, {
    position: "static", marginLeft: "8px", padding: "6px 12px", minHeight: "30px",
    borderRadius: "2px", font: "600 12px system-ui, sans-serif", cursor: "pointer"
  });
  if (!button.dataset.autoProcessState) setOrderProcessorButtonStyle(button, "#0369a1", "#075985");
  button.style.setProperty("color", "#fff", "important");
  if (sendInvoiceButton.nextElementSibling !== button) sendInvoiceButton.insertAdjacentElement("afterend", button);
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
