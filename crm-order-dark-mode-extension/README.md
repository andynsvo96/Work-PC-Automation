# CRM Order Assistant

Private Manifest V3 extension for the Automation project's CRM order pages. It provides local dark mode, a queued single-order processing control, and reliable Salesforce tab reuse. It never processes CRM report lists from the extension.

## Install locally

1. Open the dedicated CRM Chrome profile. From the Automation app, use the CRM Chrome-profile setup action if needed.
2. Visit `chrome://extensions` in that profile and enable **Developer mode**.
3. Choose **Load unpacked** and select this `crm-order-dark-mode-extension` folder.
4. Pin **CRM Order Assistant**, open a CRM order, and use **Enable dark mode** in its toolbar popup. The order page also provides single-order Auto-Process, Manual Process, Cancel, and Reachout controls.

The preference is stored locally in that Chrome profile. Removing or reloading the extension keeps the preference unless Chrome extension data is cleared.

After updating, restart the local Automation app, reload the extension at `chrome://extensions`, and refresh open CRM order pages.

## Extra Print Areas

Under **Manual Process → Extra Print Areas**, select design tabs and choose one category per tab: **Sleeve Prints**, **Reversible Prints**, or **Side Prints**. Sleeves and sides offer **Ink print / Embroidery**, followed by **Left / Right / Both**. Reversible prints are ink only and have no left/right selector.

Ink pricing uses the combined garment quantity of selected tabs with an ink or reverse request, counting each original tab once: $8 for 1–9, $7 for 10–19, $6 for 20–99, and $5 for 100+. Embroidery defaults to $15. Custom sleeve/side ink prices are shared across those ink areas; the separate reverse-price override is shared across every selected reverse tab. Unselected tabs do not affect the tier.

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

The manifest injects across the CRM host only to reach the embedded order frame; `content.js` refuses to activate anywhere except these order routes. Styling exists only in screen media, so CRM printing stays light.

## Local app bridge and processing button

The extension checks `http://127.0.0.1:5123/api/extension/bridge/status` when its popup opens, so it can confirm the local Automation app is running. It never asks for, reads, or stores the Automation app PIN. The control bridge accepts only loopback Chrome-extension requests.

The **Process order** button on an open CRM order sends only that order number to the local app. It is placed in the Automation queue, so it waits for any active task instead of overlapping another automation. It validates the address, separates mixed listed/non-listed products, splits orders with more than 10 tabs, unlocks stock as part of Order Goods, and orders every applicable stock tab. If the visible CRM page or the Order Goods result reports that shipping is too expensive (including a purchase plan exceeding the maximum shipment-cost percentage), it then runs the shipping bypasser. The chain stops for manual review on an address, separation, or split failure; it does not fall back to batch reports.

Selecting **Shipping Bypasser** from **Manual Process** is an explicit approval to use SanMar stock without the normal 10-piece per-size safety buffer. It still stops when SanMar has fewer units than the order requires. List-driven Shipping Bypasser runs and Auto-Process retain the 10-piece buffer.

The worker prefers one warehouse, but checks a closer split before stopping when that complete shipment would arrive too late. The split retains the selected buffer setting and must pass cart validation and the delivery-date check for every warehouse. It uses one customer PO and the usual production note describing the boxes and quantities from each warehouse. This worker update loads on the next queued run; no app restart, extension reload, or page refresh is required.

# Complicated EMB design selection

Reachout > Complicated EMB scans the order design tabs and offers tabs with
readable print methods, including ink and mixed-method designs. Designs already
switched from embroidery to ink printing remain selectable. Select one or more
designs, then answer "Feedback required?" with Yes or No. A single available
design is preselected, including tabs labelled View Proofs after proof creation.
The existing feedback/HDD routing is retained. Selected Design Names replace
`[DESIGN]` in one Salesforce email; the worker rechecks each selection before
processing and stops if its name has changed, the tab is unavailable, or its
print methods cannot be read. A change to another readable print method is allowed.

For an order with one active design tab, the sales note starts with
`Complicated embroidery` and omits the tab number. Orders with multiple tabs keep
the selected tab numbers, even when only one tab is selected. Retrying an order
with the earlier `Tab 1 is Complicated Embroidery` note does not add another note.

The HDD template check accepts the current Salesforce wording
`updated the design, [DESIGN], from embroidery to ink printing instead` as well
as the earlier order-level wording. It still requires the embroidery-detail
explanation and the ink-conversion text before sending.

After updating to 1.5.8, reload CRM Order Assistant at `chrome://extensions`,
then refresh open CRM order pages. The worker changes load with the next queued
task; an Automation app restart is not required for this update.
