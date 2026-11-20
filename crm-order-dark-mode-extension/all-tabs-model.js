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
    const finalAreas = new Map(session.designs.map(record => [record, []]));
    for (const group of session.groups) {
      const change = changes[group.id] || {};
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
    return { session, edits, creates, additionTargets,
      affectedTabs: [...new Set([...edits.map(edit => edit.member.record.tab), ...creates.map(create => create.record.tab)])].sort((a, b) => a - b) };
  }

  function apply(plan, adapter, resource = plan.session.resource) {
    const { session, edits, creates } = plan;
    assertFresh(session, resource);
    if (typeof adapter.updateAreaTemplate !== "function" || typeof adapter.updateAreaMethod !== "function" ||
        (creates.length && typeof adapter.addPrintArea !== "function")) {
      throw new Error("CRM print-area controls are unavailable. No changes were applied.");
    }
    // Preserve original objects/arrays, including notes, files, deleted rows, and
    // unrelated draft fields, if a native helper rejects any part of the batch.
    const backups = session.designs.map(({ design }) => ({ design, array: design.printAreas,
      hadAreas: Object.hasOwn(design, "printAreas"), hadCrud: Object.hasOwn(design, "crudAction"), crud: design.crudAction,
      entries: (design.printAreas || []).map(area => ({ area, saved: adapter.copy(area),
        hadHashKey: Object.hasOwn(area || {}, "$$hashKey"), hashKey: area?.$$hashKey })) }));
    try {
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
          if (!area) continue;
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
    return { changedAreas: edits.length, addedAreas: creates.length, affectedTabs: plan.affectedTabs };
  }

  return { clean, key, choices, createSession, assertFresh, plan, apply };
})();
