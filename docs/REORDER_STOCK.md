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
brand, style, and sufficiently close description. Search reads the visible
product card's labelled style, brand, and description, including accessibility
layouts whose main links have no layout box. Separate color-count links are
excluded. Ambiguous results stop rather than selecting another brand with the
same style number. Color matching is exact
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
Grouped warehouse quantities are read from the warehouse-breakdown popup and
checked without applying changes. Unreadable allocations require review.

S&S checkout verifies the shipping-address section is **2727 Commerce Way,
Philadelphia, PA 19154**, every warehouse uses **UPS Ground**, payment is **Net 60**,
and PO Number is exactly the CRM order number. It uses Complete Order and the
final confirmation once (shown as Confirm in the observed live Chrome session and
Yes in the supplied archive), then requires a supplier success page, a unique
order reference, and the exact labelled PO. Receipt parsing covers the observed
`Order Confirmation: <number>` heading, including combined PO/warehouse labels,
as well as the older numbered confirmation format. A live S&S checkout and its
confirmation were verified through an existing logged-in Chrome session.
S&S Reorder now uses the opted-in **CRM Order Assistant 1.7.5** extension in a
normally opened Chrome profile. The local worker sends stock instructions to
one dedicated S&S tab without launching WebDriver, activating the tab, or using
the computer's mouse/keyboard. A successful signed-in background/minimized
browser check is required first. Signed-in checks and the product workflow have
passed live in both background-tab and minimized-window modes on Windows Chrome.
The local Python worker matched G500 to Gildan 5000, selected Black, read stock
and delivery dates from 11 warehouses, then returned to the unchanged empty cart.
Both cart and product pages stayed hidden; the minimized run also verified that
the window remained minimized. Those diagnostics prohibited quantity entry,
checkout, purchase, and CRM changes. Live extension cart/checkout ordering remains
unverified until tested with a new eligible order.

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
2. Install/reload **CRM Order Assistant 1.7.5** from
   `crm-order-dark-mode-extension` at `chrome://extensions` in the normal Chrome
   profile you want to use. Existing CRM page scripts stay scoped to CRM.
3. Open the extension popup and select **Connect this browser** under **S&S
   Reorder**. Grant its optional S&S access.
4. Select **Open S&S sign-in** and sign in normally. Complete any site
   verification yourself. Credentials and the Automation PIN never pass through
   the extension or local bridge.
5. Reopen the popup, select **Background tab** or **Minimized window**, then
   **Run read-only check**. It reads S&S homepage/login/cart and 5000 product
   search without submitting login, changing stock, or buying. A successful
   **signed-in** result is required before Reorder can use that browser.
6. Queue explicit orders with **Reorder Stock** as before. Keep its dedicated
   S&S tab open and leave the S&S account cart alone. Other Chrome tabs/windows
   and applications remain usable.

Connect **exactly one** browser profile on the dashboard's host computer.
Multiple active connections stop selection instead of choosing an account.
If a browser closed without disconnecting, its connection can remain visible
for up to 75 seconds. **Disconnect this browser** immediately stops future
command delivery. Remote dashboard access still runs work on that dashboard's
host, using its local extension; there is no cross-machine browser execution.

Normal worker operations do not activate tabs or focus/restore windows. The
operator's **Open S&S sign-in** button is an explicit exception. The minimized
check opens its own unfocused minimized window. A frozen/discarded tab, a closed
or repurposed worker tab, or a browser restart stops for review. After restarting
Chrome, rerun the check; stored tab IDs never adopt another user tab. Closing
Chrome or putting the computer to sleep prevents continued operation.

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
updating. Each Chrome profile opts in separately. Live cart/checkout ordering and
macOS behavior remain unverified until checked on that setup.

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

The production-note writer waits for CRM's append-only Production Notes field in
its dedicated section. It preserves existing note history and checks that history
before appending again. Verification reopens the same order if a refresh loses
the CRM iframe context; unsaved draft text does not count as a persisted note.
These worker changes load on the next run without an app restart.

## Offline checks

```powershell
python -m unittest discover -s tests -p test_crm_reorder.py
node --test tests/test_reorder_ui.mjs
python -m unittest discover -s tests -p test_ssactivewear_bridge.py
node --test tests/test_ssactivewear_extension.mjs tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
```

`tests/fixtures/reorder_stock.html` contains sanitized markup modeled on the supplied
archives. Set `SANMAR_FIXTURE_CHROMEDRIVER` to an installed local driver, then run
`python -m unittest discover -s tests -p test_reorder_browser.py`. The browser uses
an isolated ignored profile and local files, with external host resolution disabled.
Its Windows no-sandbox flag is limited to that offline test profile; production
browser configuration is unchanged.
