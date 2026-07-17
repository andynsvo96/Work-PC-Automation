"""Create or import the shared Tailscale web-app PIN bundle."""

from __future__ import annotations

import argparse
import getpass
import json
import os

from app_security import AppSecurityConfig, create_app_security, load_app_security, store_app_security


def _write_private_json(path, payload):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(os.path.abspath(path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def command_create(options):
    pin = getpass.getpass("Choose a 6-12 digit app PIN: ")
    confirmation = getpass.getpass("Confirm app PIN: ")
    if pin != confirmation:
        raise RuntimeError("PIN confirmation did not match.")
    config = create_app_security(pin)
    store_app_security(config)
    if options.output:
        _write_private_json(options.output, config.to_dict())
        print(f"Stored app security locally and wrote the one-time transfer bundle: {options.output}")
        print("Import it on the Mac, then delete every copy of the transfer bundle.")
    else:
        print("Stored app security in this computer's OS keychain.")
    return 0


def command_import(options):
    with open(options.path, "r", encoding="utf-8-sig") as handle:
        values = json.load(handle)
    config = AppSecurityConfig(
        pin_hash=str(values.get("pin_hash") or ""),
        session_secret=str(values.get("session_secret") or ""),
    )
    if not config.pin_hash or len(config.session_secret) < 32:
        raise RuntimeError("App security transfer bundle is incomplete.")
    store_app_security(config)
    print("Imported the shared app PIN/session secret into this computer's OS keychain.")
    print("Delete the transfer bundle now.")
    return 0


def command_status(_options):
    config = load_app_security()
    print(f"App security is configured (shared session secret: {len(config.session_secret)} characters).")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--output", help="One-time JSON bundle to import on the other computer.")
    create.set_defaults(handler=command_create)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("path")
    import_parser.set_defaults(handler=command_import)
    status = subparsers.add_parser("status")
    status.set_defaults(handler=command_status)
    return parser


def main(argv=None):
    options = build_parser().parse_args(argv)
    return options.handler(options)


if __name__ == "__main__":
    raise SystemExit(main())
