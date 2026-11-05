# Reorder Stock

Reorder is a dedicated Custom Processing action for explicit order IDs and individual
CRM order links. It accepts the same validated input, deduplication, input order,
and 100-order limit as Custom Processing. CRM report/list URLs are rejected.

Select **Order IDs or order links** in the Custom Processing popup, paste the
orders, and select **Reorder Stock** at the bottom left. The entire input becomes
one local queue entry. **View orders** lists every order in input order, with
Waiting, Running, Success, Failed, or Not run status and the exact error when a
worker fails. Confirmed purchases include clickable SanMar/S&S confirmation links,
including a purchase that succeeded before a later CRM save failed. Orders with
no red stock report that result without inventing a purchase link.

Orders run serially under the existing CRM lock; a failed order does not prevent
the remaining orders from being checked. Results are checkpointed after each order.
**Retry** uses the same entry, skips successful orders, and retries only unfinished
orders with their original IDs and durable purchase receipts. Cancellation stops
the active worker and prevents starting another order. A server restart leaves
interrupted work stopped for review. Old single-order queue entries remain usable;
new batches never switch to a configured report. Other processing tools and their
saved selections are independent of this action.

## Eligibility and supplier choice

The worker inspects the stock records on each design, using CRM's
`inventoryOrder.assignedToCurrentSubcontractor` boolean. An evaluated native inline
red/yellow style is a fallback for legacy views. Computed colors, including dark
mode overrides, are not used as the source of truth.

The worker waits up to 20 seconds for the selected design's vendor PO table and
requires two stable reads. Live Angular inventory must be initialized and its
record count must match the rendered rows. Hidden designs cannot supply another
design's stock. Missing, empty, partially rendered, or unreadable stock stops for
review instead of reporting **No red vendor POs require reordering**. A verified
table containing only current-destination records still needs no Reorder.
This worker fix loads on the next queued run; no app restart or extension reload
is needed. Queue an order again if an earlier completed entry falsely reported
that no Reorder was needed.

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
closed-warehouse checks, delivery estimates, and review-page fulfillment checks.
Reorder uses no extra stock buffer: exact required quantities are sufficient,
including stock of ten pieces or fewer. Every size must still be fully available
at allocation and again before checkout. Ordinary Shipping Bypasser keeps its
existing ten-piece buffer. Searches use `https://www.sanmar.com/search?text=<style>` and
still verify the resulting product. An existing, verified inventory view can be
reused. Complete NJ-only stock is ordered manually for Robbinsville pickup.
NJ and shipped stock are prepared as separate purchases when a split requires NJ.

S&S ordering is used only by Reorder. It searches `https://www.ssactivewear.com/ps/<style>`,
including supported style conversions such as G500 to 5000, and requires a matching
brand, style, and sufficiently close description. When CRM identifies the brand as
Comfort Colors, both its `C1717` prefix format and numeric `1467CC` suffix format
map to S&S's `1717` and `1467` styles. The numeric `CC` suffix conversion requires
the Comfort Colors brand; the supplier's exact style, brand, and description must
still match. CRM product IDs and receipt source signatures retain their original
values. This worker fix loads on the next queued run without an app restart or
extension reload. Use **Retry** on the failed Reorder entry.
CORE365 is recognized, including
**CE10 Unisex Capital Performance T-Shirt**; exact style and description checks
still reject another brand or CE10L. Search reads the visible product card's
labelled style, brand, and description, including accessibility layouts whose
main links have no layout box. Separate color-count links are
excluded. Ambiguous results stop rather than selecting another brand with the
same style number.
Version 1.7.15 returns the search card's labelled brand, style, and description.
For brands outside the legacy alias list, such as Richardson, the labelled brand
must appear in CRM's description and the supplier title, match the URL's brand,
and have the exact requested style in both the card and URL. Description and
youth/adult checks still apply; multiple possible brands stop for review. A bare
style number or an unlabelled title alone cannot establish a new brand.
Richardson **112 only** uses the approved exact color aliases
`Khaki/ Khaki/ Navy` → `Khaki/ Navy CMB` and `Caramel/ Black` → `Carmel/ Black`.
Its CRM one-size/OSFM label maps to the supplier's standard `M/L` fit; `XL` stays
distinct. S&S documents this size-label equivalence on its
[112 product page](https://www.ssactivewear.com/p/richardson/112).
The saved plan retains the original CRM product alongside supplier labels; the
CRM source signature stays unchanged. Color aliases are not inferred by dropping
repeated color names or `CMB`, and do not apply to another brand or style.
Color matching is exact
after punctuation/case and grey/gray normalization.
The extension waits for the initial color and inventory to load before selecting
another color. It invokes only the matched swatch's known S&S color handler from
the page context and verifies the requested color and every inventory color ID.
Warehouse estimates must finish loading before allocation. A changed handler or
missing estimate stops for review before quantities are entered.
Both the older delivery-label field and the warehouse shipping-text layout are
supported; dates are read from the selected product's own warehouse rows.
Inventory and quantity entry read sizes from cell labels or the containing
product grid's size header. Missing, duplicate, conflicting, or mismatched
columns stop before any quantity is changed.
Stock addition and removal invoke only their known page-context controls, then
verify the cart change. Cleanup compares stock identity fields independently of
JSON key order and stops if any current or remaining stock differs.
Version 1.7.14 also recognizes S&S's unlabelled `removeCartItem` link, verifies
its product/warehouse identifiers against the exact cart row, and ignores
removed-item notices when selecting the next row. Competing or changed controls
still stop before cleanup. Reload the extension before retrying an earlier
"cart removal control is ambiguous" failure; its retained cart needs ownership
review before removal, because normal Reorder preserves pre-existing stock.
After addition, cart reads wait for the expected piece count and complete rows.
Visible "was removed" notices without stock controls are not active stock;
unreadable product rows stop for review. Checkout may use a reduced header
without the normal sign-out link, so its authenticated account controls are
recognized separately and address/payment/PO checks still apply. If navigation
closes the old page's response channel, the worker reads the destination without
clicking Checkout again. A new sign-in request requires the operator to sign in
normally before retrying.

S&S prefers Reading, then one other warehouse that can fill all products/sizes,
ordered by delivery date. Warehouse splits are a last resort. Every size must be
available; partial quantities and product substitutions are not purchased. The
cart must match product identity, color, size, piece count, and warehouse allocation.
If stock cannot cover the order, the error identifies the product, color, size,
required quantity, and available quantity across warehouses with verified
delivery dates. A successful brand/color match cannot override a stock shortage.
Grouped warehouse quantities are read from the warehouse-breakdown popup and
checked without applying changes. Unreadable allocations require review.

S&S checkout verifies the shipping-address section is **2727 Commerce Way,
Philadelphia, PA 19154**, every warehouse uses **UPS Ground**, payment is **Net 60**,
and PO Number is exactly the CRM order number. It uses Complete Order and the
final confirmation once (shown as Confirm in the observed live Chrome session and
Yes in the supplied archive). Selection is restricted to one visible Confirm/Yes
control in the current message-box dialog or older named confirmation popup,
including message boxes without an ARIA role, requiring the exact
order-submission prompt; hidden legacy controls are ignored
and nested button inputs count as one action. Competing controls stop before a
click. Both Complete Order and Confirm execute once in the page context after
rechecking the session PO, shipping address, UPS Ground, and Net 60; follow-up
messages only verify the result. Confirmation then requires a supplier success page, a unique
order reference, and the exact labelled PO. Receipt parsing covers the observed
`Order Confirmation: <number>` heading, including combined PO/warehouse labels,
as well as the older numbered confirmation format. A live S&S checkout and its
confirmation were verified through an existing logged-in Chrome session.
S&S Reorder now uses the opted-in **CRM Order Assistant 1.7.15** extension in a
normally opened Chrome profile. The local worker sends stock instructions to
one dedicated S&S tab without launching WebDriver, activating the tab, or using
the computer's mouse/keyboard. At the start of each order, it automatically opens
or reuses its own unfocused minimized window and checks the signed-in cart before
entering stock. The manual read-only browser check is optional. The worker also
checks the actual running extension version (1.7.15 or newer), so a cached older
background controller requires reloading before checkout can run. Earlier signed-in
checks and the product workflow have
passed live in both background-tab and minimized-window modes on Windows Chrome.
The local Python worker matched G500 to Gildan 5000, selected Black, read stock
and delivery dates from 11 warehouses, then returned to the unchanged empty cart.
Both cart and product pages stayed hidden; the minimized run also verified that
the window remained minimized. Those diagnostics prohibited quantity entry,
checkout, purchase, and CRM changes. A later live Reorder verified quantity
entry, exact cart contents, checkout, and supplier purchase confirmation in a
minimized Windows Chrome window. CRM production/due dates and the purchase note
were reloaded and verified.
Version 1.7.14 was also verified live on Windows with automatic minimized setup,
cleanup of a multi-size preflight cart, and exactly one Complete Order/Confirm
sequence followed by the matching supplier order confirmation. macOS remains
unverified.

A November 2, 2026 comparison used the installed Chrome 156 and Firefox 159:

- A native-first Chrome control loaded the homepage, sign-in form, and empty
  cart normally. Standard Selenium and the app's unchanged Chrome worker
  settings then loaded those same pages with HTTP 200 using that exact profile.
- The original Chrome worker profile still returned HTTP 403 at sign-in/cart.
  The initially successful control later returned HTTP 403 before credentials
  were read; reopening it without Selenium also showed the supplier error.
- Firefox under Selenium loaded the homepage but returned HTTP 403 at
  sign-in/cart. After reopening that exact profile without Selenium, the
  operator confirmed both sign-in and cart loaded normally. This supports an
  automation-session factor in Firefox, though time-dependent server behavior
  remains a possible confound. Computer Use cannot independently inspect
  Firefox here, so the native results are operator-confirmed, not HTTP readings.
- Standard Playwright with installed Chrome also returned HTTP 403 using the
  already-affected control profile. This did not establish its behavior in an
  unaffected session and does not prove a framework migration would help.

These earlier WebDriver comparisons rule out a universal Chrome/Firefox incompatibility or an
unconditional Selenium rejection. They do not reveal the supplier's exact access
rule or establish a reliable profile-initialization workaround. No production
browser/profile migration was made during those comparisons. Firefox/Playwright
are not enabled for Reorder. The comparison did not add cart stock, purchase
orders, or edit CRM.

CRM rejects a reused manual stock PO even if its older row is red. An existing
numeric order PO on source stock now stops before supplier access or purchase.
Review the earlier supplier purchase first. Confirmed purchases are preserved
when a CRM collision needs an operator-approved stock-record suffix; this never
changes the supplier PO or authorizes another purchase.

All supplier carts and delivery dates are preflighted before the first purchase.
Existing vendor cart contents stop the run and are retained. Only verified carts
created by this run are emptied after preflight. Every Reorder follows the same
date rules regardless of shipping charges: production moves to the latest stock
arrival date, rounded forward to a business day, when later than its current date.
The due date moves out when needed to allow the following business day after
production. Existing later dates are preserved. Important CRM date saves are
reloaded and verified before purchase; date changes during checkout still stop
for review. These Reorder rules load on the next worker run without an app restart.
Due-date verification also reopens the same CRM order when a refresh loses its
iframe context, using the existing production-date verification recovery.

## S&S browser connection and setup

1. Restart the local Automation app and refresh the dashboard when no task is
   running. Do not start a second server.
2. Install/reload **CRM Order Assistant 1.7.15** from
   `crm-order-dark-mode-extension` at `chrome://extensions` in the normal Chrome
   profile you want to use. Existing CRM page scripts stay scoped to CRM.
3. Open the extension popup and select **Connect this browser** under **S&S
   Reorder**. Grant its optional S&S access.
4. Select **Open S&S sign-in** and sign in normally. Complete any site
   verification yourself. Credentials and the Automation PIN never pass through
   the extension or local bridge.
5. Queue explicit orders with **Reorder Stock**. The extension automatically
   creates a minimized S&S window, or reuses its existing minimized worker window,
   and checks sign-in. Keep Chrome running, keep the worker tab open during the
   batch, and leave the S&S account cart alone. Other Chrome tabs/windows and
   applications remain usable. The connection stays enabled between orders and
   batches, and automatically reconnects after the local app becomes available.
6. Optionally use **Run read-only check** with **Background tab** or **Minimized
   window** for diagnostics. It reads homepage/login/cart and 5000 product search
   without submitting login, changing stock, or buying. This is no longer a
   prerequisite for Reorder.

Connect **exactly one** browser profile on the dashboard's host computer.
Multiple active connections stop selection instead of choosing an account.
If a browser closed without disconnecting, its connection can remain visible
for up to 75 seconds. **Disconnect this browser** immediately stops future
command delivery. Remote dashboard access still runs work on that dashboard's
host, using its local extension; there is no cross-machine browser execution.

Normal worker operations do not activate tabs or focus/restore windows. Only the
worker's own window is minimized; a background diagnostic tab in the user's main
window is left alone. The operator's **Open S&S sign-in** button explicitly focuses
the sign-in page. After Chrome restarts, the next order can create a fresh minimized
window without another manual check; stored numeric tab IDs never adopt a user tab.
A tab closed between orders can also be recreated at the next order's initial
setup. A closed, repurposed, frozen, or discarded tab during an active order stops
for review. Setup is claimed once and cannot recreate a window midway through an
order. Closing Chrome or putting the computer to sleep prevents continued operation;
Reorder does not launch a closed browser or submit credentials. If sign-in expired,
sign in normally through the popup, then Retry the original batch.

The bridge retains the existing loopback/extension-origin restriction. Its
endpoints register the opted-in browser, deliver worker-created commands, and
accept results; they cannot create purchases or queue work. Reorder creates a
leased session bound to one browser and order number. Commands are claimed once
and never replayed after a service-worker crash. The existing durable receipt
still reserves submission before a purchase click. If the final-click response
is lost, the worker may only read confirmation, never click again. Uncertain
outcomes stop for review.

The old `WorkAutomation/SSActivewear` OS credential and
`SSACTIVEWEAR_PROFILE_DIR` setting remain for compatibility/diagnostics;
extension Reorder does not read them. **Settings > Connections > S&S Activewear**
now explains extension setup instead of launching a Selenium browser.

Reorder selects **Accept All Cookies** on S&S's OneTrust banner when it is visible
and waits for the banner to close. It checks again while waiting for page controls
and before login, covering banners that appear after the initial page load. The
site stores consent in the connected browser profile. The handler is restricted
to the S&S HTTPS origin and the banner's own Accept control.

The legacy Selenium helper targets S&S account login fields explicitly, excluding the footer's
newsletter email field. The site's accessibility widget can redirect initial
field focus. Login refocuses at most once and checks focus before typing each
credential, then checks both values before submitting. Missing, ambiguous,
unfocused, or changed fields stop the login. The behavior is covered by an
isolated browser fixture; live credential login remains unverified because the
site returned its error before the corrected login flow could complete.

If S&S shows **Looks like we're having some problems**, Reorder stops with the
site's Ray ID instead of waiting for missing login or checkout controls. Review
site access in the S&S profile before retrying. An error
after a purchase click still requires reviewing supplier history and the saved
receipt before another submission. A server response of HTTP 403 before the site
loads still requires resolving access; cookie-banner handling does not suppress
that error.

Restart the app, reload the extension, and refresh CRM pages/dashboard after
updating. Each Chrome profile opts in separately. macOS behavior remains
unverified until checked on that setup.

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
product/color/size quantities; they must persist after reload. An explicitly
approved CRM stock PO alias is bound to the same order number and saved as
`crm_po` in the local confirmed receipt. Recovery verifies that alias and adds a
note linking it to the original supplier PO/reference. The supplier customer PO
and confirmed purchase are retained. These worker-only checks load on the next
worker run. Automatic minimized setup requires reloading extension 1.7.15.

The production-note writer waits for CRM's append-only Production Notes field in
its dedicated section. It preserves existing note history and checks that history
before appending again. Verification reopens the same order if a refresh loses
the CRM iframe context; unsaved draft text does not count as a persisted note.
These worker changes load on the next run without an app restart.

## Offline checks

```powershell
python -m unittest discover -s tests -p test_crm_reorder.py
node --test tests/test_reorder_ui.mjs
python -m unittest discover -s tests -p test_reorder_batch.py
python -m unittest discover -s tests -p test_ssactivewear_bridge.py
node --test tests/test_ssactivewear_extension.mjs tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
```

`tests/fixtures/reorder_stock.html` contains sanitized markup modeled on the supplied
archives. Set `SANMAR_FIXTURE_CHROMEDRIVER` to an installed local driver, then run
`python -m unittest discover -s tests -p test_reorder_browser.py`. The browser uses
an isolated ignored profile and local files, with external host resolution disabled.
Its Windows no-sandbox flag is limited to that offline test profile; production
browser configuration is unchanged.
