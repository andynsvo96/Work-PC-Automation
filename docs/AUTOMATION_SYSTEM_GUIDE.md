# Automation System Guide

## Purpose
This project is a local Windows automation platform that:
- Runs Paycom clock actions.
- Sends Slack status messages.
- Tracks weekly work hours and auto clock-out logic.
- Exposes a browser UI + HTTP API for manual control and external triggers.
- Logs every automation run in a shared audit log.
- Adds optional desktop hardware metrics and system power scheduling.

Use this document as a blueprint to build a second, separate automation with the same architecture.

## High-Level Architecture
The stack is split into 4 layers:

1. `server.py` (orchestrator/API/UI host)
- Starts Flask API on port `5123`.
- Hosts `ui_panel.html`.
- Owns timers, locks, state, retries, and scheduling.
- Launches worker scripts as subprocesses.
- Runs tray icon controls (pystray).

2. Worker scripts (single responsibility, CLI-driven)
- `paycom_clock.py`: Paycom in/out.
- `slack_team.py`: Slack in/out/custom message.
- `paycom_hours.py`: pulls weekly hours + day rows from Paycom.
- Each writes canonical result payload to `last_result.json`.

3. Shared runtime utilities
- `automation_runtime.py`: Chrome driver creation, stale Chrome cleanup, safe navigation, safe screenshot, result writing.
- `automation_audit.py`: append-only structured log writing.
- `slack_message_rotation.py`: alternating Slack message state.

4. Persistence and config
- `config.py`: single source of editable runtime settings.
- `work_hours.json`: work state (week, day rows, active shift, sync history).
- `slack_message_rotation_state.json`: alternating-message usage state.
- `automation_record_log.txt`: centralized event/result log.

## Core Design Pattern
This system uses a repeatable orchestration pattern:

1. UI/API calls a server endpoint.
2. Server acquires lock to prevent concurrent conflicting runs.
3. Server runs a worker subprocess (`python <script> <args>`).
4. Worker does browser automation, then writes `last_result.json`.
5. Server reads result JSON, applies business logic/state updates, returns API response, writes audit entry.
6. Timers/schedulers optionally trigger the same orchestration path later.

This is the main pattern you should reuse for a new automation.

## Startup and Process Model
Entry point:
- `python server.py`

On startup, `server.py`:
1. Reloads runtime config from `config.py`.
2. Kills any process already listening on port `5123`.
3. Restores any saved auto clock-out schedule from `work_hours.json`.
4. Starts Flask in a background thread.
5. Runs pystray icon in main thread.

Hidden launcher:
- `start_server_hidden.vbs` runs `pythonw.exe server.py` with no visible console.

## Key Files and Responsibilities
- `server.py`: Orchestration, Flask routes, config API, timers, tray actions, power actions, metrics integration.
- `ui_panel.html`: Full control panel; calls API endpoints with `fetch`.
- `config.py`: All uppercase assignments are editable via API/UI.
- `paycom_clock.py`: Clock in/out with dry-run and retry-aware logic.
- `slack_team.py`: Slack message send with headless/visible fallback and day message rotation.
- `paycom_hours.py`: Weekly-hour parsing + day-row extraction/flex-hour adjustment.
- `automation_runtime.py`: Shared Selenium safety/reliability helpers.
- `automation_audit.py`: Shared audit write helpers.
- `slack_message_rotation.py`: Stateful in/out message alternation.
- `work_hours.json`: Week tracking + active shift + sync history.
- `last_result.json`: Worker-to-server handoff payload.

## Subprocess Contract (Critical)
All workers follow the same result contract written to `last_result.json`:

```json
{
  "success": true,
  "message": "Human readable summary"
}
```

Optional extra fields can be included (example from `paycom_hours.py`):
- `week_hours`
- `source`
- `day_rows`

Server behavior:
- Deletes stale `last_result.json` before launching a worker.
- Waits with timeout.
- Reads JSON result if present (preferred over raw exit code).
- Falls back to exit-code success/failure if JSON is missing.

## State and Scheduling
### Work state (`work_hours.json`)
Stores:
- `week_start`
- `total_paid_hours`
- `days` map keyed by ISO date
- `active_shift`
- `last_paycom_sync`
- `sync_history`

### Auto clock-out rules
- Active only when `WORK_CLOCK_CAPPED = True`.
- Friday-only scheduling behavior is enforced.
- Auto-out time = `clock_in + remaining_week_hours + break_minutes`.
- Manual override allowed via `/work/update-schedule`.
- Timer restored after server restart.

### Lunch timer
- `/slack/lunch` starts lunch: sends lunch message immediately.
- Schedules auto-return message after 1 hour via `threading.Timer`.
- State tracked in memory + exposed by `/slack/lunch/status`.

### Power timer
- Schedules shutdown/sleep/restart countdown.
- Exposes live countdown via `/pc/status`.
- Countdown can be canceled or replaced.

## API Surface (Grouped)
Health/UI:
- `GET /health`
- `GET /ui`
- `GET /api/config`, `POST /api/config`
- `GET /api/server-runtime`
- `GET /api/metrics`

Clock-only:
- `/clock/in`, `/clock/out`
- `/clock/test/in`, `/clock/test/out`

Slack-only:
- `/slack/in`, `/slack/out`
- `/slack/lunch`, `/slack/lunch/status`, `/slack/lunch/cancel`

Combined work flow:
- `/work/in`, `/work/out`
- `/work/sync`
- `/work/schedule`, `/work/update-schedule`, `/work/cancel-schedule`
- `/work/status`

Automation testing:
- `GET /automation/test-options`
- `POST /automation/test-suite`

Power:
- `/pc/sleep`, `/pc/restart`, `/pc/shutdown`, `/pc/restart-explorer`
- `/pc/schedule`, `/pc/cancel-schedule`, `/pc/status`

## Reliability and Safety Patterns Used
- Global execution lock (`clock_lock`) prevents overlapping runs.
- State/config locks prevent race conditions on JSON/file updates.
- Headless-first browser runs with fallback to visible mode.
- Retry logic only for known transient failures.
- Stale Chrome cleanup scoped to specific profile path.
- Timeout-safe screenshot capture and driver shutdown.
- Atomic-ish config rollback on write failure.
- Audit logging on every start/success/failure path.

## Browser Profile Strategy
Two persistent Chrome profiles are used:
- `chrome_profile` for Paycom.
- `slack_chrome_profile` for Slack.

This allows stored login sessions and avoids re-auth every run.

Important for cloning:
- Always isolate profile dirs per automation target.
- Kill only Chrome processes tied to your profile path.

## Config Management Model
`/api/config` dynamically reads/writes uppercase assignments in `config.py`:
- Reads AST assignments.
- Groups fields by prefix (`PAYCOM_`, `SLACK_`, `WORK_`, other).
- Coerces values by detected type (`bool`, `number`, `list`, `string`).
- Writes assignments back to `config.py`.
- Reloads `config_module` immediately.

For a new automation, this is an excellent reusable admin/settings pattern.

## How To Recreate This For Another Automation
Use this exact skeleton:

1. Create a new worker script
- CLI args for action(s).
- Perform one automation responsibility.
- Call `write_result_payload(...)` at the end.

2. Reuse shared runtime helpers
- `build_chrome_driver`
- `kill_stale_chrome`
- `safe_get_with_partial_load`
- `safe_driver_quit`

3. Add server wrapper function
- Validate action.
- Acquire lock.
- Run worker subprocess.
- Retry transient failures.
- Write audit result.
- Return `(ok, message)`.

4. Add API endpoint(s)
- Thin route that calls wrapper and returns JSON.

5. Add UI buttons (optional)
- `fetch` to your new endpoint.
- Refresh shared status after action.

6. Add config keys
- Uppercase constants in `config.py`.
- They become editable automatically in `/api/config`.

7. Add optional state file
- Mirror `work_hours.json` pattern if your automation needs memory/scheduling.

8. Add timer/scheduler (if needed)
- Use `threading.Timer` + payload/status endpoint + cancel endpoint.

## Suggested Minimal Template For New Worker
```python
# new_task.py
from automation_runtime import write_result_payload

def run(action):
    try:
        # do work
        write_result_payload("new_task.action", "new_task.py", True, "Done")
        return 0
    except Exception as e:
        write_result_payload("new_task.action", "new_task.py", False, str(e))
        return 1
```

Then add a `server.py` wrapper that calls it with `_run_automation_script(...)`.

## Operational Notes
- `requirements.txt` currently lists: `selenium`, `webdriver-manager`, `flask`, `pystray`, `Pillow`.
- Desktop metrics in `server.py` also expect `psutil` and `pythonnet` (`import clr`) plus `LibreHardwareMonitorLib.dll`.
- Screenshots are saved under `screenshots/` on automation failures or checkpoints.
- `server.log` has runtime logs; `automation_record_log.txt` is the main action audit trail.

## What Matters Most To Copy
If you only copy 5 things for a second automation, copy these:

1. Subprocess + `last_result.json` contract.
2. Shared audit logging for every run.
3. Locking model to prevent overlap.
4. Config-as-code (`config.py`) with API update/reload.
5. Headless-first Selenium with visible fallback and targeted retries.

