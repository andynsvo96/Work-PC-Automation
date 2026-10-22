// Read-only identities. Artwork IDs can repeat; the persisted order-design ID
// identifies a tab after removals and renumbering. Rendered details are checked too.
function readCopyrightDesignIds() {
  let orderScope = null;
  for (const element of document.querySelectorAll('*')) {
    let scope;
    try { scope = window.angular.element(element).scope(); } catch (_) { continue; }
    for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
      if (scope.order?.getResource && typeof scope.copyOrder === 'function') {
        orderScope = scope;
        break;
      }
    }
    if (orderScope) break;
  }
  if (!orderScope || orderScope.editMode || orderScope.saving) {
    throw new Error('Save or leave Edit Order before scanning copyright designs.');
  }
  const resource = orderScope.order.getResource();
  return {
    order_id: String(resource.id || ''),
    designs: (resource.designs || []).map((design, index) => ({
      tab_number: index + 1,
      design_id: String(design.designId || ''),
      ...(design.id ? { order_design_id: String(design.id) } : {}),
      available: String(design.crudAction || '').toLowerCase() !== 'd'
    })).filter(design => design.available)
  };
}
