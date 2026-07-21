# CRM Order Dark Mode

Private Manifest V3 extension for the Automation project's CRM order pages. It is visual-only: it does not read or submit CRM data, call the Automation server, or run on non-order CRM views.

## Install locally

1. Open the dedicated CRM Chrome profile. From the Automation app, use the CRM Chrome-profile setup action if needed.
2. Visit `chrome://extensions` in that profile and enable **Developer mode**.
3. Choose **Load unpacked** and select this `crm-order-dark-mode-extension` folder.
4. Pin **CRM Order Dark Mode**, open a CRM order, and use **Enable dark mode** in its toolbar popup.

The preference is stored locally in that Chrome profile. Removing or reloading the extension keeps the preference unless Chrome extension data is cleared.

## Supported pages

- `https://crm2.legacy.printfly.com/order/<id>`
- The same-origin embedded order app at `https://crm2.legacy.printfly.com/app#/order/<id>`

The manifest injects across the CRM host only to reach the embedded order frame; `content.js` refuses to activate anywhere except these order routes. Styling exists only in screen media, so CRM printing stays light.

## Future app bridge

`bridge.js` reserves `http://127.0.0.1:5123` as the only future Automation app endpoint. Version 1 has no server host permission and makes no network request. A later controls release must add a purpose-built paired authentication flow; it must not reuse app PINs, browser cookies, or CRM credentials.
