# Project guidance

## Scope and orientation

This file applies to the entire repository. Work PC Automation is a Windows/macOS
Flask dashboard with Selenium workers and a Manifest V3 CRM Chrome extension.
Read `README.md` first and inspect the relevant implementation and tests before
changing behavior. Some documents under `docs/` describe earlier designs; current
code and regression tests determine what is implemented.

## Where changes belong

- `server.py`: orchestration, queues, locks, scheduling, worker subprocesses, tray lifecycle.
- `routes/`: grouped HTTP routes; `ui_panel.html`: dashboard markup, styles, and JavaScript.
- `workers/`: focused CRM, Paycom, Slack, and Salesforce browser workflows.
- `automation_runtime.py`: shared browser setup and worker result helpers.
- `runtime_paths.py` / `runtime_maintenance.py`: runtime paths and bounded cleanup.
- `config_defaults.py`: tracked non-secret defaults; `config.example.py`: setup template.
- `credential_store.py` / `manage_credentials.py`: operating-system credential storage.
- `app_security.py`: authentication; `safe_sync.py` / `version_state.py`: Git startup/update behavior.
- `crm-order-dark-mode-extension/`: extension UI, local bridge, and Salesforce tab reuse.
- `tests/`: Python regression tests, browser fixtures, and Node extension tests.

## Implementation rules

- Preserve queue serialization and existing lock ownership. Tasks run on the
  computer serving the dashboard; do not introduce implicit cross-machine execution.
- Follow the existing worker subprocess/result contract. Use shared result helpers
  and runtime paths, retaining `success`, `message`, and workflow-specific details.
- Keep new defaults non-secret and compatible with existing machine-local configs.
  Use platform helpers and preserve Windows/macOS behavior; power and hardware
  features that require Windows must remain guarded.
- Preserve authentication, loopback restrictions on the extension bridge, and
  credential-store boundaries. Never place secrets in browser code or responses.
- For CRM edits, read `docs/CRM_ORDER_EDIT_SAVE_RUNBOOK.md` and verify persisted
  values after saving. Preserve receipts and retry checks that prevent duplicate
  charges, cloned products, emails, and Slack messages. Stop for review on ambiguous
  state instead of guessing or broadening a single-order task to a report.
- For extension changes, read its README, preserve order-page scoping, and document
  any required app restart, extension reload, or page refresh.
- Keep changes focused. Update the relevant documentation when workflows, settings,
  setup steps, or user-facing behavior change.

## Local data and live operations

- Never commit `config.py`, credentials, tokens, browser profiles, local state,
  screenshots, logs, exports, downloaded drivers, or third-party binaries.
  Respect `.gitignore`; do not force-add runtime evidence.
- Put generated artifacts under the appropriate ignored `runtime/` directory.
  Do not print secret values when diagnosing configuration or authentication.
- Default to isolated tests and fixtures. Starting `server.py` can synchronize Git
  and restore schedules. Live workers can change orders, purchase stock, clock
  time, or send messages; use them only within the user's authorized live scope.
- Do not overwrite unrelated work, reset a checkout destructively, or force-push.
  Review the exact staged diff before committing or publishing.

## Validation

Use the existing virtual environment when available. Install dependencies from
`requirements.txt` when needed. Run focused tests for the changed behavior; run
the full suite for changes spanning orchestration, config, security, or runtime:

```powershell
python -m unittest discover -s tests
```

For extension bridge or Salesforce tab handling changes, also run:

```powershell
node --test tests/test_extension_bridge.mjs tests/test_salesforce_tabs.mjs
```

Use local fixtures for browser verification instead of live customer orders.
Documentation-only changes need diff, link, and factual checks; they do not need
new tests. Report the checks performed and any unverified platform behavior.
