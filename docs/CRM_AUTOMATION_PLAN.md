# CRM Order Automation — Comprehensive Build Plan
## "Stock Auto Ordering Unlock" — Phase 1

---

## 1. Overview

This document is a blueprint for building a **new, standalone** CRM order automation program
following the same architecture as the existing Paycom/Slack automation. It is written to be
fed directly into VS Code Codex / Claude Code as a structured prompt guide.

**Goal of Phase 1:** Automatically unlock Stock Auto Ordering for all orders in a filtered
CRM report list, end-to-end with no manual intervention.

**Non-goals (future phases):** Any other order processing actions. Architecture is built to be
extended.

---

## 2. High-Level Automation Flow

```
START
  │
  ▼
[1] Launch Chrome (headless-first, visible fallback)
  │
  ▼
[2] Navigate to report URL from config
  │
  ▼
[3] Detect login page?  ──YES──▶ [3a] Auto-login with credentials from config
  │                                     │
  │◀────────────────────────────────────┘
  ▼
[4] Wait for order list to fully load
  │
  ▼
[5] Select ALL orders (Shift+Click first row → all rows highlight)
  │
  ▼
[6] Wait for "Order Preview" panel to appear on right side
  │
  ▼
[7] Click the dropdown arrow inside Order Preview panel
  │
  ▼
[8] Type "unloc" → select "Stock Auto Ordering Unlocked" from dropdown
  │
  ▼
[9] Click "Apply" button
  │
  ▼
[10] Wait for confirmation modal: "Are you sure you want to apply..."
  │
  ▼
[11] Click "OK" button on modal
  │
  ▼
[12] Wait for "Update complete" green checkmark in Order Preview
  │
  ▼
[13] Write result to last_result.json  →  Server reads + logs audit entry
  │
  ▼
END
```

---

## 3. Project File Structure

```
crm_automation/
│
├── server.py                        # Flask orchestrator + tray icon
├── ui_panel.html                    # Browser control panel
├── config.py                        # All editable settings (uppercase constants)
│
├── crm_unlock_orders.py             # ★ Main worker script (Phase 1)
│
├── automation_runtime.py            # Shared: Chrome driver, safe nav, screenshots
├── automation_audit.py              # Shared: Append-only audit log writer
│
├── last_result.json                 # Worker → Server handoff (auto-deleted before each run)
├── crm_state.json                   # Persistent CRM state (last run, order count, history)
├── automation_record_log.txt        # Central audit trail
│
├── screenshots/                     # Auto-saved on failure or checkpoint
├── chrome_profile_crm/             # Dedicated Chrome profile for CRM (keeps login session)
│
├── start_server_hidden.vbs         # Launches server with no console window
└── requirements.txt                # selenium, webdriver-manager, flask, pystray, Pillow
```

---

## 4. File-by-File Specification

---

### 4.1 `config.py`

All uppercase. Readable/writable via `/api/config` endpoint automatically.

```python
# --- CRM URLs ---
CRM_LOGIN_URL = "https://crm2.legacy.printfly.com/login"
CRM_REPORT_URL = "https://crm2.legacy.printfly.com/report/967?_token=..."  # Full URL here

# --- Browser Settings ---
CRM_HEADLESS = True                        # False = visible browser for debugging
CRM_PROFILE_DIR = "chrome_profile_crm"    # Isolated from other automations
CRM_PAGE_LOAD_TIMEOUT = 30                 # Seconds before giving up on page load
CRM_ACTION_TIMEOUT = 15                    # Seconds for element interactions

# --- Retry Settings ---
CRM_MAX_RETRIES = 2
CRM_RETRY_DELAY_SECONDS = 3

# --- Dry Run ---
CRM_DRY_RUN = False                        # True = go through all steps but skip OK confirm

# --- Audit ---
CRM_AUDIT_LOG = "automation_record_log.txt"
```

---

### 4.2 `automation_runtime.py`

Reused directly from existing automation. Provides:

| Function | Purpose |
|---|---|
| `build_chrome_driver(profile_dir, headless)` | Returns configured Selenium WebDriver |
| `kill_stale_chrome(profile_dir)` | Kills Chrome processes using a specific profile |
| `safe_get_with_partial_load(driver, url, timeout)` | Navigates with partial-load tolerance |
| `safe_screenshot(driver, path)` | Timeout-safe screenshot capture |
| `safe_driver_quit(driver)` | Graceful driver shutdown without hanging |
| `write_result_payload(action, script, success, message, **extras)` | Writes `last_result.json` |

**IMPORTANT:** Copy this file verbatim from the existing automation. Do not modify it.

---

### 4.3 `automation_audit.py`

Reused directly. Provides:

| Function | Purpose |
|---|---|
| `write_audit_entry(log_path, action, status, message, details)` | Appends structured log line |

---

### 4.4 `crm_unlock_orders.py` — MAIN WORKER SCRIPT

This is the only new worker to write for Phase 1. Full specification:

**CLI contract:**
```
python crm_unlock_orders.py --action unlock_all
python crm_unlock_orders.py --action unlock_all --dry-run
```

**Internal step-by-step logic:**

```python
def run(action, dry_run=False):

    # 1. Kill any stale Chrome on this profile
    kill_stale_chrome(CRM_PROFILE_DIR)

    # 2. Build driver
    driver = build_chrome_driver(CRM_PROFILE_DIR, CRM_HEADLESS)

    try:
        # 3. Navigate to report URL
        safe_get_with_partial_load(driver, CRM_REPORT_URL, CRM_PAGE_LOAD_TIMEOUT)

        # 4. Login detection
        if is_login_page(driver):
            do_login(driver)
            safe_get_with_partial_load(driver, CRM_REPORT_URL, CRM_PAGE_LOAD_TIMEOUT)

        # 5. Wait for order rows to appear
        wait_for_order_rows(driver)

        # 6. Select all orders
        order_count = select_all_orders(driver)

        # 7. Wait for Order Preview panel
        wait_for_order_preview_panel(driver)

        # 8. Open dropdown + select "Stock Auto Ordering Unlocked"
        choose_unlock_status(driver)

        # 9. Click Apply
        click_apply(driver)

        # 10. Confirm modal (skip if dry run)
        if not dry_run:
            click_ok_on_modal(driver)

        # 11. Wait for "Update complete"
        verify_update_complete(driver)

        write_result_payload("crm.unlock_orders", "crm_unlock_orders.py",
                             True, f"Unlocked {order_count} orders successfully",
                             order_count=order_count, dry_run=dry_run)
        return 0

    except Exception as e:
        safe_screenshot(driver, f"screenshots/crm_unlock_error.png")
        write_result_payload("crm.unlock_orders", "crm_unlock_orders.py",
                             False, str(e))
        return 1

    finally:
        safe_driver_quit(driver)
```

**Selenium Selector Strategy (per UI element):**

| Step | Element | Selector Strategy | Notes |
|---|---|---|---|
| Login detection | Page URL or login form | `driver.current_url` contains `/login` OR `find_element(By.NAME, "email")` | Check URL first, fallback to element |
| Username field | Input with email icon | `By.NAME, "email"` or `By.CSS_SELECTOR, "input[type='email']"` | |
| Password field | Input with lock icon | `By.NAME, "password"` or `By.CSS_SELECTOR, "input[type='password']"` | |
| Login button | "Login" button | `By.XPATH, "//button[contains(text(), 'Login')]"` | |
| Order rows | Table data rows | `By.CSS_SELECTOR, "table tbody tr[data-id]"` or similar row selector | Inspect live DOM |
| First data row | First clickable order row | `rows[0]` from the row collection | Shift+click selects all |
| Order Preview panel | Right panel | `By.XPATH, "//*[contains(text(), 'Order Preview')]"` | Wait for visibility |
| Orders selected count | Text in panel | `By.XPATH, "//*[contains(text(), 'orders selected')]"` | Confirms selection worked |
| Dropdown arrow | Blue dropdown button | `By.CSS_SELECTOR, ".order-preview .dropdown-toggle"` or button next to input | Inspect DOM |
| Dropdown option | "Stock Auto Ordering Unlocked" | `By.XPATH, "//*[contains(text(), 'Stock Auto Ordering Unlocked')]"` | After dropdown opens |
| Apply button | "Apply" button in panel | `By.XPATH, "//button[contains(text(), 'Apply')]"` | Scoped to panel |
| OK modal button | Confirm dialog OK | `By.XPATH, "//button[contains(text(), 'OK')]"` | Wait for modal to appear |
| Update complete | Green success text | `By.XPATH, "//*[contains(text(), 'Update complete')]"` | Presence = success |

**Select All Logic — Critical Detail:**

The CRM selects all rows via Shift+Click on the first row. Selenium implementation:

```python
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

def select_all_orders(driver):
    rows = WebDriverWait(driver, 15).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, "table tbody tr.order-row"))
    )
    if not rows:
        raise Exception("No order rows found in list")
    
    # Shift+Click first row to select all
    ActionChains(driver)\
        .key_down(Keys.SHIFT)\
        .click(rows[0])\
        .key_up(Keys.SHIFT)\
        .perform()
    
    # Verify selection count matches row count
    WebDriverWait(driver, 10).until(
        EC.text_to_be_present_in_element(
            (By.XPATH, "//*[contains(text(), 'orders selected')]"),
            str(len(rows))
        )
    )
    return len(rows)
```

**Dropdown interaction — Critical Detail:**

The dropdown MUST be clicked (not just the text field) or the status does not apply:

```python
def choose_unlock_status(driver):
    # Click the dropdown ARROW button (not the text input)
    dropdown_btn = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, ".order-preview .dropdown-toggle"))
    )
    dropdown_btn.click()
    
    # Type to filter, then click the option
    search_input = driver.find_element(By.CSS_SELECTOR, ".order-preview input[type='text']")
    search_input.clear()
    search_input.send_keys("unloc")
    
    # Wait for and click the "Stock Auto Ordering Unlocked" option
    option = WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable(
            (By.XPATH, "//*[contains(@class,'dropdown-menu')]//*[contains(text(),'Stock Auto Ordering Unlocked')]")
        )
    )
    option.click()
```

---

### 4.5 `server.py`

Flask orchestrator with the following responsibilities for this automation:

**Routes to add:**

| Route | Method | Purpose |
|---|---|---|
| `/crm/unlock` | POST | Trigger unlock_all worker |
| `/crm/unlock/dry-run` | POST | Trigger dry-run (no final OK click) |
| `/crm/status` | GET | Return last run result + state |
| `/crm/state` | GET | Return `crm_state.json` |

**Server wrapper pattern (copy from existing automation):**

```python
def _run_crm_unlock(dry_run=False):
    with clock_lock:  # Reuse existing global lock or create crm_lock
        # 1. Delete stale last_result.json
        # 2. Build subprocess args
        args = ["python", "crm_unlock_orders.py", "--action", "unlock_all"]
        if dry_run:
            args.append("--dry-run")
        # 3. Run subprocess with timeout
        # 4. Read last_result.json
        # 5. Update crm_state.json
        # 6. Write audit entry
        # 7. Return (ok, message, extras)
```

**On startup**, server should:
- Check `crm_state.json` exists, create with defaults if not
- No scheduled timers needed for Phase 1

---

### 4.6 `crm_state.json`

```json
{
  "last_run_timestamp": null,
  "last_run_success": null,
  "last_run_message": null,
  "last_order_count": 0,
  "total_runs": 0,
  "total_orders_processed": 0,
  "run_history": []
}
```

`run_history` stores last 20 entries: `{ timestamp, success, order_count, message }`

---

### 4.7 `ui_panel.html`

Add a new **CRM section** to the existing UI panel (or create standalone panel):

**UI elements needed:**

```
┌─────────────────────────────────────────────┐
│  🖨️  CRM Order Automation                    │
├─────────────────────────────────────────────┤
│  Status: [Last run: Never / X orders / time] │
│                                             │
│  [ 🔓 Unlock All Orders ]  [ 🧪 Dry Run ]   │
│                                             │
│  Total processed: 0 orders across 0 runs    │
└─────────────────────────────────────────────┘
```

Each button does a `fetch` POST to the appropriate `/crm/` endpoint and refreshes status.
Status polling: call `GET /crm/status` every 2 seconds while a run is active.

---

### 4.8 `start_server_hidden.vbs`

Reuse the existing VBS launcher pattern:

```vbscript
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "pythonw.exe server.py", 0, False
```

---

## 5. Reliability & Safety Patterns to Implement

| Pattern | Implementation |
|---|---|
| **Execution lock** | Single `crm_lock = threading.Lock()` — one CRM run at a time |
| **Headless-first** | `CRM_HEADLESS = True` in config; fallback to visible on ChromeDriver error |
| **Stale Chrome cleanup** | `kill_stale_chrome(CRM_PROFILE_DIR)` before every run |
| **Timeout safety** | All `WebDriverWait` calls use `CRM_ACTION_TIMEOUT` |
| **Screenshot on failure** | `safe_screenshot()` called in every `except` block |
| **Retry for transient failures** | Wrap `_run_crm_unlock()` in retry loop for network/timeout errors only |
| **Dry run mode** | All steps execute EXCEPT clicking "OK" on confirm modal |
| **Audit every run** | `write_audit_entry()` on start, success, and every failure path |
| **Isolated Chrome profile** | `chrome_profile_crm/` — separate from Paycom and Slack profiles |
| **Config reload** | `/api/config` POST reloads `config.py` module immediately |

---

## 6. Login Detection Logic

```python
def is_login_page(driver):
    # Primary: URL check
    if "/login" in driver.current_url:
        return True
    # Fallback: look for email input on page
    try:
        driver.find_element(By.CSS_SELECTOR, "input[type='email'], input[name='email']")
        return True
    except NoSuchElementException:
        return False

def do_login(driver):
    credential = read_windows_credential(CRM_CREDENTIAL_TARGET)
    email_field = WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "input[type='email'], input[name='email']"))
    )
    email_field.clear()
    email_field.send_keys(credential.username)
    
    password_field = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
    password_field.clear()
    password_field.send_keys(credential.secret)
    
    login_btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Login')]")
    login_btn.click()
    
    # Wait until we're off the login page
    WebDriverWait(driver, 15).until(
        lambda d: "/login" not in d.current_url
    )
```

---

## 7. Codex / Claude Code Build Instructions

Use the following as a **prompt sequence** for VS Code Codex agent:

### Step 1 — Scaffold the project
```
Create a new folder called crm_automation/. 
Copy automation_runtime.py and automation_audit.py from the existing automation project.
Create empty files: server.py, ui_panel.html, config.py, crm_unlock_orders.py, crm_state.json.
Create folders: screenshots/, chrome_profile_crm/.
Create requirements.txt with: selenium, webdriver-manager, flask, pystray, Pillow
```

### Step 2 — Build config.py
```
Write config.py following section 4.1 of the build plan exactly.
All settings are uppercase constants.
Store the CRM login with `python manage_windows_credentials.py set crm`.
Use the full report URL provided.
```

### Step 3 — Build crm_unlock_orders.py
```
Write crm_unlock_orders.py as a self-contained CLI worker script.
Follow the step-by-step logic in section 4.4 of the build plan.
Use the selector strategy table as the guide for every WebDriverWait call.
Implement is_login_page(), do_login(), select_all_orders(), choose_unlock_status(), 
click_apply(), click_ok_on_modal(), verify_update_complete() as separate functions.
The main run() function calls them in order with full try/except/finally.
At the end of run(), always call write_result_payload() from automation_runtime.
Support --dry-run flag that skips the OK button click.
```

### Step 4 — Build server.py
```
Write server.py as a Flask server on port 5124 (separate from existing automation on 5123).
Implement routes: POST /crm/unlock, POST /crm/unlock/dry-run, GET /crm/status, GET /crm/state, GET /ui, GET /health.
Implement /api/config GET and POST that read/write uppercase assignments from config.py.
Use threading.Lock() for crm_lock.
On POST /crm/unlock: acquire lock, delete last_result.json, run subprocess, read result, 
update crm_state.json, write audit entry, return JSON response.
On startup: load config, kill port 5124 if occupied, start Flask in background thread,
run pystray tray icon in main thread with Quit option.
```

### Step 5 — Build ui_panel.html
```
Write a single-page HTML control panel for the CRM automation.
Include a header "CRM Order Automation", status display, two buttons (Unlock All Orders / Dry Run),
and a run history table showing last 10 runs with timestamp, order count, and status.
All buttons use fetch() to call the Flask API.
Poll GET /crm/status every 2 seconds while a run is in progress (use a running flag in response).
Style to match the existing automation UI panel aesthetic (dark theme).
```

### Step 6 — Build start_server_hidden.vbs
```
Write start_server_hidden.vbs that runs pythonw.exe server.py with no console window.
```

### Step 7 — First run test
```
Run: python crm_unlock_orders.py --action unlock_all --dry-run
Confirm it: navigates to report, logs in if needed, selects all orders, opens Order Preview,
selects the dropdown option, clicks Apply, but does NOT click OK (dry run).
Check screenshots/ folder and last_result.json for results.
```

---

## 8. DOM Inspection Notes for Codex

Before writing final selectors, Codex should be instructed to:

1. Run the script in visible mode (`CRM_HEADLESS = False`) on the report URL
2. Pause after login and use `driver.page_source` to inspect actual element classes
3. The CRM uses a legacy UI — class names may be Bootstrap-based (e.g., `btn`, `dropdown-toggle`, `dropdown-menu`)
4. The Order Preview panel likely has a unique container ID or class like `#order-preview` or `.order-preview-panel`
5. Row selection may use `tr.selected` or a data attribute — confirm by clicking manually and inspecting

**Suggested DOM discovery script (add to worker for debug mode):**
```python
if DEBUG_MODE:
    # After page load, dump rows
    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    print(f"Found {len(rows)} rows. First row classes: {rows[0].get_attribute('class')}")
    print(f"First row HTML: {rows[0].get_attribute('outerHTML')[:300]}")
```

---

## 9. Phase 2+ Extension Points

This architecture is designed to support future actions. Each new action follows the same pattern:

| Future Action | New Worker Script | New Route |
|---|---|---|
| Lock Stock Auto Ordering | `crm_lock_orders.py` | `/crm/lock` |
| Mark orders as Stock Ordered | `crm_mark_ordered.py` | `/crm/mark-ordered` |
| Export order list to CSV | `crm_export.py` | `/crm/export` |
| Process a different report URL | Config change only | Reuse existing routes with `?report=X` |
| Scheduled auto-run | `threading.Timer` in `server.py` | `/crm/schedule`, `/crm/cancel-schedule` |

To add a new action, only these things change:
1. New worker script (copy `crm_unlock_orders.py` as template)
2. New config keys in `config.py`
3. New route + wrapper in `server.py`
4. New button in `ui_panel.html`

---

## 10. Dependency Checklist

Run before starting:
```bash
pip install selenium webdriver-manager flask pystray Pillow
```

Also ensure:
- Chrome browser is installed
- ChromeDriver version matches Chrome version (webdriver-manager handles this automatically)
- Port `5124` is not in use

---

## 11. Critical Gotchas

| Gotcha | Mitigation |
|---|---|
| Dropdown MUST be clicked (not just typed into) | `click()` the dropdown toggle button first, then send_keys to filter |
| Shift+Click may not work if rows aren't standard `<tr>` | Inspect actual DOM; may need JavaScript click with shift modifier |
| Chrome profile saves login session — no re-login on second run | Login detection handles this gracefully |
| Report URL contains `_token` — token may expire | Store token in config; if page shows error, re-authenticate and retry |
| `Update complete` may appear briefly — poll until stable | Use `WebDriverWait` presence check, not timing |
| Modal OK button may be covered during animation | Add small `time.sleep(0.5)` or use `EC.element_to_be_clickable` |
| Running two instances simultaneously breaks Chrome profile | `crm_lock` prevents this |

---

*End of Phase 1 Build Plan — crm_automation v1.0*
