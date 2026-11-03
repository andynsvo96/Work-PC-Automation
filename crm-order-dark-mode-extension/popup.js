const toggle = document.getElementById("theme-toggle");
const pageStatus = document.getElementById("page-status");
const bridgeStatus = document.getElementById("bridge-status");

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
  const [bridge] = await Promise.all([
    chrome.runtime.sendMessage({ type: "crm-dark-mode:get-bridge-status" }),
    refreshPageStatus(await getActiveTab())
  ]);
  bridgeStatus.textContent = bridge && bridge.connected
    ? "Local Automation app bridge connected."
    : (bridge && bridge.message) || "Local Automation app bridge is unavailable.";
}

toggle.addEventListener("change", async () => {
  const response = await chrome.runtime.sendMessage({ type: "crm-dark-mode:set-theme", enabled: toggle.checked });
  toggle.checked = response && response.enabled === true;
  await refreshPageStatus(await getActiveTab());
});

void initialize();

const sxsConnect = document.getElementById('sxs-connect');
const sxsStatus = document.getElementById('sxs-status');
let sxsEnabled = false;
async function refreshSxsStatus() {
  const state = await chrome.runtime.sendMessage({ type: 'sxs:status' });
  sxsEnabled = state?.enabled === true;
  sxsConnect.textContent = sxsEnabled ? 'Disconnect this browser' : 'Connect this browser';
  sxsStatus.textContent = state?.message || 'S&S browser is not connected.';
}
sxsConnect.addEventListener('click', async () => {
  sxsConnect.disabled = true;
  try {
    if (!sxsEnabled && !await chrome.permissions.request({ permissions: ['scripting'], origins: ['https://www.ssactivewear.com/*'] })) {
      sxsStatus.textContent = 'S&S access was not granted. Browser connection is off.';
      return;
    }
    const result = await chrome.runtime.sendMessage({ type: 'sxs:connect', enabled: !sxsEnabled });
    if (result?.success === false) throw Error(result.message);
    await refreshSxsStatus();
  } catch (error) { sxsStatus.textContent = error.message; }
  finally { sxsConnect.disabled = false; }
});
document.getElementById('sxs-open').addEventListener('click', async () => {
  const result = await chrome.runtime.sendMessage({ type: 'sxs:open' });
  if (result?.success === false) sxsStatus.textContent = result.message;
});
document.getElementById('sxs-check').addEventListener('click', async event => {
  event.currentTarget.disabled = true;
  sxsStatus.textContent = 'Checking S&S. You can keep working in other tabs.';
  try {
    const result = await chrome.runtime.sendMessage({ type: 'sxs:probe', mode: document.getElementById('sxs-check-mode').value });
    sxsStatus.textContent = result?.message || 'S&S browser check did not return a result.';
  } catch (error) { sxsStatus.textContent = error.message; }
  finally { document.getElementById('sxs-check').disabled = false; }
});
void refreshSxsStatus();
