// Read-only Angular metadata, shared with the queued worker's live validation.
function readEmbDesignMethods() {
  let orderScope = null;
  for (const el of document.querySelectorAll('*')) {
    let scope;
    try { scope = window.angular.element(el).scope(); } catch (_) { continue; }
    for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
      if (scope.order?.getResource && typeof scope.copyOrder === 'function') {
        orderScope = scope;
        break;
      }
    }
    if (orderScope) break;
  }
  if (!orderScope) throw new Error('Could not read the order print methods. Refresh the CRM order and try again.');
  let describe;
  for (let scope = orderScope; scope; scope = scope.$parent) {
    if (typeof scope.setPrintMethodDescription === 'function') {
      describe = scope.setPrintMethodDescription.bind(scope);
      break;
    }
  }
  return (orderScope.order.getResource().designs || []).map((design, index) => {
    const methods = (design.printAreas || []).filter(area => area && area.crudAction !== 'd').map(area => {
      const method = area.printMethodDescription || area.methodDescription ||
        area.printMethod?.description || area.printMethodTemplate?.description || (describe && describe(area));
      return String(method || '').replace(/\s+/g, ' ').trim();
    });
    return {
      tab_number: index + 1,
      // Complicated EMB also applies after a design has been switched to ink.
      eligible: design.crudAction !== 'd' && methods.length > 0 && methods.every(Boolean),
      methods
    };
  });
}
