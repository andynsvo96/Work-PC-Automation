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

Reverse printing clones each selected original as **REVERSE-PRINT**, explicitly selects **Style Sub** and clicks **Apply** for each product, and selects the original vendor from its dropdown. Style and description are copied; two-color names are reversed and single colors retained. Quantities are restored by size label. The clone's unit price is the reverse price multiplied by its front/back area count (front or back: one charge; both: two). The original product prices are preserved. CRM carries over artwork and print methods.

On retry, existing clones are inspected and completed fields are skipped. Ambiguous clone matches, unreadable vendor data, unsupported sizes, or unexpected print areas stop the task for review. CRM changes and sales notes are verified after saving, before the existing Salesforce Additional Requests email is sent with `reverse prints`, the per-area charge, and the invoice link. A local receipt prevents the same reverse-print email being sent again after a successful run; receipts are stored under the app's runtime state directory.

After updating, restart the local Automation app, reload the extension, and refresh CRM order pages.

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
