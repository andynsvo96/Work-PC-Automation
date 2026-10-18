// Read-only identities. Names and quantities are checked against the rendered tabs.
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
      available: String(design.crudAction || '').toLowerCase() !== 'd'
    })).filter(design => design.available)
  };
}
