const toggle = document.getElementById("theme-toggle");
const pageStatus = document.getElementById("page-status");

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

async function initialize() {
  const response = await chrome.runtime.sendMessage({ type: "crm-dark-mode:get-theme" });
  toggle.checked = response && response.enabled === true;
  await refreshPageStatus(await getActiveTab());
}

toggle.addEventListener("change", async () => {
  const response = await chrome.runtime.sendMessage({ type: "crm-dark-mode:set-theme", enabled: toggle.checked });
  toggle.checked = response && response.enabled === true;
  await refreshPageStatus(await getActiveTab());
});

void initialize();
