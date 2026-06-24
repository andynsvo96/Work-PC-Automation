"""
Shared Selenium/runtime helpers for local automation scripts.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager

from automation_audit import log_automation_result

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = os.path.join(SCRIPT_DIR, "last_result.json")
STATUS_FILE = os.path.join(SCRIPT_DIR, "automation_status.json")

FAILURE_SCREENSHOT_MARKERS = (
    "error",
    "fail",
    "failed",
    "not_found",
    "login_required",
    "retry",
    "tiny_viewport",
    "stopped",
)
SUCCESS_SCREENSHOT_MARKERS = (
    "success",
    "sent",
    "already_",
    "dry_run_detected",
    "loaded",
)


def configure_console_utf8():
    """Ensure stdout/stderr can print Unicode emitted by web pages."""
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def write_result_payload(
    automation_name,
    source,
    success,
    message,
    extra_fields=None,
    result_file=RESULT_FILE,
    audit_log=True,
):
    """Persist the canonical automation result payload and audit outcome."""
    payload = {
        "success": bool(success),
        "message": str(message),
    }
    if isinstance(extra_fields, dict):
        for key, value in extra_fields.items():
            if value is not None:
                payload[str(key)] = value

    target_file = result_file or RESULT_FILE
    parent_dir = os.path.dirname(os.path.abspath(target_file))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    last_error = None
    for attempt in range(6):
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".tmp", delete=False, encoding="utf-8", dir=parent_dir or None) as handle:
                temp_path = handle.name
                json.dump(payload, handle)
            os.replace(temp_path, target_file)
            last_error = None
            break
        except OSError as err:
            last_error = err
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            time.sleep(0.25 * (attempt + 1))
    if last_error is not None:
        with open(target_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    if audit_log:
        try:
            log_automation_result(automation_name, success, message, source=source)
        except Exception:
            pass

    return payload


def write_status_payload(
    automation_name,
    message,
    *,
    stage=None,
    current=None,
    total=None,
    order_id=None,
    extra_fields=None,
    status_file=None,
):
    """Persist a small live-status payload for the server/UI to poll."""
    target_file = status_file or os.environ.get("AUTOMATION_STATUS_FILE") or STATUS_FILE
    parent_dir = os.path.dirname(os.path.abspath(target_file))
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    pid = os.getpid()
    existing = {}
    try:
        if os.path.exists(target_file):
            with open(target_file, "r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if (
                isinstance(loaded, dict)
                and str(loaded.get("automation_name") or "") == str(automation_name or "")
                and str(loaded.get("pid") or "") == str(pid)
            ):
                existing = loaded
    except Exception:
        existing = {}

    payload = {
        "automation_name": str(automation_name or ""),
        "message": str(message or ""),
        "updated_at": datetime.now().isoformat(),
        "pid": pid,
    }
    if stage:
        payload["stage"] = str(stage)
    if order_id:
        payload["order_id"] = str(order_id)
    elif existing.get("order_id"):
        payload["order_id"] = str(existing.get("order_id"))

    progress = existing.get("progress") if isinstance(existing.get("progress"), dict) else {}
    progress_current = current if current is not None else progress.get("current")
    progress_total = total if total is not None else progress.get("total")
    try:
        progress_current = int(progress_current)
        progress_total = int(progress_total)
    except Exception:
        progress_current = None
        progress_total = None
    if progress_total is not None and progress_total > 0 and progress_current is not None:
        progress_current = max(0, min(progress_current, progress_total))
        payload["progress"] = {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}",
        }

    if isinstance(extra_fields, dict):
        for key, value in extra_fields.items():
            if value is not None:
                payload[str(key)] = value

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".tmp", delete=False, encoding="utf-8", dir=parent_dir or None) as handle:
            temp_path = handle.name
            json.dump(payload, handle)
        os.replace(temp_path, target_file)
    except Exception:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
    return payload


def _runtime_config_bool(name, default=False):
    try:
        import config as runtime_config
        return bool(getattr(runtime_config, name, default))
    except Exception:
        return bool(default)


def _screenshot_allowed(name):
    if _runtime_config_bool("AUTOMATION_DEBUG_SCREENSHOTS", False):
        return True
    lowered = str(name or "").lower()
    failure_like = any(marker in lowered for marker in FAILURE_SCREENSHOT_MARKERS)
    success_like = any(marker in lowered for marker in SUCCESS_SCREENSHOT_MARKERS)
    if failure_like:
        return _runtime_config_bool("AUTOMATION_SCREENSHOTS_ON_FAILURE", True)
    if success_like:
        return _runtime_config_bool("AUTOMATION_SCREENSHOTS_ON_SUCCESS", False)
    return True


def take_screenshot(driver, name, screenshots_dir=None):
    if not _screenshot_allowed(name):
        print(f"Screenshot skipped by policy: {name}")
        return None
    if not screenshots_dir:
        screenshots_dir = os.path.join(SCRIPT_DIR, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    path = os.path.join(
        screenshots_dir,
        f"screenshot_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
    )
    driver.save_screenshot(path)
    print(f"Screenshot saved: {path}")
    return path


def safe_take_screenshot(driver, name, timeout=8, screenshots_dir=None):
    """Capture screenshot without allowing this step to block script shutdown."""
    done = {"finished": False, "error": None}

    def _capture():
        try:
            take_screenshot(driver, name, screenshots_dir=screenshots_dir)
        except Exception as err:
            done["error"] = err
        finally:
            done["finished"] = True

    t = threading.Thread(target=_capture, daemon=True)
    t.start()
    t.join(timeout)

    if not done["finished"]:
        print(f"Warning: screenshot capture timed out after {timeout}s ({name}).")
        return False
    if done["error"] is not None:
        print(f"Warning: screenshot capture failed ({name}): {done['error']}")
        return False
    return True


def _kill_process(pid):
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as err:
        return False, str(err)
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout or "").strip()
    if not detail:
        detail = f"taskkill exit code {result.returncode}"
    lowered = detail.lower()
    if "not found" in lowered or "no running instance" in lowered:
        # Another taskkill in this pass may have already removed it.
        return True, detail
    return False, detail


def _collect_chrome_process_entries_with_wmic():
    wmic_bin = shutil.which("wmic") or shutil.which("wmic.exe")
    if not wmic_bin:
        return None

    result = subprocess.run(
        [wmic_bin, "process", "where", "name='chrome.exe'", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    entries = []
    current_pid = None
    current_cmd = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("CommandLine="):
            current_cmd = line[len("CommandLine="):]
        elif line.startswith("ProcessId="):
            current_pid = line[len("ProcessId="):]
            if current_pid:
                entries.append((current_pid.strip(), (current_cmd or "").strip()))
            current_pid = None
            current_cmd = None
    return entries


def _collect_chrome_process_entries_with_powershell():
    powershell_bin = shutil.which("powershell") or shutil.which("powershell.exe")
    if not powershell_bin:
        return None

    # Use a temp file to avoid quoting/escaping issues in inline one-liners.
    script_path = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
            script_path = handle.name
            handle.write(
                "$ErrorActionPreference = 'SilentlyContinue'\n"
                "$rows = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Select-Object ProcessId, CommandLine\n"
                "if (-not $rows) { return }\n"
                "$rows | ForEach-Object {\n"
                "  $procId = $_.ProcessId\n"
                "  $cmd = $_.CommandLine\n"
                "  if ($null -eq $cmd) { $cmd = '' }\n"
                "  Write-Output (\"{0}`t{1}\" -f $procId, $cmd)\n"
                "}\n"
            )

        result = subprocess.run(
            [powershell_bin, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        if script_path and os.path.exists(script_path):
            try:
                os.remove(script_path)
            except Exception:
                pass

    if result.returncode not in (0,):
        return []

    entries = []
    for line in (result.stdout or "").splitlines():
        row = line.strip()
        if not row:
            continue
        pid_text, sep, cmdline = row.partition("\t")
        if not sep:
            continue
        pid_text = pid_text.strip()
        if pid_text:
            entries.append((pid_text, cmdline.strip()))
    return entries


def _normalize_profile_path_for_match(path):
    text = str(path or "").strip().strip("\"'")
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(os.path.abspath(text)))


def _chrome_cmdline_profile_path(cmdline):
    text = str(cmdline or "")
    match = re.search(r"--user-data-dir(?:=|\s+)(\"[^\"]+\"|'[^']+'|[^\s]+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return _normalize_profile_path_for_match(match.group(1))


def _chrome_cmdline_uses_profile(cmdline, profile_path):
    expected = _normalize_profile_path_for_match(profile_path)
    actual = _chrome_cmdline_profile_path(cmdline)
    return bool(expected and actual and actual == expected)


def kill_stale_chrome(profile_path, profile_label="automation"):
    """Kill Chrome processes tied to the given Selenium profile path only."""
    try:
        entries = _collect_chrome_process_entries_with_wmic()
        source_name = "wmic"
        if entries is None:
            entries = _collect_chrome_process_entries_with_powershell()
            source_name = "powershell/cim"
        if entries is None:
            print("Stale Chrome check skipped: neither wmic nor PowerShell is available on this system.")
            return 0
    except Exception as err:
        print(f"Warning: could not check for stale Chrome: {err}")
        return 0

    matched = 0
    killed = 0
    failed = []
    for pid_text, cmdline in entries:
        try:
            if cmdline and _chrome_cmdline_uses_profile(cmdline, profile_path):
                matched += 1
                ok, detail = _kill_process(pid_text)
                if ok:
                    killed += 1
                else:
                    failed.append((str(pid_text), detail))
        except Exception:
            continue

    if killed:
        print(f"Killed {killed} stale Chrome process(es) from {profile_label} profile.")
        time.sleep(1)
    elif matched:
        print(
            f"Matched {matched} stale Chrome process(es) for {profile_label} profile, "
            "but none could be terminated."
        )
    else:
        print(f"Stale Chrome check complete via {source_name}; no matching profile processes found.")
    if failed:
        print(
            "Warning: failed to terminate some Chrome processes. "
            "This can leave profile files locked.\n"
            + "\n".join([f"  PID {pid}: {detail}" for pid, detail in failed[:5]])
        )
        if len(failed) > 5:
            print(f"  ... and {len(failed) - 5} more")
    return killed


def safe_driver_quit(driver, profile_path=None, timeout=8):
    """Quit WebDriver with timeout; force cleanup if driver.quit() hangs."""
    if driver is None:
        return

    done = {"finished": False}

    def _quit():
        try:
            driver.quit()
        except Exception:
            pass
        finally:
            done["finished"] = True

    t = threading.Thread(target=_quit, daemon=True)
    t.start()
    t.join(timeout)

    if done["finished"]:
        return

    print(f"Warning: driver.quit() timed out after {timeout}s; forcing cleanup.")
    try:
        service = getattr(driver, "service", None)
        proc = getattr(service, "process", None)
        if proc and proc.poll() is None:
            proc.kill()
    except Exception:
        pass

    if profile_path:
        kill_stale_chrome(profile_path)


def find_visible(driver, css_selectors, timeout=3):
    """Try multiple CSS selectors combined into one query, return the first visible element found."""
    combined = ", ".join(css_selectors)
    try:
        return WebDriverWait(driver, timeout).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, combined))
        )
    except TimeoutException:
        return None


def _is_renderer_timeout(error):
    return "timed out receiving message from renderer" in str(error).lower()


CRM_CHALLENGE_ATTEMPTS_EXCEEDED_TEXT = "max challenge attempts exceeded"
CRM_CHALLENGE_REFRESH_HINT_TEXT = "please refresh the page to try again"


def _page_text(driver, top_level=True):
    if top_level:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

    try:
        return str(
            driver.execute_script(
                "return String((document.body && (document.body.innerText || document.body.textContent)) || '');"
            )
            or ""
        )
    except Exception:
        pass

    try:
        return str(driver.find_element(By.TAG_NAME, "body").text or "")
    except Exception:
        return ""


def crm_challenge_attempts_exceeded(driver, top_level=True):
    text = " ".join(_page_text(driver, top_level=top_level).lower().split())
    return (
        CRM_CHALLENGE_ATTEMPTS_EXCEEDED_TEXT in text
        and CRM_CHALLENGE_REFRESH_HINT_TEXT in text
    )


def refresh_if_crm_challenge_attempts_exceeded(driver, label="CRM page", cooldown_seconds=5, top_level=True):
    if not crm_challenge_attempts_exceeded(driver, top_level=top_level):
        return False

    now = time.monotonic()
    last_refresh = float(getattr(driver, "_crm_challenge_last_refresh", 0) or 0)
    if last_refresh and now - last_refresh < max(0, float(cooldown_seconds or 0)):
        return True

    setattr(driver, "_crm_challenge_last_refresh", now)
    print(f"CRM challenge attempts exceeded while loading {label}; refreshing page.")
    try:
        driver.refresh()
    except TimeoutException as err:
        print(f"Warning: timeout while refreshing {label} after CRM challenge page: {err}")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    except Exception as err:
        if _is_renderer_timeout(err):
            print(f"Warning: renderer timeout while refreshing {label} after CRM challenge page: {err}")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        else:
            raise
    time.sleep(1)
    return True


def safe_get_with_partial_load(driver, url, label):
    """Navigate and continue with a partial load on known renderer/page-load timeouts."""
    try:
        driver.get(url)
        refresh_if_crm_challenge_attempts_exceeded(driver, label)
        return True
    except TimeoutException as err:
        print(f"Warning: timeout while opening {label}: {err}")
    except Exception as err:
        if _is_renderer_timeout(err):
            print(f"Warning: renderer timeout while opening {label}: {err}")
        else:
            raise

    try:
        driver.execute_script("window.stop();")
        print(f"Continuing with partially loaded page for {label}.")
    except Exception:
        pass
    refresh_if_crm_challenge_attempts_exceeded(driver, label)
    return False


def build_chrome_driver(
    profile_path,
    headless_mode=False,
    page_load_strategy=None,
    page_load_timeout=None,
    script_timeout=None,
    extra_args=None,
):
    options = Options()
    options.add_argument(f"--user-data-dir={profile_path}")
    if headless_mode:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    # Helps avoid sporadic DevToolsActivePort startup errors on Windows.
    options.add_argument("--remote-debugging-port=0")

    if isinstance(extra_args, (list, tuple)):
        for arg in extra_args:
            text = str(arg or "").strip()
            if text:
                options.add_argument(text)

    if page_load_strategy:
        options.page_load_strategy = page_load_strategy

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    os.environ["WDM_LOCAL"] = "1"
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    if page_load_timeout:
        driver.set_page_load_timeout(page_load_timeout)
    if script_timeout:
        driver.set_script_timeout(script_timeout)
    return driver


def build_attached_chrome_driver(debugger_address="127.0.0.1:9222"):
    options = Options()
    options.add_experimental_option("debuggerAddress", debugger_address)

    os.environ["WDM_LOCAL"] = "1"
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)
