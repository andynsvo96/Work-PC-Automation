(() => {
  "use strict";

  const ordersAddress = "orders@rushordertees.com";
  const fieldSelector = [
    '[data-target-selection-name="sfdc:RecordField.OutgoingEmail.ValidatedFromAddress"]',
    '[data-target-selection-name="sfdc:RecordField.OutgoingEmail.FromAddress"]'
  ].join(",");
  const attempted = new WeakMap();
  let active = null;
  let scheduled = false;

  function visible(element) {
    return element?.isConnected && element.getClientRects().length > 0
      && getComputedStyle(element).visibility === "visible";
  }

  function enabled(element) {
    return !element.matches(":disabled") && element.getAttribute("aria-disabled") !== "true";
  }

  function isOrders(element) {
    const label = element.textContent.trim() || element.getAttribute("title") || "";
    const addresses = label.toLowerCase().match(/[^\s<>]+@[^\s<>]+/g) || [];
    return addresses.length === 1 && addresses[0] === ordersAddress;
  }

  function currentMenu(control) {
    const id = control.getAttribute("aria-controls") || control.getAttribute("aria-owns");
    return id ? document.getElementById(id) : null;
  }

  function canContinue(attempt) {
    return !attempt.cancelled && visible(attempt.field) && visible(attempt.control)
      && enabled(attempt.control);
  }

  async function waitFor(attempt, check, timeout) {
    const deadline = Date.now() + timeout;
    while (canContinue(attempt)) {
      const result = check();
      if (result) return result;
      if (Date.now() >= deadline) break;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    return null;
  }

  async function selectOrders(attempt) {
    const { control } = attempt;
    if (isOrders(control)) return;
    // A dropdown already being used belongs to the user.
    if (control.getAttribute("aria-expanded") === "true") return;
    const focused = document.activeElement;
    try {
      attempt.opened = true;
      control.click();
      const option = await waitFor(attempt, () => {
        // Aura moves this popup under <body>; use its ID rather than its location.
        const menu = currentMenu(control);
        if (!visible(menu) || control.getAttribute("aria-expanded") !== "true") return null;
        const matches = [...menu.querySelectorAll('[role="option"], [role="menuitemradio"]')]
          .filter(element => visible(element) && enabled(element) && isOrders(element));
        return matches.length === 1 ? matches[0] : null;
      }, 3000);
      if (option && canContinue(attempt)) {
        option.click();
        // Verify the selected From value, not a label in the open option list.
        await waitFor(attempt, () => isOrders(control), 1000);
      }
    } finally {
      if (!attempt.cancelled) {
        if (attempt.opened && visible(control) && control.getAttribute("aria-expanded") === "true") {
          control.click();
        }
        if (visible(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
      }
    }
  }

  function schedule() {
    if (scheduled) return;
    scheduled = true;
    setTimeout(scan, 75);
  }

  async function scan() {
    scheduled = false;
    const fields = [...document.querySelectorAll(fieldSelector)];
    for (const field of fields) {
      // Salesforce can hide and reuse a composer when opening another draft.
      if (!visible(field)) attempted.delete(field);
    }
    if (active) return;
    for (const field of fields) {
      if (!visible(field) || attempted.has(field)) continue;
      const controls = [...field.querySelectorAll('[role="combobox"]')]
        .filter(element => visible(element) && enabled(element));
      if (controls.length !== 1 || !controls[0].textContent.trim()) continue;
      const attempt = { field, control: controls[0], cancelled: false };
      attempted.set(field, attempt);
      active = attempt;
      try {
        await selectOrders(attempt);
      } catch (_error) {
        // A closing or rerendering composer must not trigger repeated clicks.
      } finally {
        active = null;
        schedule();
      }
      break;
    }
  }

  function userInteraction(event) {
    if (!event.isTrusted) return;
    const field = event.target.closest?.(fieldSelector);
    if (field) {
      const attempt = attempted.get(field) || {};
      attempt.cancelled = true;
      attempted.set(field, attempt);
    }
    // Yield immediately if the user starts clicking or typing during selection.
    if (active) active.cancelled = true;
  }

  function start() {
    document.addEventListener("pointerdown", userInteraction, true);
    document.addEventListener("keydown", userInteraction, true);
    new MutationObserver(schedule).observe(document.documentElement, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
      attributeFilter: ["class", "style", "hidden", "disabled", "aria-expanded", "aria-disabled", "aria-controls", "aria-owns"]
    });
    schedule();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
