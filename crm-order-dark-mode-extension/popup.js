const toggle = document.getElementById("theme-toggle");
const pageStatus = document.getElementById("page-status");
const bridgeStatus = document.getElementById("bridge-status");
const processorStatus = document.getElementById("processor-status");
const processButton = document.getElementById("process-button");
let processorPollTimer = null;

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
    processorStatus.textContent = (response && response.message) || "Local Automation app status is unavailable.";
    return;
  }
  const runtime = response.runtime || {};
  const steps = Array.isArray(runtime.steps) ? runtime.steps : [];
  if (runtime.queued) {
    processorStatus.textContent = "Queued behind any active CRM automation.";
    return true;
  }
  if (runtime.running) {
    processorStatus.textContent = `${runtime.currentStep || "Processing"}: ${runtime.lastMessage || "Working…"}`;
    return true;
  }
  if (runtime.lastSuccess === true && steps.length) {
    const completed = steps.filter((step) => step && step.success && !step.skipped).map((step) => step.label || step.key);
    const skipped = steps.filter((step) => step && step.skipped).map((step) => step.label || step.key);
    processorStatus.textContent = `Completed: ${completed.join(", ") || "no action needed"}.${skipped.length ? ` Not needed: ${skipped.join(", ")}.` : ""}`;
    return false;
  }
  processorStatus.textContent = runtime.lastMessage || "Ready to process the current order.";
  return false;
}

function startProcessorPolling() {
  if (processorPollTimer) clearInterval(processorPollTimer);
  const poll = async () => {
    const stillActive = await refreshProcessorStatus();
    if (!stillActive && processorPollTimer) {
      clearInterval(processorPollTimer);
      processorPollTimer = null;
    }
  };
  void poll();
  processorPollTimer = setInterval(() => { void poll(); }, 2000);
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
  if (await refreshProcessorStatus()) startProcessorPolling();
}

toggle.addEventListener("change", async () => {
  const response = await chrome.runtime.sendMessage({ type: "crm-dark-mode:set-theme", enabled: toggle.checked });
  toggle.checked = response && response.enabled === true;
  await refreshPageStatus(await getActiveTab());
});

processButton.addEventListener("click", async () => {
  const tab = await getActiveTab();
  const context = await getOrderContext(tab);
  if (!context || !context.isOrderDocument || !context.orderId) {
    processorStatus.textContent = "Open a CRM order page first.";
    return;
  }
  processButton.disabled = true;
  processorStatus.textContent = "Sending the order to the CRM automation queue…";
  try {
    const response = await chrome.runtime.sendMessage({
      type: "crm-order-automation:start",
      orderId: context.orderId,
      shippingTooExpensive: context.shippingTooExpensive === true
    });
    processorStatus.textContent = (response && response.message) || "Processing request sent.";
    if (response && response.success) startProcessorPolling();
  } finally {
    processButton.disabled = false;
  }
});

void initialize();
