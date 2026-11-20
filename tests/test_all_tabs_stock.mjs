import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const context = vm.createContext({});
vm.runInContext(await readFile(new URL("../crm-order-dark-mode-extension/all-tabs-model.js", import.meta.url), "utf8"), context);
const stock = context.CRMAllTabsStock;
const plain = value => JSON.parse(JSON.stringify(value));
const codes = ["S", "M", "L", "XL", "2XL", "3XL"];
const original = { id: 10, style: "NF1270" };
const replacement = { id: 20, style: "ST350" };
function color(catalog, id, name, sizes = codes) {
  return { name, code: name, catalogitem_id: catalog.id, type: [{ catalogItemColorId: id }],
    sizes: sizes.map((code, i) => ({ code, catalogItemSizeId: id * 100 + i })) };
}
const originalColor = color(original, 100, "BLACK/WHITE");
const black = color(replacement, 200, "BLACK");
const white = color(replacement, 201, "WHITE");
function item(id, size = "M", catalog = original, shade = originalColor) {
  return { id, catalogItemId: catalog.id, catalogItemColorId: shade.type[0].catalogItemColorId,
    style: catalog.style, color: shade.name, splitIntoSizes: "1", quantity: 1, pricePerPiece: "43.89", isStyleSub: 0,
    sizes: codes.map((code, i) => ({ id: id * 10 + i, sizeCode: code, quantity: code === size ? 1 : 0,
      pricePerPiece: code === size ? "43.89" : "0.00", namesList: code === size ? [{ name: "Keep this name" }] : [] })) };
}
function fixture(items = [item(1, "L"), item(2, "M"), item(3, "M"), item(4, "L")]) {
  const resource = { id: "1234567", salesNotes: "Unrelated draft", designs: items.map((value, i) => ({ id: 100 + i,
    designItems: Array.isArray(value) ? value : [value], printAreas: [{ description: "Front", notes: ["Own artwork"] }] })) };
  const forms = new Map(resource.designs.flatMap(design => design.designItems).map(value => [value,
    { item: value, originalItem: structuredClone(value), selectedCatalogItem: original, selectedCatalogItemStyle: value.style,
      colors: [originalColor], selectedColor: originalColor, loading: {} }]));
  const adapter = {
    formFor: member => forms.get(member.item),
    changeItem(form) {
      const value = form.item;
      const old = value.sizes.filter(size => size.crudAction !== "d");
      Object.assign(value, { catalogItemId: form.selectedCatalogItem.id,
        catalogItemColorId: form.selectedColor.type[0].catalogItemColorId,
        style: form.selectedCatalogItem.style, color: form.selectedColor.name, crudAction: "u" });
      value.sizes = form.selectedColor.sizes.map(size => ({ ...(old.find(previous => previous.sizeCode === size.code) || {}),
        sizeCode: size.code, catalogItemSizeId: size.catalogItemSizeId,
        quantity: old.find(previous => previous.sizeCode === size.code)?.quantity || 0 }));
      form.hideInfo = false;
    }
  };
  const session = stock.createSession(resource);
  const drafts = {};
  const choose = (catalog = replacement, shade = black, group = session.groups[0]) => {
    drafts[group.id] = { touched: true, form: { selectedCatalogItem: catalog,
      selectedCatalogItemStyle: catalog.style, selectedColor: shade, colors: [shade] } };
    return stock.plan(session, drafts);
  };
  return { resource, forms, session, drafts, adapter, choose };
}

test("the supplied four-tab pattern groups NF1270 BLACK/WHITE independently of L/M sizes", () => {
  const { session } = fixture();
  assert.deepEqual(plain(session.groups.map(group => [group.style, group.color, group.members.map(member => member.record.tab)])),
    [["NF1270", "BLACK/WHITE", [1, 2, 3, 4]]]);
});

test("mixed products include only exact catalog product/color pairs shared by distinct tabs", () => {
  const other = { id: 30, style: "G500" };
  const { session } = fixture([item(1, "M", other, color(other, 300, "BLACK")), item(2, "M", replacement, black),
    item(3, "L", replacement, black), item(4, "L", replacement, white)]);
  assert.deepEqual(plain(session.groups.map(group => [group.style, group.color, group.members.map(member => member.record.tab)])),
    [["ST350", "BLACK", [2, 3]]]);
  assert.equal(fixture([[item(1), item(2)], item(3, "L", replacement, black)]).session.groups.length, 0);
});

test("deleted tabs/items and Style Subs do not count toward shared stock", () => {
  const { resource } = fixture();
  resource.designs[0].crudAction = "d";
  resource.designs[1].designItems[0].crudAction = "d";
  resource.designs[2].designItems[0].isStyleSub = 1;
  assert.equal(stock.createSession(resource).groups.length, 0);
});

test("native Apply preserves each original quantity, price, names, item identity and artwork", () => {
  const { resource, adapter, choose } = fixture();
  const before = structuredClone(resource);
  const items = resource.designs.map(design => design.designItems[0]);
  const planned = choose();
  assert.deepEqual(resource, before, "Planning must not write to the order");
  assert.equal(stock.apply(planned, adapter).changedItems, 4);
  resource.designs.forEach((design, i) => {
    const value = design.designItems[0];
    assert.equal(value, items[i]);
    assert.equal(value.style, "ST350");
    assert.equal(value.color, "BLACK");
    assert.deepEqual(value.sizes.filter(size => size.quantity).map(size => [size.sizeCode, size.quantity, size.pricePerPiece, size.namesList]),
      before.designs[i].designItems[0].sizes.filter(size => size.quantity).map(size => [size.sizeCode, size.quantity, size.pricePerPiece, size.namesList]));
    assert.deepEqual(design.printAreas, before.designs[i].printAreas);
  });
});

test("a color-only change targets matching rows and leaves other colors alone", () => {
  const { resource, adapter, choose } = fixture([item(1, "L", replacement, black), item(2, "M", replacement, black),
    item(3, "S", replacement, white)]);
  const untouched = structuredClone(resource.designs[2]);
  stock.apply(choose(replacement, white), adapter);
  assert.equal(resource.designs[0].designItems[0].color, "WHITE");
  assert.equal(resource.designs[1].designItems[0].color, "WHITE");
  assert.deepEqual(resource.designs[2], untouched);
});

test("missing required sizes name every affected tab and block the entire batch before writes", () => {
  const { resource, choose } = fixture();
  const before = structuredClone(resource);
  assert.throws(() => choose(replacement, color(replacement, 210, "NAVY", ["S", "M"])), /missing sizes: L \(tabs 1, 4\)/);
  assert.deepEqual(resource, before);
  // Sizes that have zero quantity do not need to exist in the replacement.
  assert.equal(choose(replacement, color(replacement, 210, "NAVY", ["M", "L"])).edits.length, 4);
});

test("multiple missing sizes and duplicate native size codes cannot slip through", () => {
  const { choose } = fixture([item(1, "3XL"), item(2, "2XL")]);
  assert.throws(() => choose(replacement, color(replacement, 210, "NAVY", ["M"])), /3XL \(tabs 1\); 2XL \(tabs 2\)/);
  assert.throws(() => choose(replacement, color(replacement, 210, "NAVY", ["3XL", "2XL", "2XL"])), /missing sizes: 2XL/);
});

test("unselected autocomplete text, incomplete loads, wrong-product colors and Style Sub block Apply", () => {
  const { choose, drafts, session } = fixture();
  choose();
  const draft = drafts[session.groups[0].id];
  draft.form.selectedCatalogItemStyle = "Typed text";
  assert.throws(() => stock.plan(session, drafts), /CRM's suggestions/);
  choose();
  drafts[session.groups[0].id].loading = true;
  assert.throws(() => stock.plan(session, drafts), /Waiting/);
  assert.throws(() => choose(replacement, originalColor), /Waiting/);
  assert.throws(() => choose({ id: 90, style: "Style Sub" }, black), /catalog product/);
});

test("returning to the original stock is a no-op", () => {
  const { choose } = fixture();
  assert.equal(choose(original, originalColor).edits.length, 0);
});

test("stale quantities, item membership and a changed singleton stop the batch", () => {
  for (const mutate of [
    resource => { resource.designs[0].designItems[0].sizes[2].quantity = 9; },
    resource => { resource.designs[0].designItems[0].catalogItemColorId = 300; },
    resource => resource.designs[0].designItems.push(item(90)),
    resource => { resource.designs[1].crudAction = "d"; },
    resource => { resource.designs[0].designItems = [structuredClone(resource.designs[0].designItems[0])]; }
  ]) {
    const { resource, choose } = fixture();
    mutate(resource);
    assert.throws(() => choose(), /stock changed/);
  }
  const { resource, session, drafts, choose } = fixture([item(1), item(2), item(3, "L", replacement, black)]);
  choose();
  Object.assign(resource.designs[2].designItems[0], item(3));
  assert.throws(() => stock.plan(session, drafts), /stock changed/);
});

test("native failures or silently changed quantities/prices/names restore all original graph identities", () => {
  for (const fail of [() => { throw new Error("Native rejected"); }, form => { form.item.sizes.find(size => size.quantity).quantity++; },
    form => { form.item.sizes.find(size => size.quantity).pricePerPiece = "9"; },
    form => { delete form.item.sizes.find(size => size.quantity).namesList; }]) {
    const { resource, forms, adapter, choose } = fixture();
    resource.designs[0].designItems[0].sizes[2].$$hashKey = "size:original";
    const before = structuredClone(resource);
    const sizes = resource.designs[0].designItems[0].sizes;
    const size = sizes[2];
    const change = adapter.changeItem;
    let count = 0;
    adapter.changeItem = form => { change(form); if (++count === 2) fail(form); };
    assert.throws(() => stock.apply(choose(), adapter), /rolled back/);
    assert.deepEqual(resource, before);
    assert.equal(resource.designs[0].designItems[0].sizes, sizes);
    assert.equal(sizes[2], size);
    assert.equal(forms.get(resource.designs[0].designItems[0]).selectedCatalogItem, original);
  }
});

test("a print-area failure rolls back already applied stock in the combined batch", () => {
  const { resource, adapter, choose } = fixture();
  const before = structuredClone(resource);
  const area = resource.designs[0].printAreas[0];
  assert.throws(() => stock.apply(choose(), adapter, () => { area.description = "Back"; throw new Error("Area failure"); }), /Area failure.*rolled back/);
  assert.deepEqual(resource, before);
  assert.equal(resource.designs[0].printAreas[0], area);
});
