"""Cross-platform credential setup for Windows Credential Manager/macOS Keychain."""

from __future__ import annotations

import argparse
import getpass
import json
import os

from credential_store import (
    CREDENTIAL_TARGETS,
    credential_exists,
    delete_credential,
    read_credential,
    write_credential,
)


def _target(service):
    key = str(service or "").strip().lower()
    if key not in CREDENTIAL_TARGETS:
        raise ValueError(f"Unknown service '{service}'. Choose: {', '.join(sorted(CREDENTIAL_TARGETS))}")
    return CREDENTIAL_TARGETS[key]


def command_status(_options):
    for service, target in CREDENTIAL_TARGETS.items():
        print(f"{service}: {'configured' if credential_exists(target) else 'missing'}")
    return 0


def command_set(options):
    target = _target(options.service)
    username = options.username or input(f"{options.service} username/account label: ").strip()
    if options.json_file:
        with open(options.json_file, "r", encoding="utf-8-sig") as handle:
            parsed = json.load(handle)
        secret = json.dumps(parsed, separators=(",", ":"))
    else:
        secret = getpass.getpass(f"{options.service} secret/password: ")
    write_credential(target, username, secret)
    print(f"Stored {options.service} in the operating system keychain.")
    return 0


def command_delete(options):
    target = _target(options.service)
    removed = delete_credential(target, missing_ok=True)
    print(f"{'Deleted' if removed else 'No stored'} {options.service} credential.")
    return 0


def command_migrate_windows(options):
    if os.name != "nt":
        raise RuntimeError("Legacy Windows migration can only run on Windows.")
    from windows_credentials import read_windows_credential

    services = [options.service] if options.service else sorted(CREDENTIAL_TARGETS)
    migrated = 0
    for service in services:
        target = _target(service)
        legacy = read_windows_credential(target, required=False)
        if legacy is None:
            print(f"{service}: no legacy credential")
            continue
        write_credential(target, legacy.username, legacy.secret)
        migrated += 1
        print(f"{service}: migrated")
    print(f"Migrated {migrated} credential(s). Legacy values were retained for rollback.")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.set_defaults(handler=command_status)
    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("service", choices=sorted(CREDENTIAL_TARGETS))
    set_parser.add_argument("--username")
    set_parser.add_argument("--json-file")
    set_parser.set_defaults(handler=command_set)
    delete = subparsers.add_parser("delete")
    delete.add_argument("service", choices=sorted(CREDENTIAL_TARGETS))
    delete.set_defaults(handler=command_delete)
    migrate = subparsers.add_parser("migrate-windows")
    migrate.add_argument("service", choices=sorted(CREDENTIAL_TARGETS), nargs="?")
    migrate.set_defaults(handler=command_migrate_windows)
    return parser


def main(argv=None):
    options = build_parser().parse_args(argv)
    return options.handler(options)


if __name__ == "__main__":
    raise SystemExit(main())
