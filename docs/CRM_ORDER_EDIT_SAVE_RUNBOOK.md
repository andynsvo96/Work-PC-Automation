# CRM Order Edit and Save Automation Runbook

## Purpose

This document explains how the current automation opens a CRM order, enters edit mode, changes order or shipping fields, saves the order, and verifies that the save actually persisted.

Use it as a handoff prompt for Codex on another computer. The goal is that a new automation can understand the CRM behavior well enough to do actions such as:

- Open order `1234567`.
- Click `Edit Order`.
- Change a date, note, product split, shipping transaction, or status.
- Click `Save Order` or modal `Save`.
- Confirm the save by reading CRM state after the page returns to normal.

The CRM is a legacy Angular app, so the reliable path is not only "find a button and click it." The scripts use visible buttons when possible, Angular `ng-click` hooks when available, direct Angular scopes as fallback, and post-save verification.

## Current Files To Study First

These are the main CRM automation files in this repo:

| File | What it demonstrates |
|---|---|
| `workers/crm_validate_address.py` | Opens a CRM order, opens the Shipping Transaction edit modal, runs `Save & Verify Address`, handles validation popups, clicks final modal `Save`, and verifies the green `Valid Address` state. |
| `workers/crm_product_separator.py` | Opens a CRM order, enters `Edit Order`, modifies order/product data through Angular scope and DOM inputs, clicks `Save Order`, and waits until CRM exits edit mode. |
| `workers/crm_auto_splitter.py` | Uses the same order edit/save contract as product separator, plus quote copy/save flows. |
| `workers/crm_shipping_bypasser.py` | Changes CRM due date, production date, and production notes through visible `Edit Order` and `Save Order` controls, then refreshes and verifies persisted field values. |
| `workers/crm_order_goods.py` | Opens orders from rush/813 lists, reads stock tabs, handles stock unlock state, and launches SanMar order goods flow. |
| `workers/crm_unlock_orders.py` | Demonstrates report-list bulk actions through the Order Preview panel rather than direct order edit mode. |
| `server.py` and `routes/work_routes.py` | Orchestrate workers, locks, queues, state, status polling, and `last_result.json` result handoff. |
| `automation_runtime.py` | Shared Selenium helpers: Chrome driver setup, safe navigation, screenshots, status/result payloads, and safe shutdown. |
| `config.example.py` | Template for CRM URLs, credentials, profile directories, timeouts, and headless settings. |

## Architecture Pattern

Every worker follows the same local automation pattern:

1. `server.py` or a direct CLI command starts a worker in `workers/`.
2. The worker builds a Selenium Chrome driver using `automation_runtime.py`.
3. The worker opens a report URL or direct order URL.
4. The worker logs in if CRM redirects to the login page.
5. The worker performs one focused workflow.
6. The worker writes `last_result.json` with `success`, `message`, and optional details.
7. The server reads the result, updates runtime state, exposes status endpoints, and writes audit entries.

Do not build a new automation as a single giant script if it needs to run from the UI. Use the existing worker plus server wrapper pattern.

## Required Local Setup On A New Computer

1. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Create local config:

   ```powershell
   Copy-Item config.example.py config.py
   ```

3. Fill in `config.py`:

   ```python
   CRM_USERNAME = "your username"
   CRM_PASSWORD = "your password"
   CRM_LOGIN_URL = "https://crm2.legacy.printfly.com/login"
   CRM_PROFILE_DIR = "chrome_profile_crm"
   CRM_HEADLESS = True
   CRM_ALLOW_VISIBLE_FALLBACK = False
   CRM_ACTION_TIMEOUT = 15
   CRM_PAGE_LOAD_TIMEOUT = 30

   PROCESSOR_ORDER_URL_TEMPLATE = "https://crm2.legacy.printfly.com/order/{order_id}"
   ```

4. Fill the needed list/report URLs for the workflow:

   ```python
   CRM_LOCKED_URL = ""
   CRM_SHIPPING_FREE_URL = ""
   CRM_SHIPPING_RUSH_URL = ""
   CRM_SHIPPING_ALL_URL = ""
   CRM_SHIPPING_813_URL = ""
   CRM_ORDER_GOODS_RUSH_URL = ""
   CRM_813_ORDER_GOODS_URL = ""
   CRM_SHIPPING_BYPASS_URL = ""
   CRM_PUSH_BACK_RUSH_URL = ""
   CRM_PUSH_BACK_813_URL = ""
   PRODUCT_SEPARATOR_LIST_URL_FREE = ""
   PRODUCT_SEPARATOR_LIST_URL_RUSH = ""
   PRODUCT_SEPARATOR_LIST_URL_ALL = ""
   PRODUCT_SEPARATOR_LIST_URL_813 = CRM_SHIPPING_813_URL
   ```

5. For first login on a new computer, temporarily set:

   ```python
   CRM_HEADLESS = False
   CRM_ALLOW_VISIBLE_FALLBACK = True
   ```

   Run one dry run so Chrome creates `chrome_profile_crm` and saves the login session. After login works, headless mode can be turned back on.

## Opening A CRM Order

Preferred direct order URL:

```text
https://crm2.legacy.printfly.com/order/{order_id}
```

Order IDs are expected to be 7 digits. Normalize user input by stripping everything except digits, then reject it unless it is exactly 7 digits.

The current workers use this pattern:

1. Build URL from `PROCESSOR_ORDER_URL_TEMPLATE` or `https://crm2.legacy.printfly.com/order/{order_id}`.
2. Call `safe_get_with_partial_load(driver, target_url, "CRM order {order_id}")`.
3. Detect login or "not authenticated".
4. Login or click saved login if needed.
5. Reload the target order URL.
6. Wait for the CRM app context and order Angular scope.

## CRM Context And Frame Handling

The CRM may be available in the top document or inside an iframe. A new worker should not assume one or the other.

Use this approach:

1. Try top document first.
2. If the URL is not an `/app#` route or Angular is not ready, switch into CRM app iframe.
3. Wait until `window.angular` exists and `document.body.innerText` is not empty.
4. If body text contains `Not authenticated`, reload the order URL and login again.

The product separator worker demonstrates the pattern with:

- `_activate_crm_context(driver)`
- `_wait_for_crm_context(driver)`
- `_wait_for_order_scope(driver, order_id)`
- `_open_order_scope_with_reload(driver, order_url, order_id)`

The important concept is that every edit/save action must happen after the driver is focused on the actual CRM Angular app document.

## Finding The Order Angular Scope

The most reliable order edit/save path uses Angular scope, not only visible selectors.

The current workers search page nodes for a scope where:

- `scope.order` exists.
- `scope.order.getResource()` exists.
- The scope has order actions such as `copyOrder`, `editModeOn`, or `saveOrder`.

The useful JavaScript bootstrap shape is:

```javascript
function findOrderScope() {
  const nodes = Array.from(document.querySelectorAll('*'));
  for (const el of nodes) {
    let scope = null;
    try { scope = angular.element(el).scope && angular.element(el).scope(); } catch (err) {}
    for (let hops = 0; scope && hops < 8; scope = scope.$parent, hops++) {
      if (scope.order && scope.order.getResource && typeof scope.copyOrder === 'function') return scope;
    }
  }
  return null;
}
const s = findOrderScope();
if (!s) throw new Error('Order scope not found');
const r = s.order.getResource();
```

When changing Angular model values, wrap changes in `$apply` or `$digest`:

```javascript
function runInAngular(scope, fn) {
  const root = scope.$root || scope;
  if (root.$$phase) return fn();
  if (typeof scope.$apply === 'function') return scope.$apply(fn);
  const result = fn();
  if (typeof root.$digest === 'function') root.$digest();
  return result;
}
```

## Core Recipe: Edit Order And Save Order

Use this recipe when the task says something like "edit the CRM order and save it."

### 1. Open The Order

```python
order_id = normalize_order_id(raw_order_id)
order_url = f"https://crm2.legacy.printfly.com/order/{order_id}"
safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
handle_login_if_needed(driver, order_url)
wait_for_crm_context(driver)
wait_for_order_scope(driver, order_id)
```

### 2. Enter Edit Mode

Preferred: click the Angular button by `ng-click` and exact text.

```python
_click_ng_button(driver, "editModeOn();", "edit order")
```

Fallback 1: find a visible button/link/input whose text is exactly `Edit Order`.

```javascript
const button = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]')).find((el) => {
  const text = (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  return text === 'edit order';
});
if (button) {
  button.scrollIntoView({block: 'center'});
  button.click();
}
```

Fallback 2: call the Angular scope method directly.

```javascript
runInAngular(s, () => s.editModeOn());
```

### 3. Verify Edit Mode Opened

Do not continue immediately after clicking. Wait until one of these is true:

- Angular says `s.editMode` is true.
- Visible controls include `Save Order`.
- Page text includes both `save order` and an edit-only affordance such as `remove item`.

Example check:

```javascript
return {
  editMode: !!s.editMode,
  id: String(r.id || '')
};
```

Visible control check:

```javascript
const controls = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
const visible = controls.filter((el) => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
}).map((el) => ({
  text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().toLowerCase(),
  ngClick: el.getAttribute('ng-click') || '',
  disabled: !!el.disabled || el.getAttribute('disabled') !== null
}));
return {
  editOrderVisible: visible.some((item) => item.text === 'edit order' || item.ngClick === 'editModeOn();'),
  saveOrderVisible: visible.some((item) => item.text === 'save order' || item.ngClick === 'saveOrder();'),
  saveOrderEnabled: visible.some((item) => (item.text === 'save order' || item.ngClick === 'saveOrder();') && !item.disabled)
};
```

### 4. Change Fields

Prefer Angular scope updates for structured order data because the legacy CRM often keeps the real state in Angular objects.

For plain inputs or textareas, always dispatch `input` and `change` after setting values:

```javascript
textarea.focus();
textarea.value = nextValue;
textarea.dispatchEvent(new Event('input', { bubbles: true }));
textarea.dispatchEvent(new Event('change', { bubbles: true }));
```

Examples from the current workers:

- Production notes: find the textarea near a `Production Notes` label, append text, dispatch events, then save.
- Production date or due date: enter edit mode, set the labeled date field, wait for any shipping-method recalculation, acknowledge production-date warnings, then save.
- Product separation: change product/order data through Angular scope, then save and verify the order exits edit mode.

### 5. Save Order

Preferred: click the Angular `Save Order` button by `ng-click`.

```python
_click_ng_button(driver, "saveOrder();", "save order")
```

Fallback: call the Angular scope method directly.

```javascript
runInAngular(s, () => s.saveOrder());
```

Fallback for simple DOM-only flows:

```python
save = _find_clickable_by_text(driver, r"save\s+order")
if save is None:
    raise RuntimeError("CRM save order button was not found.")
_click_with_fallback(driver, save)
```

### 6. Handle Save Warnings

Some saves trigger warning modals. Current examples:

- Production date can trigger a warning about shipping options or another production date.
- The automation clicks `OK` only on matching warning modals, then tries `Save Order` again if needed.
- Never click unrelated modal buttons.

Use a narrowly scoped warning handler:

1. Find visible modal text.
2. Confirm it matches the expected warning pattern.
3. Click an exact `OK` button.
4. Retry `Save Order` only once or within a bounded timeout.

### 7. Verify Save Completion

A click is not proof. Consider the save complete only after at least two stable checks show one of these:

- `Edit Order` is visible and `Save Order` is gone.
- Angular says `s.saving` is false and `s.editMode` is false.
- The page has returned from edit mode and no save control remains enabled.

Then refresh and re-read the changed value for important data:

- Production date: refresh, re-extract order data, compare to target date.
- Due date: refresh, re-extract order data, compare to target date.
- Production note: refresh or re-read the notes if needed.
- Product changes: re-open or verify the order/product tab state.

The current save wait loop uses a bounded timeout and stable checks. Do the same.

## Core Helper: Click A Button By ng-click

This is the helper pattern used by product separator and auto splitter:

```python
def _click_ng_button(driver, ng_click, text=None):
    return bool(
        driver.execute_script(
            """
            const ngClick = arguments[0];
            const expectedText = (arguments[1] || '').toLowerCase();
            const buttons = Array.from(document.querySelectorAll('button,a,input[type=button],input[type=submit]'));
            const button = buttons.find((el) => {
              const ng = el.getAttribute('ng-click') || '';
              const text = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
              const rect = el.getBoundingClientRect();
              return ng === ngClick && (!expectedText || text === expectedText) && rect.width >= 0 && rect.height >= 0;
            });
            if (!button) return false;
            button.scrollIntoView({block: 'center', inline: 'center'});
            button.click();
            return true;
            """,
            ng_click,
            text or "",
        )
    )
```

For payment/refund-sensitive pages, copy the safer variant from `crm_auto_splitter.py`, which refuses to click controls whose label matches refund wording.

## Core Helper: Click With Fallback

Use this pattern when clicking normal DOM controls:

1. Scroll the element into view.
2. Try normal Selenium click.
3. If intercepted or stale, use JavaScript click.
4. Keep the timeout bounded.

The existing workers name this helper `_click_with_fallback(driver, element)`.

## Shipping Transaction Edit And Save

Use this recipe when the task is about the shipping address or shipping transaction, not the whole order edit mode.

### 1. Open Order Or Report

The address validator can:

- Open a direct order URL when `--order-id` is supplied.
- Otherwise open one of the configured shipping-address reports and click the first matching order.

Direct URL:

```text
https://crm2.legacy.printfly.com/order/{order_id}
```

### 2. Find Shipping Section

Look for:

- `#order-shipping`
- A visible panel containing `Shipping transaction`
- A visible panel containing `Shipping info`
- A panel near `shipping-transactions`

Scroll the shipping section into view before searching buttons.

### 3. Click The Shipping Transaction Edit Button

Preferred selectors:

```xpath
.//a[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]
.//button[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]
.//div[contains(@id, 'shipping-transactions')]//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]
```

Fallback selectors:

```xpath
//a[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]
//button[contains(@ng-click, 'openShippingTransactionModal') and not(contains(@class, 'ng-hide'))]
//a[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]
//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='edit' and not(contains(@class, 'ng-hide'))]
```

Filter fallback `Edit` buttons by ancestor text. Only click an edit button if the surrounding text references shipping.

### 4. Wait For Shipping Modal

Wait for a visible modal containing:

```text
Shipping Transaction for
```

The modal may be inside a frame. Search current context, default content, and each frame.

### 5. Save And Verify Address

Click the modal button whose text contains:

- `Save & Verify Address`
- `Save and Verify Address`

Then wait for the Address Validation result modal or an inline valid state.

Handle outcomes:

- Existing saved address: select it if it matches the current address.
- Suggested validated address: select only if it matches closely enough.
- APO/FPO or override path: set validation override when allowed.
- PO Box restrictions: skip when the configured shipping filter says not to process it.

### 6. Final Modal Save

The final shipping transaction save is different from `Save Order`.

Find a button in the shipping modal or globally whose exact normalized text is `Save`:

```xpath
.//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']
.//input[@type='submit' and translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']
//button[translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']
//input[@type='submit' and translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='save']
```

If normal Save is not reliable, the current script can call the modal Angular scope `send()` directly:

```javascript
const scope = resolveScope(modalRoot, (scope) => scope && typeof scope.send === 'function');
if (!scope || typeof scope.send !== 'function') {
  throw new Error('CRM modal scope is missing send().');
}
scope.send();
```

### 7. Verify Shipping Save

After final Save:

1. Wait until body text contains both the order ID and `Valid Address`, or the shipping panel contains `Valid Address`.
2. If CRM shows `Shipping transaction added successfully`, reload the direct order URL.
3. Re-check the shipping panel.
4. If still not confirmed, do one final reload and wait again.
5. Fail with a clear timeout message if the green valid-address state never appears.

## Report List Bulk Action Flow

Use this only for bulk report actions such as Stock Auto Ordering Unlocked. This is not the same as editing a single order.

The current `crm_unlock_orders.py` flow:

1. Open `CRM_LOCKED_URL`.
2. Login if needed.
3. Wait for report/order rows.
4. Select all target rows.
5. Wait for the right-side `Order Preview` panel.
6. Open the preview dropdown.
7. Type enough text to filter the option, such as `unloc`.
8. Select `Stock Auto Ordering Unlocked`.
9. Click `Apply`.
10. Confirm modal `OK`, unless dry-run.
11. Wait for `Update complete` or a lower report count.

When a user asks to save a specific order, do not use the Order Preview flow. Use the direct order edit/save recipe.

## Direct Date Or Note Change Flow

The shipping bypasser demonstrates a pragmatic DOM path:

### Production Date

1. Find visible `Edit Order`.
2. Click it.
3. Set the `Production Date` field.
4. Wait for `Selecting shipping method` to finish.
5. Acknowledge expected production date warning if present.
6. Find visible `Save Order`.
7. Click it.
8. If another expected warning appears, click `OK` and click `Save Order` again.
9. Wait for visible `Edit Order`.
10. Refresh the order and verify the production date equals the target date.

### Due Date

1. Find visible `Edit Order`.
2. Click it.
3. Set the `Due Date` field.
4. Click `Save Order`.
5. Wait for visible `Edit Order`.
6. Refresh the order and verify the due date equals the target date.

### Production Notes

1. Find visible `Edit Order`.
2. Click it.
3. Find the textarea near `Production Notes`.
4. Append the new note only if it is not already present.
5. Dispatch `input` and `change`.
6. Click `Save Order`.
7. Wait for visible `Edit Order`.
8. Refresh or re-read as needed.

## Dry Run Rules

Every destructive or final-save workflow should support `--dry-run`.

Dry-run should:

- Open CRM.
- Login if needed.
- Navigate to the target order/report.
- Find the controls.
- Enter edit mode if safe.
- Fill or calculate proposed values if safe.
- Stop before clicking final `Save`, final `OK`, order submission, or any external vendor submit button.
- Write a normal result payload saying what would have happened.

For shipping address validation, dry-run reaches a valid-address state but skips final modal `Save`.

For report bulk update, dry-run should skip the final confirm `OK`.

For SanMar or external stock ordering, dry-run must never submit the vendor order.

## Worker Result Contract

At the end of every worker, write `last_result.json` through `write_result_payload`:

```python
write_result_payload(
    "crm.my_new_action",
    "crm_my_new_worker.py",
    True,
    "Updated order 1234567 successfully.",
    {
        "action": "my_new_action",
        "order_id": "1234567",
        "order_ids": ["1234567"],
        "dry_run": dry_run,
        "details": details,
    },
)
```

On failure:

```python
write_result_payload(
    "crm.my_new_action",
    "crm_my_new_worker.py",
    False,
    str(exc),
    {
        "action": "my_new_action",
        "order_id": order_id,
        "dry_run": dry_run,
    },
)
```

Also take a screenshot on failure when a browser is available.

## Status Payloads During Long Runs

For multi-order workflows, write live status with `write_status_payload`:

```python
write_status_payload(
    "crm.my_new_action",
    f"Processing order {order_id}.",
    stage="editing_order",
    current=index,
    total=total,
    order_id=order_id,
)
```

The server/UI can poll this while the worker runs.

## Server And UI Integration

If adding a new automation to the dashboard:

1. Create a new worker under `workers/`.
2. Add a script path constant in `server.py`.
3. Add a runtime dict and lock if the worker can run independently.
4. Add a start function that launches the worker subprocess.
5. Delete stale `last_result.json` before launch.
6. Read the new result payload after completion.
7. Persist state/history.
8. Add route(s) in `routes/work_routes.py`.
9. Add buttons/status cards in `ui_panel.html`.

Existing route patterns include:

- `/crm/process/<mode>` and `/crm/process/status`
- `/crm/address-validator` and `/crm/address-validator/status`
- `/crm/order-goods` and `/crm/order-goods/status`
- `/crm/shipping-bypasser` and `/crm/shipping-bypasser/status`
- `/crm/product-separator` and `/crm/product-separator/status`
- `/crm/auto-splitter` and `/crm/auto-splitter/status`

Use a queue wrapper for user-triggered dashboard actions so overlapping CRM actions do not fight over the same Chrome profile.

## Safety Rules

- Never run two CRM workers against the same Chrome profile at the same time.
- Keep `CRM_PROFILE_DIR` isolated from Paycom, Slack, and SanMar profiles.
- Reject non-7-digit order IDs before navigating.
- Keep all waits bounded. Do not infinite-loop on CRM loading states.
- Never consider a save successful just because a click returned.
- After important saves, refresh and verify the data persisted.
- Stop for manual review when CRM displays a red error popup or a warning not explicitly handled.
- Do not click refund/payment controls unless the task explicitly requires it and the selector is very narrow.
- Keep credentials and report URLs in local `config.py`, not committed docs.
- On another computer, run visible mode first to confirm selectors and login.

## Troubleshooting

### CRM opens login or "Not authenticated"

Reload the target order URL after login. If it still says `Not authenticated`, clear the CRM profile or login manually in visible mode.

### `Edit Order` is visible but click does nothing

Switch to the correct CRM frame/context and try the `ng-click` route:

```python
_click_ng_button(driver, "editModeOn();", "edit order")
```

If that fails, call:

```javascript
runInAngular(s, () => s.editModeOn());
```

### `Save Order` click returns but save never completes

Poll both Angular state and visible controls:

- `s.saving`
- `s.editMode`
- `Edit Order` visible
- `Save Order` hidden
- `Save Order` disabled

Also scan red error text. If CRM says the save is blocked, raise manual review.

### Date changes do not persist

Refresh the order and re-extract the date. If CRM reverted the value, fail clearly and include expected vs actual. Production date may require acknowledging a shipping-method warning before saving.

### Shipping modal final Save not found

Search inside the modal and globally. Some modal buttons render outside the modal container. If the visible button path fails, resolve the modal Angular scope and call `send()`.

### Address becomes valid but order page does not show it

Reload the direct order URL. The current validator handles the success banner by reloading and checking the shipping panel again.

## Prompt Template For Codex On Another Computer

Use this prompt when asking Codex to build a new CRM order edit/save automation:

```text
Read docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md first. Build a new worker that edits CRM order <ORDER_ID> and saves it.

Use the existing repo patterns:
- Selenium through automation_runtime.py.
- Direct order URL https://crm2.legacy.printfly.com/order/{order_id}.
- Login recovery from the existing CRM workers.
- Switch to the CRM Angular app context before interacting.
- Enter edit mode using ng-click editModeOn(); with text "edit order", falling back to visible button click, then Angular scope editModeOn().
- Verify edit mode before changing fields.
- Make the requested field changes through Angular scope or DOM inputs with input/change events.
- Save using ng-click saveOrder(); with text "save order", falling back to Angular scope saveOrder().
- Wait until CRM exits edit mode, then refresh and verify the changed field persisted.
- Support --dry-run that stops before final Save.
- Write last_result.json using write_result_payload.
- Screenshot and write a failure payload on exceptions.

Do not mark success until the saved value is verified after refresh.
```

## Minimal Worker Skeleton

```python
import argparse
import os
import sys
import time

from selenium.common.exceptions import TimeoutException

from workers._bootstrap import ensure_project_root
ensure_project_root()

from automation_runtime import (
    build_chrome_driver,
    kill_stale_chrome,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
from config import CRM_HEADLESS, CRM_PROFILE_DIR


def normalize_order_id(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) != 7:
        raise ValueError("Order ID must be a 7-digit CRM order ID.")
    return digits


def run(order_id, dry_run=False):
    order_id = normalize_order_id(order_id)
    driver = None
    try:
        kill_stale_chrome(CRM_PROFILE_DIR, "CRM")
        driver = build_chrome_driver(CRM_PROFILE_DIR, CRM_HEADLESS)
        order_url = f"https://crm2.legacy.printfly.com/order/{order_id}"

        safe_get_with_partial_load(driver, order_url, f"CRM order {order_id}")
        # TODO: call the repo's login/context helpers or copy them from an existing CRM worker.
        # TODO: wait for CRM Angular order scope.
        # TODO: enter edit mode.
        # TODO: change fields.

        if dry_run:
            return write_result_payload(
                "crm.example_edit_order",
                "crm_example_edit_order.py",
                True,
                f"Dry run reached edit-ready state for order {order_id}; skipped Save Order.",
                {"order_id": order_id, "dry_run": True},
            )

        # TODO: click Save Order and verify after refresh.

        return write_result_payload(
            "crm.example_edit_order",
            "crm_example_edit_order.py",
            True,
            f"Saved order {order_id}.",
            {"order_id": order_id, "dry_run": False},
        )
    except Exception as exc:
        if driver is not None:
            safe_take_screenshot(driver, f"crm_example_edit_order_{order_id}_error")
        write_result_payload(
            "crm.example_edit_order",
            "crm_example_edit_order.py",
            False,
            str(exc),
            {"order_id": order_id, "dry_run": dry_run},
        )
        return 1
    finally:
        safe_driver_quit(driver, CRM_PROFILE_DIR)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(args.order_id, dry_run=args.dry_run)
    sys.exit(0 if isinstance(result, dict) and result.get("success") else 1)
```

## Final Verification Checklist

Before trusting a new CRM edit/save automation:

- It runs in visible mode on one known safe test order.
- It supports `--dry-run`.
- It refuses invalid order IDs.
- It logs in or recovers from saved-session login.
- It switches into the correct CRM app context.
- It can prove edit mode opened.
- It changes only the intended field(s).
- It clicks or calls the correct save action.
- It handles known warning modals narrowly.
- It waits until edit mode ends.
- It refreshes the order and verifies the target value persisted.
- It writes `last_result.json`.
- It takes a screenshot on failure.
- It releases/quits Chrome cleanly.
