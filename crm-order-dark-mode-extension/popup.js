const toggle = document.getElementById("theme-toggle");
const pageStatus = document.getElementById("page-status");
const bridgeStatus = document.getElementById("bridge-status");
const processorStatus = document.getElementById("processor-status");
const pairPin = document.getElementById("pair-pin");
const pairButton = document.getElementById("pair-button");
const processButton = document.getElementById("process-button");

function setStatus(message) { pageStatus.textContent = message; }

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}

async function refreshPageStatus(tab) {
  if (!tab || !tab.id) return setStatus("No active tab found.");
  try {
    const state = await chrome.tabs.sendMessage(tab.id, { type: "crm-dark-mode:get-page-state" });
    if (!state.isOrderDocument) return setStatus("Open a CRM order page to apply dark mode.");
    setStatus(state.active ? "Dark mode is active on this order." : "Dark mode is off for this order.");
  } catch (_error) {
    setStatus("Open a CRM order page to apply dark mode.");
  }
}

async function getOrderContext(tab) {
  if (!tab || !tab.id) return null;
  try {
    return await chrome.tabs.sendMessage(tab.id, { type: "crm-order-automation:get-order-context" });
  } catch (_error) {
    return null;
  }
}

async function refreshProcessorStatus() {
  const response = await chrome.runtime.sendMessage({ type: "crm-order-automation:status" });
  if (!response || !response.success) {
    processorStatus.textContent = (response && response.message) || "Pair the extension to enable order processing.";
    return;
  }
  const runtime = response.runtime || {};
  processorStatus.textContent = runtime.running
    ? `${runtime.currentStep || "Processing"}: ${runtime.lastMessage || "Working…"}`
    : (runtime.lastMessage || "Ready to process the current order.");
}

async function initialize() {
  const response = await chrome.runtime.sendMessage({ type: "crm-dark-mode:get-theme" });
  toggle.checked = response && response.enabled === true;
  const [bridge] = await Promise.all([
    chrome.runtime.sendMessage({ type: "crm-dark-mode:get-bridge-status" }),
    refreshPageStatus(await getActiveTab())
  ]);
  bridgeStatus.textContent = bridge && bridge.connected
    ? "Local Automation app bridge connected."
    : (bridge && bridge.message) || "Local Automation app bridge is unavailable.";
  await refreshProcessorStatus();
}

toggle.addEventListener("change", async () => {
  const response = await chrome.runtime.sendMessage({ type: "crm-dark-mode:set-theme", enabled: toggle.checked });
  toggle.checked = response && response.enabled === true;
  await refreshPageStatus(await getActiveTab());
});

pairButton.addEventListener("click", async () => {
  pairButton.disabled = true;
  processorStatus.textContent = "Pairing…";
  try {
    const response = await chrome.runtime.sendMessage({ type: "crm-order-automation:pair", pin: pairPin.value });
    processorStatus.textContent = (response && response.message) || "Extension paired.";
    if (response && response.success) pairPin.value = "";
  } finally {
    pairButton.disabled = false;
  }
});

processButton.addEventListener("click", async () => {
  const tab = await getActiveTab();
  const context = await getOrderContext(tab);
  if (!context || !context.isOrderDocument || !context.orderId) {
    processorStatus.textContent = "Open a CRM order page first.";
    return;
  }
  processButton.disabled = true;
  processorStatus.textContent = "Starting all-in-one processing…";
  try {
    const response = await chrome.runtime.sendMessage({
      type: "crm-order-automation:start",
      orderId: context.orderId,
      shippingTooExpensive: context.shippingTooExpensive === true
    });
    processorStatus.textContent = (response && response.message) || "Processing request sent.";
  } finally {
    processButton.disabled = false;
  }
});

void initialize();
