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
  function scopeFunction(name) {
    for (let scope = orderScope; scope; scope = scope.$parent) {
      if (typeof scope[name] === 'function') return scope[name].bind(scope);
    }
    return null;
  }
  const describe = scopeFunction('setPrintMethodDescription');
  const describeArea = scopeFunction('setPrintAreaTemplateDescription');
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  return (orderScope.order.getResource().designs || []).map((design, index) => {
    const printAreas = (design.printAreas || []).filter(area => area && area.crudAction !== 'd').map(area => {
      const method = area.printMethodDescription || area.methodDescription ||
        area.printMethod?.description || area.printMethodTemplate?.description || (describe && describe(area));
      let description = [area.description, area.printAreaDescription, area.printArea?.description,
        area.printAreaTemplate?.description].find(value => clean(value));
      if (!description && describeArea) {
        try { description = describeArea(area); } catch (_) {}
      }
      return { description: clean(description), method: clean(method) };
    });
    const methods = printAreas.map(area => area.method);
    return {
      tab_number: index + 1,
      available: design.crudAction !== 'd',
      // Complicated EMB also applies after a design has been switched to ink.
      eligible: design.crudAction !== 'd' && methods.length > 0 && methods.every(Boolean),
      methods,
      print_areas: printAreas
    };
  });
}
