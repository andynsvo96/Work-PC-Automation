// On-demand, read-only access to data CRM has already loaded. Defining this
// reader does not inspect orders, fetch data, start timers, or change tabs.
var CRMOrderScanReader = (() => {
  "use strict";
  const clean = value => typeof value === "string" || typeof value === "number"
    ? String(value).replace(/\s+/g, " ").trim() : "";
  const active = values => (values || []).filter(value => value && clean(value.crudAction).toLowerCase() !== "d");
  const quantity = value => value !== null && value !== undefined && value !== "" &&
    typeof value !== "boolean" && Number.isSafeInteger(Number(value)) && Number(value) >= 0 ? Number(value) : null;

  function routeOrderId() {
    const direct = location.pathname.match(/^\/order\/(\d{7})\/?$/);
    const embedded = /^\/app\/?$/.test(location.pathname) && location.hash.match(/^#!?\/?order\/(\d{7})\/?(?:[?#].*)?$/);
    return (direct || embedded)?.[1] || "";
  }

  function context() {
    if (!window.angular?.element) throw new Error("CRM order data is not ready. Wait for the order to load and try again.");
    let orderScope;
    const candidates = document.querySelectorAll('#view-orders-index, #order-design-details, #main-header-design-tabs, [ng-click="saveOrder();"], [ng-controller], body');
    for (const element of candidates) {
      let scope;
      try { scope = window.angular.element(element).scope(); } catch (_) { continue; }
      for (let hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
        if (scope.order?.getResource && (typeof scope.copyOrder === "function" || typeof scope.saveOrder === "function")) {
          orderScope = scope;
          break;
        }
      }
      if (orderScope) break;
    }
    if (!orderScope || orderScope.$$destroyed) throw new Error("Could not read the CRM order. Refresh the order and try again.");
    if (orderScope.editMode || orderScope.saving || orderScope.savingOrder) {
      throw new Error("Save or leave Edit Order before scanning the order.");
    }
    const resource = orderScope.order.getResource();
    const orderId = clean(resource?.id);
    if (!/^\d{7}$/.test(orderId) || (routeOrderId() && orderId !== routeOrderId()) || !Array.isArray(resource.designs)) {
      throw new Error("Could not verify the loaded order and its design tabs. Refresh the order and try again.");
    }
    return { scope: orderScope, resource, orderId };
  }

  function designName(design) {
    // CRM renders this exact default when its saved name is an empty string.
    return design.name === "" ? "My Cool Design" : clean(design.name);
  }

  function designTotals(ctx) {
    // Every design's totals are rendered together. Read rows once, including
    // CRM's duplicate totals tables, and reject conflicting copies.
    const totals = new Map();
    for (const row of document.querySelectorAll("tr")) {
      const match = clean(row.innerText || row.textContent).match(/^Design\s+(\d+)\s+Total\s*:\s*(\d+)\s+pieces?\s*\$?(-?[0-9,]+\.\d{2})$/i);
      if (!match) continue;
      const number = Number(match[1]);
      const value = { quantity: Number(match[2]), subtotal: Number(match[3].replaceAll(",", "")).toFixed(2) };
      if (totals.has(number) && JSON.stringify(totals.get(number)) !== JSON.stringify(value)) {
        throw new Error(`Conflicting quantity or total for design tab ${number}. Refresh the order before scanning.`);
      }
      totals.set(number, value);
    }
    // Use CRM's own calculation when available; never substitute a guessed
    // zero for a missing quantity or price. Cross-check rendered totals too.
    ctx.resource.designs.forEach((design, index) => {
      if (!design || clean(design.crudAction).toLowerCase() === "d") return;
      if (typeof ctx.scope.order.getDesignSums !== "function") return;
      const sums = ctx.scope.order.getDesignSums(design);
      const count = quantity(sums?.quantity);
      const price = sums?.pricePerPiece;
      if (count === null || price === null || price === undefined || price === "" ||
          typeof price === "boolean" || !Number.isFinite(Number(price))) return;
      const value = { quantity: count, subtotal: Number(price).toFixed(2) };
      if (totals.has(index + 1) && JSON.stringify(totals.get(index + 1)) !== JSON.stringify(value)) {
        throw new Error(`Design tab ${index + 1} is still updating. Wait for the order to finish loading and try again.`);
      }
      totals.set(index + 1, value);
    });
    return totals;
  }

  function copyrightDesigns(ctx, identities) {
    const totals = designTotals(ctx);
    const rows = identities.map(identity => {
      const name = designName(ctx.resource.designs[identity.tab_number - 1]);
      const total = totals.get(identity.tab_number);
      if (!name || !total) return null;
      const { available, ...ids } = identity;
      return { ...ids, design_name: name, ...total };
    });
    return rows.length && rows.every(Boolean) ? rows : null;
  }

  function stockProducts() {
    const ctx = context();
    const products = [];
    const ids = new Set();
    const tabs = [];
    let nativeForms;
    const formFor = item => {
      if (!nativeForms) {
        nativeForms = new Map();
        for (const block of document.querySelectorAll('[ng-repeat="($itemIndex, item) in design.designItems"]')) {
          const scope = window.angular.element(block).scope();
          if (scope?.item && scope.formItem) nativeForms.set(scope.item, scope.formItem);
        }
      }
      return nativeForms.get(item) || {};
    };
    for (const [index, design] of ctx.resource.designs.entries()) {
      if (!design || clean(design.crudAction).toLowerCase() === "d") continue;
      if (!Array.isArray(design.designItems)) return null;
      const tabNumber = index + 1;
      let tabQuantity = 0;
      for (const item of active(design.designItems)) {
        const split = Number(item.splitIntoSizes) !== 0;
        if (!Array.isArray(item.sizes)) return null;
        const sizes = active(item.sizes);
        if (sizes.some(size => quantity(size.quantity) === null)) return null;
        const used = sizes.filter(size => quantity(size.quantity) > 0);
        const total = split ? used.reduce((sum, size) => sum + quantity(size.quantity), 0) : quantity(item.quantity);
        if (total === null || (split && !total && quantity(item.quantity) > 0)) return null;
        if (!total) continue;
        const itemId = clean(item.id);
        if (!/^[1-9]\d*$/.test(itemId)) return null;
        if (ids.has(itemId)) throw new Error("CRM stock has duplicate product IDs. Refresh the order before scanning.");
        ids.add(itemId);
        const substitutions = active(item.styleSubs);
        const substituted = Number(item.isStyleSub) === 1 || item.isStyleSub === true;
        if (substituted && substitutions.length !== 1) return null;
        const source = substituted ? substitutions[0] : item;
        const form = !substituted && (!clean(item.style) || !clean(item.ourLabel || item.label) || !clean(item.color || item.colorName))
          ? formFor(item) : {};
        const catalog = form.selectedCatalogItem || {};
        const style = clean(source.style || form.selectedCatalogItemStyle);
        const description = substituted ? clean(source.description) : clean(item.ourLabel || item.label || catalog.ourLabel || catalog.label);
        const color = clean(source.color || source.colorName || form.selectedColor?.name);
        const labels = split ? used.map(size => clean(size.sizeCode || size.size?.name || size.size?.description ||
          size.size || size.label || size.name || size.sizeName || size.sizeType)) : [];
        if (!style || !description || !color || labels.some(label => !label) ||
            new Set(labels.map(label => label.toLowerCase())).size !== labels.length) return null;
        products.push({ tab_number: tabNumber, design_item_id: `design-item-${itemId}`, style, description, color,
          available_sizes: labels, total_quantity: total });
        tabQuantity += total;
      }
      tabs.push({ tab_number: tabNumber, quantity: tabQuantity });
    }
    if (!tabs.length) throw new Error("No available design tabs were found on this order.");
    return { order_id: ctx.orderId, tabs, products };
  }

  return { context, designName, copyrightDesigns, stockProducts, routeOrderId };
})();
