// Read-only identities. Artwork IDs can repeat; the persisted order-design ID
// identifies a tab after removals and renumbering. Loaded details avoid tab clicks.
function readCopyrightDesignIds() {
  const context = CRMOrderScanReader.context();
  const resource = context.resource;
  const scan = {
    order_id: context.orderId,
    designs: (resource.designs || []).map((design, index) => ({
      tab_number: index + 1,
      design_id: String(design.designId || ''),
      ...(design.id ? { order_design_id: String(design.id) } : {}),
      available: String(design.crudAction || '').toLowerCase() !== 'd'
    })).filter(design => design.available)
  };
  scan.loaded_designs = CRMOrderScanReader.copyrightDesigns(context, scan.designs);
  return scan;
}
