# CRM Order Assistant

Private Manifest V3 extension for the Automation project's CRM pages. It provides local order-page dark mode, queued single-order tools, list selection and rush highlighting, and reliable Salesforce tab reuse. List controls select orders and open tabs; queued tools remain scoped to individual order pages.

## Install locally

1. Open the dedicated CRM Chrome profile. From the Automation app, use the CRM Chrome-profile setup action if needed.
2. Visit `chrome://extensions` in that profile and enable **Developer mode**.
3. Choose **Load unpacked** and select this `crm-order-dark-mode-extension` folder.
4. Pin **CRM Order Assistant**, open a CRM order, and use **Enable dark mode** in its toolbar popup. The order page also provides single-order Manual Process, Cancel, Reachout, and Stock Issue controls.

The preference is stored locally in that Chrome profile. Removing or reloading the extension keeps the preference unless Chrome extension data is cleared.

After extension updates, reload the extension at `chrome://extensions` and refresh open CRM order and list pages. Updates that also change the local Automation app require an app restart as noted below.

Version **1.6.2** removes **Auto-Process** from order pages and **Process current order** from the toolbar popup. Manual Process, Cancel, Reachout, and Stock Issue remain available on order pages, with queue status and completion refreshes. Reload CRM Order Assistant at `chrome://extensions` and refresh open CRM order pages to load this removal; no local Automation app restart is needed.

## S&S Reorder browser — 1.7.7

S&S Reorder can use a normally opened Chrome profile through the extension,
without launching WebDriver. Install/reload version **1.7.7** in that profile,
restart the local Automation app, and refresh CRM pages/dashboard.

1. Open the popup and select **Connect this browser** under **S&S Reorder**.
   Grant optional S&S access and scripting permission. Other websites receive
   no new page scripts.
2. Select **Open S&S sign-in** and sign in normally. Credentials and the app PIN
   never pass through this connection.
3. Reopen the popup, select **Background tab** or **Minimized window**, then
   **Run read-only check**. This reads homepage/login/cart and 5000 search;
   it does not add stock or submit an order. A successful signed-in result is
   required before this browser can handle Reorder.
4. Use the dashboard's **Reorder Stock** action. Keep its dedicated S&S tab open
   and leave the S&S cart alone. You can use other Chrome tabs/windows or apps;
   worker operations do not focus/restore the tab or window.

Connect only one profile on the local dashboard's computer. A browser check or
manual sign-in cannot interrupt an active Reorder. A closed, changed, discarded,
or frozen tab stops for review; another user tab is never adopted. After a
browser restart, rerun the check. Last status persists if you close the popup.

The server's existing receipt reserves a purchase before submission. Commands
are claimed once, never replayed after an extension restart. A lost response
permits reading supplier confirmation without another click. Uncertain
submissions require review, and pre-existing cart stock is retained.

Signed-in browser checks and product operations have passed live in both
background tabs and minimized windows on Windows Chrome.
Search reads cards' style, brand, and title even when accessibility layout gives the
main product link no layout box; color-count links cannot replace descriptions.
The check explicitly minimizes only its newly created check window and
reports the window state when a visibility check fails. Health diagnostics are
limited to product colors, warehouse shipping labels, and known control identities;
they contain no credentials.
Color selection waits for product initialization, invokes only the matched
swatch's known selector in the page context, then verifies color, inventory color
IDs, and loaded warehouse dates. No arbitrary script or function name is accepted.
Stock addition and removal use fixed page-context controls. Addition verifies
the entered quantity and cart-count change; cleanup requires the same exact cart
before the click and verifies every remaining row afterward.
Cart reads wait for the piece count and loaded rows to agree. Removed-item
notices are excluded; unreadable stock rows still stop the run. Checkout's
authenticated reduced header is recognized by its account checkout fields.
A navigation-closed response triggers only destination checks, never another
Checkout click. A checkout sign-in redirect requires normal manual sign-in.
The final action selects one visible Confirm/Yes control inside the named order
confirmation popup; hidden legacy controls and nested button inputs cannot
create duplicate targets. Missing or competing controls stop without clicking.
Complete Order and Confirm use fixed page-context clicks, with the exact session
PO, Commerce Way address, UPS Ground, and Net 60 rechecked before either action.
Each click is dispatched once; verification messages never click again.
The live check matched Gildan 5000, selected Black, read stock and delivery dates
from 11 warehouses, and returned to the same empty cart. No quantity entry,
checkout, purchase, or CRM edit ran in that check. Live cart entry and extension
checkout still need verification with a new eligible order. Reload the extension
and rerun its check after updating code. Offline tests cover page
operations, local origin restrictions, duplicate-delivery prevention, and tab
ownership. See the [Reorder guide](../docs/REORDER_STOCK.md).

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

## Friday and weekend list highlighting

Version **1.6.4** extends the Friday rule and requires both **Production Date**
and **Fulfillment Date** (also spelled **Fulfilment Date**) or **Due Date**:

| Viewing day | Production Date | Fulfillment / Due Date |
| --- | --- | --- |
| Friday | That Friday | Immediately following Monday |
| Saturday or Sunday | Immediately following Monday | Immediately following Tuesday |

Matching orders display the CRM's neon green (`rgb(34, 236, 72)`). On weekends,
this treats Monday as the working day. Production dates in report group headers
are supported. The rule uses the computer's local calendar without a holiday
calendar. Monday through Thursday retain CRM's own urgency behavior.

Highlighting is automatic, independent of the dark-mode setting and local
Automation app. It follows list refreshes, pagination, changed dates, and local
midnight, removing stale extension highlights as the date or list changes.
Unreadable or conflicting dates are left unchanged. Printed pages retain their
original CRM colors and omit both list controls.

Reload **CRM Order Assistant** at `chrome://extensions`, then refresh open CRM
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

Reverse printing clones each selected original as **REVERSE-PRINT**, explicitly selects **Style Sub** and clicks **Apply** for each product, and selects the original vendor from its dropdown. Style and description are copied; two-color names are reversed and single colors retained. Quantities are restored by size label. The clone's unit price is the reverse price multiplied by its front/back area count (front or back: one charge; both: two). The original product prices are preserved. CRM carries over artwork and print methods.

On retry, existing clones are inspected and completed fields are skipped. Ambiguous clone matches, unreadable vendor data, unsupported sizes, or unexpected print areas stop the task for review. CRM changes and sales notes are verified after saving, before the existing Salesforce Additional Requests email is sent with `reverse prints`, the per-area charge, and the invoice link. A local receipt prevents the same reverse-print email being sent again after a successful run; receipts are stored under the app's runtime state directory.

After updating, restart the local Automation app, reload the extension, and refresh CRM order pages.

### Extra EMB Area

Choose **Extra EMB Area** inside **Manual Process → Extra Print Areas** for each desired tab. It uses the shared, editable **Embroidery** price, defaulting to **$15 per additional area per garment**. The existing area is included, so each selected tab receives one surcharge even if both chest areas already exist.

Front is converted to Front-left chest, and Front-right chest is added. If either chest area already exists, it is reused and the missing side is added. Both chest methods are set to Embroidery. Other locations and existing artwork are retained. Unsupported or duplicate chest configurations stop for review.

The sales note is `Additional embroidery area`, `$15.00 each` (or the custom price), and `emailed txted` on separate lines. The existing Additional Requests email uses `additional embroidery area`, the charge, and the invoice link. No text message is sent. Local receipts verify saved changes and prevent duplicate pricing/email on matching retries; an interrupted save with unverified prices stops for review.

## Copyright cancellation by design

Choose **Cancel → Copyright - Cancel** to scan every order design tab and list
its **Design Name**, tab number, and quantity. The scan returns to the previously
open tab. Select the affected designs and enter the required copyright reason.
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

## Local app bridge and order tools

The extension checks `http://127.0.0.1:5123/api/extension/bridge/status` when its popup opens, so it can confirm the local Automation app is running. It never asks for, reads, or stores the Automation app PIN. The control bridge accepts only loopback Chrome-extension requests.

The **Manual Process** menu beside **Send Invoice** lets you choose one automation for the open order: Address Validator, Product Separator, Auto Splitter, Order Goods, Shipping Bypasser, Push Back, or Extra Print Areas. Cancel, Reachout, and Stock Issue provide their own workflow menus. Each request targets only the current order and enters the local Automation queue, waiting for any active task. The order page shows queued, working, and review status beside Manual Process and refreshes after a task started from that page completes.

Selecting **Shipping Bypasser** from **Manual Process** is an explicit approval to use SanMar stock without the normal 10-piece per-size safety buffer. It still stops when SanMar has fewer units than the order requires. List-driven Shipping Bypasser runs retain the 10-piece buffer.

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
size quantities. Multiple swaps use corresponding numbered lists in both
placeholders. Unchanged products and unaffected quantities are excluded.

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
