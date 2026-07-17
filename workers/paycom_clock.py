"""
Paycom Clock In/Out Automation
Usage:
    python paycom_clock.py in [--dry-run|--real]
    python paycom_clock.py out [--dry-run|--real]
"""

import sys
import os
import time

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from automation_audit import log_automation_event, log_automation_result
from automation_runtime import (
    SCRIPT_DIR,
    build_chrome_driver,
    configure_console_utf8,
    find_visible,
    kill_stale_chrome,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
from config import (
    PAYCOM_URL,
    PAYCOM_DRY_RUN as CONFIG_PAYCOM_DRY_RUN,
)
from credential_store import PAYCOM_CREDENTIAL_TARGET, read_windows_credential

configure_console_utf8()

AUDIT_AUTOMATION_NAME = "paycom_clock.unknown"


def write_result(success, message):
    write_result_payload(
        AUDIT_AUTOMATION_NAME,
        "paycom_clock.py",
        success,
        message,
    )


def find_punch_button(driver, btn_text, timeout=10):
    """Find a clock punch button using multiple strategies.

    Uses normalize-space(.) instead of text() to match text inside nested elements
    (e.g. <button><span>Out Day</span></button>).
    """
    # Strategy 1: Exact match on normalized descendant text
    try:
        el = WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.XPATH,
                f"//*[(self::button or self::a or self::input or self::div or self::span) "
                f"and normalize-space(.)='{btn_text}']"
            ))
        )
        print(f"Found '{btn_text}' via exact XPath match.")
        return el
    except TimeoutException:
        pass

    # Strategy 2: Partial match on normalized descendant text (innermost clickable)
    try:
        el = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.XPATH,
                f"//*[contains(normalize-space(.), '{btn_text}') "
                f"and (self::button or self::a or self::input or self::div or self::span) "
                f"and not(ancestor::*[contains(normalize-space(.), '{btn_text}') "
                f"and (self::button or self::a or self::input or self::div or self::span)])]"
            ))
        )
        print(f"Found '{btn_text}' via partial XPath match.")
        return el
    except TimeoutException:
        pass

    # Strategy 3: CSS fallback - find all clickable elements, filter by visible text
    try:
        elements = driver.find_elements(By.CSS_SELECTOR,
            "button, a, [role='button'], input[type='button'], input[type='submit']"
        )
        for el in elements:
            if el.is_displayed() and btn_text.lower() in el.text.lower():
                print(f"Found '{btn_text}' via CSS text fallback.")
                return el
    except Exception:
        pass

    return None


def detect_clock_state(driver):
    """Read the 'Last Punch' text to determine current clock state.
    Returns (state, full_text) where state is 'in', 'out', or None.
    """
    try:
        # Find all elements containing 'Last Punch' and pick the smallest (most specific) one
        elements = driver.find_elements(By.XPATH,
            "//*[contains(normalize-space(.), 'Last Punch')]"
        )
        # Sort by text length to get the most specific element (not a parent container)
        elements = [el for el in elements if el.text.strip()]
        if elements:
            elements.sort(key=lambda el: len(el.text))
            text = elements[0].text.strip()
            # Extract just the "Last Punch - ..." line
            for line in text.splitlines():
                if "Last Punch" in line:
                    text = line.strip()
                    break
            if "In Day" in text:
                return "in", text
            elif "Out Day" in text:
                return "out", text
            return None, text
    except Exception:
        pass
    return None, ""


def dump_page_state(driver):
    """Log the current page state for debugging."""
    info = []
    try:
        info.append(f"URL: {driver.current_url}")
    except Exception:
        info.append("URL: (unable to read)")
    try:
        info.append(f"Title: {driver.title}")
    except Exception:
        info.append("Title: (unable to read)")
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        if len(body_text) > 500:
            body_text = body_text[:500] + "..."
        info.append(f"Body text: {body_text}")
    except Exception:
        info.append("Body text: (unable to read)")
    try:
        clickables = driver.find_elements(By.CSS_SELECTOR,
            "button, a, [role='button'], input[type='button'], input[type='submit']"
        )
        visible = [(el.tag_name, el.text.strip()) for el in clickables
                   if el.is_displayed() and el.text.strip()]
        info.append(f"Visible clickable elements: {visible[:20]}")
    except Exception:
        info.append("Visible clickable elements: (unable to read)")
    return "\n".join(info)


def is_paycom_login_page(driver):
    """Best-effort check for Paycom login gate after redirect."""
    try:
        if "/app/login" in (driver.current_url or "").lower():
            return True
    except Exception:
        pass

    try:
        pin_inputs = driver.find_elements(By.CSS_SELECTOR,
            "input[name='pin'], input[id*='pin'], input[placeholder*='PIN'], input[type='password'][maxlength='4']"
        )
        if pin_inputs:
            return True
    except Exception:
        pass

    return False


def _is_retryable_exception(err):
    text = f"{type(err).__name__}: {err}".lower()
    signals = (
        "session not created",
        "devtoolsactiveport",
        "chrome failed to start",
        "timed out receiving message from renderer",
        "unable to discover open pages",
        "disconnected: not connected to devtools",
        "timeout",
        "invalid session id",
    )
    return any(signal in text for signal in signals)


def _run_once(action, effective_dry_run, profile_path, headless_mode):
    driver = None
    start_time = time.time()
    mode_name = "headless" if headless_mode else "visible"
    try:
        print(f"Launching Chrome ({mode_name})...")
        kill_stale_chrome(profile_path, profile_label="Paycom automation")
        driver = build_chrome_driver(
            profile_path,
            headless_mode=headless_mode,
            page_load_strategy="eager",
            page_load_timeout=45,
            script_timeout=30,
        )

        # Step 1: Navigate to Paycom
        print("Navigating to Paycom...")
        safe_get_with_partial_load(driver, PAYCOM_URL, "Paycom clock page")

        # Step 2: Fill PIN if login page is shown (username/password auto-filled by Chrome)
        pin_field = find_visible(driver, [
            "input[name='pin']",
            "input[id*='pin']",
            "input[placeholder*='PIN']",
            "input[type='password'][maxlength='4']",
        ], timeout=3)
        if pin_field:
            print("Entering PIN...")
            pin = read_windows_credential(PAYCOM_CREDENTIAL_TARGET).secret
            pin_field.clear()
            pin_field.send_keys(pin)

        # Click Log In
        login_btn = find_visible(driver, [
            "button[type='submit']",
            "input[type='submit']",
        ], timeout=2)
        if login_btn:
            login_btn.click()
            print("Clicked Log In.")
            try:
                WebDriverWait(driver, 8).until(EC.staleness_of(login_btn))
            except TimeoutException:
                pass
        else:
            print("No login button found - may already be logged in.")

        # Step 3: Navigate to the time clock page (only if not already there)
        if "timeclock" not in (driver.current_url or "").lower():
            print("Navigating to Web Time Clock...")
            safe_get_with_partial_load(driver, PAYCOM_URL, "Paycom web time clock")

        # Fail fast if we are still gated behind login after attempting navigation.
        try:
            WebDriverWait(driver, 20).until(
                lambda d: "timeclock" in (d.current_url or "").lower() or "/app/login" in (d.current_url or "").lower()
            )
        except TimeoutException:
            pass

        if is_paycom_login_page(driver):
            page_state = dump_page_state(driver)
            print(f"Page state at login gate:\n{page_state}")
            msg = (
                "Still on Paycom login page after submitting PIN. "
                "Saved username/password may be missing in chrome_profile or an extra login challenge is required."
            )
            safe_take_screenshot(driver, f"clock_{action}_login_required_{mode_name}")
            return False, msg, bool(headless_mode)

        # Step 3.5: Check current clock state
        current_state, last_punch_text = detect_clock_state(driver)
        if last_punch_text:
            print(f"Current state: {last_punch_text}")

        # If already in the requested state, treat as success in both real and dry-run modes.
        if action == "in" and current_state == "in":
            safe_take_screenshot(driver, f"clock_{action}_already_in")
            if effective_dry_run:
                msg = f"Dry run success: already clocked in. {last_punch_text}"
            else:
                msg = f"Already clocked in. {last_punch_text}"
            return True, msg, False

        if action == "out" and current_state == "out":
            safe_take_screenshot(driver, f"clock_{action}_already_out")
            if effective_dry_run:
                msg = f"Dry run success: already clocked out. {last_punch_text}"
            else:
                msg = f"Already clocked out. {last_punch_text}"
            return True, msg, False

        # Step 4: Click In Day or Out Day
        btn_text = "In Day" if action == "in" else "Out Day"
        print(f"Looking for '{btn_text}' button...")

        punch_btn = find_punch_button(driver, btn_text)

        if punch_btn:
            if effective_dry_run:
                safe_take_screenshot(driver, f"clock_{action}_dry_run_detected")
                elapsed = time.time() - start_time
                msg = f"Dry run success: detected '{btn_text}' button and skipped click. ({elapsed:.1f}s)"
                return True, msg, False

            punch_btn.click()
            print(f"Clicked '{btn_text}' successfully!")

            # Handle any confirmation dialog
            confirm_btn = None
            try:
                confirm_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.XPATH,
                        "//*[(self::button or self::a or self::input) and "
                        "(normalize-space(.)='Confirm' or normalize-space(.)='Yes' or "
                        "normalize-space(.)='OK' or normalize-space(.)='Submit')]"
                    ))
                )
            except TimeoutException:
                pass

            if confirm_btn:
                confirm_btn.click()
                print("Confirmed the punch.")
                time.sleep(0.5)

            safe_take_screenshot(driver, f"clock_{action}_success")
            elapsed = time.time() - start_time
            msg = f"Clock-{action} completed successfully! ({elapsed:.1f}s)"
            time.sleep(1)
            return True, msg, False

        page_state = dump_page_state(driver)
        print(f"Page state when button not found:\n{page_state}")

        state_info = f" Current state: {last_punch_text}" if last_punch_text else ""
        hint = ""
        if current_state:
            expected_btn = "Out Day" if current_state == "in" else "In Day"
            if expected_btn != btn_text:
                hint = f" (You appear to be clocked {current_state}; only '{expected_btn}' is available.)"
        msg = f"Could not find '{btn_text}' button.{hint}{state_info}"
        safe_take_screenshot(driver, f"clock_{action}_button_not_found_{mode_name}")
        retryable = bool(headless_mode and not current_state)
        return False, msg, retryable
    except Exception as err:
        try:
            if driver:
                safe_take_screenshot(driver, f"clock_{action}_error_{mode_name}")
        except Exception:
            pass
        error_msg = f"Clock-{action} failed: {type(err).__name__}: {err}"
        return False, error_msg, bool(headless_mode and _is_retryable_exception(err))
    finally:
        if driver:
            safe_driver_quit(driver, profile_path=profile_path)


def run(action, dry_run=None):
    global AUDIT_AUTOMATION_NAME
    AUDIT_AUTOMATION_NAME = f"paycom_clock.{action}"
    log_automation_event(
        AUDIT_AUTOMATION_NAME,
        "STARTED",
        f"Requested action: {action}",
        source="paycom_clock.py",
    )

    if action not in ("in", "out"):
        print("Usage: python paycom_clock.py [in|out] [--dry-run|--real]")
        write_result(False, "Invalid action argument.")
        sys.exit(1)

    profile_path = os.path.join(SCRIPT_DIR, "chrome_profile")
    effective_dry_run = bool(CONFIG_PAYCOM_DRY_RUN) if dry_run is None else bool(dry_run)
    print(f"Starting Paycom clock-{action} automation...")
    if effective_dry_run:
        print("Dry-run mode enabled: the script will detect buttons but will not click them.")
    else:
        print("Real mode enabled: punch actions will be clicked when found.")

    # Headless is preferred for background runs; visible mode is a fallback for flaky login/startup cases.
    attempt_modes = [True, False]
    for idx, headless_mode in enumerate(attempt_modes, start=1):
        ok, msg, retryable = _run_once(action, effective_dry_run, profile_path, headless_mode=headless_mode)
        if ok:
            write_result(True, msg)
            print(f"RESULT:SUCCESS:{msg}")
            return

        if retryable and idx < len(attempt_modes):
            print(f"Headless attempt failed: {msg}")
            print("Retrying once in visible mode for reliability...")
            continue

        write_result(False, msg)
        print(f"RESULT:FAIL:{msg}")
        sys.exit(1)


if __name__ == "__main__":
    dry_run_override = None

    if len(sys.argv) not in (2, 3) or sys.argv[1] not in ("in", "out"):
        log_automation_result(
            "paycom_clock.invalid_invocation",
            False,
            "Invalid command-line arguments.",
            source="paycom_clock.py",
        )
        print("Usage: python paycom_clock.py [in|out] [--dry-run|--real]")
        print("  python paycom_clock.py in --dry-run   - Test clock in without clicking")
        print("  python paycom_clock.py in --real      - Real clock in")
        print("  python paycom_clock.py out --dry-run  - Test clock out without clicking")
        print("  python paycom_clock.py out --real     - Real clock out")
        sys.exit(1)

    if len(sys.argv) == 3:
        flag = (sys.argv[2] or "").strip().lower()
        if flag == "--dry-run":
            dry_run_override = True
        elif flag == "--real":
            dry_run_override = False
        else:
            log_automation_result(
                "paycom_clock.invalid_invocation",
                False,
                f"Invalid mode flag: {sys.argv[2]}",
                source="paycom_clock.py",
            )
            print(f"Invalid mode flag: {sys.argv[2]}")
            print("Usage: python paycom_clock.py [in|out] [--dry-run|--real]")
            sys.exit(1)

    run(sys.argv[1], dry_run=dry_run_override)
