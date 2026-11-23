// Serialized by chrome.scripting into the order's MAIN world. Read-only.
export function canRefreshOrderPage(expectedOrderId) {
  const route = `${location.pathname || ""}${location.hash || ""}`.match(/\/order\/(\d{7})\b/);
  if (!route || route[1] !== expectedOrderId) return false;
  const visible = node => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
  if ([...document.querySelectorAll('.modal, [role="dialog"], .ngdialog-content')].some(visible)) return false;
  if (window.__crmAllTabsHasPendingChanges?.()) return false;
  if (!window.angular?.element) return false;
  for (const node of document.querySelectorAll('#view-orders-index, #order-design-details, [ng-click="saveOrder();"]')) {
    let scope;
    try { scope = window.angular.element(node).scope(); } catch (_) { continue; }
    for (let hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
      const order = scope.order?.getResource?.();
      if (String(order?.id) !== expectedOrderId || typeof scope.saveOrder !== "function") continue;
      return !scope.editMode && !scope.saving && !scope.savingOrder && !scope.fileUploading
        && !scope.isGettingShippingMethods?.()
        && !["addSalesNotes", "addProductionNotes", "addArtNotes"].some(key => String(order[key] || "").trim());
    }
  }
  return false;
}
