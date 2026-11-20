// A bulk edit always targets the original area objects, never a renamed location.
// Kept independent of Angular/DOM so swaps, conflicts, and rollback can be tested.
var CRMAllTabsModel = (() => {
  "use strict";
  const clean = value => String(value ?? "").replace(/\s+/g, " ").trim();
  const key = value => clean(value).toLowerCase();
  const active = values => (values || []).filter(value => value && key(value.crudAction) !== "d");

  function choices(values, activeOnly = false) {
    return (values || []).filter(value => value && clean(value.description) &&
      (!activeOnly || value.active === undefined || value.active === true || String(value.active) === "1"))
      .slice().sort((a, b) => Number(a.sortOrder || 0) - Number(b.sortOrder || 0));
  }

  function resolve(values, wanted, label) {
    const found = values.filter(value => key(value.description) === key(wanted));
    if (found.length !== 1) throw new Error(`Choose an available, unambiguous ${label}: ${clean(wanted) || "none selected"}.`);
    return found[0];
  }

  function createSession(resource, describeArea, describeMethod) {
    if (!Array.isArray(resource?.designs)) throw new Error("CRM design tabs are not ready.");
    const session = { resource, orderId: String(resource.id), allDesigns: resource.designs.slice(),
      describeArea, describeMethod, designs: [], groups: [] };
    const groups = new Map();
    resource.designs.forEach((design, index) => {
      if (!design || key(design.crudAction) === "d") return;
      const record = { design, id: String(design.id ?? ""), tab: index + 1, areas: [] };
      for (const area of active(design.printAreas)) {
        const description = clean(describeArea(area));
        if (!description) throw new Error(`Tab ${record.tab} has an unnamed print area. Name or remove it on that tab first.`);
        const member = { record, area, description, method: clean(describeMethod(area)),
          // Some native controls update IDs before refreshing the description.
          identity: identity(area) };
        record.areas.push(member);
        const sourceKey = key(description);
        if (!groups.has(sourceKey)) {
          const group = { id: `g${groups.size}`, sourceKey, label: description, members: [] };
          groups.set(sourceKey, group);
          session.groups.push(group);
        }
        groups.get(sourceKey).members.push(member);
      }
      session.designs.push(record);
    });
    if (!session.designs.length) throw new Error("No available design tabs were found.");
    return session;
  }

  function identity(area) {
    return JSON.stringify([area.id, area.printAreaId, area.printAreaTemplateId, area.areaTemplateId, area.printMethodId]);
  }

  function protectedFields(area) {
    return Object.fromEntries(Object.entries(area).filter(([name]) => /note|file|upload|artwork|width|height/i.test(name)));
  }

  function assertFresh(session, resource) {
    const changed = () => { throw new Error("The order's tabs or print areas changed. Discard pending changes and read the tabs again."); };
    if (resource !== session.resource || String(resource?.id) !== session.orderId ||
        resource.designs.length !== session.allDesigns.length ||
        resource.designs.some((design, index) => design !== session.allDesigns[index]) ||
        active(resource.designs).length !== session.designs.length) changed();
    for (const record of session.designs) {
      const areas = active(record.design.printAreas);
      if (key(record.design.crudAction) === "d" || String(record.design.id ?? "") !== record.id ||
          areas.length !== record.areas.length) changed();
      record.areas.forEach((member, index) => {
        if (areas[index] !== member.area || clean(session.describeArea(member.area)) !== member.description ||
            clean(session.describeMethod(member.area)) !== member.method || identity(member.area) !== member.identity) changed();
      });
    }
  }

  function plan(session, changes, additions, statics, resource = session.resource) {
    assertFresh(session, resource);
    const templates = choices(statics.printAreaTemplates, true);
    const methods = choices(statics.printMethods);
    const edits = [];
    const removes = [];
    const finalAreas = new Map(session.designs.map(record => [record, []]));
    for (const group of session.groups) {
      const change = changes[group.id] || {};
      if (change.remove) {
        removes.push(...group.members);
        continue;
      }
      const template = change.location && key(change.location) !== group.sourceKey
        ? resolve(templates, change.location, "print-area location") : null;
      const method = change.method ? resolve(methods, change.method, "print method") : null;
      for (const member of group.members) {
        const desiredLocation = template ? clean(template.description) : member.description;
        const desiredMethod = method ? clean(method.description) : member.method;
        if (!desiredMethod) throw new Error(`Choose a print method for ${group.label} on tab ${member.record.tab}.`);
        const edit = { member, template, method: method && key(desiredMethod) !== key(member.method) ? method : null,
          description: desiredLocation, methodDescription: desiredMethod };
        if (edit.template || edit.method) edits.push(edit);
        finalAreas.get(member.record).push({ description: desiredLocation, method: desiredMethod });
      }
    }
    // Check the final layout, so Front <-> Left Chest is valid even on one tab.
    const conflicts = new Map();
    for (const [record, areas] of finalAreas) {
      const counts = new Map();
      for (const area of areas) counts.set(key(area.description), (counts.get(key(area.description)) || 0) + 1);
      for (const [location, count] of counts) {
        if (count < 2) continue;
        if (!conflicts.has(location)) conflicts.set(location, []);
        conflicts.get(location).push(record.tab);
      }
    }
    if (conflicts.size) throw new Error([...conflicts].map(([location, tabs]) =>
      `Duplicate ${location} on tabs ${tabs.join(", ")}. Choose distinct locations or complete the swap before applying.`).join(" "));

    const creates = [];
    const additionTargets = new Map();
    for (const addition of additions) {
      const template = resolve(templates, addition.location, "location for the new area");
      const method = resolve(methods, addition.method, "method for the new area");
      const targets = [];
      for (const [record, areas] of finalAreas) {
        if (areas.some(area => key(area.description) === key(template.description))) continue;
        targets.push(record.tab);
        creates.push({ record, template, method });
        areas.push({ description: clean(template.description), method: clean(method.description) });
      }
      additionTargets.set(addition.id, targets);
    }
    return { session, edits, removes, creates, additionTargets,
      affectedTabs: [...new Set([...edits.map(edit => edit.member.record.tab), ...removes.map(member => member.record.tab),
        ...creates.map(create => create.record.tab)])].sort((a, b) => a - b) };
  }

  function apply(plan, adapter, resource = plan.session.resource) {
    const { session, edits, removes, creates } = plan;
    assertFresh(session, resource);
    if (((creates.length || edits.some(edit => edit.template)) && typeof adapter.updateAreaTemplate !== "function") ||
        ((creates.length || edits.some(edit => edit.method)) && typeof adapter.updateAreaMethod !== "function") ||
        (creates.length && typeof adapter.addPrintArea !== "function") ||
        (removes.length && typeof adapter.removePrintArea !== "function")) {
      throw new Error("CRM print-area controls are unavailable. No changes were applied.");
    }
    // Preserve original objects/arrays, including notes, files, deleted rows, and
    // unrelated draft fields, if a native helper rejects any part of the batch.
    const backups = session.designs.map(({ design }) => ({ design, array: design.printAreas,
      hadAreas: Object.hasOwn(design, "printAreas"), hadCrud: Object.hasOwn(design, "crudAction"), crud: design.crudAction,
      entries: (design.printAreas || []).map(area => ({ area, saved: adapter.copy(area),
        hadHashKey: Object.hasOwn(area || {}, "$$hashKey"), hashKey: area?.$$hashKey })) }));
    const removedAreas = new Set(removes.map(member => member.area));
    try {
      for (const member of removes) adapter.removePrintArea(member.area, member.record.design);
      for (const edit of edits) {
        if (edit.template) adapter.updateAreaTemplate(edit.member.area, edit.template);
        if (edit.method) adapter.updateAreaMethod(edit.member.area, edit.method);
      }
      for (const create of creates) {
        const before = new Set(create.record.design.printAreas || []);
        adapter.addPrintArea(create.record.design);
        const added = (create.record.design.printAreas || []).filter(area => !before.has(area));
        if (added.length !== 1 || !added[0] || key(added[0].crudAction) === "d" ||
            [...before].some(area => !create.record.design.printAreas.includes(area))) throw new Error("CRM did not create exactly one print area.");
        create.area = added[0];
        adapter.updateAreaTemplate(create.area, create.template);
        adapter.updateAreaMethod(create.area, create.method);
      }
      for (const record of session.designs) {
        const expected = record.areas.filter(member => !removedAreas.has(member.area)).map(member => member.area)
          .concat(creates.filter(create => create.record === record).map(create => create.area));
        const actual = active(record.design.printAreas);
        if (actual.length !== expected.length || actual.some((area, index) => area !== expected[index])) {
          throw new Error("CRM did not remove exactly the selected print areas or changed another area's membership.");
        }
      }
      const expected = [
        ...edits.map(edit => ({ area: edit.member.area, description: edit.description, method: edit.methodDescription })),
        ...creates.map(create => ({ area: create.area, description: create.template.description, method: create.method.description }))
      ];
      for (const target of expected) {
        if (key(session.describeArea(target.area)) !== key(target.description) ||
            key(session.describeMethod(target.area)) !== key(target.method)) throw new Error("CRM did not accept a print-area change.");
      }
      for (const backup of backups) {
        for (const { area, saved } of backup.entries) {
          if (!area || removedAreas.has(area)) continue;
          const before = protectedFields(saved);
          const after = protectedFields(area);
          const equal = adapter.equals ? adapter.equals(before, after) : JSON.stringify(before) === JSON.stringify(after);
          if (!equal) throw new Error("CRM changed an existing area's notes, artwork, or dimensions.");
        }
      }
    } catch (error) {
      for (const backup of backups) {
        for (const { area, saved, hadHashKey, hashKey } of backup.entries) {
          if (!area) continue;
          for (const property of Object.keys(area)) delete area[property];
          Object.assign(area, saved);
          if (hadHashKey) area.$$hashKey = hashKey;
        }
        if (backup.array) backup.array.splice(0, backup.array.length, ...backup.entries.map(entry => entry.area));
        if (backup.hadAreas) backup.design.printAreas = backup.array;
        else delete backup.design.printAreas;
        if (backup.hadCrud) backup.design.crudAction = backup.crud;
        else delete backup.design.crudAction;
      }
      throw new Error(`${error.message} The batch was rolled back; review the order before saving.`);
    }
    return { changedAreas: edits.length, removedAreas: removes.length, addedAreas: creates.length, affectedTabs: plan.affectedTabs };
  }

  return { clean, key, choices, createSession, assertFresh, plan, apply };
})();

// Native catalog colors carry their own available size codes. Group by catalog
// identities, count distinct design tabs, and always retain each original item.
var CRMAllTabsStock = (() => {
  "use strict";
  const { clean, key } = CRMAllTabsModel;
  const active = values => (values || []).filter(value => value && key(value.crudAction) !== "d");
  const productId = item => String(item.catalogItemId || item.itemId || "");
  const colorId = item => String(item.catalogItemColorId || item.itemColorId || "");
  const colorIdentity = color => String(color?.type?.[0]?.catalogItemColorId || "");
  const fingerprint = value => JSON.stringify(value, (name, field) => name === "$$hashKey" ? undefined : field);
  const sizeCode = size => clean(size.sizeCode || size.code);
  const number = value => Number(value || 0);
  const price = value => value === "" || value == null ? "" : Number(value);

  function createSession(resource) {
    const groups = new Map();
    const session = { resource, designs: (resource.designs || []).slice(), records: [], groups: [] };
    session.designs.forEach((design, index) => {
      if (!design || key(design.crudAction) === "d") return;
      const record = { design, tab: index + 1, items: (design.designItems || []).slice() };
      session.records.push(record);
      record.items.forEach((item, itemIndex) => {
        if (!item || key(item.crudAction) === "d" || number(item.isStyleSub) || /^style[ _]sub(?:\b|_)/i.test(clean(item.style)) ||
            !productId(item) || !colorId(item) || !clean(item.style) || !clean(item.color)) return;
        const identity = JSON.stringify([productId(item), colorId(item)]);
        if (!groups.has(identity)) groups.set(identity, { id: `s${groups.size}`, identity,
          productId: productId(item), colorId: colorId(item), style: clean(item.style), color: clean(item.color), members: [] });
        groups.get(identity).members.push({ record, item, itemIndex, fingerprint: fingerprint(item) });
      });
    });
    session.groups = [...groups.values()].filter(group => new Set(group.members.map(member => member.record.tab)).size > 1);
    return session;
  }

  function assertFresh(session, resource = session.resource) {
    const fail = () => { throw new Error("The order's tabs or stock changed. Discard pending changes and read the tabs again."); };
    if (resource !== session.resource || resource.designs.length !== session.designs.length ||
        resource.designs.some((design, index) => design !== session.designs[index]) ||
        active(resource.designs).length !== session.records.length) fail();
    for (const { design, items } of session.records) {
      if (key(design.crudAction) === "d" || (design.designItems || []).length !== items.length ||
          items.some((item, index) => item !== design.designItems[index])) fail();
    }
    // Also catch a singleton being changed to/from one of the shared products.
    if (fingerprint(createSession(resource).groups.map(group => [group.identity, group.members.map(member =>
      [member.record.tab, member.itemIndex])])) !== fingerprint(session.groups.map(group => [group.identity,
      group.members.map(member => [member.record.tab, member.itemIndex])]))) fail();
    for (const group of session.groups) for (const member of group.members) {
      if (fingerprint(member.item) !== member.fingerprint) fail();
    }
  }

  function sizesInUse(item) {
    const sizes = active(item.sizes).filter(size => number(size.quantity) !== 0);
    const codes = sizes.map(sizeCode);
    if (codes.some(code => !code) || new Set(codes).size !== codes.length ||
        sizes.some(size => !Number.isFinite(number(size.quantity)) || number(size.quantity) < 0)) {
      throw new Error("A stock row has ambiguous sizes or quantities. Review that tab before using All Tabs.");
    }
    return sizes;
  }

  function summarizeSizes(group) {
    const order = new Map();
    const totals = new Map();
    let unspecified = 0;
    for (const { item } of group.members) {
      for (const size of active(item.sizes)) {
        const code = sizeCode(size);
        if (code && !order.has(code)) order.set(code, Number.isFinite(Number(size.sortOrder)) ? Number(size.sortOrder) : order.size);
      }
      const used = sizesInUse(item);
      for (const size of used) {
        const code = sizeCode(size);
        totals.set(code, (totals.get(code) || 0) + number(size.quantity));
      }
      if (String(item.splitIntoSizes) === "0") {
        unspecified += Math.max(0, number(item.quantity) - used.reduce((sum, size) => sum + number(size.quantity), 0));
      }
    }
    const sizes = [...totals].sort(([a], [b]) => order.get(a) - order.get(b))
      .map(([code, quantity]) => ({ code, quantity }));
    if (unspecified) sizes.push({ code: "Unspecified", quantity: unspecified });
    return sizes;
  }

  function sizeState(item) {
    return fingerprint({ id: item.id, split: String(item.splitIntoSizes ?? "1"),
      quantity: number(item.quantity), price: price(item.pricePerPiece),
      sizes: sizesInUse(item).map(size => ({ code: sizeCode(size), quantity: number(size.quantity),
        price: price(size.pricePerPiece), names: size.namesList })).sort((a, b) => a.code.localeCompare(b.code)) });
  }

  function plan(session, drafts, resource = session.resource) {
    const edits = [];
    if (!Object.values(drafts).some(draft => draft.touched)) return { session, edits, affectedTabs: [] };
    assertFresh(session, resource);
    for (const group of session.groups) {
      const draft = drafts[group.id];
      if (!draft?.touched) continue;
      const form = draft.form;
      const catalog = form?.selectedCatalogItem;
      const color = form?.selectedColor;
      if (draft.error) throw new Error(draft.error);
      if (!catalog || key(form.selectedCatalogItemStyle) !== key(catalog.style)) {
        throw new Error(`Choose a product from CRM's suggestions for ${group.style} ${group.color}.`);
      }
      if (/^style[ _]sub(?:\b|_)/i.test(catalog.style) || number(catalog.isStyleSub)) {
        throw new Error("Choose a catalog product. Style Sub is not supported by All Tabs stock editing.");
      }
      if (draft.loading || !colorIdentity(color) || !Array.isArray(color.sizes) ||
          !form.colors.includes(color) || String(color.catalogitem_id) !== String(catalog.id)) {
        throw new Error(`Waiting for CRM's colors and sizes for ${clean(catalog.style)}. Choose an available color.`);
      }
      const missing = new Map();
      for (const member of group.members) for (const size of sizesInUse(member.item)) {
        const code = sizeCode(size);
        if (color.sizes.filter(available => clean(available.code) === code).length === 1) continue;
        if (!missing.has(code)) missing.set(code, new Set());
        missing.get(code).add(member.record.tab);
      }
      if (missing.size) throw new Error(`${clean(catalog.style)} ${clean(color.name)} is missing sizes: ${[...missing]
        .map(([code, tabs]) => `${code} (tabs ${[...tabs].join(", ")})`).join("; ")}. Choose a product/color with every size in use.`);
      if (String(catalog.id) === group.productId && colorIdentity(color) === group.colorId) continue;
      for (const member of group.members) edits.push({ member, catalog, color, colors: form.colors, before: sizeState(member.item) });
    }
    return { session, edits, affectedTabs: [...new Set(edits.map(edit => edit.member.record.tab))].sort((a, b) => a - b) };
  }

  // A failed native stock/print batch restores the existing resource graph,
  // including original arrays, nested size objects, and Angular repeat keys.
  function checkpoint(roots) {
    const saved = new Map();
    function visit(value) {
      if (!value || typeof value !== "object" || saved.has(value)) return;
      const entries = Object.entries(value);
      saved.set(value, { entries, length: Array.isArray(value) ? value.length : null });
      for (const [, child] of entries) visit(child);
    }
    roots.forEach(visit);
    return () => {
      for (const [value, { entries, length }] of saved) {
        for (const name of Object.keys(value)) delete value[name];
        if (length !== null) value.length = length;
        for (const [name, field] of entries) value[name] = field;
      }
    };
  }

  function apply(planned, adapter, applyAreas = () => {}) {
    const { session, edits } = planned;
    assertFresh(session);
    const forms = edits.map(edit => adapter.formFor(edit.member));
    if (forms.some((form, index) => !form || form.item !== edits[index].member.item) || typeof adapter.changeItem !== "function") {
      throw new Error("CRM's native stock controls are unavailable. No changes were applied.");
    }
    const restore = checkpoint([session.resource, ...forms]);
    try {
      edits.forEach((edit, index) => {
        const form = forms[index];
        form.selectedCatalogItem = edit.catalog;
        form.selectedCatalogItemStyle = edit.catalog.style;
        form.selectedColor = edit.color;
        form.colors = edit.colors;
        adapter.changeItem(form);
        if (form.item !== edit.member.item || productId(form.item) !== String(edit.catalog.id) ||
            colorId(form.item) !== colorIdentity(edit.color) || key(form.item.style) !== key(edit.catalog.style) ||
            key(form.item.color) !== key(edit.color.name)) throw new Error("CRM did not accept a stock change.");
        if (sizeState(form.item) !== edit.before) throw new Error("CRM changed a stock row's sizes, quantities, names, or prices.");
      });
      const areas = applyAreas();
      return { changedItems: edits.length, affectedTabs: planned.affectedTabs, areas };
    } catch (error) {
      restore();
      throw new Error(`${error.message} The batch was rolled back; review the order before saving.`);
    }
  }

  return { createSession, assertFresh, sizesInUse, summarizeSizes, plan, apply, colorIdentity, productId, colorId };
})();
