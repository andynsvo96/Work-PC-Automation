const toggle = document.getElementById("theme-toggle");
const bridgeStatus = document.getElementById("bridge-status");
const hiddenSectionsToggle = document.getElementById("hidden-sections-toggle");
const hiddenSectionsPanel = document.getElementById("hidden-sections-panel");
const hiddenSectionsList = document.getElementById("hidden-sections-list");
const hiddenSectionsStatus = document.getElementById("hidden-sections-status");
const hiddenSectionsLock = document.getElementById("hidden-sections-lock");
const hiddenSectionsLockShackle = document.getElementById("hidden-sections-lock-shackle");
const sectionPreferences = globalThis.CRMOrderSections.sections;
const sectionControlsLockKey = globalThis.CRMOrderSections.lockKey;
let sectionControlsLocked = false;
let savingSectionLock = false;
let sectionPreferencesReadVersion = 0;

function renderSectionControlsLock() {
  hiddenSectionsLock.setAttribute("aria-pressed", String(sectionControlsLocked));
  hiddenSectionsLock.setAttribute("aria-label", sectionControlsLocked ? "Unlock section hide buttons" : "Lock section hide buttons");
  hiddenSectionsLock.title = sectionControlsLocked
    ? "Unlock to show the red × buttons on CRM orders"
    : "Lock to hide the red × buttons on CRM orders";
  hiddenSectionsLockShackle.setAttribute("d", sectionControlsLocked ? "M8 10V7a4 4 0 0 1 8 0v3" : "M8 10V6a4 4 0 0 1 8 0");
}

async function refreshHiddenSections() {
  const readVersion = ++sectionPreferencesReadVersion;
  try {
    const values = await chrome.storage.local.get([...sectionPreferences.map(section => section.key), sectionControlsLockKey]);
    if (readVersion !== sectionPreferencesReadVersion) return;
    sectionControlsLocked = values[sectionControlsLockKey] === true;
    renderSectionControlsLock();
    hiddenSectionsLock.disabled = savingSectionLock;
    hiddenSectionsList.replaceChildren();
    const hidden = sectionPreferences.filter(section => values[section.key] === true);
    hiddenSectionsStatus.classList.remove("error");
    hiddenSectionsStatus.textContent = hidden.length ? "Hidden on all CRM orders in this Chrome profile." : "No order sections are hidden.";
    hiddenSectionsToggle.title = `Hidden order sections (${hidden.length})`;
    for (const section of hidden) {
      const row = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = section.label;
      const restore = document.createElement("button");
      restore.type = "button";
      restore.textContent = "Unhide";
      restore.setAttribute("aria-label", `Unhide ${section.label}`);
      restore.addEventListener("click", async () => {
        restore.disabled = true;
        try {
          await chrome.storage.local.set({ [section.key]: false });
          await refreshHiddenSections();
          const next = hiddenSectionsList.querySelector("button");
          (next || hiddenSectionsToggle).focus();
        } catch (_error) {
          restore.disabled = false;
          hiddenSectionsStatus.classList.add("error");
          hiddenSectionsStatus.textContent = "Could not restore this section. Try again.";
        }
      });
      row.append(name, restore);
      hiddenSectionsList.append(row);
    }
  } catch (_error) {
    if (readVersion !== sectionPreferencesReadVersion) return;
    hiddenSectionsLock.disabled = true;
    hiddenSectionsStatus.classList.add("error");
    hiddenSectionsStatus.textContent = "Could not read hidden sections. Reopen the extension and try again.";
  }
}

hiddenSectionsLock.addEventListener("click", async () => {
  if (savingSectionLock || hiddenSectionsLock.disabled) return;
  const locked = !sectionControlsLocked;
  savingSectionLock = true;
  hiddenSectionsLock.disabled = true;
  try {
    await chrome.storage.local.set({ [sectionControlsLockKey]: locked });
    sectionControlsLocked = locked;
    renderSectionControlsLock();
    await refreshHiddenSections();
  } catch (_error) {
    hiddenSectionsStatus.classList.add("error");
    hiddenSectionsStatus.textContent = "Could not save the section lock. Try again.";
  } finally {
    savingSectionLock = false;
    hiddenSectionsLock.disabled = false;
  }
});

function closeHiddenSections() {
  hiddenSectionsPanel.hidden = true;
  hiddenSectionsToggle.setAttribute("aria-expanded", "false");
}

hiddenSectionsToggle.addEventListener("click", () => {
  const opening = hiddenSectionsPanel.hidden;
  hiddenSectionsPanel.hidden = !opening;
  hiddenSectionsToggle.setAttribute("aria-expanded", String(opening));
  if (opening) void refreshHiddenSections();
});
document.addEventListener("click", event => {
  if (!event.target.closest(".hidden-sections-control")) closeHiddenSections();
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !hiddenSectionsPanel.hidden) {
    closeHiddenSections();
    hiddenSectionsToggle.focus();
  }
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes[sectionControlsLockKey] || sectionPreferences.some(section => changes[section.key]))) void refreshHiddenSections();
});

function setStatus(message) { toggle.closest(".toggle").title = `${message} Print output stays light.`; }

async function getActiveTab() {
  const source = new URLSearchParams(location.search).get("sourceWindow");
  const tabs = await chrome.tabs.query({ active: true, ...(source !== null && /^\d+$/.test(source) ? { windowId: Number(source) } : { currentWindow: true }) });
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
void refreshHiddenSections();
