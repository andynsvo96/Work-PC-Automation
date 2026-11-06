# Reorder Stock

Reorder is a **SanMar-only** Custom Processing action for explicit order IDs and
individual CRM order links. Select **Order IDs or order links**, paste the orders,
and select **Reorder Stock** at the bottom left. The same input validation,
deduplication, input order, and 100-order limit apply. Report/list URLs are rejected.

The input creates one local queue entry. **View orders** lists each order's status,
error, and confirmed purchase links, including purchases completed before a later
CRM save failed. Orders run serially under the existing CRM lock. A failed order
does not prevent checking the remaining orders; results are checkpointed after
each order. **Retry** keeps the same entry, skips successful orders, and uses each
unfinished order's original ID and durable receipt. Cancellation stops the active
worker and prevents starting another order. Interrupted work stays stopped for
review after an app restart. Old single-order queue entries remain supported.

## S&S removal and installation

S&S Activewear website automation has been removed, including the Selenium helper,
Chrome extension worker and page scripts, local supplier bridge endpoints,
connection/profile settings, credential setup option, sign-in launcher, and
read-only browser checks. SanMar Reorder uses its existing local browser profile;
it does not need an extension supplier connection.

Restart the local app, reload **CRM Order Assistant 1.7.16** at
`chrome://extensions`, and refresh the dashboard and open CRM pages. The extension
no longer requests supplier website access, scripting, or alarms. CRM tools,
dark mode, list controls, and Salesforce tab reuse retain their existing behavior.

Non-SanMar red stock stops the whole order for manual review before any purchase;
Reorder never substitutes SanMar for another supplier. Existing receipts are kept.
A receipt containing an S&S purchase plan, confirmed purchase, or uncertain
submission stops before CRM scanning, edits, or supplier access. Historical
confirmation links remain available for review. Finish any outstanding S&S work
manually after checking supplier history and its receipt. Do not delete a receipt
to bypass the stop. Old local config values are unused; existing browser profiles
and OS credentials are not deleted by the update.

## Eligibility

The worker checks each design's stock records using CRM's
`inventoryOrder.assignedToCurrentSubcontractor` boolean. An evaluated native inline
red/yellow style is a fallback for legacy views. Computed colors, including dark
mode overrides, do not determine the destination.

The worker waits up to 20 seconds for one visible stock table and two stable
reads. Live Angular inventory must be initialized and its record count must match
the rendered rows. Hidden designs cannot supply another design's stock. Missing,
empty, partially rendered, or unreadable stock stops for review. A verified table
containing only current-destination records needs no Reorder.

- Red records identify stock assigned to a former subcontractor. Ordered status
  alone does not prove stock is available at the current production destination.
- The exact **M6 Print and Ship to Philly (PA)** banner, including its spelling
  without `(PA)`, is in-house at Commerce Way. Other subcontractors are rejected.
- A design with both red and current-destination records requires quantity review.
  A matching confirmed SanMar receipt permits recovery of that run's new CRM row.
- Unknown destinations, unsupported suppliers, and incomplete product data stop
  for review. All affected designs are scanned before supplier access.
- SanMar products are combined by product/color and size across affected designs.
  The **seven-digit CRM order number is the Customer PO**, including pickup and
  shipped purchases. Reorder does not add `ADD-`.

## Ordering and dates

SanMar reuses Shipping Bypasser's style/color mappings, quantity/cart validation,
closed-warehouse checks, delivery estimates, and review-page fulfillment checks.
Exact required stock is sufficient, including ten pieces or fewer; every size
must still be fully available at allocation and again before checkout. Ordinary
Shipping Bypasser retains its existing stock buffer.

Searches use `https://www.sanmar.com/search?text=<style>` and verify the resulting
product. An existing verified inventory view can be reused. NJ-only stock uses
Robbinsville pickup. NJ and shipped stock remain separate purchases when needed.
All carts and delivery dates are preflighted before the first purchase.
Pre-existing cart contents stop the run and are retained. Only carts prepared
and verified by this run are emptied after preflight.

Production moves to the latest arrival date, rounded forward to a business day,
when later than its current date. Due date moves out as needed to allow the
following business day after production. Existing later dates are preserved,
regardless of shipping charges. Date saves are reloaded and verified before
purchase. Changed stock, dates, or warehouse allocations stop checkout for review.

## Receipts and recovery

Atomic receipts under ignored `runtime/state/stock_reorders/` retain the red source
records, design tabs, products/quantities, numeric customer PO, saved allocations,
and each purchase's confirmation. These are separate from Shipping Bypasser's
receipts. Submission is reserved before the final supplier click. An interrupted
or unknown confirmation blocks automatic resubmission, including after restart.
Check supplier history before resolving uncertainty; never delete its receipt.

Confirmed SanMar purchases can repair missing CRM records or notes without buying
again. The worker records the numeric PO through Manual Order/Add Box, reloads the
same design, and verifies a current-destination record. Existing numeric source
POs stop before another purchase because CRM rejects duplicate Manual Order POs.
An explicitly approved CRM PO suffix saved as `crm_po` remains bound to the same
order number; the original supplier PO and confirmation are preserved.

Production notes retain supplier references, warehouse allocations, and
product/color/size quantities. The writer waits for CRM's append-only Production
Notes field, preserves existing history, and verifies the note after save/reload.
Unsaved draft text does not count as a persisted note. Recovery verifies source
stock, products, and destinations before making changes.

## Offline checks

```powershell
python -m unittest discover -s tests -p test_crm_reorder.py
python -m unittest discover -s tests -p test_reorder_batch.py
node --test tests/test_reorder_ui.mjs tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
```

`tests/fixtures/reorder_stock.html` contains sanitized CRM markup. Set
`SANMAR_FIXTURE_CHROMEDRIVER` to an installed local driver and optionally
`SANMAR_FIXTURE_CHROME` to Chrome, then run
`python -m unittest discover -s tests -p test_reorder_browser.py`. The browser uses
an isolated ignored profile and local fixtures with external host resolution
disabled. Its Windows no-sandbox flag is limited to the offline test profile.
Live SanMar purchasing and macOS behavior were not reverified for this removal.
