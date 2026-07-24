# CRM Order Assistant

Private Manifest V3 extension for the Automation project's CRM order pages. It provides local dark mode and a paired, single-order processing control. It never processes CRM report lists from the extension.

## Install locally

1. Open the dedicated CRM Chrome profile. From the Automation app, use the CRM Chrome-profile setup action if needed.
2. Visit `chrome://extensions` in that profile and enable **Developer mode**.
3. Choose **Load unpacked** and select this `crm-order-dark-mode-extension` folder.
4. Pin **CRM Order Assistant**, open a CRM order, and use **Enable dark mode** in its toolbar popup.

The preference is stored locally in that Chrome profile. Removing or reloading the extension keeps the preference unless Chrome extension data is cleared.

## Supported pages

- `https://crm2.legacy.printfly.com/order/<id>`
- The same-origin embedded order app at `https://crm2.legacy.printfly.com/app#/order/<id>`

The manifest injects across the CRM host only to reach the embedded order frame; `content.js` refuses to activate anywhere except these order routes. Styling exists only in screen media, so CRM printing stays light.

## Local app bridge and processing button

The extension checks `http://127.0.0.1:5123/api/extension/bridge/status` when its popup opens, so it can confirm the local Automation app is running. Before the processing control can be used, enter the local Automation app PIN in the popup and select **Pair**. The app returns a loopback-only token that expires after 12 hours and is scoped to this Chrome extension origin. The PIN is not stored by the extension.

Once paired, the **Process order** button on an open CRM order sends only that order number to the local app. It validates the address, separates mixed listed/non-listed products, splits orders with more than 10 tabs, unlocks stock as part of Order Goods, and orders every applicable stock tab. If the visible CRM page contains `Shipping is too expensive`, it then runs the shipping bypasser. The chain stops for manual review on an address, separation, or split failure; it does not fall back to batch reports.
