"""Read-only live authentication checks for credential-backed services.

No usernames, passwords, tokens, keys, or business records are printed.
Browser checks stop after confirming an authenticated landing page.
"""

from __future__ import annotations

import argparse
import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WORKERS_DIR = os.path.join(PROJECT_ROOT, "workers")
if WORKERS_DIR not in sys.path:
    sys.path.insert(0, WORKERS_DIR)


def check_google_sheets():
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials

    from credential_store import GOOGLE_SHEETS_CREDENTIAL_TARGET, read_json_credential

    info = read_json_credential(GOOGLE_SHEETS_CREDENTIAL_TARGET)
    credential = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    credential.refresh(Request())
    if not credential.valid:
        raise RuntimeError("Google Sheets OAuth did not return a valid access token.")
    return "Google Sheets OAuth is valid."


def check_shared_queue():
    from shared_queue import SharedQueueConfig, SupabaseQueueClient

    client = SupabaseQueueClient(SharedQueueConfig.from_keychain())
    nodes = client.list_nodes()
    return f"Shared queue authentication is valid; {len(nodes)} node(s) are visible."


def check_sanmar():
    import crm_shipping_bypasser as worker

    driver = None
    profile_path = os.path.abspath(worker.SANMAR_PROFILE_PATH)
    try:
        driver = worker._build_sanmar_driver(visible=False)
        if not worker._ensure_sanmar_logged_in(driver):
            raise RuntimeError("SanMar did not reach an authenticated page.")
        return "SanMar login is valid."
    finally:
        worker.safe_driver_quit(driver, profile_path=profile_path)


def check_salesforce():
    import crm_copyright_cancel as worker
    from automation_runtime import (
        build_chrome_driver,
        kill_stale_chrome,
        resolve_existing_automation_profile_path,
        safe_driver_quit,
        safe_get_with_partial_load,
        safe_take_screenshot,
    )
    from config import PROCESSOR_ACTION_TIMEOUT, PROCESSOR_PAGE_LOAD_TIMEOUT, PROCESSOR_PROFILE_DIR

    profile_path = resolve_existing_automation_profile_path(
        os.path.join(PROJECT_ROOT, PROCESSOR_PROFILE_DIR)
    )
    driver = None
    try:
        kill_stale_chrome(profile_path, profile_label="Salesforce health check")
        driver = build_chrome_driver(
            profile_path,
            headless_mode=True,
            page_load_strategy="eager",
            page_load_timeout=max(30, PROCESSOR_PAGE_LOAD_TIMEOUT),
            script_timeout=PROCESSOR_ACTION_TIMEOUT,
        )
        safe_get_with_partial_load(driver, "https://login.salesforce.com/", "Salesforce login")
        if worker._is_salesforce_login_page(driver):
            worker._attempt_salesforce_login(driver, timeout=45)
        if worker._is_salesforce_login_page(driver) or worker._is_salesforce_login_approval_page(driver):
            raise RuntimeError("Salesforce remained on a login or approval page.")
        return "Salesforce login is valid."
    except Exception:
        if driver is not None:
            safe_take_screenshot(driver, "salesforce_health_error")
        raise
    finally:
        safe_driver_quit(driver, profile_path=profile_path)


CHECKS = {
    "google_sheets": check_google_sheets,
    "shared_queue": check_shared_queue,
    "sanmar": check_sanmar,
    "salesforce": check_salesforce,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("service", choices=sorted(CHECKS))
    args = parser.parse_args(argv)
    try:
        message = CHECKS[args.service]()
    except Exception as exc:
        print(f"{args.service}: FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(f"{args.service}: PASS: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
