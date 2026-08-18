import {
  getLocalBridgeStatus,
  getLocalOrderProcessingStatus,
  startLocalManualOrderProcessing,
  startLocalOrderProcessing
} from "./bridge.js";

export const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";

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
  return false;
});
