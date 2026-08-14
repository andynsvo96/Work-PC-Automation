"""Shared Paycom login form handling for hours and punch automations."""

import os
import time

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from automation_runtime import find_visible
from credential_store import read_paycom_credential
import config as config_module


PAYCOM_USERNAME_SELECTORS = [
    "input[name='username']",
    "input[name='userName']",
    "input[id*='username']",
    "input[autocomplete='username']",
    "input[type='email']",
    "input[name*='user' i]",
    "input[id*='user' i]",
]
PAYCOM_PASSWORD_SELECTORS = [
    "input[name='password']",
    "input[id*='password']",
    "input[autocomplete='current-password']",
    "input[type='password']:not([maxlength='4'])",
    "input[name*='pass' i]",
    "input[id*='pass' i]",
]
PAYCOM_PIN_SELECTORS = [
    "input[name='pin']",
    "input[id*='pin']",
    "input[placeholder*='PIN']",
    "input[type='password'][maxlength='4']",
    "input[name*='pin' i]",
    "input[id*='pin' i]",
    "input[name*='ssn' i]",
    "input[id*='ssn' i]",
    "input[placeholder*='last 4' i]",
]
PAYCOM_LOGIN_BUTTON_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
]


class PaycomTrustedSessionRequiredError(RuntimeError):
    """Raised before WebDriver can create a new Paycom authentication attempt."""


def paycom_trusted_session_required():
    value = os.getenv("PAYCOM_REQUIRE_TRUSTED_SESSION")
    if value is None:
        value = getattr(config_module, "PAYCOM_REQUIRE_TRUSTED_SESSION", False)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _normalize_text(text):
    return " ".join((text or "").replace("\xa0", " ").split())


def is_paycom_login_page(driver):
    """Return whether Paycom is displaying its employee credential form."""
    try:
        current_url = (driver.current_url or "").lower()
        if "/app/login" in current_url or "ee-login" in current_url:
            return True
    except Exception:
        pass

    try:
        text = _normalize_text(driver.find_element(By.TAG_NAME, "body").text).lower()
    except Exception:
        text = ""
    has_login_labels = "username" in text and "password" in text
    has_pin_label = "last 4 digits" in text or "pin" in text
    has_submit_label = "log in" in text or "login" in text
    if has_login_labels and has_pin_label and has_submit_label:
        return True

    try:
        return bool(
            driver.find_elements(By.CSS_SELECTOR, ", ".join(PAYCOM_USERNAME_SELECTORS))
            and driver.find_elements(By.CSS_SELECTOR, ", ".join(PAYCOM_PASSWORD_SELECTORS))
            and driver.find_elements(By.CSS_SELECTOR, ", ".join(PAYCOM_PIN_SELECTORS))
        )
    except Exception:
        return False


def find_paycom_login_fields(driver):
    """Find the three visible Paycom login fields with one shared selector set."""
    return (
        find_visible(driver, PAYCOM_USERNAME_SELECTORS, timeout=3),
        find_visible(driver, PAYCOM_PASSWORD_SELECTORS, timeout=1),
        find_visible(driver, PAYCOM_PIN_SELECTORS, timeout=1),
    )


def _field_value(field):
    if field is None:
        return ""
    try:
        return str(field.get_attribute("value") or "").strip()
    except Exception:
        return ""


def _wait_for_browser_autofill(fields, timeout=2.0):
    """Give the shared Chrome profile a moment to populate saved login values."""
    present_fields = [field for field in fields if field is not None]
    if not present_fields:
        return
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if all(_field_value(field) for field in present_fields):
            return
        time.sleep(0.1)


def submit_paycom_login(
    driver,
    fields,
    *,
    context="Paycom",
    allow_credential_submission=True,
):
    """Submit a detected login form, preferring Chrome autofill before OS secrets.

    Returns True only when a login form was submitted. When there are no fields,
    the caller is already authenticated and this function makes no click.
    """
    username_field, password_field, pin_field = fields
    if not any(fields):
        return False

    if not allow_credential_submission:
        raise PaycomTrustedSessionRequiredError(
            "Paycom needs a native Chrome login. Open Paycom in regular Chrome, finish login, "
            "then close Chrome before retrying. Automation did not submit credentials."
        )

    _wait_for_browser_autofill(fields)
    missing_fields = [
        field
        for field in (username_field, password_field, pin_field)
        if field is not None and not _field_value(field)
    ]
    if missing_fields:
        credentials = read_paycom_credential()
        values = (
            (username_field, credentials.username),
            (password_field, credentials.password),
            (pin_field, credentials.pin),
        )
        for field, value in values:
            if field is not None and not _field_value(field):
                field.clear()
                field.send_keys(value)

    login_btn = find_visible(driver, PAYCOM_LOGIN_BUTTON_SELECTORS, timeout=2)
    if not login_btn:
        raise RuntimeError("Paycom login form did not expose a Log In button.")

    login_btn.click()
    print(f"Clicked Log In for {context}.")
    try:
        WebDriverWait(driver, 8).until(EC.staleness_of(login_btn))
    except TimeoutException:
        pass
    return True
