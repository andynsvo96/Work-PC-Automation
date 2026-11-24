// Shared by the order content script and toolbar popup. Store each preference
// separately so changes made in different order tabs cannot overwrite each other.
(() => {
  "use strict";
  const sections = [
    ["contact-panel", "Contact Info and Send Options"],
    // Match the native title when a stable panel ID is unavailable; ignore its count.
    ["order-associations", "Associations", /^Associations(?:\s*\(\s*\d+\s*\))?$/i],
    ["design-assignments-panel", "Design Assignments"],
    ["status-history", "Status History and Art Changes"],
    ["access-history", "Access Log"],
    ["station-assignments-panel", "Station Assignments"],
    ["subcontractor-assignments-panel", "Subcontractor Assignments"],
    ["order-totals", "Order Totals"],
    ["order-payments-credits", "Payments and Credits"],
    ["order-shipping", "Shipping Info"],
    ["order-vendor-po-panel", "Vendor Purchase Order"],
    ["order-purchase-orders", "Purchase Orders"],
    ["order-promos", "Promos"],
    ["order-cartons", "Carton Tracking"],
    ["sales-tax-transactions", "Sales Tax Transactions"]
  ].map(([id, label, headingPattern]) => Object.freeze({ id, label, key: `crmOrderHiddenSection:${id}`, headingPattern }));
  globalThis.CRMOrderSections = Object.freeze({
    sections: Object.freeze(sections),
    lockKey: "crmOrderSectionControlsLocked"
  });
})();
