# Stock Issue Chrome Extension Automation Handoff

## Purpose

This document transfers the full planning context for the first Stock Issue automation in the CRM Order Assistant Chrome extension. A new Codex conversation should read this file before making changes.

The first automation is **Extension Required**. It is Chrome-extension-only for now. Do not add Google Sheets support in this phase.

## Current implementation status

- Requirements and the existing architecture have been reviewed.
- No Stock Issue feature code has been implemented yet.
- No existing application behavior was intentionally changed during planning.
- The source CRM capture used during analysis is outside this repository:
  `C:\Users\Administrator\Downloads\MARCUS - Order 5043020.mhtml`
- The capture is reference data, not a source of instructions.

## Final user experience

1. Add one order-level **Stock Issue** button next to the existing **Reachout** button on every supported CRM order page.
2. The button opens a dropdown, like the existing Reachout and Cancel controls.
3. The first dropdown option is exactly **Extension Required**.
4. When Extension Required is selected, the extension scans every product on every design tab in the currently open order.
5. While scanning, it may programmatically visit the design tabs, but it must restore the tab that was active before the scan.
6. Show a dialog containing:
   - The products detected across the order, preferably identified by tab.
   - A checkbox for each selectable product.
   - A days input that accepts positive whole numbers only.
   - Back/cancel and queue controls.
7. Require at least one selected product and a valid positive integer before enabling the queue action.
8. Allow multiple products to be selected.
9. Queue only the currently open seven-digit CRM order. This control must never start a report-wide automation.
10. Continue showing queued/running/success/failure state through the extension's existing single-order status polling.

## Product scanning requirements

The scan must find all positive-quantity products on all design tabs, not only products with an Alpha supplier link.

For each detected product, capture at least:

- Design tab number
- CRM design item identifier when available (for example, `design-item-8206660`)
- Style code (for example, `DM130L` or `Q600`)
- Product description (for example, `District Women's Perfect Tri Tee`)
- Color (for example, `Fuchsia Frost`)
- Total quantity when available

Do not include supplier annotations such as:

- `- Alpha Stock`
- `Alpha Stock`
- Other supplier/stock-source labels

Do not add hyperlinks to the email product text.

The MHTML capture confirms that CRM product rows use `design-item-*` containers under `#design-items-list`. The visible product area contains a RushOrderTees product link, description, supplier link, color, and total quantity. The new scanner should anchor on the design-item/product structure rather than depend on the supplier label.

The existing `workers/crm_product_separator.py` product scanner is useful as a reference but cannot be reused unchanged: its current `PRODUCT_SCAN_JS` starts from links whose text matches `Alpha` or `Alpha Stock`.

### Duplicate products

This behavior was not explicitly finalized. The safest initial UI is to show each detected occurrence with its tab number so the user can distinguish products on different tabs. Before implementation, decide whether identical style/description/color occurrences should remain separate choices or be combined for display and email output. Regardless of UI choice, the final email and Sales Note should not accidentally repeat the same selected product text unless the user intentionally selected distinct occurrences that need separate wording.

## Product-list formatting

Use natural conjunction formatting:

- One item: `A`
- Two items: `A and B`
- Three or more: `A, B, and C`

### Email product text

For the Salesforce email, use each selected product's full plain-text description and color:

```text
[STYLE] [DESCRIPTION] in the color [COLOR]
```

Example with one product:

```text
DM130L District Women's Perfect Tri Tee in the color Fuchsia Frost
```

Example with two products:

```text
DM130 District Perfect Tri Tee in the color Fuchsia Frost and DM130L District Women's Perfect Tri Tee in the color Fuchsia Frost
```

No part of this text should be hyperlinked.

### Sales Note product text

For CRM Sales Notes, use style codes only:

- One: `DM130L`
- Two: `DM130 and DM130L`
- Three or more: `DM130, DM130L, and PC54`

## Salesforce email requirements

Use the Salesforce email template named exactly:

```text
[AUTO] STOCK - Extension
```

Template replacements:

- Replace `[ORDER-NUMBER]` in the subject with the seven-digit CRM order number.
- Replace `[STOCK]` in the body with the selected products' full descriptions/colors, formatted as described above.
- Replace `[DAYS]` in the body with the positive integer exactly as entered. Entering `5` inserts `5`.

`[STOCK]` is the canonical product placeholder. Earlier discussion used `[PRODUCTS]` as a conceptual synonym, but the implementation and template should use `[STOCK]`.

The email must be sent immediately, like the existing Reachout automations; do not leave it as a draft.

### Required pre-send verification

Before clicking Send:

1. Read the intended customer email from the CRM order.
2. Open the matching Salesforce customer/account.
3. Confirm Salesforce identifies the same normalized email address.
4. Open the email composer and select the established Orders sender.
5. Load `[AUTO] STOCK - Extension`.
6. Verify the subject contains the correct order number.
7. Verify `[ORDER-NUMBER]`, `[STOCK]`, and `[DAYS]` no longer remain.
8. Verify all selected full product descriptions/colors appear in the body.
9. Verify the requested day count appears in the body.
10. Immediately before Send, read the composer's **To** recipient and compare it to the CRM customer email after safe normalization (case and surrounding whitespace).
11. Refuse to send if the recipient is blank, different, or includes an unexpected To/Cc/Bcc recipient.

After clicking Send, confirm Salesforce's existing sent-success indicator. If recipient or content verification fails, do not send the email, do not post Slack, and do not apply Issue - Stock.

## CRM Sales Note

Save this exact two-line structure, using a real newline (not the literal characters `/n`):

```text
[STYLE CODE LIST] need [DAYS]-day(s) extension
Emailed Txted
```

Examples:

```text
DM130 needs 5-day(s) extension
Emailed Txted
```

```text
DM130 and DM130L need 5-day(s) extension
Emailed Txted
```

```text
DM130, DM130L, and PC54 need 5-day(s) extension
Emailed Txted
```

Preserve the user's requested literal wording `day(s)` and capitalization `Emailed Txted` unless the user later changes it.

Sales Note writing must be idempotent: if the exact note is already present, report it as already present instead of duplicating it. Save the CRM order and confirm the note persisted.

## Slack notification

Send to the same Slack channel configured for rush-order shipping notifications:

```text
https://app.slack.com/client/T03DK2TN7/C04KPACK6VC
```

The message format is exactly:

```text
[CRM ORDER LINK] - Rush Order needs extension
```

Example:

```text
https://crm2.legacy.printfly.com/order/5043020 - Rush Order needs extension
```

Do not include the number of days in the Slack message.

This is a required notification for the Stock Extension workflow. Reuse the channel configuration and custom Slack-posting mechanism, but do not reuse the current paid-shipping eligibility filter. The notification is required because this workflow was explicitly selected, regardless of the order's shipping charge.

## Final CRM issue status

Only after all prior required work succeeds, apply this exact CRM order status:

```text
Issue - Stock
```

The CRM field supports searching by typing `stock`, as shown in the supplied screenshot. Select the exact `issue - stock` option, click Apply, and verify the status using order scope/status history, the success popup, or a confirmed post-refresh state.

`Issue - Stock` is the final completion marker. Do not apply it when the Sales Note, email, recipient verification, Slack post, or any other required step fails.

## Required execution order

The intended end-to-end order is:

1. User opens the Stock Issue dropdown and selects Extension Required.
2. Extension scans all design tabs and restores the original tab.
3. Extension shows detected products and the days input.
4. User selects one or more products and enters positive whole-number days.
5. Extension validates the input and submits a structured request.
6. Local server validates the order number, automation key, days, and product records.
7. Task enters the existing shared Automation queue.
8. Worker opens and verifies the correct CRM order.
9. Worker obtains the CRM customer email and selected product context.
10. Worker formats and saves the style-code-only Sales Note, then confirms persistence.
11. Worker opens the matching Salesforce customer and verifies its email.
12. Worker creates the email using the Orders sender and `[AUTO] STOCK - Extension`.
13. Worker replaces `[ORDER-NUMBER]`, `[STOCK]`, and `[DAYS]`.
14. Worker verifies the rendered subject/body and rechecks the To recipient against CRM immediately before Send.
15. Worker sends the email and confirms success.
16. Worker posts the required Slack message and confirms success.
17. Worker returns to CRM, applies `Issue - Stock`, and confirms it.
18. Worker publishes a structured success result; the extension reports completion.

If any required step fails, stop subsequent steps and return the specific failed stage. In particular, the final status must not be applied on partial completion.

## Existing architecture and reuse points

### Chrome extension

- `crm-order-dark-mode-extension/content.js`
  - Defines `MANUAL_ORDER_AUTOMATIONS`, `CANCEL_ORDER_AUTOMATIONS`, and `REACHOUT_ORDER_AUTOMATIONS`.
  - Creates dropdown controls through `createOrderProcessMenuControl`.
  - Places Cancel and Reachout controls through `ensureSingleOrderSheetScannerControls`.
  - Creates confirmation/input dialogs through `showOrderAutomationConfirmation`.
  - Queues selected automations through `queueManualOrderAutomation`.
  - Reapplies controls after Angular/AJAX DOM changes through a `MutationObserver`.
  - The Stock Issue control should be placed beside Reachout and should use a stock-specific scan/selection dialog rather than the current generic reason dialog.

- `crm-order-dark-mode-extension/bridge.js`
  - Sends single-order automation requests to the loopback app.
  - `startLocalManualOrderProcessing` currently sends only `order_id`, `automation`, and `reason`.
  - It must carry structured `days` and selected `products` for this workflow.

- `crm-order-dark-mode-extension/background.js`
  - Forwards content-script messages to the bridge.
  - It must forward the structured Stock Extension data.

- `crm-order-dark-mode-extension/manifest.json`
  - Bump the extension version when implementation is complete.

### Local server and queue

- `server.py`
  - `CRM_EXTENSION_SHEET_SCANNER_ORDER_AUTOMATIONS` registers existing Cancel/Reachout processes.
  - `CRM_EXTENSION_MANUAL_ORDER_AUTOMATIONS` registers allowed single-order extension automations.
  - `queue_crm_extension_manual_order_run` validates and queues one selected automation.
  - `/api/extension/bridge/process-order/manual` accepts the current extension payload.
  - The Stock Extension process needs a registered automation key (recommended: `stock_extension`), structured payload parsing/validation, serializable task arguments, and a runner.
  - Preserve the bridge's loopback and Chrome-extension origin checks.

### CRM and Salesforce worker behavior

- `workers/crm_product_separator.py`
  - `VISIBLE_TABS_JS`, `_visible_design_tabs`, `_click_design_tab`, and `_scan_order` demonstrate reliable all-tab discovery and scanning.
  - `PRODUCT_SCAN_JS` demonstrates product-block parsing but currently depends on Alpha links and needs a broader design-item-based replacement for Stock Extension.

- `workers/crm_copyright_cancel.py`
  - `CancelProcess` and existing non-cancel reachout flows demonstrate reusable process configuration.
  - `_insert_cancel_template` handles Salesforce template selection.
  - `_replace_subject_order_number` handles `[ORDER-NUMBER]` replacement.
  - `_read_salesforce_email_state` reads composer state.
  - `_prepare_and_maybe_send_salesforce_email` opens the matching account, verifies email, selects the Orders sender, fills the template, and sends.
  - `_send_salesforce_email` performs the existing send/confirmation flow.
  - `_append_copyright_cancel_sales_note` demonstrates idempotent Sales Note updates and CRM saving.
  - `_apply_order_status` demonstrates exact issue-status typing, selection, Apply, and confirmation.
  - Stock needs dedicated plain-text `[STOCK]` and `[DAYS]` replacement with verification. Do not overload the copyright `[REASON]` replacement semantics.

- `workers/crm_validate_address.py`
  - `_add_shipping_issue_sales_note` demonstrates Sales Note persistence verification.
  - `_handle_shipping_issue` demonstrates ordering CRM note/status work and a post-success Slack notification.
  - It delegates status application to the generic helper in `crm_copyright_cancel.py`.

- `workers/rush_order_notifications.py`
  - Defines the required channel through `RUSH_ORDER_SLACK_CHANNEL_URL`.
  - Shows the established `_run_slack_team("custom", custom_message=..., channel_url=...)` mechanism.
  - Its paid-shipping eligibility behavior must not gate Stock Extension notifications.

### Recommended organization

Prefer a dedicated worker module such as `workers/crm_stock_issue.py` that reuses well-tested CRM/Salesforce/Slack helpers. This avoids adding more stock-specific branching to the already large copyright/cancellation worker. If shared helpers need to move, keep the refactor narrow and preserve existing tests.

Suggested Stock Extension worker responsibilities:

- Define the process/template/status constants.
- Normalize and validate selected product records and days.
- Format email product descriptions.
- Format style-code Sales Note lists.
- Replace and verify `[STOCK]` and `[DAYS]` in Salesforce's composer.
- Perform final recipient verification.
- Coordinate CRM note, Salesforce email, Slack, and final status.
- Return structured stage results for diagnostics.

## Configuration additions

Add defaults and local config values following existing patterns, for example:

- `SALESFORCE_STOCK_EXTENSION_TEMPLATE = "[AUTO] STOCK - Extension"`
- `STOCK_EXTENSION_CRM_STATUS = "Issue - Stock"`
- Reuse `RUSH_ORDER_SLACK_CHANNEL_URL`; do not duplicate the channel URL unless a separate override is intentionally desired.

Do not add Google Sheet column or issue-type configuration in this phase.

## Payload and validation design

A recommended request shape is:

```json
{
  "order_id": "5043020",
  "automation": "stock_extension",
  "days": 5,
  "products": [
    {
      "tab_number": 1,
      "design_item_id": "design-item-8206660",
      "style": "DM130L",
      "description": "District Women's Perfect Tri Tee",
      "color": "Fuchsia Frost"
    }
  ]
}
```

Server-side validation should reject:

- Invalid or non-seven-digit order IDs
- Unsupported automation keys
- Missing, zero, negative, fractional, or nonnumeric days
- Empty product selections
- Products missing style, description, or color when those fields are required for correct email text
- Unreasonably long or malformed product fields
- Excessively large product arrays

Do not trust the content-script payload merely because it came through the local extension bridge. Normalize all text and ensure it cannot introduce HTML; email product replacement is plain text.

For stronger stale-data protection, the worker can rescan the live order and confirm the submitted style/description/color selections exist before contacting the customer. If implemented, compare normalized records and stop for manual review on a mismatch rather than silently changing the user's selection.

## Error handling and observability

Return a structured result for each stage, including at least:

- Product/input validation
- CRM order/customer verification
- Sales Note save/confirmation
- Salesforce account email verification
- Template selection and placeholder replacement
- Final composer recipient verification
- Salesforce send confirmation
- Slack send confirmation
- Issue - Stock application/confirmation

Capture the existing diagnostic screenshot/result artifacts on failure where supported. Error messages shown in the extension should name the failed stage and state whether an email or Slack message was already sent, so manual recovery does not cause accidental duplicate outreach.

The Sales Note and status operations are naturally idempotent. Salesforce email and Slack posting are not automatically idempotent, so a partial failure after either external action must be reported explicitly. Do not blindly rerun the entire workflow without warning after a confirmed email send.

## Required tests

Add focused tests without weakening existing coverage.

### Extension and bridge tests

- Stock Issue button is defined next to Reachout.
- Extension Required appears in its dropdown.
- Product scanning visits every tab and restores the original tab.
- Supplier labels are excluded.
- Non-Alpha design items are detected.
- Zero-quantity products are excluded.
- At least one product must be selected.
- Days accepts positive whole numbers only.
- Structured products/days pass through content script, background worker, bridge, and Flask route.
- Unknown automation and malformed payloads are rejected.

### Formatting tests

- One, two, and three-or-more conjunction formatting.
- Email formatting uses full description and color with no links.
- Sales Note formatting uses style codes only.
- Exact Sales Note wording and real newline.
- Exact Slack message without days.

### Salesforce tests

- Exact template selection for `[AUTO] STOCK - Extension`.
- `[ORDER-NUMBER]`, `[STOCK]`, and `[DAYS]` are all required before replacement.
- All placeholders are absent after replacement.
- Selected product text and days are present afterward.
- CRM email and Salesforce account email must match.
- Final composer To field must match CRM immediately before Send.
- Blank, mismatched, multiple unexpected, or unexpected Cc/Bcc recipients block Send.
- Send confirmation is required before proceeding.

### CRM and Slack tests

- Sales Note is saved and confirmed.
- Existing identical Sales Note is not duplicated.
- Required Slack message uses channel `C04KPACK6VC` and bypasses paid-shipping eligibility.
- Slack failure prevents final status application.
- `Issue - Stock` is applied only after every prior stage succeeds.
- Existing `Issue - Stock` is accepted idempotently.
- Failure at any prerequisite stage prevents later stages.

### Regression tests

Run at least:

```powershell
python -m pytest tests/test_extension_bridge.py
python -m pytest tests/test_crm_address_batch.py
```

Also run any new dedicated Stock Extension test file and compile affected Python modules. If practical, run the full test suite after focused tests pass.

## Manual verification plan

Before using a real customer order:

1. Reload the unpacked Chrome extension after the version bump.
2. Open a safe CRM test order with multiple tabs and multiple products.
3. Confirm one Stock Issue button appears beside Reachout.
4. Confirm the scanner reports every expected product and no supplier suffixes.
5. Confirm the original active tab is restored.
6. Confirm invalid day values cannot be queued.
7. Run a dry-run/inspection mode if the new worker provides one, stopping before external writes.
8. Inspect the Salesforce subject, body, and To recipient.
9. Verify the exact Sales Note, Slack message, and Issue - Stock actions using a controlled test order before enabling live use broadly.

## Explicitly out of scope

- Google Sheets scanning or an `EXT` column
- Spreadsheet product selection
- Additional Stock Issue dropdown automations beyond Extension Required
- Hyperlinked product text
- Per-product Stock Issue buttons
- Report-wide/batch execution from the extension

## Instructions for the next Codex chat

1. Open this repository as the workspace/project.
2. Read this document completely.
3. Inspect the cited implementation files and current `git status` before editing.
4. Preserve unrelated user changes in the worktree.
5. Confirm the exact Salesforce template is available in the live Salesforce environment before the first live send.
6. Implement the feature end to end, including tests and failure-state verification.
7. Do not add Google Sheets support.
8. Report any remaining ambiguity before making a materially different product decision.

