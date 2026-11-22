// Runs in MAIN alongside CRM's Angular app. Only these visible controls can
// apply edits; there is no page-event or local-server write bridge.
(() => {
  "use strict";
  if (window.__crmAllTabsInstalled) return;
  window.__crmAllTabsInstalled = true;
  const model = CRMAllTabsModel;
  const stock = CRMAllTabsStock;
  // Match CRM's native print-area headings in recordForm/styles/orders.css.
  const printMethodColors = new Map([
    ["hd digital", "#ff5686"],
    ["screen printing", "#5bbad3"],
    ["embroidery", "#69bd45"],
    ["direct to garment", "#7e51a1"]
  ]);
  let stockScopes = [];
  let context = null;
  let state = null;
  let queued = false;
  let additionId = 0;

  function routeOrderId() {
    const direct = location.pathname.match(/^\/order\/(\d{7})\/?$/);
    const embedded = /^\/app\/?$/.test(location.pathname) && location.hash.match(/^#!?\/?order\/(\d{7})\/?(?:[?#].*)?$/);
    return (direct || embedded)?.[1] || "";
  }

  function scopeFunction(scope, name) {
    for (let current = scope, hops = 0; current && hops < 12; current = current.$parent, hops++) {
      if (typeof current[name] === "function") return current[name].bind(current);
    }
    return null;
  }

  function statics(scope) {
    for (let current = scope, hops = 0; current && hops < 12; current = current.$parent, hops++) {
      if (current.STATICS) return current.STATICS;
    }
    return window.STATICS || {};
  }

  function findContext() {
    const orderId = routeOrderId();
    if (!orderId || !window.angular?.element) return null;
    const details = document.getElementById("order-design-details");
    if (!details) return null;
    let scope = context?.details === details ? context.scope : null;
    if (!scope || scope.$$destroyed || String(scope.order?.getResource?.()?.id) !== orderId) {
      scope = null;
      for (const element of document.querySelectorAll('#view-orders-index, #order-design-details, #main-header-design-tabs, [ng-click="saveOrder();"]')) {
        let candidate;
        try { candidate = window.angular.element(element).scope(); } catch (_) { continue; }
        for (let hops = 0; candidate && hops < 12; candidate = candidate.$parent, hops++) {
          if (candidate.order?.getResource && typeof candidate.saveOrder === "function" &&
              String(candidate.order.getResource()?.id) === orderId) { scope = candidate; break; }
        }
        if (scope) break;
      }
    }
    if (!scope || !scope.editMode) return null;
    const resource = scope.order.getResource();
    if (!Array.isArray(resource?.designs) || resource.designs.filter(design => design && model.key(design.crudAction) !== "d").length < 2) return null;
    return { orderId, details, scope, resource };
  }

  function busy() {
    return Boolean(context?.scope.saving || context?.scope.savingOrder || context?.scope.fileUploading ||
      scopeFunction(context?.scope, "isGettingShippingMethods")?.());
  }

  function describeArea(area) {
    return model.clean(area.description || area.printAreaDescription || area.printArea?.description ||
      area.printAreaTemplate?.description || scopeFunction(context.scope, "setPrintAreaTemplateDescription")?.(area));
  }

  function describeMethod(area) {
    return model.clean(area.printMethodDescription || area.methodDescription || area.printMethod?.description ||
      area.printMethodTemplate?.description || scopeFunction(context.scope, "setPrintMethodDescription")?.(area));
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function button(label, className, handler) {
    const node = element("button", className, label);
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
  }

  function areaTitle(id, label) {
    const title = element("h4", "", label);
    title.id = `crm-all-tabs-title-${id}`;
    return title;
  }

  function updateAreaTitle(id, methods) {
    const title = document.getElementById(`crm-all-tabs-title-${id}`);
    const unique = [...new Set(methods.map(model.key))];
    const color = unique.length === 1 && printMethodColors.get(unique[0]);
    title.classList.toggle("crm-all-tabs-method-title", Boolean(color));
    if (color) title.style.setProperty("--all-tabs-method-color", color);
    else title.style.removeProperty("--all-tabs-method-color");
  }

  function pending() {
    return Boolean(state && (state.additions.length || Object.values(state.changes).some(change => change.location || change.method || change.remove) ||
      Object.values(state.stockDrafts).some(draft => draft.touched)));
  }

  function disposeStockControls() {
    for (const scope of stockScopes) scope.$destroy();
    stockScopes = [];
  }

  function nativeStock(member) {
    for (const block of context.details.querySelectorAll('[ng-repeat="($itemIndex, item) in design.designItems"]')) {
      const scope = window.angular.element(block).scope();
      if (scope?.item === member.item && scope.formItem?.item === member.item) return {
        scope, block, form: scope.formItem,
        input: block.querySelector('[ng-model="formItem.selectedCatalogItemStyle"]'),
        color: block.querySelector('[ng-model="formItem.selectedColor"]')
      };
    }
    throw new Error(`CRM's native stock controls on tab ${member.record.tab} are not ready. Reopen All Tabs after they load.`);
  }

  function stockForm(member) {
    const { form, input, color } = nativeStock(member);
    if (!input || !color || input.disabled || input.readOnly || color.disabled) {
      throw new Error(`CRM disabled stock editing on tab ${member.record.tab}. Review that tab before changing shared stock.`);
    }
    if (String(form.selectedCatalogItem?.id) !== stock.productId(member.item) ||
        stock.colorIdentity(form.selectedColor) !== stock.colorId(member.item) ||
        model.key(form.selectedCatalogItemStyle) !== model.key(member.item.style)) {
      throw new Error(`Apply or discard the native product selection on tab ${member.record.tab} before changing shared stock.`);
    }
    return form;
  }

  function stockQuantities(group) {
    const wrapper = element("div", "crm-all-tabs-stock-sizes");
    const table = element("table", "crm-all-tabs-stock-size-table");
    const caption = element("caption", "", "Combined quantities on the affected tabs");
    table.append(caption);
    const head = element("thead");
    const sizes = element("tr");
    const sizeLabel = element("th", "", "Size:");
    sizeLabel.scope = "row";
    sizes.append(sizeLabel);
    const body = element("tbody");
    const quantities = element("tr");
    const quantityLabel = element("th", "", "Quantity:");
    quantityLabel.scope = "row";
    quantities.append(quantityLabel);
    const totals = stock.summarizeSizes(group);
    for (const { code, quantity } of totals) {
      const size = element("th", "", code);
      size.scope = "col";
      size.dataset.stockSize = code;
      sizes.append(size);
      const cell = element("td");
      const value = element("output", "crm-all-tabs-stock-quantity", String(quantity));
      value.dataset.stockQuantity = code;
      value.setAttribute("aria-label", `${code}: ${quantity}`);
      cell.append(value);
      quantities.append(cell);
    }
    if (!totals.length) return element("p", "crm-all-tabs-help", "No quantities are assigned to these items.");
    head.append(sizes);
    body.append(quantities);
    table.append(head, body);
    wrapper.append(table);
    return wrapper;
  }

  function renderStock(panel) {
    panel.append(element("h4", "crm-all-tabs-section-title", "Shared stock"));
    panel.append(element("p", "crm-all-tabs-help", "Product/color combinations on two or more tabs. Quantities below combine their matching items; each tab keeps its own sizes and quantities."));
    const cards = element("div", "crm-all-tabs-cards crm-all-tabs-stock-cards");
    for (const group of state.stockSession.groups) {
      const card = element("section", "crm-all-tabs-card crm-all-tabs-stock-card");
      card.dataset.stockGroup = group.id;
      card.append(element("h4", "", `${group.style} — ${group.color}`));
      const tabs = [...new Set(group.members.map(member => member.record.tab))];
      card.append(element("p", "crm-all-tabs-affected", `Affects tabs ${tabs.join(", ")}`));
      const warning = element("p", "crm-all-tabs-stock-warning crm-all-tabs-error");
      warning.dataset.stockWarning = group.id;
      warning.setAttribute("role", "status");
      try {
        const native = nativeStock(group.members[0]);
        if (!native.input || !native.color || !native.form.selectedCatalogItem || !native.form.selectedColor) {
          throw new Error("CRM's product picker is still loading. Reopen All Tabs after it loads.");
        }
        const loadCatalog = scopeFunction(native.scope, "watchCatalogItemChange");
        const watchColor = scopeFunction(native.scope, "watchCatalogItemColorChange");
        if (!loadCatalog || !watchColor) throw new Error("CRM's native product loader is unavailable. Reopen All Tabs after it loads.");
        let draft = state.stockDrafts[group.id];
        if (!draft) {
          const source = stockForm(group.members[0]);
          const form = window.angular.copy(source);
          // Angular 1.2 copies selectedColor separately from colors. ng-options
          // needs the actual list object, otherwise its current selection is blank.
          const matches = form.colors.filter(color => stock.colorIdentity(color) === stock.colorIdentity(source.selectedColor));
          if (matches.length !== 1) throw new Error("CRM's current color is still loading. Reopen All Tabs after it loads.");
          form.selectedColor = matches[0];
          draft = state.stockDrafts[group.id] = { form, touched: false };
        }
        const scope = native.scope.$new();
        stockScopes.push(scope);
        scope.formItem = draft.form;
        const previewUrl = scopeFunction(native.scope, "getCatalogFileURL");
        const originalImage = native.block.querySelector('img[ng-src][alt="product"]');
        scope.crmAllTabsImage = () => previewUrl ? previewUrl(draft.form) :
          (String(draft.form.selectedCatalogItem?.id) === group.productId && stock.colorIdentity(draft.form.selectedColor) === group.colorId
            ? originalImage?.getAttribute("src") || "" : "");
        scope.crmAllTabsBusy = busy;
        scope.crmAllTabsLoading = () => Boolean(draft.loading);
        scope.crmAllTabsInput = () => { draft.touched = true; draft.error = ""; preview(); };
        scope.crmAllTabsSelect = catalog => {
          draft.touched = true;
          draft.error = "";
          draft.loading = true;
          draft.form.selectedCatalogItemStyle = catalog.style;
          draft.form.selectedCatalogItem = catalog;
          draft.form.selectedColor = null;
          draft.form.colors = [];
          // Use the native loader without its shared per-real-tab autocomplete
          // cache. This draft never points at an item in the order resource.
          window.clearTimeout(draft.timer);
          try { loadCatalog(draft.form); }
          catch (_) {
            draft.loading = false;
            draft.error = `CRM could not load colors and sizes for ${model.clean(catalog.style)}. Select the product again.`;
            preview();
            return;
          }
          draft.timer = window.setTimeout(() => {
            if (draft.loading) {
              draft.loading = false;
              draft.error = `CRM could not load colors and sizes for ${model.clean(catalog.style)}. Select the product again.`;
              preview();
            }
          }, 15000);
          preview();
        };
        scope.crmAllTabsColor = () => {
          draft.touched = true;
          draft.error = "";
          watchColor(draft.form);
          preview();
        };
        const controls = element("div", "crm-all-tabs-native-stock");
        const imageBox = element("div", "crm-all-tabs-stock-preview");
        const photo = element("img", "crm-all-tabs-stock-image");
        photo.alt = "Product preview";
        photo.setAttribute("ng-if", "crmAllTabsImage()");
        photo.setAttribute("ng-src", "{{crmAllTabsImage()}}");
        imageBox.append(photo);
        const product = element("div", "crm-all-tabs-stock-product");
        const description = element("p", "crm-all-tabs-stock-description");
        description.setAttribute("ng-bind", "formItem.selectedCatalogItem.ourLabel || formItem.selectedCatalogItem.label");
        product.append(description);
        const colorAndSizes = element("div", "crm-all-tabs-stock-color-sizes");
        controls.append(imageBox, product, colorAndSizes);
        for (const [label, template, suffix] of [["Product", native.input, "product"], ["Color", native.color, "color"]]) {
          const field = element("label", "crm-all-tabs-field");
          const control = template.cloneNode(false);
          control.id = `crm-all-tabs-stock-${suffix}-${group.id}`;
          field.htmlFor = control.id;
          field.append(element("span", "", label));
          control.removeAttribute("ng-init");
          control.removeAttribute("name");
          control.removeAttribute("disabled");
          control.removeAttribute("style");
          const nativeDisabled = control.getAttribute("ng-disabled") || "false";
          control.setAttribute("ng-disabled", `(${nativeDisabled}) || crmAllTabsBusy()${suffix === "color" ? " || crmAllTabsLoading()" : ""}`);
          if (suffix === "product") {
            control.setAttribute("typeahead-on-select", "crmAllTabsSelect($item)");
            control.setAttribute("ng-change", "crmAllTabsInput()");
          } else control.setAttribute("ng-change", "crmAllTabsColor()");
          field.append(control);
          (suffix === "product" ? product : colorAndSizes).append(field);
        }
        const note = element("p", "crm-all-tabs-stock-note");
        note.setAttribute("ng-if", "formItem.selectedCatalogItem.note");
        note.append(element("strong", "", "Product Note:"), element("br"));
        const noteText = element("span");
        noteText.setAttribute("ng-bind", "formItem.selectedCatalogItem.note");
        note.append(noteText);
        product.append(note);
        // This summary reads each original item; it never binds quantity or price
        // models to the aggregate view or copies one tab's sizes onto another.
        colorAndSizes.append(stockQuantities(group));
        card.append(controls);
        const injector = window.angular.element(context.details).injector();
        runInAngular(() => injector.get("$compile")(controls)(scope));
        scope.$watch(() => [draft.form.selectedCatalogItemStyle, draft.form.selectedCatalogItem?.id,
          stock.colorIdentity(draft.form.selectedColor), draft.form.colors.length,
          draft.form.selectedColor?.sizes?.length].join("|"), () => {
          if (draft.form.colors.includes(draft.form.selectedColor) && Array.isArray(draft.form.selectedColor?.sizes)) {
            draft.loading = false;
            window.clearTimeout(draft.timer);
          }
          preview();
        });
        const reset = button("Keep original stock", "btn btn-default crm-all-tabs-stock-reset", () => {
          window.clearTimeout(draft.timer);
          delete state.stockDrafts[group.id];
          render();
        });
        card.append(reset);
      } catch (error) {
        card.querySelector(".crm-all-tabs-native-stock")?.remove();
        warning.textContent = error.message;
      }
      card.append(warning);
      cards.append(card);
    }
    if (!state.stockSession.groups.length) cards.append(element("p", "crm-all-tabs-help", "No catalog product/color appears on more than one tab."));
    panel.append(cards);
  }

  function selectField(card, label, id, values, selected, placeholder, onChange) {
    const wrapper = element("label", "crm-all-tabs-field");
    wrapper.htmlFor = id;
    wrapper.append(element("span", "", label));
    const select = element("select", "form-control");
    select.id = id;
    select.append(new Option(placeholder, ""));
    const seen = new Set();
    for (const value of values) {
      const key = model.key(value.description);
      if (seen.has(key)) continue;
      seen.add(key);
      select.append(new Option(value.description, key));
    }
    select.value = selected;
    select.addEventListener("change", () => { onChange(select.value); preview(); });
    wrapper.append(select);
    card.append(wrapper);
    return select;
  }

  function setStatus(message, error = false) {
    const status = document.getElementById("crm-all-tabs-status");
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("crm-all-tabs-error", error);
  }

  function freshState() {
    disposeStockControls();
    for (const draft of Object.values(state?.stockDrafts || {})) window.clearTimeout(draft.timer);
    state = { session: model.createSession(context.resource, describeArea, describeMethod),
      stockSession: stock.createSession(context.resource), stockDrafts: {},
      changes: {}, additions: [], open: state?.open || false, busy: busy() };
  }

  function render() {
    disposeStockControls();
    const panel = document.getElementById("crm-all-tabs-panel");
    panel.replaceChildren();
    panel.append(element("h3", "", "All Tabs"));
    panel.append(element("p", "crm-all-tabs-help", `Edit shared stock and print areas across ${state.session.designs.length} tabs. Choose changes, apply them, then use Save Order. Notes and artwork stay with their own tabs.`));
    const status = element("p", "crm-all-tabs-status");
    status.id = "crm-all-tabs-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    panel.append(status);
    renderStock(panel);
    panel.append(element("h4", "crm-all-tabs-section-title", "Print areas"));
    const cards = element("div", "crm-all-tabs-cards");
    const catalog = statics(context.scope);
    const templates = model.choices(catalog.printAreaTemplates, true);
    const methods = model.choices(catalog.printMethods);
    for (const group of state.session.groups) {
      const card = element("section", "crm-all-tabs-card");
      card.dataset.sourceArea = group.sourceKey;
      card.append(areaTitle(group.id, group.label));
      const tabs = [...new Set(group.members.map(member => member.record.tab))];
      const change = state.changes[group.id] ||= {};
      card.dataset.pendingRemoval = String(Boolean(change.remove));
      card.classList.toggle("crm-all-tabs-removing", Boolean(change.remove));
      const remove = button(change.remove ? "Undo" : "×",
        `crm-all-tabs-remove${change.remove ? " crm-all-tabs-undo" : ""}`, () => {
          change.remove = !change.remove;
          render();
          document.getElementById(`crm-all-tabs-remove-${group.id}`)?.focus();
        });
      remove.id = `crm-all-tabs-remove-${group.id}`;
      remove.setAttribute("aria-label", change.remove ? `Undo removal of ${group.label}` : `Remove ${group.label} from tabs ${tabs.join(", ")}`);
      remove.setAttribute("aria-pressed", String(Boolean(change.remove)));
      remove.title = remove.getAttribute("aria-label");
      card.append(remove);
      const affected = element("p", "crm-all-tabs-affected", `Affects tabs ${tabs.join(", ")}`);
      affected.title = group.members.map(member => `Tab ${member.record.tab}: ${member.method || "missing method"}`).join("; ");
      card.append(affected);
      if (change.remove) card.append(element("p", "crm-all-tabs-removal-note", "Will remove this original print area from the listed tabs when you apply."));
      selectField(card, "Print area", `crm-all-tabs-location-${group.id}`, templates, change.location || "",
        `Keep ${group.label}`, value => { change.location = value === group.sourceKey ? "" : value; });
      const existingMethods = [...new Set(group.members.map(member => member.method))];
      selectField(card, "Print method", `crm-all-tabs-method-${group.id}`, methods, change.method || "",
        existingMethods.length === 1 ? `Keep ${existingMethods[0] || "existing method"}` : "Keep each tab's method (mixed)",
        value => { change.method = existingMethods.length === 1 && model.key(existingMethods[0]) === value ? "" : value; });
      cards.append(card);
    }
    if (!state.session.groups.length) cards.append(element("p", "", "These tabs have no print areas yet. Add one below."));
    for (const addition of state.additions) {
      const card = element("section", "crm-all-tabs-card crm-all-tabs-addition");
      card.dataset.addition = addition.id;
      card.append(areaTitle(addition.id, "New print area"));
      selectField(card, "Print area", `crm-all-tabs-location-${addition.id}`, templates, addition.location || "",
        "Choose a location", value => { addition.location = value; });
      selectField(card, "Print method", `crm-all-tabs-method-${addition.id}`, methods, addition.method || "",
        "Choose a method", value => { addition.method = value; });
      const affected = element("p", "crm-all-tabs-affected");
      affected.dataset.additionTargets = addition.id;
      card.append(affected);
      card.append(button("Remove new area", "btn btn-default", () => {
        state.additions = state.additions.filter(item => item !== addition);
        render();
      }));
      cards.append(card);
    }
    panel.append(cards);
    const actions = element("div", "crm-all-tabs-actions");
    const add = button("Add print area", "btn btn-default", () => {
      const addition = { id: `a${++additionId}`, location: "", method: "" };
      state.additions.push(addition);
      render();
      document.getElementById(`crm-all-tabs-location-${addition.id}`).focus();
    });
    add.id = "crm-all-tabs-add";
    const apply = button("Apply changes", "btn btn-primary", applyChanges);
    apply.id = "crm-all-tabs-apply";
    const discard = button("Discard pending changes", "btn btn-default", () => {
      try { freshState(); render(); } catch (error) { setStatus(error.message, true); }
    });
    discard.id = "crm-all-tabs-discard";
    actions.append(add, apply, discard);
    panel.append(actions);
    preview();
  }

  function preview() {
    if (!state) return;
    const apply = document.getElementById("crm-all-tabs-apply");
    if (!apply) return;
    let planned;
    let stockPlan;
    let error;
    try { planned = model.plan(state.session, state.changes, state.additions, statics(context.scope), context.resource); }
    catch (failure) { error = failure; }
    try { stockPlan = stock.plan(state.stockSession, state.stockDrafts, context.resource); }
    catch (failure) { error ||= failure; }
    for (const group of state.session.groups) {
      updateAreaTitle(group.id, group.members.map(member => state.changes[group.id]?.method || member.method));
    }
    for (const group of state.stockSession.groups) {
      const draft = state.stockDrafts[group.id];
      const warning = document.querySelector(`[data-stock-warning="${group.id}"]`);
      if (!draft || !warning) continue;
      try {
        stock.plan(state.stockSession, { [group.id]: draft }, context.resource);
        warning.textContent = "";
      } catch (failure) { warning.textContent = failure.message; }
    }
    // Even an incomplete new row shows its scope once its location is chosen.
    for (const addition of state.additions) {
      updateAreaTitle(addition.id, [addition.method]);
      const label = document.querySelector(`[data-addition-targets="${addition.id}"]`);
      let tabs = planned?.additionTargets.get(addition.id);
      if (!tabs && addition.location) {
        try {
          const method = model.choices(statics(context.scope).printMethods)[0]?.description;
          const partial = state.additions.slice(0, state.additions.indexOf(addition) + 1)
            .filter(item => item.location).map(item => ({ ...item, method: item.method || method }));
          tabs = model.plan(state.session, state.changes, partial, statics(context.scope), context.resource).additionTargets.get(addition.id);
        } catch (_) { /* The conflict is explained in the main status. */ }
      }
      label.textContent = tabs ? (tabs.length ? `Will add to tabs ${tabs.join(", ")}; existing areas are kept.` : "Already present on every tab; no areas will be added.")
        : "Choose a location to see affected tabs.";
    }
    const hasWork = Boolean(planned && (planned.edits.length || planned.removes.length || planned.creates.length || stockPlan?.edits.length));
    apply.disabled = Boolean(error || !hasWork || busy());
    document.getElementById("crm-all-tabs-discard").disabled = !pending();
    for (const control of document.querySelectorAll("#crm-all-tabs-panel select:not([ng-model]), #crm-all-tabs-add, .crm-all-tabs-remove, .crm-all-tabs-stock-reset")) {
      control.disabled = busy() || (control.tagName === "SELECT" && control.closest("[data-pending-removal]")?.dataset.pendingRemoval === "true");
    }
    if (error) setStatus(error.message, true);
    else if (hasWork) setStatus(`Ready: ${stockPlan.edits.length} stock rows to change, ${planned.edits.length} existing areas to change, ${planned.removes.length} areas to remove, ${planned.creates.length} areas to add. Affects tabs ${[...new Set([...planned.affectedTabs, ...stockPlan.affectedTabs])].sort((a, b) => a - b).join(", ")}.`);
    else if (pending()) setStatus("No changes are needed. Discard pending changes to return to the current areas.");
    else setStatus("Choose a shared product/color, change a print area or method, or remove an area across its listed tabs.");
  }

  function runInAngular(fn) {
    let result;
    let failure;
    const run = () => { try { result = fn(); } catch (error) { failure = error; } };
    if (context.scope.$root?.$$phase) run();
    else context.scope.$apply(run);
    if (failure) throw failure;
    return result;
  }

  function applyChanges() {
    if (!context || !state || busy()) return;
    if (!context.scope.editMode || routeOrderId() !== context.orderId) { sync(); return; }
    try {
      // Re-read immediately before applying, even if the preview was valid.
      context.resource = context.scope.order.getResource();
      const planned = model.plan(state.session, state.changes, state.additions, statics(context.scope), context.resource);
      const stockPlan = stock.plan(state.stockSession, state.stockDrafts, context.resource);
      if (!planned.edits.length && !planned.removes.length && !planned.creates.length && !stockPlan.edits.length) return;
      const applyAreas = () => model.apply(planned, {
        updateAreaTemplate: scopeFunction(context.scope, "updateAreaTemplate"),
        updateAreaMethod: scopeFunction(context.scope, "updateAreaMethod"),
        addPrintArea: scopeFunction(context.scope, "addPrintArea"),
        removePrintArea: scopeFunction(context.scope, "removePrintArea"),
        copy: value => window.angular.copy(value),
        equals: window.angular.equals
      }, context.resource);
      const result = runInAngular(() => stockPlan.edits.length ? stock.apply(stockPlan, {
        formFor: stockForm,
        changeItem: scopeFunction(context.scope, "changeDesignItem")
      }, applyAreas) : { changedItems: 0, areas: applyAreas() });
      freshState();
      render();
      const areas = result.areas;
      const tabs = [...new Set([...areas.affectedTabs, ...stockPlan.affectedTabs])].sort((a, b) => a - b);
      setStatus(`Applied ${result.changedItems} stock changes, ${areas.changedAreas} existing-area changes, removed ${areas.removedAreas} areas, and added ${areas.addedAreas} areas on tabs ${tabs.join(", ")}. Review the tabs, then Save Order to keep them.`);
    } catch (error) {
      setStatus(error.message, true);
      document.getElementById("crm-all-tabs-apply").disabled = true;
    }
  }

  function setOpen(open) {
    if (state) state.open = open;
    document.documentElement.classList.toggle("crm-all-tabs-view", open);
    const panel = document.getElementById("crm-all-tabs-panel");
    if (panel) panel.hidden = !open;
    for (const id of ["crm-all-tabs-header", "crm-all-tabs-detail"]) {
      const tab = document.getElementById(id);
      tab?.classList.toggle("crm-all-tabs-selected", open);
      const control = tab?.matches("button") ? tab : tab?.querySelector("button");
      control?.setAttribute("aria-pressed", String(open));
    }
  }

  function open() {
    try {
      sync();
      if (!context) return;
      if (!state || !pending()) freshState();
      render();
      setOpen(true);
    } catch (error) {
      const panel = document.getElementById("crm-all-tabs-panel");
      panel?.replaceChildren(element("p", "crm-all-tabs-error", error.message));
      setOpen(true);
    }
  }

  function clear() {
    disposeStockControls();
    for (const draft of Object.values(state?.stockDrafts || {})) window.clearTimeout(draft.timer);
    setOpen(false);
    for (const id of ["crm-all-tabs-header", "crm-all-tabs-detail", "crm-all-tabs-panel"]) document.getElementById(id)?.remove();
    context = null;
    state = null;
  }

  function sync() {
    let next;
    try { next = findContext(); } catch (_) { return; }
    if (!next) { if (context) clear(); return; }
    if (context && (context.orderId !== next.orderId || context.details !== next.details)) clear();
    context = next;
    const content = context.details.querySelector(".tab-content");
    if (!content) return;
    const nativeHeader = document.querySelector('#main-header-design-tabs button[ng-repeat]');
    if (nativeHeader) {
      let tab = document.getElementById("crm-all-tabs-header");
      if (!tab) {
        tab = button("All Tabs", "btn btn-default crm-all-tabs-tab", open);
        tab.id = "crm-all-tabs-header";
        tab.setAttribute("aria-controls", "crm-all-tabs-panel");
      }
      if (nativeHeader.previousElementSibling !== tab) nativeHeader.before(tab);
    }
    const nativeDetail = context.details.querySelector(".nav-tabs .order-form-design-tab");
    if (nativeDetail) {
      let tab = document.getElementById("crm-all-tabs-detail");
      if (!tab) {
        tab = element("li", "crm-all-tabs-tab");
        tab.id = "crm-all-tabs-detail";
        const link = button("All Tabs", "crm-all-tabs-detail-button", open);
        link.setAttribute("aria-controls", "crm-all-tabs-panel");
        tab.append(link);
      }
      if (nativeDetail.previousElementSibling !== tab) nativeDetail.before(tab);
    }
    if (content && !document.getElementById("crm-all-tabs-panel")) {
      const panel = element("section", "crm-all-tabs-panel");
      panel.id = "crm-all-tabs-panel";
      panel.hidden = true;
      content.prepend(panel);
      if (state) { render(); setOpen(state.open); }
    }
    if (state && state.busy !== busy()) {
      state.busy = busy();
      if (document.getElementById("crm-all-tabs-apply")) preview();
    }
  }

  function scheduleSync() {
    if (queued) return;
    queued = true;
    window.setTimeout(() => { queued = false; sync(); }, 60);
  }

  document.addEventListener("click", event => {
    const target = event.target.closest?.("button, a, li.order-form-design-tab");
    if (!target || !context) return;
    if (/^\s*saveOrder\s*\(/.test(target.getAttribute("ng-click") || "") && pending()) {
      event.preventDefault();
      event.stopImmediatePropagation();
      open();
      setStatus("Apply or discard the pending All Tabs changes before Save Order.", true);
      return;
    }
    if (!target.closest(".crm-all-tabs-tab") &&
        (target.closest("#main-header-design-tabs") || target.closest("#order-design-details .nav-tabs"))) setOpen(false);
    scheduleSync();
  }, true);
  window.addEventListener("hashchange", scheduleSync);
  window.addEventListener("popstate", scheduleSync);
  window.addEventListener("focus", scheduleSync);
  const observer = new MutationObserver(scheduleSync);
  observer.observe(document, { subtree: true, childList: true, attributes: true,
    attributeFilter: ["class", "ng-show", "disabled"] });
  // Also catch Angular-only state changes and programmatic route transitions.
  window.setInterval(sync, 800);
  sync();
})();
