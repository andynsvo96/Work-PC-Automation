import {
  getLocalBridgeStatus,
  getLocalOrderProcessingStatus,
  startLocalManualOrderProcessing,
  startLocalOrderProcessing
} from "./bridge.js";
import { isSalesforceUrl, rankSalesforceTabs } from "./salesforce-tabs.mjs";

export const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";

async function focusTabWindow(tab) {
  if (!tab || !Number.isInteger(tab.windowId)) return;
  try {
    const currentWindow = await chrome.windows.get(tab.windowId);
    const update = { focused: true };
    if (currentWindow && currentWindow.state === "minimized") update.state = "normal";
    await chrome.windows.update(tab.windowId, update);
  } catch (_error) {
    // The tab was still reused even if Chrome refused to focus its window.
  }
}

async function reuseSalesforceTab(url, options = {}) {
  if (!isSalesforceUrl(url)) {
    return { success: false, message: "The requested link is not a Salesforce URL." };
  }

  const tabs = await chrome.tabs.query({});
  const candidates = rankSalesforceTabs(tabs, options);
  for (const candidate of candidates) {
    try {
      const updated = await chrome.tabs.update(candidate.id, { url, active: true });
      await focusTabWindow(updated || candidate);
      return { success: true, reused: true, tabId: candidate.id };
    } catch (_error) {
      // A tab can disappear between query and update. Try the next match.
    }
  }

  if (options.createIfMissing !== true) {
    return { success: true, reused: false };
  }

  const createOptions = { url, active: true };
  if (Number.isInteger(options.preferredWindowId)) {
    createOptions.windowId = options.preferredWindowId;
  }
  const created = await chrome.tabs.create(createOptions);
  await focusTabWindow(created);
  return { success: true, reused: false, tabId: created && created.id };
}

async function replaceCreatedSalesforceTarget(details) {
  if (!details || !isSalesforceUrl(details.url)) return;
  let sourceTab = null;
  try {
    sourceTab = await chrome.tabs.get(details.sourceTabId);
  } catch (_error) {
    // The source can close immediately after launching the new target.
  }
  const result = await reuseSalesforceTab(details.url, {
    excludeTabId: details.tabId,
    preferredTabId: details.sourceTabId,
    preferredWindowId: sourceTab && sourceTab.windowId,
    createIfMissing: false
  });
  if (!result.reused) return;
  try {
    await chrome.tabs.remove(details.tabId);
  } catch (_error) {
    // The short-lived target may already have been closed by Chrome or the user.
  }
}

chrome.webNavigation.onCreatedNavigationTarget.addListener((details) => {
  void replaceCreatedSalesforceTarget(details).catch(() => {
    // Leave the newly-created target alone if tab discovery is unavailable.
  });
}, {
  url: [
    { hostSuffix: "salesforce.com" },
    { hostSuffix: "force.com" },
    { hostSuffix: "visualforce.com" }
  ]
});

async function getThemeEnabled() {
  const values = await chrome.storage.local.get(THEME_STORAGE_KEY);
  return values[THEME_STORAGE_KEY] === true;
}

async function setThemeEnabled(enabled) {
  const value = enabled === true;
  await chrome.storage.local.set({ [THEME_STORAGE_KEY]: value });
  return value;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.type !== "string") return false;
  if (message.type === "crm-dark-mode:get-theme") {
    getThemeEnabled().then((enabled) => sendResponse({ enabled }));
    return true;
  }
  if (message.type === "crm-dark-mode:set-theme") {
    setThemeEnabled(message.enabled).then((enabled) => sendResponse({ enabled }));
    return true;
  }
  if (message.type === "crm-dark-mode:get-bridge-status") {
    getLocalBridgeStatus().then(sendResponse);
    return true;
  }
  if (message.type === "crm-order-automation:start") {
    startLocalOrderProcessing(message.orderId, message.shippingTooExpensive)
      .then(sendResponse)
      .catch((error) => sendResponse({ success: false, message: error.message || "Could not queue processing." }));
    return true;
  }
  if (message.type === "crm-order-automation:manual-start") {
    startLocalManualOrderProcessing(message.orderId, message.automation, message.reason, {
      days: message.days,
      colors: message.colors,
      products: message.products
    })
      .then(sendResponse)
      .catch((error) => sendResponse({ success: false, message: error.message || "Could not queue manual processing." }));
    return true;
  }
  if (message.type === "crm-order-automation:status") {
    getLocalOrderProcessingStatus()
      .then(sendResponse)
      .catch((error) => sendResponse({ success: false, message: error.message || "Could not load processing status." }));
    return true;
  }
  if (message.type === "crm-salesforce:open-link") {
    const sourceTab = _sender && _sender.tab;
    reuseSalesforceTab(message.url, {
      preferredTabId: sourceTab && sourceTab.id,
      preferredWindowId: sourceTab && sourceTab.windowId,
      createIfMissing: true
    })
      .then(sendResponse)
      .catch((error) => sendResponse({
        success: false,
        message: error && error.message || "Could not open the Salesforce link."
      }));
    return true;
  }
  return false;
});
