"""
CRM unlock automation worker.
Usage:
    python crm_unlock_orders.py --action unlock_all
    python crm_unlock_orders.py --action unlock_all --dry-run
"""

import argparse
import os
import re
import sys
import time

from _bootstrap import ensure_project_root_on_path

ensure_project_root_on_path()

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

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
from config import (
    CRM_ACTION_TIMEOUT,
    CRM_ALLOW_VISIBLE_FALLBACK,
    CRM_DRY_RUN as CONFIG_CRM_DRY_RUN,
    CRM_HEADLESS,
    CRM_LOGIN_URL,
    CRM_PAGE_LOAD_TIMEOUT,
    CRM_PROFILE_DIR,
    CRM_LOCKED_URL,
)
from credential_store import CRM_CREDENTIAL_TARGET, read_windows_credential

configure_console_utf8()

AUTOMATION_NAME = "crm.unlock_orders"
UNLOCK_OPTION_TEXT = "Stock Auto Ordering Unlocked"
PROFILE_PATH = os.path.join(SCRIPT_DIR, CRM_PROFILE_DIR)
ORDER_ID_PATTERN = re.compile(r"\b\d{7}\b")
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
ORDER_ROW_SELECTORS = [
    (By.CSS_SELECTOR, "table tbody tr.order-row"),
    (By.CSS_SELECTOR, "table tbody tr[data-id]"),
    (By.CSS_SELECTOR, "table.table tbody tr[data-id]"),
    (By.CSS_SELECTOR, "table tbody tr"),
]
PREVIEW_TITLE_SELECTORS = [
    (By.XPATH, "//*[normalize-space()='Order Preview']"),
    (By.XPATH, "//*[contains(normalize-space(.), 'Order Preview')]"),
]
PREVIEW_PANEL_SELECTORS = [
    (By.CSS_SELECTOR, ".order-preview"),
    (By.CSS_SELECTOR, "#order-preview"),
    (By.CSS_SELECTOR, ".order-preview-panel"),
    (By.XPATH, "//*[normalize-space()='Order Preview']/ancestor::div"),
    (By.XPATH, "//*[contains(normalize-space(.), 'Order Preview')]/ancestor::div"),
]
PREVIEW_DROPDOWN_TOGGLE_SELECTORS = [
    (By.XPATH, ".//button[contains(@class, 'dropdown-toggle')]"),
    (By.XPATH, ".//*[@role='button' and contains(@class, 'dropdown-toggle')]"),
    (By.XPATH, ".//button[@aria-haspopup='true' and not(contains(normalize-space(.), 'Apply'))]"),
    (By.XPATH, ".//button[not(contains(normalize-space(.), 'Apply')) and (.//*[name()='svg'] or .//*[contains(@class, 'caret')] or contains(normalize-space(.), '▼'))]"),
]
PREVIEW_INPUT_SELECTORS = [
    (By.CSS_SELECTOR, "input[type='text']"),
    (By.CSS_SELECTOR, "input.form-control"),
    (By.XPATH, ".//input[@type='text']"),
    (By.XPATH, ".//input[not(@type) or @type='search']"),
]
DROPDOWN_INPUT_SELECTORS = [
    (By.CSS_SELECTOR, ".order-preview input[type='text']"),
    (By.CSS_SELECTOR, "#order-preview input[type='text']"),
    (By.CSS_SELECTOR, ".order-preview-panel input[type='text']"),
    (By.XPATH, "//div[contains(@class, 'dropdown-menu')]//input[@type='text']"),
    (By.XPATH, "//input[@type='text' and (contains(@placeholder, 'Search') or contains(@class, 'search'))]"),
]
DROPDOWN_OPTION_SELECTORS = [
    (By.XPATH, "//*[contains(@class, 'dropdown-menu')]//*[normalize-space()='Stock Auto Ordering Unlocked']"),
    (By.XPATH, "//*[normalize-space()='Stock Auto Ordering Unlocked']"),
    (By.XPATH, "//*[contains(normalize-space(.), 'Stock Auto Ordering Unlocked') and not(self::script)]"),
]
APPLY_BUTTON_SELECTORS = [
    (By.XPATH, ".//button[normalize-space()='Apply']"),
    (By.XPATH, ".//button[contains(normalize-space(.), 'Apply')]"),
    (By.XPATH, ".//a[normalize-space()='Apply']"),
    (By.XPATH, ".//*[@role='button' and normalize-space()='Apply']"),
    (By.XPATH, ".//*[contains(@class, 'btn') and normalize-space()='Apply']"),
    (By.XPATH, ".//*[self::div or self::span][normalize-space()='Apply']"),
    (By.CSS_SELECTOR, "button.btn-primary"),
    (By.CSS_SELECTOR, ".btn.btn-primary"),
]
SELECTED_UNLOCK_TEXT_SELECTORS = [
    (By.XPATH, ".//*[contains(normalize-space(.), 'Stock Auto Ordering Unlocked')]"),
    (By.XPATH, ".//input[@value='Stock Auto Ordering Unlocked']"),
]
SELECTION_TEXT_SELECTORS = [
    (
        By.XPATH,
        "//*[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'orders selected')]",
    ),
]
SELECTED_ROW_SELECTORS = [
    # CRM applies this Angular-controlled class to selected report rows.
    (By.CSS_SELECTOR, "table tbody tr.previewPaneSelected"),
    (By.CSS_SELECTOR, "table tbody tr.selected"),
    (By.CSS_SELECTOR, "table tbody tr.table-active"),
    (By.CSS_SELECTOR, "table tbody tr.bg-info"),
    (By.CSS_SELECTOR, "table tbody tr[aria-selected='true']"),
]

CONFIRM_MODAL_SELECTORS = [
    (By.XPATH, "//*[contains(normalize-space(.), 'Are you sure you want to apply')]"),
    (By.XPATH, "//*[contains(normalize-space(.), 'Are you sure')]"),
    (By.CSS_SELECTOR, ".modal-dialog"),
]
OK_BUTTON_SELECTORS = [
    (By.XPATH, "//button[normalize-space()='OK']"),
    (By.XPATH, "//button[normalize-space()='Ok']"),
    (By.XPATH, "//button[contains(normalize-space(.), 'OK')]"),
]
UPDATE_COMPLETE_SELECTORS = [
    (By.XPATH, "//*[contains(normalize-space(.), 'Status updated to include Stock Auto Ordering Unlocked')]"),
    (By.XPATH, "//*[contains(normalize-space(.), 'Status updated to include')]"),
    (By.XPATH, "//*[contains(normalize-space(.), 'Update complete')]"),
    (By.XPATH, "//*[contains(normalize-space(.), 'Updated complete')]"),
    (By.XPATH, "//*[contains(normalize-space(.), 'updated successfully')]"),
]
SUCCESS_STATUS_TEXT_MARKERS = (
    'status updated to include stock auto ordering unlocked',
    'status updated to include',
    'updated successfully',
    'update complete',
)
APPLY_PROGRESS_TEXT_MARKERS = (
    'applying "stock auto ordering unlocked"',
    "applying 'stock auto ordering unlocked'",
    'applying stock auto ordering unlocked',
)
NO_ORDERS_TEXT_MARKERS = (
    'no orders found',
    'no records found',
    'no matching records found',
    'no data available',
    'no results found',
    'nothing to display',
)
LOGIN_USERNAME_SELECTORS = [
    (By.NAME, "email"),
    (By.NAME, "username"),
    (By.NAME, "login"),
    (By.CSS_SELECTOR, "input[type='email']"),
    (By.CSS_SELECTOR, "input[name='email']"),
    (By.CSS_SELECTOR, "input[name='username']"),
    (By.CSS_SELECTOR, "input[id='username']"),
    (By.CSS_SELECTOR, "input[name='login']"),
    (By.CSS_SELECTOR, "input[id='login']"),
    (
        By.XPATH,
        "//input[(not(@type) or @type='text' or @type='email') and (contains(translate(@name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'user') or contains(translate(@id, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'user') or contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'user') or contains(translate(@placeholder, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'email'))]",
    ),
]
LOGIN_PASSWORD_SELECTORS = [
    (By.NAME, "password"),
    (By.CSS_SELECTOR, "input[type='password']"),
    (By.CSS_SELECTOR, "input[name='password']"),
]
LOGIN_BUTTON_SELECTORS = [
    (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
    (By.XPATH, "//button[contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
    (By.XPATH, "//input[@type='submit' and contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'login')]"),
    (By.XPATH, "//input[@type='submit' and contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'sign in')]"),
]
LOGIN_HINT_TEXTS = ("login", "sign in")
LOGIN_RETRY_INTERVAL_SECONDS = 3
IGNORABLE_LOGIN_ERROR_TEXT_MARKERS = (
    "could not obtain auth token from api",
)
POST_LOGIN_TEXT_MARKERS = (
    "filters:",
    "orders selected",
    "order preview",
    "stock auto ordering",
)


def _is_retryable_exception(err):
    text = f"{type(err).__name__}: {err}".lower()
    return any(signal in text for signal in RETRYABLE_EXCEPTION_SIGNALS)


def _crm_attempt_modes():
    modes = [bool(CRM_HEADLESS)]
    if modes[0] and bool(CRM_ALLOW_VISIBLE_FALLBACK):
        modes.append(False)
    return modes


def _resolve_report_url(list_url=None):
    return str(list_url or CRM_LOCKED_URL or "").strip()


def _validate_runtime_config(list_url=None):
    report_url = _resolve_report_url(list_url)
    if not report_url:
        raise RuntimeError("The CRM unlocker report URL is empty in config.py.")
    if "..." in report_url:
        raise RuntimeError(
            "The CRM unlocker report URL still contains a placeholder token. Replace it with the full CRM locked-orders URL before running."
        )
    if not str(CRM_LOGIN_URL or "").strip():
        raise RuntimeError("CRM_LOGIN_URL is empty in config.py.")
    read_windows_credential(CRM_CREDENTIAL_TARGET)


def _wait_for_any(root, selectors, timeout=None, condition="visible"):
    timeout = timeout or CRM_ACTION_TIMEOUT
    deadline = time.time() + max(1, timeout)
    last_error = None
    while time.time() < deadline:
        for by, value in selectors:
            try:
                element = root.find_element(by, value)
                if condition == "presence":
                    return element
                if condition == "visible" and element.is_displayed():
                    return element
                if condition == "clickable" and element.is_displayed() and element.is_enabled():
                    return element
            except Exception as exc:
                last_error = exc
        time.sleep(0.2)
    selector_text = " | ".join(f"{by}={value}" for by, value in selectors[:4])
    if last_error:
        raise TimeoutException(f"Timed out waiting for {condition} element: {selector_text} ({last_error})")
    raise TimeoutException(f"Timed out waiting for {condition} element: {selector_text}")


def _visible_elements(root, selectors):
    for by, value in selectors:
        try:
            elements = root.find_elements(by, value)
        except Exception:
            continue
        visible = []
        for element in elements:
            try:
                if element.is_displayed():
                    visible.append(element)
            except StaleElementReferenceException:
                continue
        if visible:
            return visible
    return []


def _element_rect(driver, element):
    try:
        rect = driver.execute_script(
            "const r = arguments[0].getBoundingClientRect();"
            "return {left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height};",
            element,
        )
    except Exception:
        return {}
    return rect or {}


def _find_best_preview_panel(driver, timeout=None):
    deadline = time.time() + max(timeout or CRM_ACTION_TIMEOUT, 1)
    while time.time() < deadline:
        try:
            viewport_width = float(driver.execute_script("return window.innerWidth || document.documentElement.clientWidth || 0;") or 0)
        except Exception:
            viewport_width = 0.0

        candidate_elements = []
        for title in _visible_elements(driver, PREVIEW_TITLE_SELECTORS):
            candidate_elements.append(title)
            try:
                candidate_elements.extend(title.find_elements(By.XPATH, "./ancestor::div"))
            except Exception:
                pass
        candidate_elements.extend(_visible_elements(driver, PREVIEW_PANEL_SELECTORS))

        best_panel = None
        best_score = None
        seen = set()
        for panel in candidate_elements:
            try:
                panel_id = panel.id
            except Exception:
                panel_id = None
            if panel_id and panel_id in seen:
                continue
            if panel_id:
                seen.add(panel_id)

            try:
                if not panel.is_displayed():
                    continue
            except Exception:
                continue

            inputs = _visible_elements(panel, PREVIEW_INPUT_SELECTORS)
            apply_buttons = _visible_elements(panel, APPLY_BUTTON_SELECTORS)
            if not inputs or not apply_buttons:
                continue

            rect = _element_rect(driver, panel)
            width = float(rect.get("width") or 0)
            height = float(rect.get("height") or 0)
            left = float(rect.get("left") or 0)
            area = width * height
            if width < 180 or height < 120:
                continue

            has_selection_text = 1 if _visible_elements(panel, SELECTION_TEXT_SELECTORS) else 0
            right_half = 1 if (viewport_width and left >= (viewport_width * 0.5)) else 0
            score = (right_half, has_selection_text, left, -area)
            if best_score is None or score > best_score:
                best_score = score
                best_panel = panel

        if best_panel is not None:
            rect = _element_rect(driver, best_panel)
            print(
                "Resolved Order Preview panel at "
                f"x={float(rect.get('left') or 0):.0f}, y={float(rect.get('top') or 0):.0f}, "
                f"w={float(rect.get('width') or 0):.0f}, h={float(rect.get('height') or 0):.0f}."
            )
            return best_panel
        time.sleep(0.25)

    raise TimeoutException("The Order Preview panel with its input and Apply button did not become available.")


def _find_best_preview_input(driver, panel):
    deadline = time.time() + max(CRM_ACTION_TIMEOUT, 8)
    while time.time() < deadline:
        best_input = None
        best_score = None
        for element in _visible_elements(panel, PREVIEW_INPUT_SELECTORS):
            try:
                if not element.is_displayed() or not element.is_enabled():
                    continue
            except Exception:
                continue
            rect = _element_rect(driver, element)
            width = float(rect.get("width") or 0)
            left = float(rect.get("left") or 0)
            if width < 120:
                continue
            score = (width, left)
            if best_score is None or score > best_score:
                best_score = score
                best_input = element
        if best_input is not None:
            return best_input
        time.sleep(0.2)

    raise TimeoutException("The Order Preview text input was not available.")


def _click_with_fallback(driver, element):
    try:
        element.click()
        return
    except (ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException):
        pass

    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    except Exception:
        pass

    try:
        element.click()
        return
    except Exception:
        pass

    driver.execute_script("arguments[0].click();", element)


def _extract_first_number(text):
    match = re.search(r"(\d+)", str(text or ""))
    if match:
        return int(match.group(1))
    return None


def _order_ids_from_text(text):
    ids = []
    seen = set()
    for match in ORDER_ID_PATTERN.findall(str(text or "")):
        if match in seen:
            continue
        seen.add(match)
        ids.append(match)
    return ids


def _extract_row_order_id(row):
    try:
        for link in row.find_elements(By.CSS_SELECTOR, "a"):
            matches = _order_ids_from_text(link.text)
            if matches:
                return matches[0]
    except Exception:
        pass

    try:
        row_text = row.text or ""
    except Exception:
        row_text = ""
    matches = _order_ids_from_text(row_text)
    return matches[0] if matches else None


def _collect_order_ids(rows):
    ids = []
    seen = set()
    for row in rows or []:
        try:
            order_id = _extract_row_order_id(row)
        except StaleElementReferenceException:
            continue
        if not order_id or order_id in seen:
            continue
        seen.add(order_id)
        ids.append(order_id)
    return ids


def _format_order_ids(order_ids):
    cleaned = [str(value).strip() for value in (order_ids or []) if str(value).strip()]
    return ", ".join(cleaned)


def _body_text(driver):
    try:
        return " ".join((driver.find_element(By.TAG_NAME, "body").text or "").split())
    except Exception:
        return ""


def _visible_order_rows(driver):
    for by, value in ORDER_ROW_SELECTORS:
        try:
            rows = driver.find_elements(by, value)
        except Exception:
            continue
        visible_rows = []
        for row in rows:
            try:
                if row.is_displayed() and row.find_elements(By.CSS_SELECTOR, "td"):
                    visible_rows.append(row)
            except StaleElementReferenceException:
                continue
        if visible_rows:
            return visible_rows
    return []


def _current_order_row_count(driver):
    return len(_visible_order_rows(driver))


def _looks_like_no_orders_state(driver):
    body_text = _body_text(driver).lower()
    return any(marker in body_text for marker in NO_ORDERS_TEXT_MARKERS)


def _has_update_success_message(driver):
    for element in _visible_elements(driver, UPDATE_COMPLETE_SELECTORS):
        text = " ".join((element.text or "").split()).lower()
        if any(marker in text for marker in SUCCESS_STATUS_TEXT_MARKERS):
            return True
    body_text = _body_text(driver).lower()
    return any(marker in body_text for marker in SUCCESS_STATUS_TEXT_MARKERS)


def _has_unlock_apply_progress(driver):
    body_text = _body_text(driver).lower()
    return any(marker in body_text for marker in APPLY_PROGRESS_TEXT_MARKERS)


def is_login_page(driver):
    try:
        current_url = str(driver.current_url or "").strip().lower()
        login_url = str(CRM_LOGIN_URL or "").strip().lower()
        if "/login" in current_url or "/signin" in current_url:
            return True
        if login_url and current_url.rstrip("/") == login_url.rstrip("/"):
            return True
    except Exception:
        pass

    username_fields = _visible_elements(driver, LOGIN_USERNAME_SELECTORS)
    password_fields = _visible_elements(driver, LOGIN_PASSWORD_SELECTORS)
    login_buttons = _visible_elements(driver, LOGIN_BUTTON_SELECTORS)

    if password_fields and (username_fields or login_buttons):
        return True

    if password_fields:
        try:
            body_text = " ".join((driver.find_element(By.TAG_NAME, "body").text or "").lower().split())
        except Exception:
            body_text = ""
        if any(marker in body_text for marker in LOGIN_HINT_TEXTS):
            return True

    return False


def do_login(driver):
    credential = read_windows_credential(CRM_CREDENTIAL_TARGET)

    username_field = _wait_for_any(driver, LOGIN_USERNAME_SELECTORS, condition="clickable")
    username_field.clear()
    username_field.send_keys(credential.username)

    password_field = _wait_for_any(driver, LOGIN_PASSWORD_SELECTORS, condition="clickable")
    password_field.clear()
    password_field.send_keys(credential.secret)

    login_button = _wait_for_any(driver, LOGIN_BUTTON_SELECTORS, condition="clickable")
    _click_with_fallback(driver, login_button)

    login_reclick_after = time.time() + LOGIN_RETRY_INTERVAL_SECONDS
    deadline = time.time() + max(CRM_ACTION_TIMEOUT, 15)
    while time.time() < deadline:
        if not is_login_page(driver):
            return
        body_text = _body_text(driver).lower()
        if _visible_order_rows(driver) or _looks_like_no_orders_state(driver):
            return
        if any(marker in body_text for marker in POST_LOGIN_TEXT_MARKERS):
            return
        if time.time() >= login_reclick_after:
            if any(marker in body_text for marker in IGNORABLE_LOGIN_ERROR_TEXT_MARKERS):
                print("Ignoring the CRM auth-token login banner and pressing Login again...")
            else:
                print("CRM still appears to be on the login page. Pressing Login again...")
            try:
                login_button = _wait_for_any(driver, LOGIN_BUTTON_SELECTORS, timeout=2, condition="clickable")
                _click_with_fallback(driver, login_button)
            except Exception:
                pass
            login_reclick_after = time.time() + LOGIN_RETRY_INTERVAL_SECONDS
        time.sleep(0.25)

    raise TimeoutException("CRM login did not complete before the timeout expired.")


def login_if_needed(driver, context_message=""):
    if not is_login_page(driver):
        return False

    prefix = f"{context_message} " if context_message else ""
    print(f"{prefix}CRM login detected. Submitting credentials...")
    do_login(driver)
    return True


def wait_for_order_rows(driver, timeout=None, allow_no_orders=False):
    deadline = time.time() + max(timeout or CRM_ACTION_TIMEOUT, 10)
    while time.time() < deadline:
        if is_login_page(driver):
            raise TimeoutException("CRM login page is visible, so order rows are not available yet.")
        visible_rows = _visible_order_rows(driver)
        if visible_rows:
            return visible_rows
        if allow_no_orders and _looks_like_no_orders_state(driver):
            return []
        time.sleep(0.25)
    if allow_no_orders:
        return []
    raise TimeoutException("No CRM order rows became available before the timeout expired.")


def _open_locked_report_rows(driver, list_url=None):
    report_url = _resolve_report_url(list_url)
    print("Opening CRM report...")
    safe_get_with_partial_load(driver, report_url, "CRM report page")

    if login_if_needed(driver):
        safe_get_with_partial_load(driver, report_url, "CRM report page after login")

    try:
        rows = wait_for_order_rows(driver, allow_no_orders=True)
        if rows or _looks_like_no_orders_state(driver):
            return rows
        print("The locked-orders report was blank without a no-orders message; reloading once to confirm.")
        safe_get_with_partial_load(driver, report_url, "CRM report blank-state recovery reload")
        if login_if_needed(driver):
            safe_get_with_partial_load(driver, report_url, "CRM report blank-state recovery after login")
        return wait_for_order_rows(driver, allow_no_orders=True)
    except TimeoutException as row_error:
        if login_if_needed(driver, context_message="Order rows were not available on the first pass."):
            safe_get_with_partial_load(driver, report_url, "CRM report page after login")
            return wait_for_order_rows(driver, allow_no_orders=True)
        raise row_error


def _selected_row_count(driver):
    best_count = 0
    for by, value in SELECTED_ROW_SELECTORS:
        try:
            rows = driver.find_elements(by, value)
        except Exception:
            continue
        visible_rows = 0
        for row in rows:
            try:
                if row.is_displayed():
                    visible_rows += 1
            except StaleElementReferenceException:
                continue
        best_count = max(best_count, visible_rows)
    return best_count


def _wait_for_selection_count(driver, expected_count):
    deadline = time.time() + max(CRM_ACTION_TIMEOUT, 10)
    while time.time() < deadline:
        for element in _visible_elements(driver, SELECTION_TEXT_SELECTORS):
            text = element.text or ""
            count = _extract_first_number(text)
            if count is not None and count >= expected_count:
                return
        if _selected_row_count(driver) >= expected_count:
            return
        time.sleep(0.25)
    raise TimeoutException(f"The CRM never showed {expected_count} selected orders after Shift+Click.")


def _shift_click_row(driver, row):
    """Perform the CRM range-selection gesture, with a DOM-event fallback."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", row)
    except Exception:
        pass

    try:
        ActionChains(driver).key_down(Keys.SHIFT).click(row).key_up(Keys.SHIFT).perform()
        return
    except Exception:
        # Release the modifier in case the native action failed mid-gesture.
        try:
            ActionChains(driver).key_up(Keys.SHIFT).perform()
        except Exception:
            pass

    driver.execute_script(
        "const row = arguments[0];"
        "row.scrollIntoView({block: 'center'});"
        "row.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, shiftKey: true, view: window}));",
        row,
    )


def select_all_orders(driver, rows=None):
    rows = rows if rows is not None else wait_for_order_rows(driver)
    if not rows:
        return 0

    # CRM only creates a range when a normal click establishes the first
    # selection.  Holding Shift while clicking the top row without that
    # anchor selects only the top row, so the multi-order controls never
    # appear.  Mirror the manual gesture: anchor on the bottom order, then
    # Shift+Click the top order to include every visible order.
    first_row = rows[0]
    last_row = rows[-1]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", last_row)
    except Exception:
        pass

    try:
        ActionChains(driver).click(last_row).perform()
    except Exception:
        driver.execute_script(
            "const row = arguments[0];"
            "row.scrollIntoView({block: 'center'});"
            "row.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));",
            last_row,
        )

    _wait_for_selection_count(driver, 1)

    _shift_click_row(driver, first_row)

    _wait_for_selection_count(driver, len(rows))
    return len(rows)


def wait_for_order_preview_panel(driver, timeout=None):
    return _find_best_preview_panel(driver, timeout=timeout)


def select_all_orders_with_preview(driver, rows=None):
    """Select the report range and recover when CRM misses the preview update."""
    rows = rows if rows is not None else wait_for_order_rows(driver)
    selected_count = select_all_orders(driver, rows=rows)

    # The selection count can update even when the Angular preview pane misses
    # the first range event.  Give it a brief chance, then mirror the manual
    # recovery gesture the team uses: hold Shift and click the top order again.
    try:
        return selected_count, wait_for_order_preview_panel(driver, timeout=3)
    except TimeoutException:
        print("Order Preview did not appear after range selection; retrying Shift+Click on the top order...")

    _shift_click_row(driver, rows[0])
    _wait_for_selection_count(driver, len(rows))
    return selected_count, wait_for_order_preview_panel(driver)


def _wait_for_unlock_status_selected(panel):
    deadline = time.time() + 1.0
    while time.time() < deadline:
        for element in _visible_elements(panel, SELECTED_UNLOCK_TEXT_SELECTORS):
            text = " ".join((element.text or "").split())
            value = " ".join((element.get_attribute("value") or "").split())
            if UNLOCK_OPTION_TEXT in text or UNLOCK_OPTION_TEXT in value:
                return True
        time.sleep(0.1)
    print("Warning: the Order Preview panel did not echo the selected unlock text after a short wait, but continuing to Apply/confirmation validation.")
    return False


def choose_unlock_status(driver, panel):
    preview_input = _find_best_preview_input(driver, panel)
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", preview_input)
    except Exception:
        pass

    _click_with_fallback(driver, preview_input)
    preview_input.send_keys(Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL, "a")
    preview_input.send_keys(Keys.DELETE)
    print("Typing unlock status into the Order Preview field...")
    preview_input.send_keys(UNLOCK_OPTION_TEXT)

    try:
        option = _wait_for_any(driver, DROPDOWN_OPTION_SELECTORS, timeout=4, condition="clickable")
    except TimeoutException:
        dropdown_button = _wait_for_any(panel, PREVIEW_DROPDOWN_TOGGLE_SELECTORS, timeout=4, condition="clickable")
        _click_with_fallback(driver, dropdown_button)
        option = _wait_for_any(driver, DROPDOWN_OPTION_SELECTORS, timeout=6, condition="clickable")

    print("Selecting Stock Auto Ordering Unlocked from the dropdown...")
    _click_with_fallback(driver, option)
    _wait_for_unlock_status_selected(panel)


def get_apply_button(panel):
    try:
        return _wait_for_any(panel, APPLY_BUTTON_SELECTORS, timeout=6, condition="clickable")
    except TimeoutException:
        for element in _visible_elements(panel, APPLY_BUTTON_SELECTORS):
            text = " ".join((element.text or "").split())
            if text == "Apply" or "Apply" in text:
                print("Warning: Apply control was visible but not reported as clickable; continuing with the visible element.")
                return element
        raise


def click_apply(driver, panel):
    apply_button = get_apply_button(panel)
    print("Clicking Apply in the Order Preview panel...")
    _click_with_fallback(driver, apply_button)


def wait_for_confirmation_modal(driver, timeout=10):
    return _wait_for_any(driver, CONFIRM_MODAL_SELECTORS, timeout=timeout, condition="visible")


def maybe_wait_for_confirmation_modal(driver, timeout=2):
    try:
        wait_for_confirmation_modal(driver, timeout=timeout)
        return True
    except TimeoutException:
        return False


def click_ok_on_modal(driver):
    wait_for_confirmation_modal(driver)
    ok_button = _wait_for_any(driver, OK_BUTTON_SELECTORS, timeout=10, condition="clickable")
    _click_with_fallback(driver, ok_button)


def _wait_for_order_count_to_settle(driver, previous_order_count=None, timeout=2.5):
    deadline = time.time() + max(0.5, float(timeout or 0))
    last_count = None
    stable_reads = 0
    change_seen = previous_order_count is None

    while time.time() < deadline:
        current_count = _current_order_row_count(driver)
        if previous_order_count is not None and current_count != previous_order_count:
            change_seen = True

        if current_count == last_count:
            stable_reads += 1
            if change_seen and stable_reads >= 2:
                return current_count
        else:
            last_count = current_count
            stable_reads = 1

        time.sleep(0.15)

    return last_count if last_count is not None else _current_order_row_count(driver)


def verify_update_complete(driver, previous_order_count=None):
    normal_wait_seconds = max(CRM_ACTION_TIMEOUT, 15)
    progress_wait_seconds = max(CRM_ACTION_TIMEOUT * 6, 90)
    started_at = time.time()
    deadline = started_at + normal_wait_seconds
    progress_deadline = started_at + progress_wait_seconds
    saw_apply_progress = False
    while time.time() < deadline:
        if _has_update_success_message(driver):
            print("Detected CRM success message after Apply.")
            remaining_order_count = _wait_for_order_count_to_settle(
                driver,
                previous_order_count=previous_order_count,
                timeout=2.0,
            )
            return {
                "success_message_seen": True,
                "remaining_order_count": remaining_order_count,
                "no_orders_remaining": remaining_order_count == 0,
            }
        if _has_unlock_apply_progress(driver):
            saw_apply_progress = True
            deadline = min(progress_deadline, time.time() + normal_wait_seconds)
        time.sleep(0.25)
    if saw_apply_progress:
        raise TimeoutException("The CRM was still applying the unlock update when the extended wait expired.")
    raise TimeoutException("The CRM did not show the unlock success message before the timeout expired.")


def _run_once(action, dry_run=False, headless_mode=True, list_url=None):
    driver = None
    mode_name = "headless" if headless_mode else "visible"
    try:
        print(f"Launching CRM browser in {mode_name} mode...")
        kill_stale_chrome(PROFILE_PATH, profile_label="CRM automation")
        driver = build_chrome_driver(
            PROFILE_PATH,
            headless_mode=headless_mode,
            page_load_strategy="eager",
            page_load_timeout=CRM_PAGE_LOAD_TIMEOUT,
            script_timeout=CRM_ACTION_TIMEOUT,
        )

        all_order_ids = []
        seen_order_ids = set()
        total_order_count = 0
        refresh_passes = 0
        confirmation_reached_any = False
        last_completion = {}

        while True:
            refresh_passes += 1
            rows = _open_locked_report_rows(driver, list_url=list_url)

            if not rows:
                print("No CRM orders were detected in the report.")
                return {
                    "action": action,
                    "order_count": total_order_count,
                    "order_ids": all_order_ids,
                    "dry_run": dry_run,
                    "headless": headless_mode,
                    "no_orders": total_order_count == 0,
                    "apply_ready": total_order_count > 0,
                    "apply_clicked": bool(total_order_count and not dry_run),
                    "confirmation_reached": confirmation_reached_any,
                    "update_complete": bool(total_order_count and not dry_run),
                    "success_message_seen": bool(last_completion.get("success_message_seen")) if last_completion else False,
                    "remaining_order_count": 0,
                    "no_orders_remaining": True,
                    "refresh_passes": refresh_passes,
                }

            order_ids = _collect_order_ids(rows)
            new_ids = [order_id for order_id in order_ids if order_id not in seen_order_ids]
            if order_ids:
                print(f"Captured {len(order_ids)} order ID(s): {_format_order_ids(order_ids)}")
            else:
                print("Warning: no 7-digit order IDs were captured from the visible rows before selection.")
            if order_ids and not new_ids and not dry_run:
                print("All visible locked orders were already attempted in this run; ending the refresh loop.")
                return {
                    "action": action,
                    "order_count": total_order_count,
                    "order_ids": all_order_ids,
                    "dry_run": False,
                    "headless": headless_mode,
                    "no_orders": False,
                    "apply_ready": total_order_count > 0,
                    "apply_clicked": total_order_count > 0,
                    "confirmation_reached": confirmation_reached_any,
                    "update_complete": total_order_count > 0,
                    "success_message_seen": bool(last_completion.get("success_message_seen")) if last_completion else False,
                    "remaining_order_count": len(rows),
                    "no_orders_remaining": False,
                    "refresh_passes": refresh_passes,
                }

            order_count, preview_panel = select_all_orders_with_preview(driver, rows=rows)
            print(f"Selected {order_count} orders.")

            choose_unlock_status(driver, preview_panel)
            get_apply_button(preview_panel)

            for order_id in order_ids:
                if order_id not in seen_order_ids:
                    seen_order_ids.add(order_id)
                    all_order_ids.append(order_id)
            total_order_count += order_count

            if dry_run:
                print("Dry run confirmed the Apply button is clickable; skipping the Apply click.")
                return {
                    "action": action,
                    "order_count": order_count,
                    "order_ids": order_ids,
                    "dry_run": True,
                    "headless": headless_mode,
                    "apply_ready": True,
                    "apply_clicked": False,
                    "confirmation_reached": False,
                    "refresh_passes": refresh_passes,
                }

            click_apply(driver, preview_panel)
            confirmation_reached = maybe_wait_for_confirmation_modal(driver, timeout=2)
            confirmation_reached_any = confirmation_reached_any or confirmation_reached
            if confirmation_reached:
                click_ok_on_modal(driver)
            last_completion = verify_update_complete(driver, previous_order_count=order_count)
            if _looks_like_no_orders_state(driver) or bool(last_completion.get("no_orders_remaining")):
                print(f"Finished CRM unlock refresh pass {refresh_passes}; reopening the locked-orders list to confirm no more orders remain...")
            else:
                print(f"Finished CRM unlock refresh pass {refresh_passes}; reopening the locked-orders list to look for additional orders...")
    except Exception:
        if driver is not None:
            safe_take_screenshot(driver, f"crm_unlock_error_{mode_name}")
        raise
    finally:
        safe_driver_quit(driver, profile_path=PROFILE_PATH)


def run(action, dry_run=False, visible=False, list_url=None):
    started_at = time.monotonic()
    final_mode = bool(CRM_HEADLESS)
    final_error = None
    try:
        _validate_runtime_config(list_url=list_url)

        attempt_modes = [False] if visible else _crm_attempt_modes()

        errors = []
        for index, headless_mode in enumerate(attempt_modes, start=1):
            try:
                result = _run_once(action, dry_run=dry_run, headless_mode=headless_mode, list_url=list_url)
                if dry_run:
                    message = (
                        f"Dry run prepared {result['order_count']} orders and confirmed the Apply button was clickable; the Apply click was skipped."
                    )
                elif result.get("no_orders"):
                    message = "No orders detected in the CRM report. Nothing needed to be unlocked."
                else:
                    remaining_count = int(result.get("remaining_order_count", 0))
                    suffix = " No orders remain in the list." if remaining_count <= 0 else f" {remaining_count} order(s) remain visible after the update."
                    if result.get("confirmation_reached"):
                        message = f"Unlocked {result['order_count']} orders successfully.{suffix}"
                    else:
                        message = (
                            f"Unlocked {result['order_count']} orders successfully with no confirmation modal shown after Apply.{suffix}"
                        )
                write_result_payload(
                    AUTOMATION_NAME,
                    "crm_unlock_orders.py",
                    True,
                    message,
                    extra_fields={**result, "duration_seconds": round(max(0.0, time.monotonic() - started_at), 1)},
                )
                return 0
            except Exception as err:
                errors.append((headless_mode, err))
                if index < len(attempt_modes) and headless_mode and _is_retryable_exception(err):
                    print(f"Headless CRM attempt failed with a retryable error: {err}")
                    print("Retrying once in visible mode...")
                    time.sleep(1)
                    continue
                break

        final_mode, final_error = errors[-1]
    except Exception as err:
        final_mode = bool(CRM_HEADLESS)
        final_error = err

    write_result_payload(
        AUTOMATION_NAME,
        "crm_unlock_orders.py",
        False,
        str(final_error),
        extra_fields={
            "action": action,
            "dry_run": dry_run,
            "headless": final_mode,
            "retryable": _is_retryable_exception(final_error),
            "error_type": type(final_error).__name__,
            "duration_seconds": round(max(0.0, time.monotonic() - started_at), 1),
        },
    )
    return 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="CRM order unlock automation worker")
    parser.add_argument("--action", choices=["unlock_all"], required=True)
    parser.add_argument("--visible", action="store_true", help="Run Chrome visibly instead of headless for testing.")
    parser.add_argument("--dry-run", action="store_true", help="Prepare the unlock selection and confirm Apply is clickable without clicking Apply.")
    parser.add_argument('--list-url', help='Override CRM_LOCKED_URL for a mode-specific unlock report.')
    return parser.parse_args(argv)


if __name__ == "__main__":
    options = parse_args()
    effective_dry_run = bool(options.dry_run or CONFIG_CRM_DRY_RUN)
    sys.exit(run(options.action, dry_run=effective_dry_run, visible=bool(options.visible), list_url=options.list_url))
