import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../crm-order-dark-mode-extension/all-tabs-model.js", import.meta.url), "utf8");
const context = vm.createContext({});
vm.runInContext(source, context);
const model = context.CRMAllTabsModel;
const plain = value => JSON.parse(JSON.stringify(value));
const statics = {
  printAreaTemplates: ["Front", "Back", "Front-Left Chest", "Side Left"].map((description, i) => ({ id: i + 1, description, active: "1" })),
  printMethods: ["HD Digital", "Embroidery"].map((description, i) => ({ id: i + 1, description }))
};
const area = (description, printMethodDescription = "HD Digital") => ({ description, printMethodDescription,
  notes: [{ text: "Keep this tab's note" }], designFiles: [{ id: 42 }], width: "12", height: "10" });
function fixture(layout = [["Front"], ["Front", "Back"], ["Front-Left Chest"]]) {
  const resource = { id: "1234567", designs: layout.map((locations, i) => ({ id: i + 100, designId: "shared-artwork-id",
    printAreas: locations.map(location => area(location)), designItems: [{ pricePerPiece: "25.00" }] })) };
  const session = model.createSession(resource, value => value.description, value => value.printMethodDescription);
  const adapter = {
    copy: structuredClone,
    updateAreaTemplate(value, template) { value.description = template.description; value.printAreaTemplateId = template.id; value.crudAction = "u"; },
    updateAreaMethod(value, method) { value.printMethodDescription = method.description; value.printMethodId = method.id; value.crudAction = "u"; },
    addPrintArea(design) { design.printAreas.push({ crudAction: "c", notes: [], designFiles: [] }); },
    removePrintArea(value) { value.crudAction = "d"; }
  };
  return { resource, session, adapter, plan: (changes = {}, additions = [], values = statics) => model.plan(session, changes, additions, values) };
}

test("the union lists each original location with its real affected tabs", () => {
  const { session } = fixture();
  assert.deepEqual(plain(session.groups.map(group => [group.label, group.members.map(member => member.record.tab)])),
    [["Front", [1, 2]], ["Back", [2]], ["Front-Left Chest", [3]]]);
  assert.equal(session.designs.length, 3);
});

test("simultaneous Front/chest swap uses original rows and preserves per-tab artwork, notes and prices", () => {
  const { resource, session, adapter, plan } = fixture();
  const originals = resource.designs.map(design => design.printAreas.slice());
  const before = structuredClone(resource);
  const [front, back, chest] = session.groups;
  const result = model.apply(plan({ [front.id]: { location: "front-left chest" },
    [chest.id]: { location: "front" }, [back.id]: { location: "side left" } }), adapter);
  assert.deepEqual(resource.designs.map(design => design.printAreas.map(value => value.description)),
    [["Front-Left Chest"], ["Front-Left Chest", "Side Left"], ["Front"]]);
  assert.deepEqual(plain(result), { changedAreas: 4, removedAreas: 0, addedAreas: 0, affectedTabs: [1, 2, 3] });
  resource.designs.forEach((design, i) => {
    assert.deepEqual(design.designItems, before.designs[i].designItems);
    design.printAreas.forEach((value, j) => {
      assert.equal(value, originals[i][j]);
      for (const field of ["notes", "designFiles", "width", "height"]) assert.deepEqual(value[field], before.designs[i].printAreas[j][field]);
    });
  });
});

test("swaps on the same tab pass final-state collision checking", () => {
  const { resource, session, plan, adapter } = fixture([["Front", "Front-Left Chest"]]);
  const [front, chest] = session.groups;
  assert.throws(() => plan({ [front.id]: { location: "front-left chest" } }), /Duplicate front-left chest on tabs 1/);
  model.apply(plan({ [front.id]: { location: "front-left chest" }, [chest.id]: { location: "front" } }), adapter);
  assert.deepEqual(resource.designs[0].printAreas.map(value => value.description), ["Front-Left Chest", "Front"]);
});

test("every conflicting tab is reported before any mutation", () => {
  const { resource, session, plan } = fixture([["Front", "Back"], ["Front"], ["Back", "Front"]]);
  const before = structuredClone(resource);
  assert.throws(() => plan({ [session.groups[0].id]: { location: "back" } }), /Duplicate back on tabs 1, 3/);
  assert.deepEqual(resource, before);
});

test("location-only edits retain mixed methods; explicit methods update each matching original area", () => {
  const { resource, adapter } = fixture([["Front"], ["Front"]]);
  resource.designs[1].printAreas[0].printMethodDescription = "Embroidery";
  const read = () => model.createSession(resource, value => value.description, value => value.printMethodDescription);
  let session = read();
  model.apply(model.plan(session, { g0: { location: "back" } }, [], statics), adapter);
  assert.deepEqual(resource.designs.map(design => design.printAreas[0].printMethodDescription), ["HD Digital", "Embroidery"]);
  session = read();
  model.apply(model.plan(session, { g0: { method: "embroidery" } }, [], statics), adapter);
  assert.deepEqual(resource.designs.map(design => design.printAreas[0].printMethodDescription), ["Embroidery", "Embroidery"]);
});

test("adding an area fills only missing locations and preserves existing methods and files", () => {
  const { resource, plan, adapter } = fixture();
  const existingBack = resource.designs[1].printAreas[1];
  const expected = structuredClone(existingBack);
  const planned = plan({}, [{ id: "new", location: "back", method: "embroidery" }]);
  assert.deepEqual(plain(planned.additionTargets.get("new")), [1, 3]);
  model.apply(planned, adapter);
  assert.deepEqual(resource.designs.map(design => design.printAreas.length), [2, 2, 2]);
  assert.equal(resource.designs[1].printAreas[1], existingBack);
  assert.deepEqual(existingBack, expected);
  assert.equal(resource.designs[0].printAreas[1].printMethodDescription, "Embroidery");
  assert.notEqual(resource.designs[0].printAreas[1], resource.designs[2].printAreas[1]);
});

test("addition targets use the completed swaps and never create duplicate new locations", () => {
  const { session, plan } = fixture([["Front"], ["Front"]]);
  const planned = plan({ [session.groups[0].id]: { location: "back" } }, [
    { id: "a", location: "front", method: "hd digital" }, { id: "b", location: "front", method: "embroidery" }
  ]);
  assert.deepEqual(plain(planned.additionTargets.get("a")), [1, 2]);
  assert.deepEqual(plain(planned.additionTargets.get("b")), []);
  assert.equal(planned.creates.length, 2);
});

test("deleted designs/areas are excluded without renumbering real tabs", () => {
  const { resource } = fixture([["Front"], ["Front"], ["Back"]]);
  resource.designs[0].crudAction = "d";
  resource.designs[1].printAreas.push({ ...area("Back"), crudAction: "d" });
  const session = model.createSession(resource, value => value.description, value => value.printMethodDescription);
  assert.deepEqual(plain(session.groups.map(group => group.members.map(member => member.record.tab))), [[2], [3]]);
});

test("changed tabs, IDs, memberships, locations and methods stop stale batches", () => {
  for (const change of [
    resource => resource.designs.reverse(),
    resource => resource.designs.push({ id: 999, printAreas: [] }),
    resource => { resource.designs[0].id = "different"; },
    resource => { resource.designs[0].crudAction = "d"; },
    resource => { resource.designs[0].printAreas[0].description = "Back"; },
    resource => { resource.designs[0].printAreas[0].printMethodId = 999; },
    resource => { resource.designs[0].printAreas[0].printMethodDescription = "Embroidery"; },
    resource => { resource.designs[0].printAreas[0].crudAction = "d"; },
    resource => resource.designs[0].printAreas.push(area("Back"))
  ]) {
    const { resource, plan } = fixture();
    change(resource);
    assert.throws(() => plan({ g0: { location: "side left" } }), /changed.*Discard/);
  }
  const { resource, session } = fixture();
  assert.throws(() => model.assertFresh(session, structuredClone(resource)), /changed/);
});

test("unrelated native draft edits are preserved while applying a bulk location edit", () => {
  const { resource, plan, adapter } = fixture();
  resource.designs[0].printAreas[0].notes.push({ text: "An additional native note" });
  resource.designs[0].designItems[0].pricePerPiece = "26.00";
  model.apply(plan({ g0: { location: "side left" } }), adapter);
  assert.equal(resource.designs[0].printAreas[0].notes.at(-1).text, "An additional native note");
  assert.equal(resource.designs[0].designItems[0].pricePerPiece, "26.00");
});

test("invalid or ambiguous catalog selections and incomplete additions stop before editing", () => {
  const { resource, plan } = fixture();
  const before = structuredClone(resource);
  for (const change of [{ location: "nonexistent" }, { method: "nonexistent" }]) assert.throws(() => plan({ g0: change }), /available, unambiguous/);
  assert.throws(() => plan({}, [{ id: "a", location: "back" }]), /method for the new area/);
  assert.throws(() => plan({ g0: { location: "back" } }, [], { ...statics,
    printAreaTemplates: [...statics.printAreaTemplates, { description: "Back", active: "1" }] }), /unambiguous/);
  assert.deepEqual(resource, before);
});

test("a native failure rolls back previous edits, additions and nested note changes", () => {
  const { resource, plan, adapter } = fixture();
  const before = structuredClone(resource);
  const arrays = resource.designs.map(design => design.printAreas);
  const areas = arrays.map(values => values.slice());
  let count = 0;
  const update = adapter.updateAreaTemplate;
  adapter.updateAreaTemplate = (value, template) => {
    update(value, template);
    value.notes?.push({ text: "native intermediate update" });
    if (++count === 4) throw new Error("Native helper rejected this change");
  };
  const planned = plan({ g0: { location: "side left" } }, [{ id: "a", location: "front", method: "embroidery" }]);
  assert.throws(() => model.apply(planned, adapter), /rolled back/);
  assert.deepEqual(resource, before);
  resource.designs.forEach((design, i) => {
    assert.equal(design.printAreas, arrays[i]);
    design.printAreas.forEach((value, j) => assert.equal(value, areas[i][j]));
  });
});

test("rejected writes and malformed native additions are verified and rolled back", () => {
  for (const broken of [
    { updateAreaTemplate() {} },
    { addPrintArea() {} },
    { addPrintArea(design) { design.printAreas.push({}, {}); } }
  ]) {
    const { resource, plan, adapter } = fixture();
    const before = structuredClone(resource);
    Object.assign(adapter, broken);
    const planned = plan({ g0: { location: "side left" } }, [{ id: "a", location: "front", method: "hd digital" }]);
    assert.throws(() => model.apply(planned, adapter), /rolled back/);
    assert.deepEqual(resource, before);
  }
});

test("native helpers cannot silently change original notes, artwork or dimensions", () => {
  for (const field of ["notes", "designFiles", "width", "height"]) {
    const { resource, plan, adapter } = fixture();
    const before = structuredClone(resource);
    const update = adapter.updateAreaTemplate;
    adapter.updateAreaTemplate = (value, template) => { update(value, template); delete value[field]; };
    assert.throws(() => model.apply(plan({ g0: { location: "side left" } }), adapter), /notes, artwork, or dimensions.*rolled back/);
    assert.deepEqual(resource, before);
  }
});

test("rollback supports tabs without printAreas and sparse/deleted native entries", () => {
  const { resource, adapter } = fixture([["Front"], []]);
  delete resource.designs[1].printAreas;
  resource.designs[0].printAreas.push(null, { ...area("Back"), crudAction: "d" });
  const before = structuredClone(resource);
  const session = model.createSession(resource, value => value.description, value => value.printMethodDescription);
  adapter.addPrintArea = design => { design.printAreas ||= []; design.printAreas.push({}); design.crudAction = "u"; };
  adapter.updateAreaMethod = () => { throw new Error("Native failure"); };
  assert.throws(() => model.apply(model.plan(session, {}, [{ id: "a", location: "back", method: "embroidery" }], statics), adapter), /rolled back/);
  assert.deepEqual(resource, before);
});

test("rollback retains Angular repeat identities when angular.copy omits hash keys", () => {
  const { resource, plan, adapter } = fixture();
  resource.designs[0].printAreas[0].$$hashKey = "object:original";
  const before = structuredClone(resource);
  adapter.copy = value => { const copy = structuredClone(value); delete copy.$$hashKey; return copy; };
  adapter.updateAreaTemplate = () => { throw new Error("Native failure"); };
  assert.throws(() => model.apply(plan({ g0: { location: "side left" } }), adapter), /rolled back/);
  assert.deepEqual(resource, before);
});

test("all-tabs assets stay scoped to CRM alongside the popup's scripting permission", async () => {
  const manifest = JSON.parse(await readFile(new URL("../crm-order-dark-mode-extension/manifest.json", import.meta.url), "utf8"));
  const content = manifest.content_scripts.find(script => script.js.includes("all-tabs.js"));
  assert.equal(content.world, "MAIN");
  assert.equal(content.all_frames, true);
  assert.deepEqual(content.matches, ["https://crm2.legacy.printfly.com/*"]);
  assert.deepEqual(content.js, ["all-tabs-model.js", "all-tabs.js"]);
  assert.deepEqual(manifest.permissions, ["storage", "tabs", "webNavigation", "scripting"]);
});

test("removal targets the exact original location without removing either chest area", () => {
  const { resource, plan, adapter } = fixture([["Front", "Front-Left Chest", "Front-Right Chest"], ["Front", "Back"], ["Front-Left Chest"]]);
  const before = structuredClone(resource);
  const planned = plan({ g0: { remove: true } });
  assert.deepEqual(plain(planned.removes.map(member => member.record.tab)), [1, 2]);
  assert.deepEqual(resource, before, "Planning a removal must not change the native order");
  const result = model.apply(planned, adapter);
  assert.deepEqual(resource.designs.map(design => design.printAreas.filter(value => value.crudAction !== "d").map(value => value.description)),
    [["Front-Left Chest", "Front-Right Chest"], ["Back"], ["Front-Left Chest"]]);
  assert.deepEqual(plain(result), { changedAreas: 0, removedAreas: 2, addedAreas: 0, affectedTabs: [1, 2] });
  for (const [i, design] of resource.designs.entries()) {
    assert.deepEqual(design.designItems, before.designs[i].designItems);
    for (const [j, value] of design.printAreas.entries()) {
      for (const field of ["notes", "designFiles", "width", "height"]) assert.deepEqual(value[field], before.designs[i].printAreas[j][field]);
    }
  }
});

test("pending removal overrides that row's pending rename and method without changing its original targets", () => {
  const { resource, plan, adapter } = fixture([["Front", "Back"], ["Front"], ["Back"]]);
  const planned = plan({ g0: { remove: true, location: "not a template", method: "not a method" } });
  assert.equal(planned.edits.length, 0);
  model.apply(planned, adapter);
  assert.deepEqual(resource.designs.map(design => design.printAreas.filter(value => value.crudAction !== "d").map(value => value.description)),
    [["Back"], [], ["Back"]]);
});

test("removing an original Back lets Front move to Back without a final collision", () => {
  const { resource, plan, adapter } = fixture([["Front", "Back"]]);
  model.apply(plan({ g0: { location: "back" }, g1: { remove: true } }), adapter);
  assert.equal(resource.designs[0].printAreas[0].description, "Back");
  assert.equal(resource.designs[0].printAreas[1].crudAction, "d");
});

test("removal and addition of the same location creates only the new requested areas", () => {
  const { resource, plan, adapter } = fixture([["Front"], ["Back"]]);
  const original = resource.designs[0].printAreas[0];
  const planned = plan({ g0: { remove: true } }, [{ id: "new", location: "front", method: "embroidery" }]);
  assert.deepEqual(plain(planned.additionTargets.get("new")), [1, 2]);
  model.apply(planned, adapter);
  for (const design of resource.designs) {
    const front = design.printAreas.filter(value => value.crudAction !== "d" && value.description === "Front");
    assert.equal(front.length, 1);
    assert.equal(front[0].printMethodDescription, "Embroidery");
    assert.notEqual(front[0], original);
    assert.deepEqual(front[0].designFiles, []);
  }
});

test("removing all groups preserves tab identities with both native soft-delete and splice behavior", () => {
  for (const splice of [false, true]) {
    const { resource, session, plan, adapter } = fixture();
    const designs = resource.designs.slice();
    if (splice) adapter.removePrintArea = (value, design) => design.printAreas.splice(design.printAreas.indexOf(value), 1);
    model.apply(plan(Object.fromEntries(session.groups.map(group => [group.id, { remove: true }]))), adapter);
    assert.equal(resource.designs.length, 3);
    resource.designs.forEach((design, i) => {
      assert.equal(design, designs[i]);
      assert.deepEqual(design.printAreas.filter(value => value.crudAction !== "d"), []);
    });
  }
});

test("a no-op or wrong-target native removal is rejected and fully rolled back", () => {
  for (const remove of [() => {}, (_value, design) => design.printAreas.splice(1, 1)]) {
    const { resource, plan, adapter } = fixture([["Front", "Back"]]);
    const before = structuredClone(resource);
    const originalArray = resource.designs[0].printAreas;
    const originalAreas = originalArray.slice();
    adapter.removePrintArea = remove;
    assert.throws(() => model.apply(plan({ g0: { remove: true } }), adapter), /selected print areas.*rolled back/);
    assert.deepEqual(resource, before);
    assert.equal(resource.designs[0].printAreas, originalArray);
    resource.designs[0].printAreas.forEach((value, i) => assert.equal(value, originalAreas[i]));
  }
});

test("later failures roll back removals, edits and additions, preserving repeat identities and nested notes/files", () => {
  const { resource, plan, adapter } = fixture([["Front", "Back"], ["Front", "Back"]]);
  resource.designs[0].printAreas[0].$$hashKey = "object:front";
  const before = structuredClone(resource);
  const arrays = resource.designs.map(design => design.printAreas);
  const objects = arrays.map(values => values.slice());
  adapter.removePrintArea = (value, design) => { value.notes.length = 0; value.designFiles.length = 0; design.printAreas.splice(design.printAreas.indexOf(value), 1); };
  const update = adapter.updateAreaMethod;
  adapter.updateAreaMethod = (value, method) => { update(value, method); throw new Error("Addition rejected"); };
  const planned = plan({ g0: { remove: true }, g1: { location: "side left" } }, [{ id: "new", location: "front", method: "embroidery" }]);
  assert.throws(() => model.apply(planned, adapter), /Addition rejected.*rolled back/);
  assert.deepEqual(resource, before);
  resource.designs.forEach((design, i) => {
    assert.equal(design.printAreas, arrays[i]);
    design.printAreas.forEach((value, j) => assert.equal(value, objects[i][j]));
  });
});

test("removal requires a native helper before any other batch writes", () => {
  const { resource, plan, adapter } = fixture();
  const before = structuredClone(resource);
  delete adapter.removePrintArea;
  assert.throws(() => model.apply(plan({ g0: { remove: true }, g1: { location: "side left" } }), adapter), /controls are unavailable/);
  assert.deepEqual(resource, before);
});

test("a removal-only batch needs only CRM's removal helper", () => {
  const { resource, plan, adapter } = fixture([["Front"]]);
  delete adapter.updateAreaTemplate;
  delete adapter.updateAreaMethod;
  delete adapter.addPrintArea;
  model.apply(plan({ g0: { remove: true } }), adapter);
  assert.equal(resource.designs[0].printAreas[0].crudAction, "d");
});
