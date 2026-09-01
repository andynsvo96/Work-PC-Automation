"""
Shared Selenium/runtime helpers for local automation scripts.
"""

import json
import os
import re
import shutil
import signal
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

try:
    import psutil
except Exception:
    psutil = None

from automation_audit import log_automation_result
from runtime_paths import SCREENSHOTS_DIR, result_file, state_file

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULT_FILE = result_file("last_result.json")
STATUS_FILE = state_file("automation_status.json")
PLATFORM_PROFILE_ROOT = os.path.join(SCRIPT_DIR, "runtime", "browser_profiles")
LEGACY_PROFILE_FALLBACK_ENV = "AUTOMATION_USE_LEGACY_PROFILES"
MACOS_PAYCOM_KEYCHAIN_PROFILE = "chrome_profile_keychain_v2"
MAILTO_CLICK_GUARD_SCRIPT = r"""
(function () {
  if (window.__automationMailtoClickGuardInstalled) return;
  window.__automationMailtoClickGuardInstalled = true;

  const CRM_HOST_RE = /(^|\.)crm2\.legacy\.printfly\.com$/i;
  const BLOCKED_CRM_TEXT_RE = /\b(email\s+customer|send\s+invoice)\b/i;
  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function textOf(node) {
    if (!node) return "";
    return [
      node.innerText,
      node.textContent,
      node.value,
      node.getAttribute && node.getAttribute("aria-label"),
      node.getAttribute && node.getAttribute("title"),
      node.getAttribute && node.getAttribute("data-original-title")
    ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  }

  function closestInteractive(node) {
    for (let cur = node; cur && cur !== document; cur = cur.parentElement) {
      if (cur.matches && cur.matches("a[href],button,input,[role='button'],[onclick],[ng-click],.btn,[class*='btn']")) {
        return cur;
      }
    }
    return null;
  }

  function shouldBlock(target) {
    const interactive = closestInteractive(target);
    if (!interactive) return false;
    // A workflow can deliberately opt in to a single CRM control when it has
    // its own safety checks for the resulting dialog.  This is used to open
    // the invoice dialog only long enough to copy its View Invoice link and
    // then cancel it; it never authorizes a mailto link or the dialog's Send
    // action.
    if (interactive.dataset && interactive.dataset.automationAllowClick === "true") return false;
    const href = String(interactive.getAttribute && interactive.getAttribute("href") || "");
    if (/^\s*mailto:/i.test(href)) return true;
    if (!CRM_HOST_RE.test(window.location.hostname || "")) return false;
    const text = textOf(interactive);
    return BLOCKED_CRM_TEXT_RE.test(text) || EMAIL_RE.test(text);
  }

  document.addEventListener("click", function (event) {
    if (!shouldBlock(event.target)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    console.warn("Automation blocked a customer email/mailto click.");
  }, true);
})();
"""

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


def install_mailto_click_guard(driver):
    """Block accidental browser handoff to the OS email client."""
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": MAILTO_CLICK_GUARD_SCRIPT},
        )
    except Exception:
        pass
    try:
        driver.execute_script(MAILTO_CLICK_GUARD_SCRIPT)
    except Exception:
        pass


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
        screenshots_dir = SCREENSHOTS_DIR
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
    if os.name != "nt":
        if psutil is not None:
            try:
                process = psutil.Process(int(pid))
                process.terminate()
                process.wait(timeout=8)
                return True, ""
            except psutil.NoSuchProcess:
                return True, "Process was already stopped."
            except Exception as err:
                return False, str(err)
        try:
            process_id = int(pid)
            os.kill(process_id, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                try:
                    os.kill(process_id, 0)
                except ProcessLookupError:
                    return True, ""
                time.sleep(0.2)
            return False, "Timed out waiting for the Chrome process to exit."
        except ProcessLookupError:
            return True, "Process was already stopped."
        except Exception as err:
            return False, str(err)
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


def _collect_chrome_process_entries_with_psutil():
    if psutil is None:
        return None
    entries = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = str(process.info.get("name") or "").lower()
            if "chrome" not in name and "google chrome" not in name:
                continue
            cmdline = " ".join(str(value) for value in (process.info.get("cmdline") or []))
            entries.append((str(process.info.get("pid")), cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return entries


def _collect_chrome_process_entries_with_posix_ps():
    """Collect Chrome process command lines on macOS/Linux without psutil."""
    if os.name == "nt":
        return None
    ps_bin = shutil.which("ps")
    if not ps_bin:
        return None
    try:
        result = subprocess.run(
            [ps_bin, "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return []
    entries = []
    for line in (result.stdout or "").splitlines():
        pid_text, _separator, command = line.strip().partition(" ")
        if pid_text.isdigit() and command:
            entries.append((pid_text, command.strip()))
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


def _platform_profile_name(system_name=None):
    """Return a stable directory name for local, OS-specific browser state."""
    raw = str(system_name or sys.platform or "").strip().lower()
    if raw in {"darwin", "mac", "macos"}:
        return "macos"
    if raw.startswith("win"):
        return "windows"
    if raw.startswith("linux"):
        return "linux"
    return raw or "unknown"


def _legacy_profile_fallback_enabled(system_name=None):
    """Return whether this machine should retain its pre-isolation profiles.

    The environment variable is useful for one-off launches.  The matching
    config setting makes the rollback survive normal Windows app restarts.
    Explicit ``system_name`` callers are test/migration helpers, so they only
    honor the environment override and are not affected by this machine's
    local config.
    """
    value = os.getenv(LEGACY_PROFILE_FALLBACK_ENV, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if system_name is not None:
        return False
    try:
        import config as runtime_config
    except Exception:
        return False
    configured = getattr(runtime_config, LEGACY_PROFILE_FALLBACK_ENV, False)
    if isinstance(configured, str):
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return bool(configured)


def resolve_automation_profile_path(profile_path, *, system_name=None, profiles_root=None):
    """Resolve persistent automation profiles into local OS-specific folders.

    Only top-level, repository-managed profile directories are remapped.  This
    leaves temporary/parallel worker profiles and explicit external paths
    untouched.  The original top-level profile is never moved or deleted, so
    setting ``AUTOMATION_USE_LEGACY_PROFILES=1`` is an immediate rollback.
    """
    legacy_path = _normalize_profile_path_for_match(profile_path)
    if not legacy_path:
        return legacy_path
    if _legacy_profile_fallback_enabled(system_name=system_name):
        return legacy_path

    try:
        relative_path = os.path.relpath(legacy_path, SCRIPT_DIR)
    except ValueError:
        return legacy_path
    if os.path.dirname(relative_path) or relative_path.startswith(".."):
        return legacy_path

    profile_name = os.path.basename(relative_path)
    if profile_name != "slack_chrome_profile" and not profile_name.startswith("chrome_profile"):
        return legacy_path

    platform_name = _platform_profile_name(system_name)
    storage_name = profile_name
    if platform_name == "macos" and profile_name == "chrome_profile":
        # ChromeDriver normally injects --use-mock-keychain on macOS. Profiles
        # authenticated in native Chrome then cannot reliably reuse the same
        # encrypted Paycom device/session state. Keep the previous Mac profile
        # untouched and initialize a real-Keychain generation for Paycom only.
        storage_name = MACOS_PAYCOM_KEYCHAIN_PROFILE
    root = os.path.abspath(profiles_root or os.getenv("AUTOMATION_PROFILE_ROOT") or PLATFORM_PROFILE_ROOT)
    return os.path.join(root, platform_name, storage_name)


def _uses_macos_paycom_keychain(profile_path):
    return _platform_profile_name() == "macos" and os.path.basename(
        _normalize_profile_path_for_match(profile_path)
    ) == MACOS_PAYCOM_KEYCHAIN_PROFILE


def resolve_existing_automation_profile_path(profile_path, *, system_name=None, profiles_root=None):
    """Use the OS-specific profile when initialized, otherwise retain legacy state.

    Parallel workers clone an existing signed-in profile before starting Chrome.
    This compatibility helper lets those workers continue using the untouched
    legacy profile until the new per-OS profile has been set up once.
    """
    legacy_path = _normalize_profile_path_for_match(profile_path)
    resolved_path = resolve_automation_profile_path(
        legacy_path,
        system_name=system_name,
        profiles_root=profiles_root,
    )
    if resolved_path != legacy_path and not os.path.isdir(resolved_path) and os.path.isdir(legacy_path):
        return legacy_path
    return resolved_path


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


def is_chrome_profile_in_use(profile_path):
    """Return whether a running Chrome process owns this exact profile path.

    Unlike ``kill_stale_chrome``, this is read-only and is intended for setup
    flows that must never interrupt an operator's visible login session.
    """
    profile_path = resolve_automation_profile_path(profile_path)
    try:
        entries = _collect_chrome_process_entries_with_psutil()
        if entries is None:
            entries = _collect_chrome_process_entries_with_posix_ps()
        if entries is None:
            entries = _collect_chrome_process_entries_with_wmic()
        if entries is None:
            entries = _collect_chrome_process_entries_with_powershell()
    except Exception:
        return False
    return any(cmdline and _chrome_cmdline_uses_profile(cmdline, profile_path) for _pid, cmdline in (entries or []))


def kill_stale_chrome(profile_path, profile_label="automation"):
    """Kill Chrome processes tied to the given Selenium profile path only."""
    profile_path = resolve_automation_profile_path(profile_path)
    try:
        entries = _collect_chrome_process_entries_with_psutil()
        source_name = "psutil"
        if entries is None:
            entries = _collect_chrome_process_entries_with_posix_ps()
            source_name = "posix ps"
        if entries is None:
            entries = _collect_chrome_process_entries_with_wmic()
            source_name = "wmic"
        if entries is None:
            entries = _collect_chrome_process_entries_with_powershell()
            source_name = "powershell/cim"
        if entries is None:
            print("Stale Chrome check skipped: no supported process inspector is available on this system.")
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


def safe_driver_quit(driver, profile_path=None, timeout=8, keep_browser_open=False):
    """Quit WebDriver with timeout; force cleanup if driver.quit() hangs."""
    if driver is None:
        return

    if keep_browser_open or os.getenv("AUTOMATION_KEEP_BROWSER_OPEN", "").strip().lower() in ("1", "true", "yes", "on"):
        # Do not send WebDriver's QUIT command in visible debugging mode.
        # Chrome's detach option alone is not honored consistently by every
        # ChromeDriver build, while this leaves the final browser state intact.
        print("Leaving the visible browser open for operator setup or inspection.")
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
CRM_AUTHENTICATION_ERROR_TEXTS = (
    "not authenticated",
    "not authorized",
)
CRM_HOST_TEXT = "crm2.legacy.printfly.com"


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


def _crm_page_texts(driver, include_frames=True, max_frame_depth=2):
    """Return top-level and same-session frame text while restoring top-level context."""
    texts = []
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    def _collect(depth):
        texts.append(_page_text(driver, top_level=False))
        if not include_frames or depth >= max_frame_depth:
            return

        try:
            frames = list(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
        except Exception:
            return

        for frame in frames:
            switched = False
            try:
                driver.switch_to.frame(frame)
                switched = True
                _collect(depth + 1)
            except Exception:
                pass
            finally:
                if not switched:
                    continue
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass

    try:
        _collect(0)
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return texts


def crm_authentication_error(driver, top_level=True):
    """Detect the transient CRM Error / Not authenticated modal, including in frames."""
    if top_level:
        texts = _crm_page_texts(driver, include_frames=True)
    else:
        texts = [_page_text(driver, top_level=False)]

    for page_text in texts:
        normalized = " ".join(str(page_text or "").lower().split())
        if any(marker in normalized for marker in CRM_AUTHENTICATION_ERROR_TEXTS):
            return True
    return False


def _crm_recoverable_error_reason(driver, top_level=True):
    if crm_challenge_attempts_exceeded(driver, top_level=top_level):
        return "challenge attempts exceeded"

    try:
        current_url = str(driver.current_url or "").lower()
    except Exception:
        current_url = ""
    if CRM_HOST_TEXT in current_url and crm_authentication_error(driver, top_level=top_level):
        return "authentication error"
    return ""


def refresh_if_crm_recoverable_error(driver, label="CRM page", cooldown_seconds=5, top_level=True):
    reason = _crm_recoverable_error_reason(driver, top_level=top_level)
    if not reason:
        return False

    now = time.monotonic()
    last_refresh = float(getattr(driver, "_crm_recovery_last_refresh", 0) or 0)
    if last_refresh and now - last_refresh < max(0, float(cooldown_seconds or 0)):
        return True

    setattr(driver, "_crm_recovery_last_refresh", now)
    print(f"CRM {reason} while loading {label}; refreshing page.")
    try:
        driver.refresh()
    except TimeoutException as err:
        print(f"Warning: timeout while refreshing {label} after CRM {reason}: {err}")
        try:
            driver.execute_script("window.stop();")
        except Exception:
            pass
    except Exception as err:
        if _is_renderer_timeout(err):
            print(f"Warning: renderer timeout while refreshing {label} after CRM {reason}: {err}")
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass
        else:
            raise
    time.sleep(1)
    return True


def refresh_if_crm_challenge_attempts_exceeded(driver, label="CRM page", cooldown_seconds=5, top_level=True):
    """Backward-compatible entry point for all transient CRM error refreshes."""
    return refresh_if_crm_recoverable_error(
        driver,
        label=label,
        cooldown_seconds=cooldown_seconds,
        top_level=top_level,
    )


def safe_get_with_partial_load(driver, url, label):
    """Navigate and continue with a partial load on known renderer/page-load timeouts."""
    try:
        driver.get(url)
        refresh_if_crm_recoverable_error(driver, label)
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
    refresh_if_crm_recoverable_error(driver, label)
    return False


def build_chrome_driver(
    profile_path,
    headless_mode=False,
    page_load_strategy=None,
    page_load_timeout=None,
    script_timeout=None,
    extra_args=None,
    detach=False,
):
    legacy_profile_path = _normalize_profile_path_for_match(profile_path)
    profile_path = resolve_automation_profile_path(profile_path)
    if profile_path != legacy_profile_path and not os.path.exists(profile_path):
        print(
            f"Initializing local {_platform_profile_name()} Chrome profile: {profile_path}. "
            f"Legacy profile remains unchanged at {legacy_profile_path}; set "
            f"{LEGACY_PROFILE_FALLBACK_ENV}=1 to roll back."
        )
    os.makedirs(profile_path, exist_ok=True)
    options = Options()
    options.add_argument(f"--user-data-dir={profile_path}")
    if headless_mode:
        options.add_argument("--headless=new")
        options.add_argument("--window-position=-32000,-32000")
    elif detach or os.getenv("AUTOMATION_KEEP_BROWSER_OPEN", "").strip().lower() in ("1", "true", "yes", "on"):
        # ChromeDriver honors this option when quit() is called, allowing a
        # visible debugging session to remain open after an automation ends.
        options.add_experimental_option("detach", True)
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

    excluded_switches = ["enable-automation"]
    if _uses_macos_paycom_keychain(profile_path):
        # Let Chrome use Apple's real Keychain for Paycom cookies and device
        # trust, matching a normal native Chrome launch. This does not disable
        # Paycom MFA or hCaptcha and does not affect Windows.
        excluded_switches.extend(["use-mock-keychain", "password-store"])
    options.add_experimental_option("excludeSwitches", excluded_switches)
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option(
        "prefs",
        {
            "protocol_handler.excluded_schemes": {"mailto": True},
            "profile.default_content_setting_values.protocol_handlers": 2,
        },
    )

    os.environ["WDM_LOCAL"] = "1"
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    install_mailto_click_guard(driver)

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
    driver = webdriver.Chrome(service=service, options=options)
    install_mailto_click_guard(driver)
    return driver
