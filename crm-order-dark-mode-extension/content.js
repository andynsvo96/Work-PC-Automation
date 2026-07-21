const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";
const ROOT_CLASS = "crm-order-dark-mode";
const ROOT_ATTRIBUTE = "data-crm-order-dark-mode";

let themeEnabled = false;
let refreshQueued = false;

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
