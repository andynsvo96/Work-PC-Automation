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

## CRM Order Assistant and Newer Workflows

- **Custom Processing lists:** in Processing's **New main automation run**, select **Custom**, then choose a **CRM list link** or **Order IDs or order links**. The second option accepts up to 100 orders as seven-digit numbers (with an optional `#`), individual CRM order links, or a mix, separated by commas, spaces, or new lines. Direct `/order/1234567` and embedded `/app#/order/1234567` links are converted to IDs before queuing; duplicates run once even when pasted as both an ID and a link. Report links belong in **CRM list link**. Select **Use this input**, choose the tools appropriate for the list, and **Add to Queue**. All seven tools are available and retain their existing save-verification and retry rules. Order ID lists target only those orders, in the pasted order, for each selected tool. If a step needs review, later tools skip that order while the remaining orders continue; failed-order retries stay targeted. Unlocker uses the configured locked-orders report and verifies a single-order selection before Apply. Custom keeps separate tool selections and offers **Edit input**; choosing another list mode restores that mode's selections. Queued, repeated, and scheduled runs retain their original custom input. Address Validator uses its existing All-mode per-order shipping rules. Restart the local app and refresh the dashboard to load this change; no extension reload is needed.
- **Order-page controls:** Chrome extension with dark mode, queued single-order Auto-Process, Manual Process, Cancel, Reachout, and Stock Issue actions, plus Salesforce tab reuse across windows in the same Chrome profile.
- **Single-order processing:** address validation, mixed-product separation, auto-splitting, stock unlocking and goods ordering, with conditional shipping bypass and stops for manual review when prerequisite steps fail.
- **Batch address validation:** an order that still times out after the bounded recovery attempts is recorded for manual review. The validator continues to the remaining eligible orders and excludes attempted orders from later list scans in that run. A completed batch shows its success/review counts; the order's timeout remains available in **View Errors**. Restart the local app and refresh the dashboard to load the summary update.
- **Extra print areas:** sleeve, side, and reversible printing, plus additional embroidery areas, with design selection, configurable pricing, saved-change verification, and retry receipts for supported workflows.
  Sleeve-only requests priced at $0.00 use Salesforce **[AUTO] Comp Sleeves**, add the selected sleeve areas, and retain existing product prices. Restart the local app to load this worker update; no extension reload is needed.
  Email success uses the shared Salesforce Activity confirmation and bounded resend policy described below.
- **Complicated embroidery:** select designs, including those already switched to ink printing, and route feedback requests through the existing Salesforce email workflow.
- **Stock issues:** dedicated size, color, and extension-required workflows using order-page selections and queued workers.
  Extension Required lets you choose affected sizes for each product/color. All detected order sizes start checked; **Clear** deselects them so you can choose specific sizes. CRM Sales Notes and the Salesforce email include each product/color's selected sizes. Restart the local app, reload CRM Order Assistant, and refresh CRM order pages after installing this update.
  Extension Required and Suggest Different Size/Color use the shared Salesforce Activity confirmation described below.
  Suggest Different Size and Color emails also use color-before-description wording and keep each product/color's affected sizes separate. Their generated stock availability sentence works for one or multiple products. Restart the local app to load message wording updates.
- **Sheets Scanner and Salesforce setup:** saved Salesforce worker profiles, setup and authentication checks, verification-code prompts, and scheduled or repeating queue runs.
- **Salesforce email confirmation:** every Salesforce email sender, including cancellations, reachouts, complicated embroidery, Extra Print Areas/Comp Sleeves, and all Stock Issue emails, requires a new matching outgoing email in Activity. After Send, it waits up to 30 seconds, refreshes Salesforce if the email is missing, and checks for another 30 seconds. If Activity is still missing, it rebuilds and verifies the same draft and sends once more. The second attempt gets the same Activity/refresh checks. If it remains unconfirmed, the affected order stops, the dashboard records the error, and the app sends a desktop notification when its tray icon is available. Durable receipts under `runtime/state/extra_print_email_confirmations/` block any later automatic resend, including queue retries; inspect Activity before resolving a receipt manually. Existing unconfirmed receipts remain blocked. Workflow-specific receipts keep Size and Color emails separate even when their subjects match. Delayed Activity can cause the permitted resend to deliver a duplicate; Activity confirmation does not prove inbox delivery. Restart the local app to load this update; no extension reload is needed.
- **Salesforce refresh indicator:** if email Activity is missing and the confirmation check refreshes Salesforce, the result shows **Salesforce refresh used** with the order number. The warning appears in queue messages and Sheets Scanner run/order history, and triggers a desktop notification when the tray icon is available, even if the email is later confirmed successfully. The receipt also retains the refresh flag and count. An old receipt reused on a later run does not trigger another refresh notification. Restart the local app and refresh the dashboard to load this update; no extension reload is needed.
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

## Shipping Bypasser Product/Color Mappings

Use **Settings → Shipping mappings** to add an exact CRM product/color to SanMar product/color mapping. Each CRM product appears once and contains its own color list. Advanced fields support the SanMar inventory/pricing button, expected style IDs, and report labels. The same values remain directly editable in `shipping_bypasser_product_color_mappings.json`; malformed JSON and duplicate product/color entries fail with a specific error instead of being silently ignored.

Shipping Bypasser prioritizes usable Robbinsville, NJ stock, including partial quantities of a size, while retaining the configured stock buffer and closed-warehouse checks. NJ is ordered separately under the original customer PO using **Pick Up at warehouse → Robbinsville**; NJ never uses UPS, including for Mach 6 orders. If stock remains, the worker orders it separately under `ADD-<original PO>`, using one or multiple warehouses that exclude NJ. Both carts, POs, and ADD delivery dates are checked before the first purchase. Each purchase is confirmed and recorded in CRM; the second uses **Add box**. Production notes identify the ADD PO and list each product/color's sizes, piece quantities, and warehouse. A multi-warehouse ADD note also lists its box count under the same ADD PO. The note is refreshed and checked after saving.

When no usable NJ stock is involved, the existing process and original PO remain unchanged: prefer a complete shipment from one warehouse, then consider a split across the closest warehouses if the complete shipment arrives too late. Cart validation, the latest-arrival production/due-date check, and free-shipping due-date extensions retain their behavior.

NJ/ADD allocations and purchase states are saved under `runtime/state/shipping_bypass_nj_orders/`, alongside the existing submission receipts. A confirmed NJ purchase does not complete an unfinished ADD purchase. A retry uses the saved allocation, repairs missing CRM records or notes, and never repeats a confirmed purchase. An uncertain submission, missing allocation receipt, or changed CRM product/color/quantity stops for review. History retains both PO confirmation links and shows incomplete purchases as needing attention. Restart the local app and refresh the dashboard to load the history changes; no extension reload is needed.

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

Verify the NJ/ADD history display with:

```powershell
node --test tests/test_nj_stock_history.mjs
```

Optional local browser verification for NJ pickup, distinct CRM POs, Add box selection, and persisted production notes uses `tests/fixtures/sanmar_nj_checkout.html`. Set `SANMAR_FIXTURE_CHROMEDRIVER` to a local ChromeDriver path (and `SANMAR_FIXTURE_CHROME` if Chrome is not in its usual location), then run `python -m unittest discover -s tests -p test_sanmar_nj_browser.py`. It uses an isolated headless profile under ignored runtime folders and never opens live vendor or CRM pages.

You can also run a syntax compile pass:

```powershell
python -m compileall automation_audit.py automation_runtime.py server.py slack_message_rotation.py routes workers tests
```

## Notes

Login secrets are stored as Generic Credentials for the current Windows user under the `WorkAutomation/*` targets. `config.py` and browser profile directories can still contain private operational URLs and active sessions, so they remain ignored and should not be force-added to Git.

For contributor guidance, see [AGENTS.md](AGENTS.md). For deeper implementation details, see [the system guide](docs/AUTOMATION_SYSTEM_GUIDE.md) and [CRM edit/save runbook](docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md). Older planning documents may describe earlier versions; check current code and tests when implementing changes.
