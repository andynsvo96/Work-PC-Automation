# Work PC Automation

Cross-platform Windows/macOS automation dashboard for CRM and Salesforce workflows, Paycom time tracking, Slack updates, and private remote controls. Includes the CRM Order Assistant Chrome extension for working directly from an order page. Desktop power and hardware metrics remain Windows-only.

The app runs as a local Flask server with a browser control panel and a tray icon. Worker scripts handle the browser automation through Selenium, while the server coordinates scheduling, locks, retries, runtime state, result history, stage timing, and audit logging.

## What It Does

- Runs Paycom clock-in and clock-out actions.
- Syncs weekly Paycom hours and tracks local work-hour state.
- Calculates auto clock-out timing against a configurable weekly hour cap.
- Sends Slack start/end/lunch/custom status messages.
- Rotates day-specific Slack messages.
- Runs CRM automation workers for address validation, stock unlocks, rush goods ordering, auto-splitting, shipping bypasses, push-back handling, Sleeve Prints reachouts, and queue-driven issue processing.
- Scans supported queue rows for cancellation, reachout, auto-split, and manual stock-order workflows.
- Provides a local web UI and HTTP API for manual controls and external triggers.
- Records automation results in a shared audit log.
- Keeps local state, result JSON, screenshots, logs, debug output, and cloned browser profiles under ignored runtime folders.
- Supports hidden startup through a Windows Script Host launcher.
- Supports Safe Sync & Start on both operating systems, a macOS LaunchAgent, independent local queues, and a PIN-protected Android control board over Tailscale.
- Supports authenticated manual and opt-in automatic text/PNG clipboard transfer between Windows and macOS without persisting clipboard contents.
- Checks GitHub before every supported startup and fast-forwards a clean checkout before loading the app.
- Automatically runs Safe Sync & Start when a clean checkout falls behind, waiting for active automation or timers before restarting. The Update button can also save tracked local edits, synchronize `main`, publish them, and restart without discarding work.
- Runs every queued task on the computer serving the current dashboard; use only one OS at a time when adding work.
- Runs Home Assistant/Alexa HTTP triggers on the computer whose private URL receives the request.
- Can clear finished local queue history without touching running or waiting tasks.
- Sorts the Queue list by **Latest first** (default), **Oldest first**, or **Errors first**. Latest/oldest use the time a task was queued; Errors first puts failed tasks first, newest first within each group. The selection stays active during queue refreshes. Position numbers show execution order, and drag-and-drop still reorders waiting tasks. Refresh the dashboard to load the control; no app restart or extension reload is needed.

## CRM Order Assistant and Newer Workflows

- **Custom Processing lists:** in Processing's **New main automation run**, select **Custom**, then choose a **CRM list link** or **Order IDs or order links**. The second option accepts up to 100 orders as seven-digit numbers (with an optional `#`), individual CRM order links, or a mix, separated by commas, spaces, or new lines. Direct `/order/1234567` and embedded `/app#/order/1234567` links are converted to IDs before queuing; duplicates run once even when pasted as both an ID and a link. Report links belong in **CRM list link**. Select **Use this input**, choose the tools appropriate for the list, and **Add to Queue**. All seven tools are available and retain their existing save-verification and retry rules. Order ID lists target only those orders, in the pasted order, for each selected tool. If a step needs review, later tools skip that order while the remaining orders continue; failed-order retries stay targeted. Unlocker uses the configured locked-orders report and verifies a single-order selection before Apply. Custom keeps separate tool selections and offers **Edit input**; choosing another list mode restores that mode's selections. Queued, repeated, and scheduled runs retain their original custom input. Address Validator uses its existing All-mode per-order shipping rules. Restart the local app and refresh the dashboard to load this change; no extension reload is needed.
- **Order-page controls:** Chrome extension with dark mode, queued single-order Auto-Process, Manual Process, Cancel, Reachout, and Stock Issue actions, plus Salesforce tab reuse across windows in the same Chrome profile.
- **Copyright cancellation by design:** Cancel > Copyright - Cancel scans every tab's Design Name. Tabs that share the same artwork Design ID remain individually selectable using their saved order-tab IDs. Selecting all tabs retains whole-order cancellation and **[AUTO] Copyright Cancel**. Selecting some removes only those designs, accepts purchased-stock tabs retained at QTY 0/$0.00, verifies the recalculated credit, and uses **[AUTO] Copyright Cancel - Partial** while the remaining order stays active. Partial work and financial submissions have recovery receipts; retry the original queued task. Restart the local app, reload CRM Order Assistant 1.6.1, and refresh CRM order pages after updating. See the extension guide for refund checks and recovery details.
- **Partial copyright cancellation recovery:** if a refund is completed while a task is stopped after removing designs, Retry verifies the exact credit amount, reduced Paid, zero Balance Due, unchanged total/fees, and retained transaction history before resuming the Sales Note and email. It does not issue the refund again. A missing Sales Note is saved again once after reload and must persist before the workflow continues. Note entry waits for Edit Order to initialize, and an unsaved draft is saved rather than treated as a completed note. Unrelated payment changes still stop for review; existing Salesforce email confirmation receipts still apply. The next worker loads this change; no app restart, extension reload, or page refresh is needed. Keep the original queue entry and recovery receipt.
- **Single-order processing:** address validation, mixed-product separation, auto-splitting, stock unlocking and goods ordering, with conditional shipping bypass and stops for manual review when prerequisite steps fail.
- **Auto Splitter stock changes during a split:** the worker rechecks the original's stock before trimming, after saving, and after retained-order payment allocation. If CRM orders stock during a split that began with unordered stock, the worker switches to the stock-ordered cancellation method: reuse the existing split orders, create a replacement for split 1, verify all split contents, follow the existing vendor/PO routing rules, allocate the original payment, then cancel the original through the existing verified cleanup. Saved design contents, shipping shares, and promo allocation stay consistent with the already-created orders. A trimmed original uses its current subtotal for the Refund fee and only its remaining payment for the manual transfer transaction. The switch and new order IDs are saved for targeted retries. An uncertain replacement conversion or ambiguous stock state stops for review before creating another replacement. If stock remains unordered, planned moved tabs retained by CRM are accepted only with verified zero quantity and $0.00 totals. Restart the local app and refresh the dashboard to load recovery changes; no extension reload is needed.
- **Separator screenprinting review:** the Auto Separator warning checks only the source design tabs actually being separated. Screenprinting on an untouched tab or a tab excluded because of custom names and numbers does not trigger the warning. Print methods are checked before separation changes the designs. The next separator worker uses this change automatically; no app restart or extension reload is needed.
- **Batch address validation:** an order that still times out after the bounded recovery attempts is recorded for manual review. The validator continues to the remaining eligible orders and excludes attempted orders from later list scans in that run. A completed batch shows its success/review counts; the order's timeout remains available in **View Errors**. Restart the local app and refresh the dashboard to load the summary update.
- **Extra print areas:** sleeve, side, and reversible printing, plus additional embroidery areas, with design selection, configurable pricing, saved-change verification, and retry receipts for supported workflows.
  The worker waits up to 45 seconds for the invoice popup's non-PDF **View Invoice** link, checking both the CRM app and its parent page. It cancels the popup after copying the link and never sends the CRM invoice. A timeout reports the wait explicitly.
  Sleeve-only requests priced at $0.00 use Salesforce **[AUTO] Comp Sleeves**, add the selected sleeve areas, and retain existing product prices. Restart the local app to load this worker update; no extension reload is needed.
  Email success uses the shared Salesforce Activity confirmation and bounded resend policy described below.
- **Extension task retries:** failed or canceled Auto-Process, Manual Process (including Extra Print Areas), Cancel, Reachout, and Stock Issue tasks offer **Retry** beside **Delete** in the Automation queue. Retry uses the same entry, order, selections, prices, and reason. Existing recovery receipts and review stops still apply. Restart the local app and refresh the dashboard to load the retry controls and invoice wait; no extension reload is needed. Queue entries are cleared on restart, so an older failed request must be queued again with the same selections and prices; durable workflow receipts remain available.
- **Complicated embroidery:** select affected print areas under each design, including designs already switched to ink printing, then choose the existing Yes/No feedback route. Templates with `[PRINT]` and `[DESIGN]` name each selected area with its own design in one email, using lowercase wording such as `back print for "Team Logo"`. The worker rechecks names and areas before proceeding. Restart the local app, reload CRM Order Assistant, and refresh CRM order pages after updating.
- **Oversize embroidery:** Reachout > Oversize EMB to HDD scans design tabs and their current print areas, including designs already switched to ink. Front and Back areas start checked for every design; chest areas and other locations start unchecked. Adjust the affected areas before queuing; one Salesforce email replaces `[PRINT]` and `[DESIGN]` while keeping each area paired with its Design Name. The queued worker rechecks names and areas before proceeding. Reload CRM Order Assistant and refresh CRM order pages to load the automatic Front/Back selection; no local app restart is needed for this selection change.
- **Stock issues:** dedicated size, color, and extension-required workflows using order-page selections and queued workers.
  Extension Required lets you choose affected sizes for each product/color. All detected order sizes start checked; **Clear** deselects them so you can choose specific sizes. CRM Sales Notes and the Salesforce email include each product/color's selected sizes. Restart the local app, reload CRM Order Assistant, and refresh CRM order pages after installing this update.
  Extension Required and Suggest Different Size/Color use the shared Salesforce Activity confirmation described below.
  Suggest Different Size and Color emails also use color-before-description wording and keep each product/color's affected sizes separate. Their generated stock availability sentence works for one or multiple products. Restart the local app to load message wording updates.
- **Sheets Scanner and Salesforce setup:** saved Salesforce worker profiles, setup and authentication checks, verification-code prompts, and scheduled or repeating queue runs.
- **Salesforce Contact email skips:** Sheets Scanner and single-order workflows such as Oversize EMB to HDD report **Failed** when the business account's different email structure prevents sending the required email. The single-order result keeps the reason and completed CRM details; Sheets Scanner retains the row and writes the error for review. This also applies to email recovery and dry runs that encounter this unsupported structure. The next worker uses this change automatically; refresh the dashboard to see new results. No app restart or extension reload is needed.
- **Salesforce email confirmation:** every Salesforce email sender, including cancellations, reachouts, complicated embroidery, Extra Print Areas/Comp Sleeves, and all Stock Issue emails, requires a new matching outgoing email in Activity. After Send, it waits up to 30 seconds, refreshes Salesforce if the email is missing, and checks for another 30 seconds. If Activity is still missing, it rebuilds and verifies the same draft and sends once more. The second attempt gets the same Activity/refresh checks. If it remains unconfirmed, the affected order stops, the dashboard records the error, and the app sends a desktop notification when its tray icon is available. Durable receipts under `runtime/state/extra_print_email_confirmations/` block any later automatic resend, including queue retries; inspect Activity before resolving a receipt manually. Existing unconfirmed receipts remain blocked. Workflow-specific receipts keep Size and Color emails separate even when their subjects match. Delayed Activity can cause the permitted resend to deliver a duplicate; Activity confirmation does not prove inbox delivery. Restart the local app to load this update; no extension reload is needed.
- **Salesforce refresh indicator:** if email Activity is missing and the confirmation check refreshes Salesforce, the result shows **Salesforce refresh used** with the order number. The warning appears in queue messages and Sheets Scanner run/order history, and triggers a desktop notification when the tray icon is available, even if the email is later confirmed successfully. The receipt also retains the refresh flag and count. An old receipt reused on a later run does not trigger another refresh notification. Restart the local app and refresh the dashboard to load this update; no extension reload is needed.
- **Slack delivery confirmation:** Slack sends use the Send button and require a new matching message with a Slack timestamp in the intended conversation, followed by a refresh that still shows the message. Clearing the composer alone does not confirm delivery. Only confirmed posts enter local Slack history. An uncertain submission stops automatic resends and browser fallback; inspect the conversation before using a Slack-only action again. Work In and Work Out save the successful Paycom punch before attempting Slack and report a failed task if Slack fails, with the Paycom success retained in the message. Their Retry control stays unavailable after a recorded punch so recovery cannot repeat it. Restart the local app and refresh the dashboard to load the server changes; no extension reload is needed. These changes do not resend earlier missing messages.
- **Slack visibility:** local post history and paid-rush notifications for supported order workflows.
- **Settings and connectivity:** shipping product/color mapping editor, service connection controls, OS credential storage, private tablet access, and authenticated Windows/macOS clipboard transfer.

See the [CRM Order Assistant guide](crm-order-dark-mode-extension/README.md) for installation, workflow details, and reload instructions. The extension is loaded locally through Chrome's **Load unpacked** control.

## Project Layout

- `server.py` - main Flask server, scheduler, tray app, and orchestration layer.
- `ui_panel.html` - local browser control panel.
- `workers/` - Selenium worker scripts for Paycom, Slack, and CRM workflows.
- `routes/` - grouped Flask route modules.
- `automation_runtime.py` - shared Selenium/runtime helpers.
- `runtime_paths.py` - centralized paths for ignored local runtime artifacts.
- `runtime_maintenance.py` - bounded retention for disposable runtime evidence.
- `config_defaults.py` - tracked non-secret setting contract inherited by each machine's `config.py`.
- `automation_audit.py` - audit log helpers.
- `credential_store.py` / `manage_credentials.py` - Windows Credential Manager and macOS Keychain integration.
- `service_health.py` - read-only live authentication checks that never print secret values.
- `safe_sync.py` - non-destructive Git fetch/fast-forward startup.
- `slack_message_rotation.py` - alternating Slack message state logic.
- `config.example.py` - safe template for local runtime settings.
- `shipping_bypasser_product_color_mappings.json` - editable CRM-to-SanMar product and color mappings used by Shipping Bypasser.
- `docs/` - fuller system guide and CRM automation notes.
- `crm-order-dark-mode-extension/` - CRM Order Assistant extension and local app bridge.
- `tests/` - regression coverage for CRM workflows, queues, security, credentials, cross-platform runtime, sync, and extension behavior.
- `AGENTS.md` - repository guidance for coding agents, validation, and handling local data.

## Local Setup

This repo intentionally does not commit real credentials, browser sessions, logs, screenshots, state files, or machine-local binaries.

1. Install Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Create your local config. It imports the tracked defaults, so new safe
   settings added by future updates are available automatically:

   ```powershell
   Copy-Item config.example.py config.py
   ```

3. Fill in `config.py` with non-secret local values such as Slack channel URLs and CRM report URLs.

4. Store login secrets in Windows Credential Manager (the prompts do not echo passwords). Paycom setup prompts for its username, password, and four-digit PIN:

   ```powershell
   python manage_windows_credentials.py set paycom
   python manage_windows_credentials.py set crm
   python manage_windows_credentials.py set sanmar
   python manage_windows_credentials.py set salesforce
   python manage_windows_credentials.py set google_sheets
   ```

5. Start the server:

   ```powershell
   python server.py
   ```

6. Open the local UI:

   ```text
   http://127.0.0.1:5123/ui
   ```

For hidden startup on Windows, use `start_server_hidden.vbs`; it now performs Safe Sync & Start. For macOS and Android/Tailscale onboarding, follow [`docs/MAC_AND_TABLET_SETUP.md`](docs/MAC_AND_TABLET_SETUP.md).
A direct `python server.py` launch also performs the same safe startup check. Uncommitted, ahead, or diverged work is never overwritten; the app starts locked and explains what must be resolved.

ChromeDriver launches do not inherit the dashboard's open handles. This prevents
a detached setup driver from keeping port 5123 occupied after a Windows server
restart. Restart the local app to load this change for setup and connection tests;
queued workers load it on their next run. No extension reload is needed. If an
older driver already holds the port, exit that orphaned driver before starting
the app again.

If a worker reports **Windows Application Control blocked ChromeDriver (WinError 4551)**,
Windows prevented the browser driver from running before an automation session
started. The error includes the selected driver path. Check **Windows Security >
App & browser control** and **Event Viewer > Applications and Services Logs >
Microsoft > Windows > CodeIntegrity > Operational**, event **3077**, for the
blocked file and policy. The `VerifiedAndReputableDesktop` policy belongs to
[Smart App Control](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/app-control-for-business/operations/inbox-appcontrol-policies).
[Microsoft's FAQ](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions)
explains that it has no individual-app exception; have the device owner or Windows
administrator review the driver and available security options. Retrying the
same blocked driver or switching browser visibility does not resolve the policy.
The app does not change Windows security settings. For a clock action blocked at
this stage, that attempt submitted no Paycom punch; check Paycom before manually
recording or retrying a punch. Restart the local app to load this diagnostic for
in-process browser setup; queued workers load it on their next run. No extension
reload is needed.

## Shipping Bypasser Product/Color Mappings

Shipping Bypasser resolves each design's PO from the selected tab before searching the rest of the order page. A tab without an `H-` prefix keeps its own PO when another tab has an `H-` PO, so receipt checks compare the correct design's products and quantities. Numeric POs such as `5450885` and numeric POs with hyphens such as `5450885-3` are supported, and leading zeros are preserved. This fix loads with the next worker run; no app restart, extension reload, or page refresh is needed. Existing purchase receipts remain intact.

Shipping Bypasser supports XXS in SanMar inventory tables, quantity entry, and cart verification, including mobile and multiple-size cart rows. XXS remains separate from XS; warehouse allocation and the existing stock buffer still apply. This fix loads with the next worker run; no app restart, extension reload, or page refresh is needed.

Shipping mappings include Bella+Canvas CRM style `3200` and SanMar style `BC3200`, with 26 CRM color entries matching the supplied SanMar labels, including discontinued colors and both Deep Heather/Black spellings. `GREY/LT RED TRIBLEND` maps to `Grey/ Red Triblend`, and `WHT FLCK/CHAR TRIBLND` maps to `White Fleck/ Charcoal-Black Triblend`. CRM-only neon colors and White Fleck/Navy remain unmapped because they were absent from the supplied SanMar color list. Workers load these mappings on their next run. Use **Reload** in **Settings > Shipping mappings** to see the new entries; no app restart or extension reload is needed.

Shipping Bypasser checks each active design's visible vendor stock records before ordering. A recorded SanMar, S&S, or Local Inventory PO skips that tab even when splitting the order assigned it a different tab PO, the stock status still says unordered, or dark mode changes the row color. Tabs with no recorded stock remain eligible; saved NJ/ADD allocations still resume unfinished purchases through their existing receipts. This worker change loads with the next queued run; no app restart, extension reload, or page refresh is needed.

Use **Settings → Shipping mappings** to add an exact CRM product/color to SanMar product/color mapping. Each CRM product appears once and contains its own color list. Advanced fields support the SanMar inventory/pricing button, expected style IDs, and report labels. The same values remain directly editable in `shipping_bypasser_product_color_mappings.json`; malformed JSON and duplicate product/color entries fail with a specific error instead of being silently ignored.

Shipping Bypasser reads the order's subcontractor banner before ordering. Mach 6 / Mach6 Manufacturing orders always ship by **UPS to Mach6 in California**; they never use pickup. Prefer suitable stock near California, including a complete non-NJ multi-warehouse allocation before falling back to NJ. When NJ is needed, both NJ-only and mixed NJ/non-NJ allocations ship UPS under the **same original customer PO**, without an ADD prefix. Unsupported or unclear subcontractors stop for review and never default to in-house production.

For in-house orders, prefer a complete single-warehouse shipment instead of taking NJ stock first. Routine NJ-only orders belong to normal CRM ordering; an unordered NJ-only allocation reaching Shipping Bypasser stops for review without a purchase. When multiple warehouses are needed and the allocation includes NJ, the worker purchases NJ separately under the original customer PO using **Pick Up at warehouse**, then **Robbinsville**. The remaining stock ships UPS under `ADD-<original PO>`, using one or multiple non-NJ warehouses, and is recorded through **Add box**. In-house multi-warehouse allocations without NJ use UPS and the same original PO across boxes. The configured stock buffer, manual buffer override, and closed-warehouse checks are retained.

For NJ/ADD orders, both carts, POs, and ADD delivery dates are checked before the first purchase. Each purchase is confirmed and recorded in CRM. Production notes identify the ADD PO and each product/color's sizes, piece quantities, and warehouse; multi-warehouse ADD notes include the box count. Ordinary shipped multi-warehouse notes retain their same-PO box counts. UPS selection is checked for the selected warehouses, and review-page fulfillment is verified before submission. Cart validation, late single-warehouse fallback, the latest-arrival production/due-date check, and free-shipping due-date extensions retain their behavior.

NJ/ADD allocations and purchase states are saved under `runtime/state/shipping_bypass_nj_orders/`, alongside the existing submission receipts. A confirmed NJ purchase does not complete an unfinished ADD purchase. An in-house retry uses its saved allocation, repairs missing CRM records or notes, and never repeats a confirmed purchase. A saved NJ/ADD allocation on a subcontractor order stops for review, including a completed receipt; confirmed purchases and receipts remain intact. Uncertain submissions, missing allocation receipts, and changed CRM product/color/quantity also stop for review. This fix does not change fulfillment on previously submitted SanMar orders. History retains both PO confirmation links and shows incomplete purchases as needing attention. The routing correction loads with the next worker run; no app restart, extension reload, or page refresh is required.

Shipping Bypasser keeps each tab's PO links with that tab; a partial order can still name another tab's purchases in its labeled summary. Production-note edits target the Production Notes field and stop for review if the field is ambiguous. NJ/ADD notes are checked after save and refresh for up to 12 seconds. If the note is missing, the worker refreshes and retries the note save once, preserving existing text and avoiding a duplicate note or repeated stock purchases. A second failed verification stops for review and captures a CRM error screenshot under `runtime/screenshots/`; the purchase receipts remain available for a later note-only recovery. The worker changes load on the next run. Restart the local app and refresh the dashboard to load the PO-link correction; no extension reload is needed.

## Runtime Files

The following are created or maintained locally and are ignored by Git:

- `config.py`
- `runtime/state/` for active JSON state and templates
- `runtime/results/` for latest and historical result JSON
- `runtime/screenshots/` for browser screenshots
- `runtime/logs/` for `server.log` and `automation_record_log.txt`
- `runtime/generated_profiles/` for temporary cloned browser profiles
- `runtime/debug/` for old backups, exports, temp files, and test artifacts
- browser profile folders such as `chrome_profile_crm/`
- driver downloads and cache folders

Keeping these files local prevents credentials, login sessions, audit history, and generated artifacts from being published.

## Testing

Run the regression suite with:

```powershell
python -m unittest discover -s tests
```

Run the extension's Node tests with:

```powershell
node --test tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
```

Verify Queue sorting and its interaction with task controls and execution order with:

```powershell
node --test tests/test_queue_sort.mjs
```

Verify the NJ/ADD history display with:

```powershell
node --test tests/test_nj_stock_history.mjs
```

Optional local browser verification for NJ pickup, distinct CRM POs, Add box selection, and persisted production notes uses `tests/fixtures/sanmar_nj_checkout.html`. Set `SANMAR_FIXTURE_CHROMEDRIVER` to a local ChromeDriver path (and `SANMAR_FIXTURE_CHROME` if Chrome is not in its usual location), then run `python -m unittest discover -s tests -p test_sanmar_nj_browser.py`. It uses an isolated headless profile under ignored runtime folders and never opens live vendor or CRM pages.

Auto Splitter's stock-race and recovery regressions run with `python -m unittest discover -s tests -p test_crm_splitter_stock_race.py`. To include its local empty-tab browser fixture, set `SPLITTER_FIXTURE_CHROMEDRIVER` to a local ChromeDriver path and optionally `SPLITTER_FIXTURE_CHROME` to the Chrome executable. It uses an isolated profile under `runtime/generated_profiles/` and opens only `tests/fixtures/crm_retained_order.html`.

You can also run a syntax compile pass:

```powershell
python -m compileall automation_audit.py automation_runtime.py server.py slack_message_rotation.py routes workers tests
```

## Notes

Login secrets are stored as Generic Credentials for the current Windows user under the `WorkAutomation/*` targets. `config.py` and browser profile directories can still contain private operational URLs and active sessions, so they remain ignored and should not be force-added to Git.

For contributor guidance, see [AGENTS.md](AGENTS.md). For deeper implementation details, see [the system guide](docs/AUTOMATION_SYSTEM_GUIDE.md) and [CRM edit/save runbook](docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md). Older planning documents may describe earlier versions; check current code and tests when implementing changes.
