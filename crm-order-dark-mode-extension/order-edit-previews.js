// MAIN world: read the current design and the same image configuration used by
// CRM's native preview template. This view never changes order data.
(() => {
  "use strict";
  if (window.__crmEditPreviewsInstalled) return;
  window.__crmEditPreviewsInstalled = true;
  let queued = false;
  let signature = "";
  let layoutParent = null;
  let layoutTarget = null;
  const previewId = "crm-edit-design-previews";
  const images = [["f", "Front Preview"], ["b", "Back Preview"],
    ["sl", "Sleeve Left Preview"], ["sr", "Sleeve Right Preview"], ["t", "Inside Tag Line"]];

  function orderId() {
    const direct = location.pathname.match(/^\/order\/(\d{7})\/?$/);
    const embedded = /^\/app\/?$/.test(location.pathname) && location.hash.match(/^#!?\/?order\/(\d{7})\/?(?:[?#].*)?$/);
    return (direct || embedded)?.[1] || "";
  }

  function findScope(root, id) {
    if (!window.angular?.element) return null;
    for (const node of [root, ...root.querySelectorAll('[ng-click="saveOrder();"], #order-design-details')]) {
      let scope;
      try { scope = window.angular.element(node).scope(); } catch (_) { continue; }
      for (let hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
        if (scope.order?.getResource && String(scope.order.getResource()?.id) === id && typeof scope.saveOrder === "function") return scope;
      }
    }
    return null;
  }

  function inherited(scope, property) {
    for (let hops = 0; scope && hops < 12; scope = scope.$parent, hops++) {
      if (scope[property] !== undefined) return scope[property];
    }
    return undefined;
  }

  function clearLayout() {
    layoutParent?.classList.remove("crm-edit-preview-layout");
    layoutTarget?.classList.remove("crm-edit-preview-target");
    layoutParent = null;
    layoutTarget = null;
  }

  function place(panel, root) {
    const allTabs = root.querySelector("#crm-all-tabs-panel:not([hidden])");
    const areaColumn = root.querySelector('#order-design-details > .tab-content > .tab-pane.active [ng-repeat*="in design.printAreas"]');
    const target = allTabs?.querySelector(".crm-all-tabs-cards:not(.crm-all-tabs-stock-cards)")
      || areaColumn?.parentElement
      || root.querySelector("#order-design-details > .tab-content");
    if (!target) {
      clearLayout();
      if (panel.parentElement !== root) root.append(panel);
      return;
    }
    if (layoutTarget !== target || layoutParent !== target.parentElement) clearLayout();
    layoutTarget = target;
    layoutParent = target.parentElement;
    if (!layoutParent.classList.contains("crm-edit-preview-layout")) layoutParent.classList.add("crm-edit-preview-layout");
    if (!layoutTarget.classList.contains("crm-edit-preview-target")) layoutTarget.classList.add("crm-edit-preview-target");
    // Add a sibling to the native row. Its forms, repeat anchors, scope, and
    // inputs keep their original parents and identities.
    if (panel.parentElement !== layoutParent || panel.nextElementSibling !== target) layoutParent.insertBefore(panel, target);
  }

  function clear() {
    document.getElementById(previewId)?.remove();
    clearLayout();
    signature = "";
  }

  function sync() {
    const id = orderId();
    const root = document.getElementById("view-orders-index");
    const scope = id && root && findScope(root, id);
    if (!scope || !scope.editMode) { clear(); return; }
    const resource = scope.order.getResource();
    const user = inherited(scope, "user");
    if (!user || typeof user.isAny !== "function" || !resource.features) { clear(); return; }
    const isVendor = Boolean(user?.isAny?.("vendor"));
    if (inherited(scope, "transfersOnly") || resource.features?.approved ||
        (isVendor && Number(resource.features?.releasedToVendor) !== 1)) { clear(); return; }
    const index = Number(inherited(scope, "activeDesignIndex"));
    const design = Number.isInteger(index) && resource.designs?.[index];
    if (!design?.designId || String(design.crudAction).toLowerCase() === "d") { clear(); return; }
    const env = inherited(scope, "ENV_CONFIG") || window.ENV_CONFIG;
    let base;
    try {
      base = new URL(env?.rushorderteesURL);
      if (base.protocol !== "https:" || base.username || base.password) { clear(); return; }
    } catch (_) { clear(); return; }
    const nextSignature = JSON.stringify([id, index, String(design.designId), base.href]);
    const existing = document.getElementById(previewId);
    if (signature === nextSignature && existing) { place(existing, root); return; }
    clear();
    const panel = document.createElement("section");
    panel.id = previewId;
    panel.className = "crm-edit-previews";
    const heading = document.createElement("h4");
    heading.textContent = "Design Studio Previews";
    const caption = document.createElement("p");
    caption.className = "crm-edit-previews-caption";
    caption.textContent = `Highlighted tab ${index + 1}`;
    const grid = document.createElement("div");
    grid.className = "crm-edit-previews-grid";
    const designLink = new URL("design/", base);
    designLink.searchParams.set("design", String(design.designId));
    for (const [suffix, title] of images) {
      const link = document.createElement("a");
      link.href = designLink.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const image = document.createElement("img");
      const url = new URL("design/ZoomImage.php", base);
      url.search = new URLSearchParams({ src: `${design.designId}_${suffix}`, x: "310", y: "350",
        width: "395", height: "420", ext: "jpg", scale: "1" }).toString();
      image.alt = title;
      image.title = title;
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener("error", () => { link.hidden = true; });
      image.src = url.href;
      link.append(image);
      grid.append(link);
    }
    panel.append(heading, caption, grid);
    place(panel, root);
    signature = nextSignature;
  }

  function schedule() {
    if (queued) return;
    queued = true;
    setTimeout(() => { queued = false; sync(); }, 60);
  }
  new MutationObserver(records => {
    // All Tabs hides the native pane as soon as its root class changes. Move
    // the preview in the same update instead of leaving it in that hidden pane
    // until the normal debounced scan.
    if (records.some(record => record.target === document.documentElement && record.attributeName === "class")) sync();
    else schedule();
  }).observe(document, { subtree: true, childList: true,
    attributes: true, attributeFilter: ["class"] });
  window.addEventListener("hashchange", schedule);
  window.addEventListener("popstate", schedule);
  window.setInterval(sync, 800);
  sync();
})();
