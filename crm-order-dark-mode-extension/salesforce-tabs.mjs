const SALESFORCE_HOST_SUFFIXES = [
  "salesforce.com",
  "force.com",
  "visualforce.com"
];

export function isSalesforceUrl(value) {
  try {
    const url = new URL(String(value || ""));
    if (url.protocol !== "https:" && url.protocol !== "http:") return false;
    const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
    return SALESFORCE_HOST_SUFFIXES.some(
      (suffix) => hostname === suffix || hostname.endsWith(`.${suffix}`)
    );
  } catch (_error) {
    return false;
  }
}

function tabSalesforceUrl(tab) {
  const candidates = [tab && tab.pendingUrl, tab && tab.url];
  return candidates.find(isSalesforceUrl) || "";
}

export function rankSalesforceTabs(tabs, options = {}) {
  const excludeTabId = Number(options.excludeTabId);
  const preferredTabId = Number(options.preferredTabId);
  const preferredWindowId = Number(options.preferredWindowId);

  return (Array.isArray(tabs) ? tabs : [])
    .filter((tab) => (
      tab
      && Number(tab.id) !== excludeTabId
      && Boolean(tabSalesforceUrl(tab))
    ))
    .sort((left, right) => {
      const comparisons = [
        Number(Number(right.id) === preferredTabId) - Number(Number(left.id) === preferredTabId),
        Number(Number(right.windowId) === preferredWindowId) - Number(Number(left.windowId) === preferredWindowId),
        Number(right.active === true) - Number(left.active === true),
        Number(right.lastAccessed || 0) - Number(left.lastAccessed || 0),
        Number(right.id || 0) - Number(left.id || 0)
      ];
      return comparisons.find((value) => value !== 0) || 0;
    });
}
