// Read-only transport. No work is performed until a picker requests a scan.
document.addEventListener("crm-stock-products-request", event => {
  if (!CRMOrderScanReader.routeOrderId()) return;
  let result;
  try { result = { scan: CRMOrderScanReader.stockProducts() }; }
  catch (error) { result = { error: error.message }; }
  document.dispatchEvent(new CustomEvent("crm-stock-products-response", {
    detail: JSON.stringify({ id: event.detail, ...result })
  }));
});
