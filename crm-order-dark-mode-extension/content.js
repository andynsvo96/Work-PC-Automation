const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";
const ROOT_CLASS = "crm-order-dark-mode";
const ROOT_ATTRIBUTE = "data-crm-order-dark-mode";

let themeEnabled = false;
let refreshQueued = false;

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

function ensureOrderProcessorButton() {
  if (!isOrderDocument() || !document.body) return;
  const sendInvoiceButton = findSendInvoiceButton();
  const existingButton = document.getElementById("crm-order-automation-button");
  if (!sendInvoiceButton) {
    existingButton?.remove();
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
      try {
        const response = await chrome.runtime.sendMessage({
          type: "crm-order-automation:start",
          orderId,
          shippingTooExpensive: pageShowsShippingTooExpensive()
        });
        button.textContent = response && response.success ? "Processing started" : "Pair in extension";
        if (!response || !response.success) button.title = (response && response.message) || "Pair the extension from its toolbar popup, then try again.";
      } catch (_error) {
        button.textContent = "Pair in extension";
        button.title = "Pair the extension from its toolbar popup, then try again.";
      } finally {
        setTimeout(() => { button.disabled = false; button.textContent = "Auto-Process"; }, 4000);
      }
    });
  }

  Object.assign(button.style, {
    position: "static", marginLeft: "8px", padding: "6px 12px", minHeight: "30px",
    borderRadius: "2px", font: "600 12px system-ui, sans-serif", cursor: "pointer"
  });
  button.style.setProperty("background", "#0369a1", "important");
  button.style.setProperty("border", "1px solid #075985", "important");
  button.style.setProperty("color", "#fff", "important");
  if (sendInvoiceButton.nextElementSibling !== button) sendInvoiceButton.insertAdjacentElement("afterend", button);
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
