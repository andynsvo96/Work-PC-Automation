const toggle = document.getElementById("theme-toggle");
const bridgeStatus = document.getElementById("bridge-status");
const hiddenSectionsToggle = document.getElementById("hidden-sections-toggle");
const hiddenSectionsPanel = document.getElementById("hidden-sections-panel");
const hiddenSectionsList = document.getElementById("hidden-sections-list");
const hiddenSectionsStatus = document.getElementById("hidden-sections-status");
const sectionPreferences = globalThis.CRMOrderSections.sections;

async function refreshHiddenSections() {
  try {
    const values = await chrome.storage.local.get(sectionPreferences.map(section => section.key));
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
    hiddenSectionsStatus.classList.add("error");
    hiddenSectionsStatus.textContent = "Could not read hidden sections. Reopen the extension and try again.";
  }
}

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
  if (area === "local" && sectionPreferences.some(section => changes[section.key])) void refreshHiddenSections();
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
