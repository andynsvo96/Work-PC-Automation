# CRM Order Assistant

Private Manifest V3 extension for the Automation project's CRM pages. It provides local order-page dark mode, native editing across orders in one Chrome window, bulk stock and print-area editing within an order, queued single-order tools, list selection and rush highlighting, reliable Salesforce tab reuse, and an automatic Orders email sender default. List controls select orders and open tabs; queued tools remain scoped to individual order pages.

## Install locally

1. Open the dedicated CRM Chrome profile. From the Automation app, use the CRM Chrome-profile setup action if needed.
2. Visit `chrome://extensions` in that profile and enable **Developer mode**.
3. Choose **Load unpacked** and select this `crm-order-dark-mode-extension` folder.
4. Pin **CRM Order Assistant** and open its toolbar popup. Use **Edit All Orders** for CRM orders in that window, or the **Dark mode** toggle at the top right for styling. The order page also provides single-order Manual Process, Cancel, Reachout, and Stock Issue controls.

The preference is stored locally in that Chrome profile. Removing or reloading the extension keeps the preference unless Chrome extension data is cleared.

After extension updates, reload the extension at `chrome://extensions` and refresh open CRM order/list and Salesforce pages. Updates that also change the local Automation app require an app restart as noted below.

Version **1.6.2** removes **Auto-Process** from order pages and **Process current order** from the toolbar popup. Manual Process, Cancel, Reachout, and Stock Issue remain available on order pages, with queue status and completion refreshes. Reload CRM Order Assistant at `chrome://extensions` and refresh open CRM order pages to load this removal; no local Automation app restart is needed.

## Exclude orders during batch editing - 1.8.6

Uncheck an included order while editing to continue with the other orders. This
also works after an Apply validation conflict, a blocked rescan, or a conflict
detected before Save All starts. The shared draft stays intact, and print-area
cards and counts update to include only the remaining orders. Apply validates
those orders again; Save All and Cancel unsaved edits also skip excluded orders.
Selection is locked while an action is running.

Excluding an order leaves its native editor and any existing edits untouched.
Use its order link to resolve that editor manually. An excluded order cannot be
checked again in the active batch; resolve open native editors and scan again
to start a new selection. Batch-owned excluded editors retain their reservation
against another window's batch until they are resolved and the source window is
scanned again. Interrupted or uncertain saves remain for review and are never
retried by changing selection.

Reload **CRM Order Assistant 1.8.6** at `chrome://extensions` and reopen the popup
or expanded editor. Extension reload clears the current in-memory batch, so
resolve any existing native edits first. No app restart, order-page refresh, or
new permission is needed for this update.

## Compact automation dialogs - 1.8.5

Order-page automation dialogs omit workflow descriptions and explanatory hints.
They retain titles, product details, selection controls, field labels, selected-tab
and cancellation-scope indicators, scan status, and validation/error messages.
Shared price inputs show their affected tabs without the pricing explanation.

Reload **CRM Order Assistant 1.8.5** at `chrome://extensions` and refresh open CRM
order pages. No app restart or new permission is needed.

## On-demand order scans - 1.8.4

Stock Issue, Copyright - Cancel, Complicated EMB, and Oversize EMB read CRM's
already-loaded order data when you open their selection dialog. When the required
fields are available, they collect every active product/design tab together,
leave the highlighted tab untouched, and skip the old per-tab waits. Stock keeps
each product's saved item IDs, color, positive size quantities, and tab membership;
copyright keeps distinct saved tab IDs even when artwork is shared. Removed tabs,
removed products, and zero-quantity stock are excluded from stock selection.

The reader performs no background scan, network request, prefetch, or timer on
order opening. It does not cache selections across orders. Each scan reads the
current resource again before returning and stops if the data changed. Missing
required fields retain the existing visible-tab scan, which restores the original
tab. Conflicting totals, wrong orders, unsaved edits, and active saves stop for
review. A missing/old reader requests an extension reload and page refresh.

Copyright and embroidery workers use the same read-only helpers for fresh
verification during their existing queued workflows. Save/reload verification,
payment checks, email/Slack receipts, retry rules, and queue serialization retain
their existing behavior. This change speeds up reading order data; it does not
remove CRM saves or external-service waits.

Reload **CRM Order Assistant 1.8.4** at `chrome://extensions` and refresh open CRM
order pages. No app restart or new permission is needed; workers load the change
on their next queued run. Offline regression checks include:

```powershell
node --test tests/test_order_scan_reader.mjs tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
python -m unittest discover -s tests -p 'test_copyright_cancel*.py'
python -m unittest discover -s tests -p 'test_complicated_emb*.py'
python -m unittest discover -s tests -p 'test_all_tabs_stock_browser.py'
```

The local Chrome fixtures check that complete scans make no tab clicks or fixed
waits, and cover fallback scans and saved-value/retry verification. Live CRM
timing, installed-extension behavior, and macOS remain unverified.

## Native Save All without refresh - 1.8.3

**Save All** starts the native **Save Order** action on every included unique
order together, after checking that all included editors still match the batch
and belong to the originating window. It records the entire submission intent
before starting any save, then waits for per-order native completion. A slow or
failed order does not delay submitting the others. Saved orders are shown
individually; failed, blocked, or uncertain saves remain for CRM review without
automatic retries.

The extension no longer refreshes saved order tabs or duplicate tabs. CRM keeps
its own normal Save Order behavior and validation dialogs. **Check save status**
only inspects values already loaded in the current pages; it never reloads,
saves, or adds notes. It does not perform a fresh server read. Inspect the actual
order pages to confirm the result. Cancel unsaved edits skips orders with a
confirmed native save completion.

Reload **CRM Order Assistant 1.8.3** at `chrome://extensions` and reopen the popup.
No app restart or new permission is needed. This release does not change the
CRM page adapter; refresh an order page only if it requests updated controls.

## All Orders apply, discard, and larger editor - 1.8.2

**Apply changes** validates all included orders before applying edits; the
separate Preview changes button is removed. Inspect the actual CRM order pages
before using **Save All**. The footer has one row: **Discard pending
changes**, **Cancel unsaved edits**, and **Check save status** on the left;
**Apply changes** and **Save All** on the right. Very narrow windows
wrap controls when necessary to keep them accessible.

**Cancel unsaved edits** uses native Cancel and automatically chooses **No** only
on CRM's exact "Some changes were made to the form, do you want to review and
save them?" warning for an unchanged, batch-owned editor. It never chooses Yes.
Unrelated dialogs, multiple dialogs, ambiguous buttons, and disabled No controls
stop for review. After discarding, the original print areas, products, artwork,
and saved note histories must match the pre-edit baseline, including when CRM
reloads during cancellation. No save or note submission is retried.

The toolbar popup now uses Chrome's full 600-pixel maximum height when the screen
allows it, with tighter spacing and a single-row footer. For more room, choose
**Expand editor**. It opens the same batch in a taller, resizable Chrome window
sized to the available monitor space. Its drafts and results remain bound to the
original window's orders; the new window is never used as the order source.
Clicking Expand again focuses the existing expanded window. Order links bring
the original order window forward for manual inspection. Closing either editor
view does not discard the batch or interrupt an initiated operation.

Reload **CRM Order Assistant 1.8.2** at `chrome://extensions` and refresh open CRM
pages. No app restart or additional extension permission is needed.

## Popup header - 1.8.1

The popup shows the local Automation app bridge status at the top left and the
**Dark mode** toggle at the top right, above the All Orders editor. The separate
CRM Order Assistant title, window badge, and Extension settings section are
removed. Hover over the dark-mode toggle for the current page's styling status.
Order selection remains limited to the originating Chrome window.

Reload **CRM Order Assistant 1.8.1** at `chrome://extensions` and refresh open CRM
pages. No local Automation app restart is needed.

## All Orders popup editor - 1.8.0

Click the extension icon to open **All Orders editor** directly in the toolbar
popup. It reads CRM order tabs in that Chrome window, including single-product-tab
orders, multi-product-tab orders, and embedded `/app#/order/…` pages. Reports,
other websites, and other windows are excluded. One order open in several browser
tabs appears once, with its duplicate-tab count. The dark-mode control is at the
top right, and the local-app connection status is at the top left.

1. Use **Scan this window**, review the order list, and select the orders to
   include. The first popup opening in a window scans automatically without
   editing anything. An already-editing, loading, frozen, discarded, or ambiguous
   order is excluded with its reason. Activate sleeping tabs and finish loading,
   or resolve their existing edits, before scanning again.
2. Click **Edit All Orders**. Each included page's enabled native **Edit Order**
   action starts together. Shared edits require every included order's editor to
   be ready. Resolve an entry failure or uncheck that failed order to continue.
   A duplicate tab with unsaved edits blocks that order as well. Orders may also
   be unchecked later during editing, including after a validation conflict.
   Their native editors stay untouched; the remaining shared draft is retained.
3. Set print-area locations or methods in the All Tabs-style cards. Each card
   lists the exact order numbers and internal product-tab numbers it affects.
   Mixed methods stay unchanged until explicitly selected. Location swaps,
   **Add print area**, and removal with **× / Undo** use the existing All Tabs
   model: additions fill only missing locations; duplicate final locations block
   the batch. Artwork, dimensions, existing area notes, stock, sizes, and prices
   remain with their original tabs. Changing a location does not redraw or resize
   Design Studio artwork.
4. Optionally enter **Add Sales Note**, **Add Production Note**, or **Add Art
   Note**. These are order-level additions, submitted once per included order
   through its native `addSalesNotes`, `addProductionNotes`, and `addArtNotes`
   inputs. They do not replace, edit, or remove saved notes. Blank inputs add
   nothing; new draft text remains editable until applied. A requested native
   add-note input that is disabled, unavailable, or ambiguous blocks the batch;
   the extension does not change Art Note options to bypass that restriction.
5. **Apply changes** validates every included order before updating the unsaved
   native editors. Conflicts block the batch before mutation; resolve or uncheck
   the affected orders, then apply to the remaining orders. **Discard pending
   changes** clears the popup draft before Apply. After Apply, inspect the actual
   order pages, then use **Save All** to keep the changes.
6. **Save All** starts every included order's native **Save Order** action
   together, retaining CRM's validation and warning dialogs. It waits for each
   native save to finish and lists each result. The extension does not refresh
   any tabs afterward. Clicking an order number focuses its existing tab for
   manual inspection. Failed or uncertain saves stay listed for review and are
   never retried automatically.

The popup draft and batch results survive closing/reopening the popup, and an
initiated operation runs in the extension service worker after the popup closes.
State uses extension-only `chrome.storage.session`; it is kept in browser memory,
is not synced or sent to the local app, and clears on browser shutdown or
extension reload/disable/update. A service-worker interruption marks unfinished
work for review instead of resuming edits, appending notes, or repeating saves.
**Check save status** only inspects the values already loaded in the current
pages; it never refreshes, submits notes, or saves. Review unknown outcomes in CRM
before starting a new batch. Each new scan starts with an empty draft.

**Cancel unsaved edits** invokes CRM's native Cancel only for batch-owned editors
whose contents still match the batch and chooses **No** on its exact discard
warning. It cannot undo a completed save or discard unrelated manual edits.
Resolve remaining errors on the affected order pages.
Tabs are rechecked for their order ID, original window, and document before each
action; closing, moving, or navigating a tab stops work on that target. The same
order cannot join unfinished All Orders batches in two open windows.

This feature uses the logged-in Chrome session and CRM's native Angular controls
and helpers. It requires no Selenium/Python worker, automation queue, or running
local Automation app. Version **1.8.0** adds Chrome's **scripting** permission to
invoke the adapter in the selected CRM document; it adds no host access and no
page-event or local-server write channel. It does not order stock, send messages,
or alter the existing single-order queued workflows.

Reload **CRM Order Assistant 1.8.0** at `chrome://extensions`, accept Chrome's
permission prompt if shown, and refresh open CRM order pages before using it.
**No local Automation app restart is needed.**

Offline checks use sanitized fixtures and local Angular, with all persistence
on loopback. The popup harness simulates the extension transport and a second
window; it never opens live customer orders. Live CRM saves, installed-extension
behavior, and macOS remain unverified. This environment's Chrome debugging API
does not permit loading an unpacked extension for the additional installation
smoke check; the popup/transport replay uses the actual shipped modules instead.

```powershell
node --test tests/test_all_orders.mjs tests/test_all_tabs.mjs tests/test_all_tabs_stock.mjs tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
python -m unittest discover -s tests -p test_all_orders_browser.py
```

The browser replay needs a local ChromeDriver and the existing ignored Angular
asset at `runtime/debug/all_tabs_stock_vendor/angular.js`. Screenshots are kept
under ignored `runtime/debug/all_orders_tests/`.

## All Tabs print-area title colors - 1.7.29

In **Edit Order > All Tabs**, print-area titles use CRM's native method colors:
**HD Digital** pink (`#ff5686`), **Screen Printing** blue (`#5bbad3`),
**Embroidery** green (`#69bd45`), and **Direct To Garment** purple (`#7e51a1`).
The full title bar uses the same color in light and dark mode, with white text.
Other methods and rows keeping mixed methods retain their uncolored titles.

Titles follow staged method selections, including new print areas. Choosing
**Keep** restores the original single-method color or uncolored mixed title;
discarding pending changes also restores the current order's colors.

Reload **CRM Order Assistant 1.7.29** at `chrome://extensions` and refresh open
CRM order pages. **No local Automation app restart is needed.**

## All Tabs shared stock editing - 1.7.28

In **Edit Order > All Tabs**, **Shared stock** lists each native catalog
product/color combination that appears on **two or more distinct active product
tabs in this order**. Sizes do not split a group. For example, NF1270 BLACK/WHITE
on four tabs with L, M, M, and L produces one row affecting tabs 1, 2, 3, and 4.
G500 on tab 1, ST350 BLACK on tabs 2 and 3, and ST350 WHITE on tab 4 produces only
the ST350 BLACK row. Duplicate items on just one tab do not qualify. Removed tabs,
removed items, and custom Style Sub items are excluded.

Version **1.7.28** uses a horizontal native-style product row with the product
preview, description, optional product note, native product input, and color
dropdown. The current color starts selected and stays selected when the panel
is rebuilt. The **Size / Quantity** table shows combined, read-only quantities
for matching items on the affected tabs, with no prices or quantity editors.
Only sizes in use are listed. For example, L × 1 on tab 2 and XL × 2 on tab 3
show **L: 1, XL: 2**; another color on tab 4 does not count. NF1270 BLACK/WHITE
with L, M, M, and L shows **M: 2, L: 2**. Items without assigned sizes show their
remaining quantity as **Unspecified**. The row stacks on narrow windows.

Choose a product from **CRM's native autocomplete suggestions**, then a color
from its native dropdown. Only that row's original matching items change. Each
item keeps its own sizes in use, quantities, prices, and names; each tab keeps its
artwork and notes. Changing only the color is supported. A replacement must be a
native catalog product; free-text product names and Style Sub are not accepted.

The replacement color's native catalog size list must include every size with a
nonzero quantity on every affected tab. Otherwise, the row names the missing
sizes and tab numbers, and **Apply changes** stays disabled for the entire batch,
including pending print-area edits. Empty size options do not block a change.
Catalog colors and sizes must finish loading before applying. A product that
does not load within 15 seconds requires another explicit selection.

Selections are staged until **Apply changes**, which calls CRM's native product
Apply helper for each matching item and verifies the product/color and retained
sizes, quantities, names, and prices. A native rejection or verification failure
rolls back the combined stock/print-area batch. Resolve any pending product
selection on a real tab first; disabled native stock controls remain blocked.
**Keep original stock** resets one stock row;
**Discard pending changes** resets all staged stock and print-area edits. Pending
selections survive navigation between real tabs and All Tabs, and block Save
Order until applied or discarded.

Review the affected tabs, use CRM's **Save Order**, then refresh and confirm the
saved product/color and each tab's size quantities. Apply changes does not save
the order. CRM's normal Cancel discards applied, unsaved changes.

Reload **CRM Order Assistant 1.7.28** at `chrome://extensions` and refresh open CRM
order pages. **No local Automation app restart is needed.** The feature works
independently of dark mode and requires no new extension permissions.

Offline model checks:

```powershell
node --test tests/test_all_tabs.mjs tests/test_all_tabs_stock.mjs
python -m unittest discover -s tests -p 'test_all_tabs*browser.py'
```

The native stock browser replay additionally uses local, untracked Angular 1.2,
legacy UI Bootstrap, and native CRM product scripts under
`runtime/debug/all_tabs_stock_vendor/` (or `ALL_TABS_STOCK_VENDOR_DIR`); see
`tests/test_all_tabs_stock_browser.py` for the asset contract. It skips when those
assets or a local ChromeDriver are unavailable. Catalog responses and saving are
local fixtures; it never opens or edits a live customer order. Live CRM save
behavior and macOS browser behavior still require verification in those environments.

## Multi-tab controls - 1.7.26

**All Tabs** and **Order All** appear only on orders with more than one active
product tab. They stay hidden on single-tab orders, including after tabs are
removed, and return when a second tab is added. Tabs marked for deletion do not
count. All Tabs still requires Edit Order; Order All still requires eligible
automatic stock outside edit mode, even when only one of several tabs needs it.

Reload **CRM Order Assistant 1.7.26** at `chrome://extensions` and refresh open
CRM order pages. No local Automation app restart is needed.

## Editing previews and Order All - 1.7.22

**Design Studio Previews stay at the bottom during Edit Order.** They follow
the highlighted real tab, including while All Tabs is open, and show that tab
number above the images. Front, Back, sleeve, and inside-tag images use the same
artwork IDs, site configuration, and image sizes as CRM's normal previews.
Unavailable images are hidden. Existing transfers-only, approved-order, and
vendor-release restrictions still apply. Leaving edit mode returns to CRM's
normal preview section. Previews display the saved Design Studio artwork;
changing a print-area label does not redraw that artwork.

**Order All** appears after the last real tab in both tab bars outside Edit
Order, on orders with multiple active product tabs when at least one tab has an
enabled automatic **Order Goods** control.
In **1.7.24**, one click immediately submits CRM's own **Order Goods** request
for every eligible tab in the current order. It works with the local Automation
app closed and creates no automation job. The extension uses the same native CRM
inventory service as each individual button, keeps the highlighted tab, and
shows per-tab results. Requests start together; the page refreshes once after
all requests return confirmed success, so one tab's normal five-second reload
cannot interrupt the rest. The customer's tab count stays the same.

An order-wide **Ordered** status does not hide Order All when a tab still has
stock available to order, such as newly added sizes. When no tab is eligible,
including when all stock is ordered, Order All is hidden. Disabled/locked or
hidden native controls, **Manual Order** dialogs, and **Add Box** are excluded.
Both Order All buttons stay disabled during requests or while native inventory
is still loading/ordering. Locked stock remains locked; this button never applies
an unlock status. A failed, timed-out, or interrupted request shows the affected
tab on its own line with a **bold tab number**. In **1.7.25**, errors no longer
disable Order All: press it again to retry currently eligible failed tabs.
Confirmed purchases are excluded from the retry, including when a successful
tab's native control remains busy until refresh. Retries always require another
click; the extension never retries automatically. Browser-tab
session receipts are written before submission and retain confirmed results
across refreshes. Confirmed stock contents are not submitted again through Order
All; new quantities/sizes may be ordered after CRM exposes an eligible button.
If an earlier extension version already submitted an automation task for this
order, its saved pending/uncertain request is still honored. Check that existing
queue entry before starting direct stock ordering; updating does not cancel it.

Version **1.7.23** fixes a missing Order All button on CRM's native inventory
tables. Eligibility reads the vendor's header cell as well as older data-cell
layouts. Available stock now shows Order All after the last real tab; the same
ordered/locked/manual-order exclusions remain in effect.

Reload **CRM Order Assistant 1.7.25** at `chrome://extensions` and refresh open CRM
order pages. **No local Automation app restart is needed** for these extension
changes. Order All works directly in the current CRM browser session.

Offline verification uses local images, stubbed native CRM responses, and stock
fixtures; it never submits a real purchase:

```powershell
python -m unittest discover -s tests -p test_order_qol_browser.py
node --test tests/test_order_all_batch.mjs
```

## All Tabs print-area editing - 1.7.21

In **Edit Order** on orders with multiple active product tabs, **All Tabs**
appears before tab 1 in both design-tab bars.
Your highlighted real tab stays selected until you click All Tabs. The added
control is a local view; the order's real tabs, numbering, and customer tab count
stay the same. It disappears after leaving edit mode and is excluded from printing.

All Tabs lists the **union of print-area locations** across available design tabs,
with the affected tab numbers on every row. For example, Front on tabs 1 and 2,
Back on tab 2, and Front-Left Chest on tab 3 produce three rows. Each row edits
its original areas, so swapping Front and Front-Left Chest keeps the correct
targets. It changes **print-area location and print method**. Each area's notes,
artwork, dimensions, and product prices stay with their original tab. Mixed
methods remain separate until you explicitly select a method for that row.

1. Open **Edit Order**, then **All Tabs**.
2. Choose location or method changes. Complete both sides of a swap before applying.
3. Use **Add print area** to select a new location and method. Its preview lists
   only tabs missing that location after your pending edits. Existing areas and
   their methods are kept; selecting an already-present location adds nothing.
   In **1.7.22**, the red **×** at the top right of an existing area stages its
   removal from exactly the listed tabs. Removing Front leaves Front-Left Chest
   and Front-Right Chest in place. **Undo** restores that pending row; **Discard
   pending changes** clears all staged removals. Remove several rows to remove
   several locations, or every row to remove all areas. Removals are applied with
   the rest of the batch, then saved with **Save Order**.
4. Review the affected tabs and click **Apply changes**. Conflicts identify every
   tab that would have duplicate locations and block the batch. No changes are
   applied until the entire batch validates.
5. Inspect the real tabs, then use CRM's **Save Order**. After saving, refresh and
   confirm the intended locations/methods persisted. CRM's normal save validation
   still applies. Apply changes updates the unsaved order; it does not save it.

Pending edits stay available when switching between real tabs and All Tabs.
**Discard pending changes** rereads the current order without undoing changes
already applied to the native editor. Save Order is blocked while bulk edits are
pending, so they cannot be silently omitted. CRM's normal Cancel discards unsaved
native edits. If tabs or areas change after the bulk preview, applying stops;
discard the pending bulk edits and read the tabs again. A rejected native helper
rolls back the batch before reporting the error.

This feature works independently of dark mode and the local Automation app. It
uses CRM's existing Angular print-area controls on the current order, including
the embedded order app, without additional extension permissions.

Reload **CRM Order Assistant 1.7.21** at `chrome://extensions` and refresh open CRM
order pages. **No local Automation app restart is needed.**

Offline tests use sanitized order markup and an isolated Chrome profile. They
verify saved values after a fixture save and reload, without editing live orders:

```powershell
node --test tests/test_all_tabs.mjs
python -m unittest discover -s tests -p test_all_tabs_browser.py
```

Set `CHROMEDRIVER` if a local driver is not available under `.wdm/`. Live CRM saves
and macOS browser behavior require verification on their respective environments.

## SanMar-only Reorder - 1.7.16

Version **1.7.16** removes the S&S browser connection, sign-in launcher, read-only
browser checks, background worker, alarms, scripting permission, and supplier
website access. Reorder runs only through the local app's SanMar worker and no
longer needs an extension supplier connection. CRM order tools, dark mode, list
controls, and Salesforce tab reuse retain their existing behavior.

Restart the local Automation app, reload CRM Order Assistant at
`chrome://extensions`, and refresh the dashboard and open CRM pages. Existing
S&S purchase receipts and historical confirmation links remain available; their
unfinished tasks require manual review. See the [Reorder guide](../docs/REORDER_STOCK.md).

## Open Applicable and Select Applicable

Version **1.6.6** provides two adjacent buttons at the right of the report title,
below **Show Images & Sizes**, on the currently loaded CRM list page:

- **Open Applicable** (previously named Select Applicable) selects the **tan**,
  **neon-green**, and **purple** orders using CRM's native selection, deselects
  any other colors, and opens one new order tab per applicable order. Tabs open
  in list order in the **same Chrome window**; the CRM list stays active with
  those orders selected. Brown, pale green, maroon, and other colors are excluded.
- **Select Applicable** applies the same **tan**, **neon-green**, and **purple**
  selection, deselecting other colors, without opening tabs. It uses CRM's native
  selection and requires no connection to the local Automation app or tab-opening
  bridge.

Applicable status colors include tan (`#f3c49c`), native/extension neon green
(`#20df4b` / `#22ec48`), and purple (`#86209f` / `#9c27b0`).

Version **1.7.18** excludes stock-issue orders and dimmed rows from both buttons,
including max-rush orders. Rush highlighting respects the original CRM status
color: it applies only to undimmed tan, neon-green, or purple orders. Stock-issue
orders keep their original status color instead of turning dark green. Already
selected issue orders are deselected by either button and never opened.

Native selection may turn a row gray with a yellow selection marker; its
underlying status color still determines eligibility for both buttons. Both native
neon green and the extension's rush highlight qualify. The regular and **Lite**
links to one order produce only one new tab. Both buttons exclude hidden rows,
image-preview rows, and orders on other pagination pages. Clicking Open Applicable
again after completion opens a fresh set of tabs; repeating Select Applicable
keeps the applicable orders selected. Both buttons are disabled while either action runs.

If CRM's selection cannot be verified or the list changes during selection, no
tabs are opened. If Chrome opens only part of a batch, the status reports the
count and keeps the selection; inspect the open tabs before clicking again.

## Max-rush list highlighting

Version **1.7.20** adds a daily **1 PM cutoff**, using the computer's local time.
Starting at **1:00 PM**, orders with **Production Date** set to **today or the
next business day** qualify as max rush when their **Fulfillment Date** (also
spelled **Fulfilment Date**) or **Due Date** is the **business day after that**:

| Viewing day, from 1 PM | Production Date | Fulfillment / Due Date |
| --- | --- | --- |
| Monday | That Monday or Tuesday | Immediately following Wednesday |
| Tuesday | That Tuesday or Wednesday | Immediately following Thursday |
| Wednesday | That Wednesday or Thursday | Immediately following Friday |
| Thursday | That Thursday or Friday | Immediately following Monday |
| Friday | That Friday or Monday | Immediately following Tuesday |
| Saturday or Sunday | That viewing day or Monday | Immediately following Tuesday |

The existing rules remain active **all day**: Friday production due the following
Monday qualifies on Friday; Monday production due Tuesday qualifies on Saturday
and Sunday. Before 1 PM, Monday through Thursday retain CRM's own urgency behavior.

Matching orders display the CRM's neon green (`rgb(34, 236, 72)`). Business-day
calculations skip Saturday and Sunday, without a holiday calendar. Both production
and fulfillment/due dates must be readable and consistent. Production dates in
report group headers are supported. Stock-issue and dimmed orders keep their
original colors and remain excluded from both list buttons.

Version **1.7.17** fills the entire matching order, including its companion
image-preview row, so no tan or purple strip remains underneath. Selecting the
order temporarily hides the green and shows CRM's normal selection color and
marker. Deselecting restores the full green highlight. Both list buttons still
recognize eligible selected rush orders as applicable.

Highlighting is automatic, independent of the dark-mode setting and local
Automation app. It follows list refreshes, pagination, changed dates, the local
1 PM cutoff, and midnight, removing stale extension highlights as the time or
list changes.
Unreadable or conflicting dates are left unchanged. Printed pages retain their
original CRM colors and omit both list controls.

Reload **CRM Order Assistant 1.7.20** at `chrome://extensions`, then refresh open CRM
list/report pages. **No local Automation app restart is needed.**

## Retry an unfinished automation

In the local Automation dashboard's queue, every failed or canceled extension
automation offers **Retry** beside **Delete**: Manual Process,
Cancel, Reachout, and Stock Issue. Retry keeps the original queue entry and its
order number, selected designs/products/sizes, prices, and reason. It waits in
the same local queue and uses the workflow's existing saved-state checks and
receipts. Completed tasks do not offer Retry. An uncertain purchase, refund,
email, or Slack submission still stops for review when its recovery checks
cannot confirm the result.

Restart the local Automation app and refresh the dashboard to enable retries
for Extra Print Areas and all three Stock Issue actions. No extension reload or
CRM page refresh is needed for this update.
Queue entries and their original requests now persist locally across app updates
and restarts, until you use **Delete** or **Clear finished**. Waiting requests
resume in queue order. A request interrupted while running remains visible as
failed or canceled for review; it never reruns automatically. Retry retains its
original selections and uses the same recovery receipts. Restart the app and
refresh the dashboard to enable persistence; no extension reload is needed.
Saving begins with this app version. A request already lost under an older
version must be queued again with the same selections and prices; its durable
workflow receipts remain available to the recovery checks.

## Extra Print Areas

Under **Manual Process → Extra Print Areas**, select design tabs and choose one category per tab: **Sleeve Prints**, **Reversible Prints**, or **Side Prints**. Sleeves and sides offer **Ink print / Embroidery**, followed by **Left / Right / Both**. Reversible prints are ink only and have no left/right selector.

The worker waits up to **45 seconds** for the invoice popup's non-PDF **View
Invoice** link, checking the CRM app frame and the parent page as it loads. It
copies the link and clicks **Cancel**; it never sends the CRM invoice. If the
link does not appear, the task fails with the timeout in its message. Use
**Retry** on that original queue entry to retain the request and resume through
the existing checks for saved areas, pricing, clones, notes, and email receipts.

Ink pricing uses the combined garment quantity of selected tabs with an ink or reverse request, counting each original tab once: $8 for 1–9, $7 for 10–19, $6 for 20–99, and $5 for 100+. Embroidery defaults to $15. Custom sleeve/side ink prices are shared across those ink areas; the separate reverse-price override is shared across every selected reverse tab. Unselected tabs do not affect the tier.

For sleeve ink-print requests, the Salesforce email names the price **per sleeve print**, including when both sleeves are selected. For example: `There will be an additional charge of $2.66 per sleeve print to include this request.` Restart the local Automation app to load this wording change; no extension reload or CRM page refresh is needed.

For **Sleeve Prints** only, setting every selected sleeve method's price to **$0.00** uses the Salesforce **[AUTO] Comp Sleeves** template. Left, right, or both sleeve areas are still added with the selected print method, and existing product and size prices are retained and verified after saving and reloading the order. This also supports sleeve embroidery priced at $0.00. Requests that include side, reversible, or extra embroidery areas, or any paid sleeves, keep the **[AUTO] Additional Requests** template.

New zero-price sleeve Sales Notes read `Comped Sleeve prints` and `Emailed` on separate lines. Matching notes from the earlier `$0.00 per sleeve` wording are retained on retries to avoid duplicate notes.

The Comp Sleeves template does not need the paid email's `[REQUEST]`, `[COST]`, or `[INVOICE_LINK]` placeholders; any of those present are filled and verified. The order number and Salesforce Activity confirmation remain required. Restart the local Automation app to load this worker update; no extension reload or page refresh is needed.

Reverse printing clones each selected original as **REVERSEPRINT** (without a hyphen), explicitly selects **Style Sub** and clicks **Apply** for each product, and selects the original vendor from its dropdown. Style and description are copied; two-color names are reversed and single colors retained. Quantities are restored by size label. The clone's unit price is the reverse price multiplied by its front/back area count (front or back: one charge; both: two). The original product prices are preserved. CRM carries over artwork and print methods.

On retry, existing clones are inspected and completed fields are skipped. Older **REVERSE-PRINT** clones are renamed. CRM has saved that hyphenated name as **H-PRINT**, causing verification to fail before email. An H-PRINT tab is reused only when its artwork identity, print areas, Style Sub vendor/style/description, reversed colors, and size quantities match the source. Uncertain or duplicate matches stop for review before another clone is created. CRM changes and sales notes are verified after saving and reloading the order, before the existing Salesforce Additional Requests email is sent with `reverse prints`, the per-area charge, and the invoice link. A local receipt prevents the same reverse-print email being sent again after a successful run; receipts are stored under the app's runtime state directory.

For the reverse-print naming/recovery fix, restart the local Automation app and refresh the dashboard, then use **Retry** on the original queue entry to retain its selections, price, and email receipts. No extension reload is needed. Refresh the CRM order page after the task completes.

### Extra EMB Area

Choose **Extra EMB Area** inside **Manual Process → Extra Print Areas** for each desired tab. It uses the shared, editable **Embroidery** price, defaulting to **$15 per additional area per garment**. The existing area is included, so each selected tab receives one surcharge even if both chest areas already exist.

Front is converted to Front-left chest, and Front-right chest is added. If either chest area already exists, it is reused and the missing side is added. Both chest methods are set to Embroidery. Other locations and existing artwork are retained. Unsupported or duplicate chest configurations stop for review.

The sales note is `Additional embroidery area`, `$15.00 each` (or the custom price), and `emailed txted` on separate lines. The existing Additional Requests email uses `additional embroidery area`, the charge, and the invoice link. No text message is sent. Local receipts verify saved changes and prevent duplicate pricing/email on matching retries; an interrupted save with unverified prices stops for review.

## Copyright cancellation by design

Choose **Cancel → Copyright - Cancel** to scan every order design tab and list
its **Design Name**, tab number, and quantity. Loaded data is read without changing
tabs; a visible-tab fallback returns to the previously open tab. Select the
affected designs and enter the required copyright reason.
Tabs can share the same artwork **Design ID** and Design Name. Each remains
individually selectable using its persisted order-tab ID, including after another
tab is removed and the remaining tabs are renumbered.
Multiple designs start unchecked; a single design starts checked. **Select all**
and **Clear** change the selection, and the dialog explains its scope before
**Queue task** is enabled. The dialog wraps long design names without horizontal
scrolling and marks **Full cancellation** in red when every tab is selected.

- Selecting every tab uses the existing whole-order cancellation/refund flow and
  Salesforce **[AUTO] Copyright Cancel**.
- Selecting some tabs uses Salesforce **[AUTO] Copyright Cancel - Partial**. Only
  the affected tabs are removed using CRM's red-X action. Purchased-stock tabs
  may remain at **QTY 0** with a **$0.00 design total**. The remaining order stays
  active; its other designs and quantities are verified after saving and reload.

Both templates receive the order number, entered reason, and selected Design
Names. A partial cancellation refunds only the verified credit balance after
CRM recalculates the remaining order. It does not add the whole-order Refund
fee. Stripe refunds are checked against the saved total, paid amount, credit
balance, input, and available refund; the saved payment and refund transaction
are verified afterward. Non-Stripe payments use the existing Salesforce
**Refund Pending** Case route with the partial amount in the Case subject.
Whole-order cancelled-stock Slack routing is skipped for partial cancellations.

An unreadable or changed design, inconsistent price/payment, unsupported Contact
email, or missing/wrong template stops for review. Refund emails are not sent
when the removal produces no refundable credit. Partial work is recorded under
`runtime/state/copyright_partial_cancellations/`. Use **Retry on the original
Automation queue task** after an interruption so its original design IDs and
scope are retained. An unconfirmed refund or Case submission stops for review;
automatic retries do not submit it again. Existing Salesforce Activity
confirmation and email receipts also apply, with separate receipts for each
partial selection. Keep these receipts until recovery is complete.

If a refund was completed while a task was stopped after design removal, Retry
can recognize it even when the partial receipt still says `removed`. It checks
the exact refund, reduced Paid, zero Balance Due, unchanged total/fees, and all
original transaction rows before continuing with the Sales Note and email.
It never repeats that verified refund. A missing Sales Note gets one more save
attempt after reload and must persist before the workflow continues. Other
payment changes and uncertain email submissions still stop for review.
These recovery fixes load with the next queued worker: keep the original task
and use **Retry**. No app restart, extension reload, or CRM refresh is needed
for these fixes.

The extension always requires a complete scan and explicit design selection.
Existing Sheets Scanner copyright rows continue to target the whole order.
The worker's `--copyright-designs-json` argument carries the complete scan and
selected IDs for a selected single-order request; `--dry-run` validates the
selection and email draft without removing/saving designs, refunding, creating
a Case, or sending email.

After updating to **1.6.1**, restart the local Automation app, reload CRM Order
Assistant at `chrome://extensions`, and refresh open CRM order pages.
Selected copyright requests use a dedicated bridge endpoint. An older app that
has not been restarted rejects them; the extension never falls back to the
old whole-order copyright endpoint.

Version 1.6.1 fixes the repeated "Could not identify every design tab" error for
orders that reuse artwork across tabs. Both the app and extension need the update.
Existing queued requests and recovery receipts remain compatible when their
artwork IDs identify tabs uniquely. Ambiguous older requests stop for review.

For dialog appearance updates only, reload CRM Order Assistant and refresh the
CRM order page; the local Automation app does not need a restart.

## Stock Issue: Extension Required

Choose **Stock Issue → Extension Required** on an order page, select the affected
products/colors, and enter the extension days. Each product starts with all its
detected order sizes checked. Use its **Clear** button to deselect those sizes,
then check only the sizes that need an extension. Each selected product requires
at least one size before **Queue task** is enabled. Products with unreadable order
sizes cannot be queued for this workflow.

The saved CRM Sales Note and Salesforce email place the color before the product
description and describe each product/color with its own sizes. One size uses
`size x-large`; multiple sizes use `sizes medium, large, and x-large`.
Selecting every detected size uses `all ordered sizes`
(a product with only one ordered size still names that size). CRM size codes such
as `XL` are expanded for the message text. A note can read `An extension of 5 days
is needed for 5040 black Bayside Performance T-Shirts in size x-large`,
followed by the existing `Emailed Txted` line.

After updating to **1.5.9**, restart the local Automation app, reload CRM Order
Assistant at `chrome://extensions`, and refresh open CRM order pages.

Restart the local Automation app to load changes to the sales notes and email
wording.

## Stock Issue: Suggest Different Size or Color

The Size and Color emails use the same color-before-description wording as
Extension Required, such as `5040 black Bayside Performance T-Shirts in size
medium`. Multiple sizes use `in sizes medium and large`. Each product/color
keeps its own affected sizes, and the Size email names all selected sizes even
when every order size was selected. Suggested replacement sizes or colors remain
alternatives joined with `or`.

The generated draft changes the template's stock availability sentence to
`The [selected products] cannot currently be supplied because of stock shortages`.
This works for both one product and multiple products without choosing `is` or
`are` from catalog descriptions. The draft is verified before sending.

Restart the local Automation app to load this wording update.

## Unresponsive cancellation and item suggestion emails

Version **1.7.12** provides these email options on the current order:

- **Cancel > Unresponsive** runs the existing unresponsive cancellation/refund
  workflow. Its editable **[AUTO] CANCEL - Unresponsive** starter template
  requires `[ORDER-NUMBER]` in the title and contains placeholder body copy.
- **Stock Issue > Suggest Different Item** scans the CRM order's products/colors.
  Check each affected item. A required **Suggested replacement product** input
  appears directly underneath each checked product/color. A single detected
  product starts checked; multiple products start unchecked so you choose every
  affected item explicitly. Each checked item also shows its detected order sizes,
  initially all checked. Use **Clear** to choose particular sizes or **Select all**
  to restore all sizes for that item. **Queue task** requires at least one size
  and a replacement for every checked item. Products with unreadable order sizes
  cannot be queued. Unchecking hides its controls and excludes the item from the request;
  checking it again restores the text entered while this dialog is open.
  The **[AUTO] STOCK - Item** template requires `[ORDER-NUMBER]` in the title
  and `[STOCK]` and `[PRODUCT]` in the body. These placeholders use corresponding
  stock and replacement lists in the same order: one item, two joined with
  `and`, or three or more separated by commas with a final `and`. Commas inside
  a replacement remain part of that item's text. All pairs use one email.
  Only `[STOCK]` receives size wording: `in size medium`, `in sizes medium and
  x-large`, or exactly `in all sizes` when every detected size is selected,
  including an item with only one detected size. `[PRODUCT]` uses your replacement
  text without adding sizes. The worker rechecks saved item IDs, product details, sizes, and
  quantities before preparing the email. Save any actual replacement in CRM
  through your normal editing workflow.

Edit placeholder copy in **Settings > Salesforce Template** before using these
emails. Both starter templates are added once, preserving any existing saved
content, renames, or deliberate removals. Missing required placeholders stop
the email for review.

Suggest Different Item uses the local queue, recipient/Orders sender checks,
Salesforce Activity confirmation, and bounded resend policy. Its per-order
receipt under `runtime/state/stock_issue_item_emails/` prevents a title edit from
repeating a confirmed email. A changed replacement, stock, recipient, or an
edited draft after an uncertain attempt stops for Activity review. Use **Retry**
on the original queue entry to retain its selections and entered replacement.

After Salesforce Activity confirms the email, the worker appends this **Sales
Note**, using the same stock sizes and corresponding replacement list as the email:

```text
[STOCK] stock issue
Suggest [PRODUCT]
Emailed Txted
```

Each line is separated by a real newline. The saved note must persist after a
reload. The worker then sends `[CRM ORDER LINK] - Rush Order stock issue` to the
same Rush Order Slack channel used by the other stock issue reachouts when the
dashboard's **Post to M6** switch is On. It requires a confirmed Slack delivery
receipt, or an intentional skip while that switch is Off, before applying the configured stock issue
status (normally **Issue - Stock**) and verifying it after a reload. Retrying the
original queue entry reuses the confirmed email and Slack receipts and skips an
already saved note. A failed or uncertain follow-up stops the task at that stage
for recovery; it cannot report completion before all stages succeed.

The worker enters Edit Order before setting the Sales Note draft and verifies
saved Sales Notes after reloading. An unsaved draft or a matching Production Note
does not count. A missing Sales Note gets one save retry. Existing Production
Notes remain intact. Restart the local app and refresh the dashboard after this
correction; no extension reload or CRM page refresh is required. Retry the
original failed queue entry, including tasks stopped at `production_note`, to
reuse its confirmed email and finish the Sales Note, Slack, and status steps.

**Post to M6** sits at the right end of the dashboard's main tab bar. It defaults
to On, persists on the computer running the local app, and applies to both
dashboard automation and extension requests through that app. Off skips only
M6 (`T03DK2TN7/C04KPACK6VC`); other Slack channels retain their behavior. It does
not suppress CRM saves, status updates, or Salesforce emails. Changes apply at
the Slack step without an extension reload. Turning it back on does not send
messages skipped by finished tasks.

Previously queued requests retain their original combined replacement text on
Retry. The worker does not guess individual replacements from that older text.
An existing confirmed single-item email is recognized after updating; changed
or ambiguous older multi-item receipts stop for review before another send.
Requests queued before size selection retain their original whole-item scope.
Confirmed emails with the same stock/replacement pairs can be reused when all
sizes are selected. A changed size selection or an unconfirmed draft whose
wording changes stops for Activity review instead of starting another send budget.
Older app versions reject the new requests rather than dropping the sizes.

Restart the local Automation app, refresh the dashboard, reload CRM Order
Assistant at `chrome://extensions`, and refresh open CRM order pages.

## Salesforce Orders email default - 1.7.19

Each newly opened Salesforce Lightning email composer automatically selects
**Orders &lt;orders@rushordertees.com&gt;** in its **From** dropdown. This works
independently of CRM dark mode and the local Automation app, including composers
opened after navigating between Salesforce records without refreshing the page.

An email already using Orders is left alone. After the default is applied, you
can manually choose another sender for that draft. The extension yields if you
start clicking or typing while its selection is pending. It uses only the
composer's From dropdown and its linked popup, with an exact email-address match;
Orders Team and similar addresses do not qualify. It never clicks Send or edits
the recipients, subject, or body.

If Orders is missing, disabled, ambiguous, or the dropdown does not load within
three seconds, selection stops and the current sender remains. A rejected
selection is not retried. Check the visible From value before sending. Reload
**CRM Order Assistant 1.7.19** at `chrome://extensions` and refresh open Salesforce
pages to enable this feature. No local Automation app restart is needed.

Offline browser verification uses sanitized markup based on saved Salesforce
pages and an isolated Chrome profile; it never opens a live customer record:

```powershell
python -m unittest discover -s tests -p test_salesforce_sender_browser.py
```

Set `CHROMEDRIVER` if a local driver is not already available under `.wdm/`.

## Salesforce tab reuse

Clicking a Salesforce link in CRM navigates an existing Salesforce tab instead of opening a duplicate. The search includes every normal Chrome window in the current browser profile; if the reused tab is in another window, that window and tab are focused. If no Salesforce tab is open, the link opens normally in a new tab.

The extension also catches Salesforce links that create a new tab or window through browser navigation and folds them into the existing Salesforce tab. Chrome extensions cannot reuse tabs from another Chrome profile, an incognito session where the extension is disabled, or a separate browser application.

## Supported pages

- `https://crm2.legacy.printfly.com/order/<id>`
- The same-origin embedded order app at `https://crm2.legacy.printfly.com/app#/order/<id>`

Dark mode and order tools remain scoped to these order routes. The separate
list highlighter supports `/report`, `/reports`, and `/orders` list routes,
including their embedded `/app#` forms. It only styles rows containing one
identifiable order and matching readable production and fulfillment/due dates.
Both list buttons use the native selectable rows in CRM's report table. The manifest injects
across the CRM host to reach embedded frames; `content.js` keeps its order-page
gate. Styling exists only in screen media, so CRM printing stays light.

The separate sender content script runs on HTTPS `*.salesforce.com` and
`*.force.com` pages, including matching frames, and acts only on Salesforce's
OutgoingEmail From fields. It uses the extension's existing Salesforce host
access and adds no permissions or local-app requests.

## Local app bridge and order tools

### Parallel order workers — 1.8.7

Supported single-order actions share the local app's existing 1–8 worker setting
in **Settings > System & workers**: all five Cancel choices, Copyright Removal
and Copyright Reachout, Complicated EMB (both routes), Oversize EMB, Address
Validator, Product Separator, Order Goods, and Push Back. An arriving order can
take a free slot while earlier orders are still working. Each order keeps its
own queue entry, progress, report, cancellation, and receipt-preserving Retry.
Only different orders run together. Work stops admitting new orders at an
intervening unsupported queue entry, so it runs after the current workers drain.
Stock Issue, Stock Swap, Extra Print Areas, Auto Splitter, Shipping Bypasser,
Reorder Stock, dry runs, list runs, and scheduled/repeating tasks retain their existing
queue execution. Push Back's vendor-cart fallback remains serialized.

Cancellation/reachout slots reuse the existing Salesforce Worker Setup/Test
profiles; sign in to any newly enabled slots there. Saved queue requests retain
their exact reason and design/area selections. Restart recovery never replays an
interrupted running action automatically; review it and use its original Retry.

Order-page status is now scoped to its own queued request. Matching open order
tabs, including duplicate tabs, refresh once after completion. Native edits,
unsaved notes, staged All Tabs edits, and open dialogs defer the refresh until
they close. Lost app connections are checked again automatically. Native All
Orders Save All keeps its existing no-refresh behavior.

Restart the local app and refresh the dashboard, reload **CRM Order Assistant
1.8.7** at `chrome://extensions`, and refresh open CRM order pages. No new
extension permission is needed. QGT spreadsheet support is unchanged.

The extension checks `http://127.0.0.1:5123/api/extension/bridge/status` when its popup opens, so it can confirm the local Automation app is running. It never asks for, reads, or stores the Automation app PIN. The control bridge accepts only loopback Chrome-extension requests.

The **Manual Process** menu beside **Send Invoice** lets you choose one automation for the open order: Address Validator, Product Separator, Auto Splitter, Order Goods, Shipping Bypasser, Push Back, or Extra Print Areas. Cancel, Reachout, and Stock Issue provide their own workflow menus. Each request targets only the current order and enters the local Automation queue, waiting for any active task. The order page shows queued, working, and review status beside Manual Process and refreshes after a task started from that page completes.

Selecting **Shipping Bypasser** from **Manual Process** is an explicit approval to use SanMar stock without the normal 10-piece per-size safety buffer. It still stops when SanMar has fewer units than the order requires. List-driven Shipping Bypasser runs retain the 10-piece buffer.

Manually queued single-order Shipping Bypasser tasks also support in-house NJ-only stock: the worker verifies **Pick Up at warehouse > Robbinsville**, uses the original customer PO, and records one CRM Manual Order without an ADD PO or Add box. Retry retains this behavior and the existing purchase receipts. Automatic Processing and list-driven runs keep the NJ-only review stop. Restart the local Automation app to enable this change; no extension reload or page refresh is needed.

The worker prefers one warehouse, but checks a closer split before stopping when that complete shipment would arrive too late. The split retains the selected buffer setting and must pass cart validation and the delivery-date check for every warehouse. It uses one customer PO and the usual production note describing the boxes and quantities from each warehouse. This worker update loads on the next queued run; no app restart, extension reload, or page refresh is required.

## Stock Swap reachout

Version **1.6.7** adds **Reachout > Stock Swap**. Save the replacement products
in CRM first, then queue the action from that order. It sends one Salesforce
email covering every detected swap on that order. It reads the original items
from `https://crm2.legacy.printfly.com/app#/quotes/<linked-quote-id>` and the
replacement items from the saved current order, including **Style Sub** fields.
The product comparison reads both pages; the workflow records the confirmed
customer reachout in the order's Sales Notes.

The quote ID comes from the saved order's **Quote** link (`quoteId`), rather
than assuming it equals the order number. For example, an order numbered
**1234567** can link to quote **7654321**. The worker checks
the quote ID, linked order when present, and customer account before comparison.
It waits for CRM's identity, design, and product data to load, and supports the
original quote's `items`/`po` fields as well as the order's `designItems`/`PO`.

Edit **[AUTO] Stock Swap** in the app's **Settings > Salesforce Template**.
The initial title is `Order #[ORDER-NUMBER] - Stock Swap`; the body contains
placeholder copy for you to edit. Keep `[ORDER-NUMBER]` in the title,
`[STOCK]` for the original item(s), and `[PRODUCT]` for the replacement item(s)
in the body. Each item includes its style, color, description, and affected
sizes. Multiple swaps use corresponding numbered lists in both
placeholders. Unchanged products and unaffected quantities are excluded.

The email and Sales Note name one affected size as `in size medium` and a
partial selection as `in sizes medium and large`. When the affected sizes cover
every ordered size for that product/color and there is more than one size,
they use `in all sizes`. A product with only one ordered size still names that
size. Original and replacement products are checked independently against their
own complete ordered sizes, including other design tabs. Stock Swap detects
these sizes automatically from the saved quote and order.
Size quantities remain part of the comparison and retry checks. Existing
confirmed email receipts and earlier Sales Notes are retained; a changed draft
after an unconfirmed attempt still stops for review. This wording update loads
with the next queued worker; no app restart or extension reload is needed.

The scanner matches design tabs by unique saved IDs, artwork/name, or name,
then compares style/color and quantities by size. Saved product IDs distinguish
multiple equal-quantity swaps when available. A missing or wrong quote,
multiple quote options, unmatched designs, added/removed quantities, or
ambiguous replacement pairs stop for review before sending. A single original
and replacement with equal total quantities can have different size labels;
the email and Sales Note retain each item's own sizes. Multiple products still
require an unambiguous pairing.

The existing recipient/Orders sender checks, Salesforce Activity confirmation,
and bounded resend policy apply. Per-order receipts under
`runtime/state/stock_swap_emails/` also prevent title edits from repeating a
confirmed email. An uncertain send, changed swap, or changed recipient requires
review of Salesforce Activity; keep the original queue entry and receipts.
Use **Retry** on that entry after resolving the reported issue.

After Salesforce Activity confirms the email, the workflow appends this Sales
Note, using the same original and replacement item lists as the email:

```text
Swapped [STOCK] to [PRODUCT]
Emailed
```

It preserves existing notes and draft text, reloads the order to verify the new
note persisted, and skips a matching saved note on retries. If the note fails
to save, Retry uses the confirmed-email receipt and recovers the note without
resending the email. Dry runs preview the note without saving it. The Sales
Note update loads on the next queued run; no app restart or extension reload
is needed for this worker update.

The loading, linked-quote, and original-quote product-field fixes also load on
the next queued run; no app restart or extension reload is needed.

For a single-order draft check, `python workers/crm_stock_swap.py --order-id
1234567 --dry-run` reads both pages and verifies the Salesforce draft without
clicking Send. The queue uses the same worker subprocess and saved result contract.

Restart the local app, refresh the dashboard, reload CRM Order Assistant at
`chrome://extensions`, and refresh open CRM order pages after updating.

# Complicated EMB design selection

Reachout > Complicated EMB scans the order design tabs and offers tabs with
readable print methods, including ink and mixed-method designs. Designs already
switched from embroidery to ink printing remain selectable. Choose the affected
print area(s) under each Design Name, then answer "Feedback required?" with Yes
or No. A single design with one area is preselected, including tabs labelled
View Proofs after proof creation. Multiple designs or areas require selection.
The scan returns to the tab that was open before scanning.

Both feedback and HDD routes retain their selected areas. Templates containing
`[PRINT] for [DESIGN]` or `[PRINT] for "[DESIGN]"` list each selected area with
its own design in one email, for example `front print and back print for
"Team Logo" and the front-left chest print for "Crest"`. Area names use lowercase
and include `print`; Design Names keep their original case and template quotes.
Straight and smart quotes are supported. Earlier `[DESIGN]`-only templates,
including feedback templates, remain supported.

The worker rechecks each selection before processing and stops if its name or
area changed, the tab is unavailable, or its print methods cannot be read.
A change to another readable print method is allowed. Older queued requests
without area selections can resolve one unambiguous area per selected design;
multiple areas stop for selection through the extension. Sheets Scanner runs
without design selections require one available design with one area.

For an order with one active design tab, the sales note starts with
`Complicated embroidery` and omits the tab number. Orders with multiple tabs keep
the selected tab numbers, even when only one tab is selected. Retrying an order
with the earlier `Tab 1 is Complicated Embroidery` note does not add another note.

The HDD template check accepts the current Salesforce wording
`updated the [PRINT] for "[DESIGN]", from embroidery to ink printing instead`,
the earlier `updated the design, [DESIGN]` wording, and order-level wording.
It still requires the embroidery-detail
explanation and the ink-conversion text before sending.

After updating to 1.5.11, restart the local Automation app, reload CRM Order
Assistant at `chrome://extensions`, and refresh open CRM order pages.

## Oversize EMB print-area selection

Reachout > Oversize EMB to HDD scans all available design tabs and lists their
current print areas, such as `Front-left Chest` and `Back`. Readable ink and
mixed-method designs remain available after switching from embroidery. **Front**
and **Back** areas start checked across every scanned design. Chest areas,
including **Front-left Chest** and **Front-right Chest**, and other locations
start unchecked, even when they are the only area. Adjust the affected area(s)
under each Design Name as needed, then select **Queue task**.
The scan returns you to the design tab that was open before scanning.

The `[AUTO] Oversize Embroidery` template must contain `[PRINT] for [DESIGN]` or
a quoted Design Name placeholder, such as `[PRINT] for "[DESIGN]"`. Straight
and smart quotation marks are supported and retained around each design name.
One email names each selected area with its own design: for example,
`the back print for Team Logo and the front-left chest print for Crest`. Area
names use lowercase and include `print`; Design Names keep their original case.
This email wording update loads with the next queued task. The worker preserves
surrounding template formatting and links, rechecks selected Design Names and
print areas before CRM notes and email preparation, and stops for review when
an area changed, is unavailable, or cannot be identified uniquely. The existing
email Activity confirmation, resend limit, and durable receipts still apply.

Sheets Scanner runs without extension selections can resolve a single available
design with one print area. Multiple designs or areas stop for selection through
the extension instead of guessing which area is oversized.

After updating to 1.5.12, reload CRM Order Assistant at `chrome://extensions`
and refresh open CRM order pages to load the automatic Front/Back selection.
No local Automation app restart is needed for this selection change.
