"""
Shared Selenium/runtime helpers for local automation scripts.
"""

import json
import os
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


def take_screenshot(driver, name, screenshots_dir=None):
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


def kill_stale_chrome(profile_path, profile_label="automation"):
    """Kill Chrome processes tied to the given Selenium profile path only."""
    profile_path_lower = os.path.normpath(profile_path).lower()
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
            if cmdline and profile_path_lower in cmdline.lower():
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


def safe_get_with_partial_load(driver, url, label):
    """Navigate and continue with a partial load on known renderer/page-load timeouts."""
    try:
        driver.get(url)
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
