const CRM_ORIGIN = "https://crm2.legacy.printfly.com";

function isCRMListUrl(value) {
  try {
    const url = new URL(value);
    if (url.origin !== CRM_ORIGIN) return false;
    const path = url.pathname.replace(/\/$/, "");
    const route = path === "/app" ? url.hash.replace(/^#!?\/?/, "/").split("?")[0] : path;
    return /^\/(?:reports?|orders)(?:\/|$)/i.test(route);
  } catch (_error) {
    return false;
  }
}

export async function openApplicableCRMOrders(orderIds, sender, tabs = chrome.tabs) {
  if (!Number.isInteger(sender?.tab?.id) || !isCRMListUrl(sender.url)
      || !Array.isArray(orderIds) || !orderIds.length
      || !Array.from(orderIds).every(id => typeof id === "string" && id.length === 7 && /^\d{7}$/.test(id))) {
    return { success: false, openedOrderIds: [], message: "Open applicable orders from a CRM list with valid order IDs." };
  }
  const ids = [...new Set(orderIds)];
  const openedOrderIds = [];
  try {
    // Resolve the source again: the user may have moved or navigated its tab.
    const source = await tabs.get(sender.tab.id);
    if (!isCRMListUrl(source.url) || !Number.isInteger(source.windowId)) {
      throw new Error("The source is no longer a CRM list.");
    }
    for (const id of ids) {
      await tabs.create({
        url: `${CRM_ORIGIN}/order/${id}`,
        windowId: source.windowId,
        openerTabId: source.id,
        index: source.index + 1 + openedOrderIds.length,
        active: false
      });
      openedOrderIds.push(id);
    }
    return { success: true, openedOrderIds };
  } catch (_error) {
    // Never retry a partly opened batch automatically.
    return {
      success: false, openedOrderIds,
      message: `Opened ${openedOrderIds.length} of ${ids.length} order tabs. Selection remains in the CRM list. Check the tabs before trying again.`
    };
  }
}
