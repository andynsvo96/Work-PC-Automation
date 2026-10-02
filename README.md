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

- **Order-page controls:** Chrome extension with dark mode, queued single-order Auto-Process, Manual Process, Cancel, Reachout, and Stock Issue actions, plus Salesforce tab reuse across windows in the same Chrome profile.
- **Single-order processing:** address validation, mixed-product separation, auto-splitting, stock unlocking and goods ordering, with conditional shipping bypass and stops for manual review when prerequisite steps fail.
- **Extra print areas:** sleeve, side, and reversible printing, plus additional embroidery areas, with design selection, configurable pricing, saved-change verification, and retry receipts for supported workflows.
  Email success requires a new matching email row in Salesforce Activity after Send. An unconfirmed attempt stops for manual review and blocks resending; inspect Activity before resolving its local receipt under `runtime/state/extra_print_email_confirmations/`. Restart the app after installing this change; no extension reload is needed.
- **Complicated embroidery:** select designs, including those already switched to ink printing, and route feedback requests through the existing Salesforce email workflow.
- **Stock issues:** dedicated size, color, and extension-required workflows using order-page selections and queued workers.
  Extension Required lets you choose affected sizes for each product/color. All detected order sizes start checked; **Clear** deselects them so you can choose specific sizes. CRM Sales Notes and the Salesforce email include each product/color's selected sizes. Restart the local app, reload CRM Order Assistant, and refresh CRM order pages after installing this update.
  Extension Required also trials the Salesforce Activity confirmation used by Extra Print Areas, including Lightning and Aura timeline layouts. It records check timing and blocks resending after an unconfirmed attempt. This check has not been rolled out to other Salesforce email workflows.
- **Sheets Scanner and Salesforce setup:** saved Salesforce worker profiles, setup and authentication checks, verification-code prompts, and scheduled or repeating queue runs.
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

Shipping Bypasser prefers a complete shipment from one warehouse. Before rejecting that shipment as too late, it checks a split across the closest available warehouses using the same stock-buffer setting. It verifies the rebuilt cart and each warehouse's UPS arrival date, then applies the production/due-date check to the latest arrival. An accepted split submits one order with the same customer PO and saves the usual production note listing the boxes and pieces from each warehouse. Missing split delivery dates stop for review. Existing free-shipping due-date extensions retain their behavior.

This worker update takes effect on the next queued run; no app restart, extension reload, or CRM page refresh is required.

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

You can also run a syntax compile pass:

```powershell
python -m compileall automation_audit.py automation_runtime.py server.py slack_message_rotation.py routes workers tests
```

## Notes

Login secrets are stored as Generic Credentials for the current Windows user under the `WorkAutomation/*` targets. `config.py` and browser profile directories can still contain private operational URLs and active sessions, so they remain ignored and should not be force-added to Git.

For contributor guidance, see [AGENTS.md](AGENTS.md). For deeper implementation details, see [the system guide](docs/AUTOMATION_SYSTEM_GUIDE.md) and [CRM edit/save runbook](docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md). Older planning documents may describe earlier versions; check current code and tests when implementing changes.
