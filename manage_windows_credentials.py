"""Manage this project's secrets in Windows Credential Manager.

Examples:
    python manage_windows_credentials.py status
    python manage_windows_credentials.py migrate-config --delete-source-files
    python manage_windows_credentials.py set crm
"""

from __future__ import annotations

import argparse
import ast
import getpass
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

from windows_credentials import (
    CREDENTIAL_TARGETS,
    CRM_CREDENTIAL_TARGET,
    GOOGLE_SHEETS_CREDENTIAL_TARGET,
    PAYCOM_CREDENTIAL_TARGET,
    SALESFORCE_CREDENTIAL_TARGET,
    SANMAR_CREDENTIAL_TARGET,
    credential_exists,
    read_windows_credential,
    write_windows_credential,
)
from credential_store import build_paycom_secret


PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.py"
SENSITIVE_CONFIG_KEYS = {
    "PIN",
    "PAYCOM_USERNAME",
    "PAYCOM_PASSWORD",
    "CRM_USERNAME",
    "CRM_PASSWORD",
    "SANMAR_USERNAME",
    "SANMAR_PASSWORD",
    "SALESFORCE_USERNAME",
    "SALESFORCE_PASSWORD",
    "GOOGLE_SHEETS_CREDENTIALS_FILE",
}


def _load_local_config():
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Local config was not found: {CONFIG_PATH}")
    spec = importlib.util.spec_from_file_location("migration_local_config", CONFIG_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load local config.py.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assignment_ranges(source: str) -> list[tuple[int, int]]:
    tree = ast.parse(source)
    ranges = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id in SENSITIVE_CONFIG_KEYS:
            ranges.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return ranges


def _remove_sensitive_config_assignments() -> None:
    source = CONFIG_PATH.read_text(encoding="utf-8-sig")
    lines = source.splitlines()
    for start, end in sorted(_assignment_ranges(source), reverse=True):
        del lines[start - 1 : end]
    new_source = "\n".join(lines).rstrip() + "\n"
    handle, temp_name = tempfile.mkstemp(
        prefix="config.", suffix=".credential-migration.tmp", dir=str(PROJECT_ROOT)
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(new_source)
        os.replace(temp_name, CONFIG_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _login_plan(module, username_key: str, password_key: str, target: str, label: str):
    username = str(getattr(module, username_key, "") or "").strip()
    password = str(getattr(module, password_key, "") or "")
    if bool(username) != bool(password):
        raise RuntimeError(f"{label} has only one of username/password in config.py; migration stopped.")
    if username and password:
        return target, username, password, "migrated"
    if credential_exists(target):
        return None, None, None, "already present"
    return None, None, None, "not configured"


def migrate_config(delete_source_files: bool = False) -> int:
    module = _load_local_config()
    plans = []
    statuses = {}

    paycom_username = str(getattr(module, "PAYCOM_USERNAME", "") or "").strip()
    paycom_password = str(getattr(module, "PAYCOM_PASSWORD", "") or "")
    pin = str(getattr(module, "PIN", "") or "")
    if any((paycom_username, paycom_password, pin)) and not all((paycom_username, paycom_password, pin)):
        raise RuntimeError(
            "Paycom migration requires PAYCOM_USERNAME, PAYCOM_PASSWORD, and PIN in config.py."
        )
    if paycom_username:
        plans.append((PAYCOM_CREDENTIAL_TARGET, paycom_username, build_paycom_secret(paycom_password, pin)))
        statuses[PAYCOM_CREDENTIAL_TARGET] = "migrated"
    elif credential_exists(PAYCOM_CREDENTIAL_TARGET):
        statuses[PAYCOM_CREDENTIAL_TARGET] = "already present"
    else:
        statuses[PAYCOM_CREDENTIAL_TARGET] = "not configured"

    for username_key, password_key, target, label in (
        ("CRM_USERNAME", "CRM_PASSWORD", CRM_CREDENTIAL_TARGET, "CRM"),
        ("SANMAR_USERNAME", "SANMAR_PASSWORD", SANMAR_CREDENTIAL_TARGET, "SanMar"),
        (
            "SALESFORCE_USERNAME",
            "SALESFORCE_PASSWORD",
            SALESFORCE_CREDENTIAL_TARGET,
            "Salesforce",
        ),
    ):
        target_value, username, password, status = _login_plan(
            module, username_key, password_key, target, label
        )
        statuses[target] = status
        if target_value:
            plans.append((target_value, username, password))

    google_source = None
    google_path_value = str(getattr(module, "GOOGLE_SHEETS_CREDENTIALS_FILE", "") or "").strip()
    if google_path_value:
        google_source = Path(google_path_value)
        if not google_source.is_absolute():
            google_source = PROJECT_ROOT / google_source
        google_source = google_source.resolve()
        if not google_source.is_file():
            raise RuntimeError("The configured Google Sheets service-account file was not found.")
        google_info = json.loads(google_source.read_text(encoding="utf-8-sig"))
        if not isinstance(google_info, dict) or not google_info.get("client_email") or not google_info.get("private_key"):
            raise RuntimeError("The Google Sheets service-account file is missing required fields.")
        compact_json = json.dumps(google_info, separators=(",", ":"), ensure_ascii=False)
        plans.append(
            (
                GOOGLE_SHEETS_CREDENTIAL_TARGET,
                str(google_info["client_email"]),
                compact_json,
            )
        )
        statuses[GOOGLE_SHEETS_CREDENTIAL_TARGET] = "migrated"
    elif credential_exists(GOOGLE_SHEETS_CREDENTIAL_TARGET):
        statuses[GOOGLE_SHEETS_CREDENTIAL_TARGET] = "already present"
    else:
        statuses[GOOGLE_SHEETS_CREDENTIAL_TARGET] = "not configured"

    for target, username, secret in plans:
        write_windows_credential(target, username, secret)

    for target, username, secret in plans:
        stored = read_windows_credential(target)
        if stored.username != username or stored.secret != secret:
            raise RuntimeError(f"Verification failed for Windows credential '{target}'.")

    _remove_sensitive_config_assignments()

    if delete_source_files and google_source is not None:
        try:
            google_source.relative_to(PROJECT_ROOT)
        except ValueError:
            print("Google credential source is outside the project and was not deleted.")
        else:
            google_source.unlink()

    for target in CREDENTIAL_TARGETS.values():
        print(f"{target}: {statuses.get(target, 'not configured')}")
    print("Plaintext credential assignments were removed from config.py.")
    return 0


def set_credential(service: str) -> int:
    target = CREDENTIAL_TARGETS[service]
    if service == "paycom":
        username = input("Paycom username: ").strip()
        password = getpass.getpass("Paycom password: ")
        pin = getpass.getpass("Paycom 4-digit PIN: ")
        secret = build_paycom_secret(password, pin)
    elif service == "google_sheets":
        source = Path(input("Service-account JSON path: ").strip().strip('"')).expanduser().resolve()
        info = json.loads(source.read_text(encoding="utf-8-sig"))
        username = str(info.get("client_email") or "").strip()
        secret = json.dumps(info, separators=(",", ":"), ensure_ascii=False)
    else:
        username = input(f"{service.replace('_', ' ').title()} username: ").strip()
        secret = getpass.getpass(f"{service.replace('_', ' ').title()} password: ")
    write_windows_credential(target, username, secret)
    stored = read_windows_credential(target)
    if stored.username != username or stored.secret != secret:
        raise RuntimeError(f"Verification failed for Windows credential '{target}'.")
    print(f"Stored and verified: {target}")
    return 0


def show_status() -> int:
    for service, target in CREDENTIAL_TARGETS.items():
        status = "present" if credential_exists(target) else "missing"
        print(f"{service}: {target} ({status})")
    return 0


def export_google_sheets(destination: str) -> int:
    """Restore the service-account JSON without printing credential contents."""
    credential = read_windows_credential(GOOGLE_SHEETS_CREDENTIAL_TARGET)
    info = json.loads(credential.secret)
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise RuntimeError(
            f"Windows credential '{GOOGLE_SHEETS_CREDENTIAL_TARGET}' is not a valid service-account payload."
        )

    output_path = Path(destination)
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path
    output_path = output_path.resolve()
    if output_path.exists():
        raise RuntimeError(f"Refusing to overwrite existing credential file: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=str(output_path.parent),
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(info, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp_name, output_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)

    print(f"Restored Google Sheets service-account JSON: {output_path}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    migrate_parser = subparsers.add_parser(
        "migrate-config", help="Move existing config.py credentials into Windows Credential Manager."
    )
    migrate_parser.add_argument(
        "--delete-source-files",
        action="store_true",
        help="Delete an in-project Google service-account JSON after verified migration.",
    )
    set_parser = subparsers.add_parser("set", help="Create or replace one credential.")
    set_parser.add_argument("service", choices=sorted(CREDENTIAL_TARGETS))
    export_parser = subparsers.add_parser(
        "export-google-sheets",
        help="Restore the Google service-account JSON from Windows Credential Manager.",
    )
    export_parser.add_argument(
        "destination",
        nargs="?",
        default=r"keys\printfly-468803-4ac8ef0bd4ad.json",
    )
    subparsers.add_parser("status", help="Show targets and presence without displaying values.")
    args = parser.parse_args(argv)
    if args.command == "migrate-config":
        return migrate_config(delete_source_files=args.delete_source_files)
    if args.command == "set":
        return set_credential(args.service)
    if args.command == "export-google-sheets":
        return export_google_sheets(args.destination)
    return show_status()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
