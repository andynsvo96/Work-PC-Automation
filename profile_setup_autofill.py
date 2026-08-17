"""Open a setup profile and safely prefill its login form from OS secrets.

This module deliberately never clicks a sign-in button. A person completes
login and any two-factor or CAPTCHA challenge in the detached browser.
"""

from __future__ import annotations

import json
import time
import os
import subprocess
import sys
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
    PAYCOM_CREDENTIAL_TARGET,
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


def open_native_setup_profile(service, profile_path, url, chrome_executable):
    """Open a real Chrome profile without any WebDriver/testing identity.

    Paycom's initial device-trust enrollment must be completed in native Chrome
    because its invisible hCaptcha can reject WebDriver-controlled sessions,
    including otherwise-correct six-digit MFA codes.
    This function does not automate or bypass login, MFA, or CAPTCHA.
    """
    service = str(service or "").strip().lower()
    if is_chrome_profile_in_use(profile_path):
        raise ChromeProfileInUseError(
            f"The {service.title()} setup profile is already open. Close that profile window, then try Setup again."
        )
    os.makedirs(profile_path, exist_ok=True)
    chrome_args = [
        f"--user-data-dir={profile_path}",
        "--profile-directory=Default",
        "--new-window",
        url,
    ]
    if sys.platform == "darwin":
        # A direct executable launch can be forwarded into the existing
        # default Chrome instance, dropping the requested user-data-dir.
        # Launch Services -n forces a separate native app instance.
        command = ["open", "-na", "Google Chrome", "--args", *chrome_args]
    else:
        command = [chrome_executable, *chrome_args]
    subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return SetupAutofillResult(service, (), credential_available=False)


_USERNAME_SELECTORS = (
    "input[name='username']", "input[name='userName']", "input[name='j_username']",
    "input[name='email']", "input[name='login']", "input[name='loginfmt']",
    "input[id*='username' i]", "input[id*='email' i]", "input[type='email']",
    "input[autocomplete='username']", "input[autocomplete='email']",
)
_PASSWORD_SELECTORS = (
    "input[name='password']", "input[name='j_password']", "input[id*='password' i]",
    "input[type='password']:not([maxlength='4'])", "input[autocomplete='current-password']",
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
    value = str(value)
    try:
        current_value = str(element.get_attribute("value") or "")
    except Exception:
        current_value = ""
    if current_value != value:
        element.clear()
        element.send_keys(value)
    try:
        return str(element.get_attribute("value") or "") == value
    except Exception:
        # Some test doubles and unusual browser elements do not expose a
        # readable value even though send_keys succeeded.
        return True


def _element_token(label, element):
    """Return a stable key so a visible field is not cleared every poll."""
    try:
        remote_id = str(element.id or "")
    except Exception:
        remote_id = ""
    return label, remote_id or id(element)


def _expected_fields(service):
    fields = {"username", "password"}
    if service == "paycom":
        fields.add("PIN")
    return fields


def _credential_values(service):
    service = str(service or "").strip().lower()
    if service == "paycom":
        try:
            credential = read_paycom_credential()
            return credential.username, credential.password, credential.pin
        except CredentialStoreError as complete_error:
            # Paycom credentials created before July 23, 2026 stored the
            # username and password but had no PIN field. Setup should still
            # prefill those recoverable values instead of silently opening an
            # entirely blank form. The normal clock/hour workers continue to
            # require the complete credential and therefore remain fail-safe.
            legacy = read_credential(PAYCOM_CREDENTIAL_TARGET)
            username = str(legacy.username or "").strip()
            password = str(legacy.secret or "")
            if username == "PIN":
                raise complete_error
            try:
                payload = json.loads(password)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                password = str(payload.get("password") or "")
            if username and password:
                return username, password, ""
            raise complete_error
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
        expected = _expected_fields(service)
        filled = []
        filled_elements = set()
        while time.monotonic() < deadline:
            username_field = _first_visible(driver, _USERNAME_SELECTORS)
            password_field = _first_visible(driver, _PASSWORD_SELECTORS)
            pin_field = _first_visible(driver, _PIN_SELECTORS) if service == "paycom" else None
            for label, element, value in (
                ("username", username_field, username),
                ("password", password_field, password),
                ("PIN", pin_field, pin),
            ):
                if element is None:
                    continue
                token = _element_token(label, element)
                if token not in filled_elements and _fill(element, value):
                    filled_elements.add(token)
                    if label not in filled:
                        filled.append(label)
            if expected.issubset(filled):
                return SetupAutofillResult(service, tuple(filled))
            time.sleep(0.25)
        return SetupAutofillResult(service, tuple(filled))
    finally:
        if driver:
            # Setup is handed to the operator after credentials are filled.
            # Do not send WebDriver's QUIT command, which closes the exact
            # browser window the operator needs for login and two-factor auth.
            safe_driver_quit(driver, profile_path=profile_path, keep_browser_open=True)
