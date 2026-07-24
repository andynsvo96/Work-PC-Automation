import {
  getLocalBridgeStatus,
  getLocalOrderProcessingStatus,
  pairLocalBridge,
  startLocalOrderProcessing
} from "./bridge.js";

export const THEME_STORAGE_KEY = "crmOrderDarkModeEnabled";
export const BRIDGE_TOKEN_STORAGE_KEY = "crmOrderAutomationBridgeToken";

async function getThemeEnabled() {
  const values = await chrome.storage.local.get(THEME_STORAGE_KEY);
  return values[THEME_STORAGE_KEY] === true;
}

async function setThemeEnabled(enabled) {
  const value = enabled === true;
  await chrome.storage.local.set({ [THEME_STORAGE_KEY]: value });
  return value;
}

async function getBridgeToken() {
  const values = await chrome.storage.local.get(BRIDGE_TOKEN_STORAGE_KEY);
  return String(values[BRIDGE_TOKEN_STORAGE_KEY] || "");
}

async function clearBridgeToken() {
  await chrome.storage.local.remove(BRIDGE_TOKEN_STORAGE_KEY);
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
  if (message.type === "crm-order-automation:pair") {
    pairLocalBridge(message.pin)
      .then(async (response) => {
        if (!response || !response.token) throw new Error((response && response.message) || "Pairing did not return a token.");
        await chrome.storage.local.set({ [BRIDGE_TOKEN_STORAGE_KEY]: response.token });
        sendResponse({ success: true, message: response.message });
      })
      .catch((error) => sendResponse({ success: false, message: error.message || "Could not pair the extension." }));
    return true;
  }
  if (message.type === "crm-order-automation:start") {
    getBridgeToken()
      .then((token) => {
        if (!token) throw new Error("Pair the extension with the local Automation app first.");
        return startLocalOrderProcessing(token, message.orderId, message.shippingTooExpensive);
      })
      .then(sendResponse)
      .catch(async (error) => {
        if (/pair the extension|pair.*local app|401/i.test(String(error.message || ""))) await clearBridgeToken();
        sendResponse({ success: false, message: error.message || "Could not start processing." });
      });
    return true;
  }
  if (message.type === "crm-order-automation:status") {
    getBridgeToken()
      .then((token) => {
        if (!token) return { success: false, paired: false, message: "Pair the extension with the local Automation app first." };
        return getLocalOrderProcessingStatus(token);
      })
      .then(sendResponse)
      .catch(async (error) => {
        if (/pair the extension|pair.*local app|401/i.test(String(error.message || ""))) await clearBridgeToken();
        sendResponse({ success: false, message: error.message || "Could not load processing status." });
      });
    return true;
  }
  return false;
});
