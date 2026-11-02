# Reorder Stock

Reorder is a dedicated Custom Processing action for explicit order IDs and individual
CRM order links. It accepts the same validated input, deduplication, input order,
and 100-order limit as Custom Processing. CRM report/list URLs are rejected.

Select **Order IDs or order links** in the Custom Processing popup, paste the
orders, and select **Reorder Stock** at the bottom left. Each order gets a local
serialized queue entry with its own result and Retry control. Other processing
tools and their saved selections are independent of this action. Queue restoration
retains the exact order ID and never switches to a configured report.

## Eligibility and supplier choice

The worker inspects the stock records on each design, using CRM's
`inventoryOrder.assignedToCurrentSubcontractor` boolean. An evaluated native inline
red/yellow style is a fallback for legacy views. Computed colors, including dark
mode overrides, are not used as the source of truth.

- Red means the purchase was assigned to the former subcontractor. Ordered status
  flags alone do not mean stock is available at the current production destination.
- The exact **M6 Print and Ship to Philly (PA)** banner, and its assignment-panel
  spelling without `(PA)`, are in-house at Commerce Way. Other subcontractor orders
  cannot enter Reorder.
- A design with both red and current-destination records stops for quantity review.
  A matching confirmed Reorder receipt permits recovery of that run's new CRM row.
- A design with red records from multiple suppliers, an unknown destination, or
  incomplete product data also stops for review.
- The red record's supplier selects SanMar or S&S; there is no supplier substitution.
  All affected designs for one supplier are combined by product/color and size.
- Both suppliers receive the **seven-digit CRM order number as Customer PO**, even
  when the old red PO used a customer name or a subcontractor suffix. SanMar pickup
  and shipped purchases use this same numeric PO; Reorder does not add `ADD-`.

## Ordering

SanMar reuses Shipping Bypasser's style/color mapping, quantity and cart checks,
closed-warehouse checks, ten-piece stock buffer, delivery estimates, and review-page
fulfillment checks. Searches use `https://www.sanmar.com/search?text=<style>` and
still verify the resulting product. An existing, verified inventory view can be
reused. Complete NJ-only stock is ordered manually for Robbinsville pickup.
NJ and shipped stock are prepared as separate purchases when a split requires NJ.

S&S ordering is used only by Reorder. It searches `https://www.ssactivewear.com/ps/<style>`,
including supported style conversions such as G500 to 5000, and requires a matching
brand, style, and sufficiently close description. Ambiguous results stop rather
than selecting another brand with the same style number. Color matching is exact
after punctuation/case and grey/gray normalization.

S&S prefers Reading, then one other warehouse that can fill all products/sizes,
ordered by delivery date. Warehouse splits are a last resort. Every size must be
available; partial quantities and product substitutions are not purchased. The
cart must match product identity, color, size, piece count, and warehouse allocation.
Grouped warehouse quantities are read from the warehouse-breakdown popup and
checked without applying changes. Unreadable allocations require review.

S&S checkout verifies the shipping-address section is **2727 Commerce Way,
Philadelphia, PA 19154**, every warehouse uses **UPS Ground**, payment is **Net 60**,
and PO Number is exactly the CRM order number. It uses Complete Order and the
final Yes confirmation once, then requires a supplier success page, a unique
numeric order reference, and the exact labelled PO. The supplied archives cover
search/product/cart/checkout and CRM stock records; an actual S&S success page has
not yet been supplied. Its final receipt parser has offline coverage but still
needs comparison with an existing successful S&S vendor confirmation before live
acceptance. No live purchase was made during development.

All supplier carts and delivery dates are preflighted before the first purchase.
Existing vendor cart contents stop the run and are retained. Only verified carts
created by this run are emptied after preflight. For free shipping, production can
move to the stock arrival date and due date moves out when needed to retain a
production day. Paid/rush stock arriving on or after the required production
deadline stops for review. Important CRM date saves are reloaded and verified.

## Credentials and setup

On Windows, use **Settings > Connections > S&S Activewear > Set credentials**, or:

```powershell
.\.venv\Scripts\python.exe manage_credentials.py set ssactivewear
```

Enter the S&S email/username and password in that local terminal. The password is
hidden. The Generic Credential target is `WorkAutomation/SSActivewear`; the same
command uses Keychain on macOS. Credentials are not stored in config or browser
code and are not returned by HTTP endpoints.

**Open profile setup** opens the dedicated S&S profile and fills saved credentials
without submitting. Complete login there if needed. The non-secret default is
`SSACTIVEWEAR_PROFILE_DIR = "chrome_profile_ssactivewear"`.

Restart the local app and refresh the dashboard after updating. No extension reload
is needed. Windows secure-prompt launch was checked during development; macOS's
interactive terminal and live S&S login remain unverified on this machine.

## Receipts and recovery

Atomic receipts are stored under ignored `runtime/state/stock_reorders/`, separately
from ordinary Shipping Bypasser receipts. They identify the red source records,
design tabs, products and quantities, numeric customer PO, saved allocations, and
each purchase's confirmation.

The receipt reserves a submission before the final supplier click. An interruption
or unknown confirmation blocks automatic resubmission, including after a restart.
Review supplier history before resolving this state; do not delete the receipt to
retry. Changed source stock/products/destinations also stop recovery.

Confirmed purchases are retained if a later supplier or CRM step fails. Retry can
resume an unsubmitted saved purchase or repair CRM without repeating confirmed
purchases. The worker records the numeric PO with the correct supplier through
Manual Order/Add Box, reloads the same design, and verifies a current-destination
record. Production notes retain supplier references, warehouse allocations, and
product/color/size quantities; they must persist after reload.

## Offline checks

```powershell
python -m unittest discover -s tests -p test_crm_reorder.py
node --test tests/test_reorder_ui.mjs
```

`tests/fixtures/reorder_stock.html` contains sanitized markup modeled on the supplied
archives. Set `SANMAR_FIXTURE_CHROMEDRIVER` to an installed local driver, then run
`python -m unittest discover -s tests -p test_reorder_browser.py`. The browser uses
an isolated ignored profile and local files, with external host resolution disabled.
Its Windows no-sandbox flag is limited to that offline test profile; production
browser configuration is unchanged.
