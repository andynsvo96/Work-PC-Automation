# Project guidance

## Scope and orientation

This file applies to the entire repository. Work PC Automation is a Windows/macOS
Flask dashboard with Selenium workers and the Manifest V3 CRM Order Assistant
Chrome extension. It covers CRM/Salesforce workflows, SanMar stock purchasing,
Paycom time tracking, Slack messages, Google Sheets scanning, and private remote
controls. Some extension editing and ordering actions run natively without the app.
Read `README.md` first and inspect the relevant implementation and tests before
changing behavior. Some documents under `docs/` describe earlier designs; current
code and regression tests determine what is implemented. Read the extension's
`README.md` for extension work and `docs/MAC_AND_TABLET_SETUP.md` for platform,
startup, remote-access, or clipboard changes.

## Where changes belong

- `server.py`: orchestration, persistent queue, scheduling, worker subprocesses,
  result reports, locks, and tray lifecycle; many routes also remain here.
- `queue_order_work.py` / `workers/crm_queue_order.py`: supported single-order
  pool requests and isolated worker dispatch; `process_locks.py`: shared vendor-cart locks.
- `routes/`: grouped HTTP routes; `ui_panel.html`: dashboard markup, styles, and
  JavaScript; `static/`: Salesforce template editor and dashboard assets.
- `workers/`: focused CRM, Paycom, Slack, and Salesforce browser workflows.
- `automation_runtime.py`: shared browser setup and worker result helpers.
- `runtime_paths.py` / `runtime_maintenance.py`: runtime paths and bounded cleanup.
- `config_defaults.py`: tracked non-secret defaults; `config.example.py`: setup template.
- `platform_runtime.py` / `node_preferences.py`: platform capabilities, worker
  capacity, and machine-local preferences.
- `credential_store.py` / `manage_credentials.py`: operating-system credential storage.
- `app_security.py`: authentication; `safe_sync.py` / `version_state.py`: Git startup/update behavior.
- `clipboard_runtime.py` / `configure_clipboard.py`: authenticated cross-system clipboard.
- `salesforce_templates.py` / `salesforce_email_html.py`: local template library
  and HTML cleanup; `workers/salesforce_*`: composition, verification, and messaging.
- `slack_delivery_receipts.py` / `slack_post_history.py` / `m6_posting.py`:
  guarded order notifications, confirmed post history, and the local M6 switch.
- `shipping_bypasser_mappings.py` / `shipping_bypasser_product_color_mappings.json`:
  shared CRM-to-SanMar product/color rules.
- `crm-order-dark-mode-extension/`: native All Orders/All Tabs editors, native
  stock ordering, order-page tools, list helpers, local bridge, and Salesforce tab reuse.
- `tests/`: Python regression tests, browser fixtures, and Node extension tests.

## Queue and worker rules

- Tasks run on the computer serving the dashboard. Windows and macOS have
  independent queues; do not introduce implicit cross-machine execution or assume
  a receipt on one computer protects an action on the other.
- Preserve existing lock ownership and queue boundaries. Explicit supported
  single-order jobs can share the configured 1-8 worker pool; eligibility lives
  in `queue_order_work.py`. The same order cannot overlap, and an incompatible
  queue entry drains the pool before running. Do not broaden pool eligibility to
  reports, batches, repeats, or shared purchasing workflows without matching safeguards.
- Keep worker profiles, status files, result files, and reports isolated per
  order and attempt. Preserve the CRM lock around the pool and the process lock
  protecting Push Back's shared SanMar cart.
- Preserve `runtime/state/automation_queue.json`, its execution descriptors,
  selections, results, and retry details. Waiting/idle tasks restore their order
  and timing; interrupted running tasks stop for review instead of replaying.
  Finished history remains until explicitly deleted or cleared. Interrupted
  Communications tasks and recorded Paycom punches must not gain unsafe Retry paths.
- Preserve combined schedule/repeat timing: initial scheduling accepts a later
  time today, long runs skip elapsed intervals without catch-up, and a zero-minute
  repeat waits for the initial schedule before repeating after each finish.
- Follow the existing worker subprocess/result contract. Use shared result helpers
  and runtime paths, retaining `success`, `message`, and workflow-specific details.

## CRM, purchasing, and messages

- For CRM edits, read `docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md` and verify persisted
  values after saving. Preserve existing notes, product identities, quantities,
  prices, and selections; ambiguous saved state stops for review.
- Normal Order Goods delegates to `workers/crm_order_goods_only.py`: one browser,
  up to 25 background order tabs, eligible native stock submissions only. It does
  not unlock stock, investigate supplier errors, retry purchases, or refresh to
  verify purchases. Keep **Submitted** distinct from purchase confirmation.
  Shared native helpers and their feedback/save checks remain in use by Push Back.
- Keep durable purchase, refund, clone, price-change, email, Slack, and payment-text
  receipts. Never delete or advance a receipt to bypass an uncertain outcome.
  Retry the original task with its original inputs and reconcile saved effects
  before continuing. Do not broaden a single-order request into a report run.
- Shipping Bypasser and Reorder Stock share SanMar mappings. Preserve cart,
  product/color/size/quantity, PO, warehouse, subcontractor, and fulfillment checks,
  including NJ/ADD allocation receipts. Manual NJ-only pickup permission must
  remain scoped to the explicitly supported single-order path.
- Preserve Salesforce recipient/From checks, Activity confirmation, bounded
  recovery, and duplicate-send receipts. Template changes must retain stable worker
  references, placeholder validation, revision checks, HTML cleanup, and backups;
  see `docs/SALESFORCE_TEMPLATE_EDITOR.md`.
- Slack delivery needs a new matching timestamped message that survives refresh;
  clearing the composer is insufficient. Unconfirmed submissions stop resends.
  The local **Post to M6** switch applies when the step runs; Off intentionally
  skips only that conversation, permits remaining workflow steps, and never backfills.

## Extension rules

- Preserve order-page scoping for queued tools and original Chrome-window scoping
  for popup batches, including the expanded editor. List controls only select/open
  applicable orders; they must not silently start queued workflows.
- Native All Orders/All Tabs editing and Order All actions do not require the
  local app. Preserve draft/exclusion handling, duplicate-tab checks, eligible
  stock filtering, and session receipts. Uncertain stock submissions require review.
- All Orders **Save All** starts native saves without refreshing tabs. **Check
  save status** only reads current pages. Keep guarded completion refreshes for
  queued tools and confirmed native stock actions separate from editor saves;
  refreshes must respect native edits, notes, dialogs, and existing draft safeguards.
- Document any required app restart, extension reload at `chrome://extensions`,
  and page refresh. Do not introduce new host permissions casually.

## Configuration and security

- Keep new defaults non-secret and compatible with existing machine-local configs.
  Use platform helpers and preserve Windows/macOS behavior; power and hardware
  features that require Windows must remain guarded.
- Preserve PIN/session authentication, CSRF checks, startup version locks,
  loopback/origin restrictions on the extension bridge, and signed clipboard-peer
  authentication. Keep secrets in Windows Credential Manager/macOS Keychain;
  never place secrets in browser code or responses or persist clipboard contents.
- Keep changes focused. Update the relevant documentation when workflows, settings,
  setup steps, or user-facing behavior change.

## Local data and live operations

- Never commit `config.py`, credentials, tokens, browser profiles, local state,
  local template libraries, screenshots, logs, exports, downloaded drivers, or
  third-party binaries.
  Respect `.gitignore`; do not force-add runtime evidence.
- Put generated artifacts under the appropriate ignored `runtime/` directory.
  Do not print secret values when diagnosing configuration or authentication.
- Default to isolated tests and fixtures. Starting `server.py` can synchronize Git
  and restore schedules. Live workers can change orders, purchase stock, clock
  time, or send messages; use them only within the user's authorized live scope.
- Do not overwrite unrelated work, reset a checkout destructively, or force-push.
  Review the exact staged diff before committing or publishing.

## Validation

Use the existing virtual environment when available (`.venv\Scripts\python.exe`
on Windows or `.venv/bin/python` on macOS), or activate it before the commands
below. Install dependencies from `requirements.txt` when needed. Run focused
tests for the changed behavior; run the full suite for changes spanning
orchestration, config, security, or runtime:

```powershell
python -m unittest discover -s tests
```

For extension bridge or Salesforce tab handling changes, also run:

```powershell
node --test tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
```

Choose the other relevant suites: `test_queue_parallel.py` and
`test_queue_persistence.py` for queue ownership/recovery, `test_scheduled_repeat.py`
for timing, `test_crm_order_goods*.py` and `test_order_all_batch.mjs` for native
stock submissions, and `test_all_orders.mjs`, `test_all_tabs*.mjs`,
`test_order_refresh.mjs`, and `test_order_scan_reader.mjs` for extension editing
and completion behavior. Dashboard logic also has Node coverage for reports,
sorting, scheduling, work plans, and stock history. See `README.md` and the
extension README for focused commands and browser prerequisites.

To run every Node regression on Windows:

```powershell
node --test (Get-ChildItem tests/test_*.mjs).FullName
```

Use local fixtures for browser verification instead of live customer orders.
Use an isolated profile under ignored runtime folders and a local ChromeDriver
(`CHROMEDRIVER` for suites that support it); some suites require additional local
fixture assets or suite-specific environment variables. Report skipped checks.
Documentation-only changes need diff, link, and factual checks; they do not need
new tests. Report the checks performed and any unverified platform behavior.
