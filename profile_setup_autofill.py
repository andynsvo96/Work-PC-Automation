"""Open a setup profile and safely prefill its login form from OS secrets.

This module deliberately never clicks a sign-in button. A person completes
login and any two-factor or CAPTCHA challenge in the detached browser.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from selenium.webdriver.common.by import By

from automation_runtime import (
    build_chrome_driver,
    is_chrome_profile_in_use,
    safe_driver_quit,
    safe_get_with_partial_load,
)
from credential_store import (
    CRM_CREDENTIAL_TARGET,
    SANMAR_CREDENTIAL_TARGET,
    SALESFORCE_CREDENTIAL_TARGET,
    SLACK_CREDENTIAL_TARGET,
    CredentialStoreError,
    read_credential,
    read_paycom_credential,
)


@dataclass(frozen=True)
class SetupAutofillResult:
    service: str
    fields_filled: tuple[str, ...]
    credential_available: bool = True

    @property
    def message(self) -> str:
        if not self.credential_available:
            return f"Opened {self.service.title()} setup profile; no stored login is available to fill."
        if self.fields_filled:
            return f"Opened {self.service.title()} setup profile and filled: {', '.join(self.fields_filled)}."
        return f"Opened {self.service.title()} setup profile; no login fields were detected (it may already be signed in)."


class ChromeProfileInUseError(RuntimeError):
    """Raised when setup would collide with an operator-owned Chrome window."""


_USERNAME_SELECTORS = (
    "input[name='username']", "input[name='userName']", "input[name='email']", "input[name='login']",
    "input[id*='username' i]", "input[id*='email' i]", "input[type='email']", "input[autocomplete='username']",
)
_PASSWORD_SELECTORS = (
    "input[name='password']", "input[id*='password' i]", "input[type='password']:not([maxlength='4'])",
    "input[autocomplete='current-password']",
)
_PIN_SELECTORS = (
    "input[name='pin']", "input[id*='pin' i]", "input[placeholder*='PIN' i]", "input[type='password'][maxlength='4']",
)


def _first_visible(driver, selectors):
    for selector in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue
        for element in elements:
            try:
                if element.is_displayed() and element.is_enabled():
                    return element
            except Exception:
                continue
    return None


def _fill(element, value):
    if not element or not str(value or ""):
        return False
    element.clear()
    element.send_keys(str(value))
    return True


def _credential_values(service):
    service = str(service or "").strip().lower()
    if service == "paycom":
        credential = read_paycom_credential()
        return credential.username, credential.password, credential.pin
    targets = {
        "crm": CRM_CREDENTIAL_TARGET,
        "sanmar": SANMAR_CREDENTIAL_TARGET,
        "slack": SLACK_CREDENTIAL_TARGET,
        "salesforce": SALESFORCE_CREDENTIAL_TARGET,
    }
    target = targets.get(service)
    if not target:
        raise ValueError(f"Unsupported setup profile '{service}'.")
    credential = read_credential(target)
    return credential.username, credential.secret, ""


def open_and_prefill_setup_profile(service, profile_path, url, wait_seconds=10):
    """Launch a visible detached profile and populate fields from OS storage."""
    service = str(service or "").strip().lower()
    if is_chrome_profile_in_use(profile_path):
        raise ChromeProfileInUseError(
            f"The {service.title()} setup profile is already open. Close that profile window, then try Setup again."
        )
    try:
        username, password, pin = _credential_values(service)
        credential_available = True
    except CredentialStoreError:
        # The setup window is still useful for a first manual login. Never
        # expose secrets or backend/keychain details in the UI response.
        username, password, pin = "", "", ""
        credential_available = False
    driver = None
    try:
        driver = build_chrome_driver(
            profile_path, headless_mode=False, page_load_strategy="eager", page_load_timeout=30,
            script_timeout=20, detach=True,
        )
        safe_get_with_partial_load(driver, url, f"{service.title()} setup page")
        if not credential_available:
            return SetupAutofillResult(service, (), credential_available=False)
        deadline = time.monotonic() + max(1, int(wait_seconds))
        while time.monotonic() < deadline:
            username_field = _first_visible(driver, _USERNAME_SELECTORS)
            password_field = _first_visible(driver, _PASSWORD_SELECTORS)
            pin_field = _first_visible(driver, _PIN_SELECTORS) if service == "paycom" else None
            if username_field or password_field or pin_field:
                filled = []
                if _fill(username_field, username):
                    filled.append("username")
                if _fill(password_field, password):
                    filled.append("password")
                if _fill(pin_field, pin):
                    filled.append("PIN")
                return SetupAutofillResult(service, tuple(filled))
            time.sleep(0.25)
        return SetupAutofillResult(service, ())
    finally:
        if driver:
            # Setup is handed to the operator after credentials are filled.
            # Do not send WebDriver's QUIT command, which closes the exact
            # browser window the operator needs for login and two-factor auth.
            safe_driver_quit(driver, profile_path=profile_path, keep_browser_open=True)
