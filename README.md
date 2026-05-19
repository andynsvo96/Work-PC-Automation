# Work PC Automation

Local Windows automation dashboard for work-day routines: Paycom clock actions, Slack status messages, weekly hour tracking, CRM order workflows, desktop power timers, and optional system metrics.

The app runs as a local Flask server with a browser control panel and a tray icon. Worker scripts handle the actual browser automation through Selenium, while the server coordinates scheduling, locks, retries, state updates, and audit logging.

## What It Does

- Runs Paycom clock-in and clock-out actions.
- Syncs weekly Paycom hours and tracks local work-hour state.
- Calculates auto clock-out timing against a configurable weekly hour cap.
- Sends Slack start/end/lunch/custom status messages.
- Rotates day-specific Slack messages.
- Runs CRM automation workers for address validation, stock unlocks, rush goods ordering, and auto-splitting.
- Provides a local web UI and HTTP API for manual controls and external triggers.
- Records automation results in a shared audit log.
- Supports hidden startup through a Windows Script Host launcher.

## Project Layout

- `server.py` - main Flask server, scheduler, tray app, and orchestration layer.
- `ui_panel.html` - local browser control panel.
- `workers/` - Selenium worker scripts for Paycom, Slack, and CRM workflows.
- `routes/` - grouped Flask route modules.
- `automation_runtime.py` - shared Selenium/runtime helpers.
- `automation_audit.py` - audit log helpers.
- `slack_message_rotation.py` - alternating Slack message state logic.
- `config.example.py` - safe template for local runtime settings.
- `docs/` - fuller system guide and CRM automation notes.
- `tests/` - regression tests for CRM batch/address behavior.

## Local Setup

This repo intentionally does not commit real credentials, browser sessions, logs, screenshots, state files, or machine-local binaries.

1. Install Python dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Create your local config:

   ```powershell
   Copy-Item config.example.py config.py
   ```

3. Fill in `config.py` with local-only values such as Paycom PIN, Slack channel URLs, CRM credentials, and CRM report URLs.

4. Start the server:

   ```powershell
   python server.py
   ```

5. Open the local UI:

   ```text
   http://127.0.0.1:5123/ui
   ```

For hidden startup on Windows, use `start_server_hidden.vbs`.

## Runtime Files

The following are created or maintained locally and are ignored by Git:

- `config.py`
- `work_hours.json`
- `crm_state.json`
- `crm_processing_state.json`
- `crm_address_validator_state.json`
- `slack_message_rotation_state.json`
- `last_result.json`
- `automation_record_log.txt`
- `server.log`
- browser profile folders such as `chrome_profile_crm/`
- screenshots, exports, backups, driver downloads, and cache folders

Keeping these files local prevents credentials, login sessions, audit history, and generated artifacts from being published.

## Testing

Run the regression suite with:

```powershell
python -m unittest discover -s tests
```

You can also run a syntax compile pass:

```powershell
python -m compileall automation_audit.py automation_runtime.py server.py slack_message_rotation.py routes workers tests
```

## Notes

This project is designed for a single trusted Windows workstation. Treat `config.py` and browser profile directories as sensitive because they may contain credentials, active sessions, or private operational URLs.

For deeper implementation details, see `docs/AUTOMATION_SYSTEM_GUIDE.md`.
