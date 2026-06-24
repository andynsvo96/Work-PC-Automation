"""
Slack Team Message Automation
Usage:
    python slack_team.py in [--test-url]
    python slack_team.py out [--test-url]
    python slack_team.py custom --message "your message" [--test-url]
"""

import argparse
import os
import sys
import time
from urllib.parse import urlparse

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from automation_audit import log_automation_event, log_automation_result
from automation_runtime import (
    SCRIPT_DIR,
    build_chrome_driver,
    configure_console_utf8,
    kill_stale_chrome,
    safe_driver_quit,
    safe_get_with_partial_load,
    safe_take_screenshot,
    write_result_payload,
)
import config as config_module
from config import (
    SLACK_CHANNEL_URL,
    SLACK_CHANNEL_URL_TEST,
    SLACK_FORCE_HEADLESS,
)
from slack_message_rotation import record_slack_day_message_use, select_slack_day_message

configure_console_utf8()

AUDIT_AUTOMATION_NAME = "slack_team.unknown"
MIN_VIEWPORT_WIDTH = 700
MIN_VIEWPORT_HEIGHT = 420
HARD_MIN_VIEWPORT_WIDTH = 500
HARD_MIN_VIEWPORT_HEIGHT = 260
SLACK_CLIENT_URL_MARKER = "app.slack.com/client/"
RETRYABLE_EXCEPTION_SIGNALS = (
    "session not created",
    "devtoolsactiveport",
    "chrome failed to start",
    "disconnected: not connected to devtools",
    "timed out receiving message from renderer",
    "timeout",
    "invalid session id",
    "unable to discover open pages",
)
RETRYABLE_ISSUES = {
    "navigation_failed",
    "navigation_exception",
    "composer_not_found",
    "send_not_confirmed",
    "tiny_viewport",
}

# Ordered from strict selectors to fallback selectors.
COMPOSER_SELECTORS = [
    'div[data-qa="message_input"] div[role="textbox"][contenteditable="true"]',
    'div[data-qa="message_input"] div[contenteditable="true"]',
    'div[data-qa="message_input"] [contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"][aria-label*="Message"]',
    'div[aria-label*="message"][contenteditable="true"]',
    'div[role="textbox"][contenteditable="true"]',
]
def write_result(success, message):
    write_result_payload(
        AUDIT_AUTOMATION_NAME,
        "slack_team.py",
        success,
        message,
    )


def _is_retryable_exception(err):
    text = f"{type(err).__name__}: {err}".lower()
    return any(signal in text for signal in RETRYABLE_EXCEPTION_SIGNALS)


def _get_viewport_size(driver):
    try:
        width, height = driver.execute_script("return [window.innerWidth || 0, window.innerHeight || 0];")
        return int(width), int(height)
    except Exception:
        return 0, 0


def _has_usable_viewport(driver):
    width, height = _get_viewport_size(driver)
    print(f"Viewport size: {width}x{height}")
    return width >= MIN_VIEWPORT_WIDTH and height >= MIN_VIEWPORT_HEIGHT


def _has_hard_min_viewport(driver):
    width, height = _get_viewport_size(driver)
    return width >= HARD_MIN_VIEWPORT_WIDTH and height >= HARD_MIN_VIEWPORT_HEIGHT


def _force_viewport_metrics(driver, width=1600, height=1000):
    try:
        driver.execute_cdp_cmd(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": int(width),
                "height": int(height),
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        time.sleep(0.3)
        return True
    except Exception:
        return False


def ensure_window_ready(driver):
    """Try to recover from tiny viewport state before interacting with Slack."""
    for _ in range(3):
        try:
            driver.set_window_rect(0, 0, 1920, 1080)
        except Exception:
            try:
                driver.set_window_size(1920, 1080)
            except Exception:
                pass
        try:
            driver.execute_script("window.resizeTo(1920, 1080);")
        except Exception:
            pass
        _force_viewport_metrics(driver, width=1600, height=1000)
        if _has_usable_viewport(driver):
            return True
        time.sleep(0.7)
    return False


def _wait_for_client_url(driver, timeout=8):
    try:
        WebDriverWait(driver, timeout).until(lambda d: SLACK_CLIENT_URL_MARKER in (d.current_url or ""))
        return True
    except TimeoutException:
        return False


def is_expected_slack_channel(current_url, channel_url):
    """Return True when current URL is Slack client and matches configured channel path."""
    try:
        current = urlparse(current_url or "")
        target = urlparse(channel_url or "")
    except Exception:
        return False

    current_path = (current.path or "").rstrip("/").lower()
    target_path = (target.path or "").rstrip("/").lower()
    current_host = (current.netloc or "").lower()
    target_host = (target.netloc or "").lower()

    if "slack.com" not in current_host:
        return False
    if target_host and "slack.com" in target_host and current_host != target_host:
        return False
    if not current_path.startswith("/client/"):
        return False
    if target_path.startswith("/client/"):
        return current_path.startswith(target_path)
    return True


def is_slack_login_page(driver):
    """Best-effort login gate detection."""
    try:
        current_url = (driver.current_url or "").lower()
    except Exception:
        current_url = ""

    if any(token in current_url for token in ("/signin", "/checkcookie", "/ssb/signin")):
        return True
    if "app.slack.com/client/" in current_url:
        return False

    try:
        login_inputs = driver.find_elements(
            By.CSS_SELECTOR,
            "input[type='email'], input[type='password'], input[name='email'], input[name='password']",
        )
        if login_inputs:
            return True
    except Exception:
        pass

    try:
        body_text = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        if "sign in to slack" in body_text or "enter your workspace" in body_text:
            return True
    except Exception:
        pass

    return False


def navigate_to_slack_channel(driver, channel_url):
    """Navigate to target channel with fallbacks and verify destination."""
    safe_get_with_partial_load(driver, channel_url, "Slack channel")
    _wait_for_client_url(driver, timeout=10)
    if is_expected_slack_channel(driver.current_url, channel_url):
        return True

    try:
        driver.execute_script("window.location.replace(arguments[0]);", channel_url)
        time.sleep(1.0)
    except Exception:
        pass
    if is_expected_slack_channel(driver.current_url, channel_url):
        return True

    try:
        driver.switch_to.new_window("tab")
        safe_get_with_partial_load(driver, channel_url, "Slack channel (new tab)")
        time.sleep(1.0)
    except Exception as err:
        print(f"Could not open Slack in new tab: {err}")
    return is_expected_slack_channel(driver.current_url, channel_url)


def _composer_text(driver, composer):
    try:
        text = (composer.text or "").strip()
        if text:
            return text
    except Exception:
        pass

    try:
        text = driver.execute_script(
            "const el = arguments[0];"
            "return (el.innerText || el.textContent || el.value || '').trim();",
            composer,
        )
        return str(text or "").strip()
    except Exception:
        return ""


def _focus_composer(driver, composer):
    try:
        composer.click()
        return True
    except Exception:
        try:
            driver.execute_script("arguments[0].focus();", composer)
            return True
        except Exception:
            return False


def _collect_composer_candidates(driver):
    candidates = []
    seen = set()
    for selector in COMPOSER_SELECTORS:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for raw in elements:
            element = raw
            try:
                if (element.get_attribute("contenteditable") or "").lower() != "true":
                    nested = element.find_elements(By.CSS_SELECTOR, '[contenteditable="true"]')
                    if nested:
                        element = nested[0]
            except Exception:
                pass

            try:
                element_id = getattr(element, "id", None) or str(element)
                if element_id in seen:
                    continue
                seen.add(element_id)

                if not element.is_displayed() or not element.is_enabled():
                    continue

                rect = element.rect or {}
                width = float(rect.get("width") or 0.0)
                height = float(rect.get("height") or 0.0)
                y_pos = float(rect.get("y") or 0.0)
                if width < 120 or height < 18:
                    continue

                aria = (element.get_attribute("aria-label") or "").lower()
                data_qa = (element.get_attribute("data-qa") or "").lower()
                if any(bad in aria for bad in ("search", "jump to")):
                    continue
                if "search" in data_qa:
                    continue

                # Prefer the lowest visible textbox in the viewport (Slack composer is near bottom).
                score = y_pos + (width * height / 100000.0)
                candidates.append((score, selector, element))
            except Exception:
                continue

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [(selector, element) for _, selector, element in candidates]


def wait_for_composer(driver, timeout=18):
    end = time.time() + max(1.0, float(timeout))
    while time.time() < end:
        candidates = _collect_composer_candidates(driver)
        if candidates:
            selector, element = candidates[0]
            print(f"Found Slack composer via: {selector}")
            return element, selector
        time.sleep(0.6)
    return None, ""


def _did_recent_message_match(driver, message):
    target = (message or "").strip()
    if not target:
        return False
    selectors = [
        '[data-qa="message-text"]',
        "div.c-message__body",
    ]
    for selector in selectors:
        try:
            rows = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            rows = []
        for row in rows[-8:]:
            try:
                text = (row.text or "").strip()
                if text == target:
                    return True
            except Exception:
                continue
    return False


def set_composer_message(driver, composer, message):
    if not _focus_composer(driver, composer):
        return False
    time.sleep(0.15)

    try:
        composer.send_keys(Keys.CONTROL, "a")
        composer.send_keys(Keys.BACKSPACE)
    except Exception:
        pass

    try:
        composer.send_keys(message)
        time.sleep(0.2)
        typed = _composer_text(driver, composer)
        if typed and message.strip() in typed:
            return True
    except Exception:
        pass

    try:
        driver.execute_script(
            "const el = arguments[0];"
            "const text = arguments[1];"
            "el.focus();"
            "if ('value' in el) { el.value = text; }"
            "el.textContent = text;"
            "el.dispatchEvent(new Event('input', { bubbles: true }));"
            "el.dispatchEvent(new Event('change', { bubbles: true }));",
            composer,
            message,
        )
        time.sleep(0.25)
        typed = _composer_text(driver, composer)
        return bool(typed and message.strip() in typed)
    except Exception:
        return False


def submit_message(driver, composer, message):
    target = (message or "").strip()
    try:
        composer.send_keys(Keys.ENTER)
    except Exception:
        return False
    time.sleep(0.9)

    after_first = _composer_text(driver, composer)
    if target and target not in after_first:
        return True

    try:
        composer.send_keys(Keys.CONTROL, Keys.ENTER)
        time.sleep(0.8)
        after_second = _composer_text(driver, composer)
        if target and target not in after_second:
            return True
    except Exception:
        after_second = ""

    # Only fall back to history check if composer text cannot be read after submit.
    if not after_second and not after_first:
        return _did_recent_message_match(driver, target)
    return False


def open_channel_and_find_composer(driver, channel_url, action, attempts=2):
    """Open Slack channel with retries and return (composer, issue_code, selector)."""
    last_issue = "composer_not_found"
    for attempt in range(1, attempts + 1):
        try:
            if attempt > 1:
                driver.get("about:blank")
                time.sleep(0.5)
                ensure_window_ready(driver)

            print(f"Navigating to Slack channel (attempt {attempt}/{attempts})...")
            reached = navigate_to_slack_channel(driver, channel_url)
            time.sleep(1.8 if attempt == 1 else 2.3)
            ensure_window_ready(driver)
            current_url = driver.current_url or ""
            print(f"Current URL after navigation: {current_url}")
            safe_take_screenshot(driver, f"slack_{action}_loaded_attempt{attempt}")

            if not reached:
                last_issue = "navigation_failed"
                print("Navigation did not reach the target Slack channel.")
                safe_take_screenshot(driver, f"slack_{action}_navigation_retry_{attempt}")
                continue

            if is_slack_login_page(driver):
                last_issue = "login_required"
                safe_take_screenshot(driver, f"slack_{action}_login_required")
                return None, last_issue, ""

            width, height = _get_viewport_size(driver)
            print(f"Viewport check before composer: {width}x{height}")
            if width < HARD_MIN_VIEWPORT_WIDTH or height < HARD_MIN_VIEWPORT_HEIGHT:
                print("Viewport below hard minimum. Attempting metrics recovery...")
                for recover_round in range(1, 3):
                    _force_viewport_metrics(driver, width=1600, height=1000)
                    ensure_window_ready(driver)
                    width, height = _get_viewport_size(driver)
                    print(f"Viewport after recovery {recover_round}: {width}x{height}")
                    if width >= HARD_MIN_VIEWPORT_WIDTH and height >= HARD_MIN_VIEWPORT_HEIGHT:
                        break
                    # Re-load channel once while keeping the same browser session.
                    safe_get_with_partial_load(driver, channel_url, "Slack channel viewport recovery")
                    time.sleep(0.8)
                if width < HARD_MIN_VIEWPORT_WIDTH or height < HARD_MIN_VIEWPORT_HEIGHT:
                    last_issue = "tiny_viewport"
                    print("Viewport is too small for reliable Slack interaction.")
                    safe_take_screenshot(driver, f"slack_{action}_tiny_viewport_{attempt}")
                    return None, last_issue, ""
            elif width < MIN_VIEWPORT_WIDTH or height < MIN_VIEWPORT_HEIGHT:
                print(f"Viewport is below preferred size ({width}x{height}); continuing best effort.")

            composer, selector = wait_for_composer(driver, timeout=16 if attempt == 1 else 10)
            if composer:
                return composer, "", selector

            # Soft refresh inside the same attempt before giving up.
            safe_get_with_partial_load(driver, channel_url, "Slack channel refresh")
            time.sleep(1.0)
            composer, selector = wait_for_composer(driver, timeout=7)
            if composer:
                return composer, "", selector

            last_issue = "composer_not_found"
            print(f"Composer not found on attempt {attempt}.")
            safe_take_screenshot(driver, f"slack_{action}_composer_retry_{attempt}")
        except Exception as err:
            print(f"Slack navigation attempt {attempt} failed: {err}")
            last_issue = "navigation_exception"
            safe_take_screenshot(driver, f"slack_{action}_nav_error_{attempt}")
            time.sleep(1.0)

    return None, last_issue, ""


def resolve_slack_channel_url(force_test_url=False, channel_url=None):
    explicit_url = str(channel_url or "").strip()
    if explicit_url:
        return explicit_url
    if force_test_url:
        print("Slack test mode enabled: using Slack test channel URL.")
        return SLACK_CHANNEL_URL_TEST

    use_test = os.getenv("SLACK_USE_TEST_URL", "").strip().lower() in ("1", "true", "yes", "on")
    if use_test:
        print("SLACK_USE_TEST_URL enabled: using Slack test channel URL.")
        return SLACK_CHANNEL_URL_TEST
    return SLACK_CHANNEL_URL


def _resolve_day_message(action, now=None):
    return select_slack_day_message(config_module, action, now=now)


def _run_once(action, message, channel_url, profile_path, headless_mode):
    start_time = time.time()
    mode_name = "headless" if headless_mode else "visible"
    driver = None
    try:
        print(f"Launching Slack browser session ({mode_name})...")
        kill_stale_chrome(profile_path, profile_label="Slack automation")
        driver = build_chrome_driver(
            profile_path,
            headless_mode=headless_mode,
            page_load_strategy="eager",
            page_load_timeout=40,
            script_timeout=25,
            extra_args=(["--new-window"] if not headless_mode else None),
        )

        if not headless_mode and not ensure_window_ready(driver):
            print("Visible browser viewport is below preferred size; continuing best effort.")

        composer, issue, selector = open_channel_and_find_composer(
            driver,
            channel_url,
            action,
            attempts=(2 if headless_mode else 2),
        )
        if not composer:
            if issue == "login_required":
                msg = "Slack login is required for slack_chrome_profile. Run setup_slack_profile.bat and log in once."
                return False, msg, False
            msg = (
                "Could not find Slack message composer after navigation retries "
                f"(last_issue={issue}). Is the Slack profile logged in? Run setup_slack_profile.bat"
            )
            return False, msg, issue in RETRYABLE_ISSUES

        print(f"Using composer selector: {selector}")
        if not set_composer_message(driver, composer, message):
            safe_take_screenshot(driver, f"slack_{action}_compose_failed")
            return False, "Unable to place message text in Slack composer.", True

        if not submit_message(driver, composer, message):
            safe_take_screenshot(driver, f"slack_{action}_send_not_confirmed")
            return False, "Slack message send could not be confirmed.", True

        safe_take_screenshot(driver, f"slack_{action}_sent")
        elapsed = time.time() - start_time
        return True, f"Slack '{message}' sent successfully! ({elapsed:.1f}s)", False
    except Exception as err:
        if driver:
            safe_take_screenshot(driver, f"slack_{action}_error_{mode_name}")
        error_msg = f"Slack team message failed: {type(err).__name__}: {err}"
        return False, error_msg, _is_retryable_exception(err)
    finally:
        if driver:
            safe_driver_quit(driver, profile_path=profile_path)


def run(action, force_test_url=False, custom_message=None, channel_url=None):
    global AUDIT_AUTOMATION_NAME
    AUDIT_AUTOMATION_NAME = f"slack_team.{action}"
    log_automation_event(
        AUDIT_AUTOMATION_NAME,
        "STARTED",
        f"Requested action: {action}",
        source="slack_team.py",
    )

    if action not in ("in", "out", "custom"):
        return False, "Invalid action argument."

    message_selection = None
    if action == "custom":
        message = str(custom_message or "").strip()
        if not message:
            return False, "Custom Slack message cannot be empty."
        print(f"Detected custom message: '{message}'")
    else:
        message_selection = _resolve_day_message(action)
        day_name = str(message_selection.get("display_day_name") or message_selection.get("day_name") or "Unknown")
        message = str(message_selection.get("message") or "")
        if not message.strip():
            return False, f"Slack {action} message is empty for {day_name}. Check config.py."
        if bool(message_selection.get("alternating_active")):
            variant = "alternate" if message_selection.get("variant") == "alternate" else "primary"
            print(f"Detected {day_name} message ({variant}): '{message}'")
        else:
            print(f"Detected {day_name} message: '{message}'")
    channel_url = resolve_slack_channel_url(force_test_url=force_test_url, channel_url=channel_url)
    profile_path = os.path.join(SCRIPT_DIR, "slack_chrome_profile")

    if SLACK_FORCE_HEADLESS:
        mode_plan = [True]
        mode_attempts = {True: 2}
        print("SLACK_FORCE_HEADLESS is enabled; skipping visible browser mode.")
    else:
        # Headless first is more stable for locked/minimized desktop sessions.
        mode_plan = [True, False]
        mode_attempts = {True: 2, False: 1}

    last_msg = "Slack automation failed."
    for mode in mode_plan:
        attempts = mode_attempts.get(mode, 1)
        for attempt in range(1, attempts + 1):
            print(f"Starting Slack {action} attempt {attempt}/{attempts} in {'headless' if mode else 'visible'} mode...")
            ok, msg, retryable = _run_once(action, message, channel_url, profile_path, headless_mode=mode)
            if ok:
                if message_selection and not force_test_url:
                    try:
                        record_slack_day_message_use(message_selection)
                    except Exception as e:
                        print(f"Warning: could not record Slack alternating message usage: {e}")
                return True, msg
            last_msg = msg
            if retryable and attempt < attempts:
                print(f"Retryable failure detected: {msg}")
                time.sleep(1.5)
                continue
            break

    return False, last_msg


def _parse_args(argv):
    parser = argparse.ArgumentParser(description="Send Slack day messages or a custom message.")
    parser.add_argument("action", choices=("in", "out", "custom"), help="Slack action to run.")
    parser.add_argument(
        "--message",
        dest="custom_message",
        default="",
        help="Custom message text (required for action 'custom').",
    )
    parser.add_argument(
        "--test-url",
        action="store_true",
        help="Send to SLACK_CHANNEL_URL_TEST instead of the default channel.",
    )
    parser.add_argument(
        "--channel-url",
        default="",
        help="Explicit Slack channel URL. Overrides default/test channel selection.",
    )
    args = parser.parse_args(argv)
    if args.action == "custom" and not str(args.custom_message or "").strip():
        parser.error("--message is required when action is 'custom'.")
    return args


if __name__ == "__main__":
    try:
        parsed = _parse_args(sys.argv[1:])
    except SystemExit:
        log_automation_result(
            "slack_team.invalid_invocation",
            False,
            "Invalid command-line arguments.",
            source="slack_team.py",
        )
        raise

    ok, message = run(
        parsed.action,
        force_test_url=bool(parsed.test_url),
        custom_message=parsed.custom_message,
        channel_url=parsed.channel_url,
    )
    write_result(ok, message)
    if ok:
        print(f"RESULT:SUCCESS:{message}")
        sys.exit(0)
    print(f"RESULT:FAIL:{message}")
    sys.exit(1)
