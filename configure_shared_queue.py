"""Interactive Supabase shared-queue onboarding without putting secrets in Git."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import uuid

from credential_store import SHARED_QUEUE_CREDENTIAL_TARGET, write_credential
from platform_runtime import get_platform_snapshot
from shared_queue import SharedQueueConfig, SupabaseQueueClient, TaskPayloadCipher
from version_state import get_git_version_state


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _prompt(label, supplied=None, *, secret=False):
    if supplied:
        return str(supplied).strip()
    value = getpass.getpass(f"{label}: ") if secret else input(f"{label}: ")
    value = str(value).strip()
    if not value:
        raise RuntimeError(f"{label} is required.")
    return value


def _connection_values(options, *, workspace_id):
    encryption_key = options.encryption_key
    if not encryption_key and getattr(options, "generate_encryption_key", False):
        encryption_key = TaskPayloadCipher.generate_key()
        print("Generated a new queue encryption key. Reuse this same key when joining the Mac.")
    values = {
        "supabase_url": _prompt("Supabase project URL", options.url),
        "anon_key": _prompt("Supabase anon key", options.anon_key),
        "email": _prompt("Supabase node user email", options.email),
        "password": _prompt("Supabase node user password", secret=True),
        "workspace_id": str(workspace_id),
        "encryption_key": _prompt("Shared Fernet encryption key", encryption_key, secret=True),
    }
    return values


def _store(node_key, values):
    config = SharedQueueConfig.from_mapping(values, node_key=node_key)
    write_credential(
        SHARED_QUEUE_CREDENTIAL_TARGET,
        config.node_key,
        json.dumps(values, separators=(",", ":")),
    )
    print(f"Stored shared queue credentials for node '{config.node_key}' in the OS keychain.")
    return config


def command_bootstrap(options):
    placeholder_workspace = str(uuid.UUID(int=0))
    values = _connection_values(options, workspace_id=placeholder_workspace)
    temporary = SharedQueueConfig.from_mapping(values, node_key=options.node_key)
    workspace_id = SupabaseQueueClient(temporary).create_workspace(options.name)
    values["workspace_id"] = str(workspace_id).strip('"')
    config = _store(options.node_key, values)
    print(f"Created workspace: {config.workspace_id}")
    print("Create the Mac Auth user in Supabase, then run add-member with that user's UUID.")
    return 0


def command_join(options):
    if options.transfer_file:
        with open(options.transfer_file, "r", encoding="utf-8-sig") as handle:
            transfer = json.load(handle)
        options.url = options.url or transfer.get("supabase_url")
        options.anon_key = options.anon_key or transfer.get("anon_key")
        options.workspace_id = options.workspace_id or transfer.get("workspace_id")
        options.encryption_key = options.encryption_key or transfer.get("encryption_key")
    values = _connection_values(options, workspace_id=_prompt("Workspace UUID", options.workspace_id))
    _store(options.node_key, values)
    return 0


def command_status(_options):
    config = SharedQueueConfig.from_keychain()
    print(f"node: {config.node_key}")
    print(f"workspace: {config.workspace_id}")
    print(f"supabase: {config.supabase_url}")
    return 0


def command_test(_options):
    config = SharedQueueConfig.from_keychain()
    state = get_git_version_state(SCRIPT_DIR)
    commit = str(state.get("commit") or "unknown")
    response = SupabaseQueueClient(config).heartbeat(
        commit=commit,
        capabilities=get_platform_snapshot().capabilities,
        display_name=socket.gethostname(),
    )
    print(json.dumps(response, indent=2))
    return 0 if response and response.get("eligible") else 2


def command_add_member(options):
    config = SharedQueueConfig.from_keychain()
    result = SupabaseQueueClient(config).add_workspace_member(options.user_id, options.role)
    print(json.dumps(result, indent=2))
    return 0


def command_set_gate(options):
    config = SharedQueueConfig.from_keychain()
    state = get_git_version_state(SCRIPT_DIR)
    commit = options.commit or state.get("commit")
    if not commit:
        raise RuntimeError("Could not determine the Git commit.")
    result = SupabaseQueueClient(config).set_version_gate(commit)
    print(json.dumps(result, indent=2))
    return 0


def command_export_transfer(options):
    config = SharedQueueConfig.from_keychain()
    payload = {
        "supabase_url": config.supabase_url,
        "anon_key": config.anon_key,
        "workspace_id": config.workspace_id,
        "encryption_key": config.encryption_key,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(os.path.abspath(options.output), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    print(f"Wrote one-time Mac queue transfer bundle: {options.output}")
    print("Import it on the Mac, then delete every copy.")
    return 0


def _connection_arguments(parser, *, include_workspace):
    parser.add_argument("--node-key", default=socket.gethostname())
    parser.add_argument("--url")
    parser.add_argument("--anon-key")
    parser.add_argument("--email")
    parser.add_argument("--encryption-key")
    parser.add_argument("--generate-encryption-key", action="store_true")
    if include_workspace:
        parser.add_argument("--workspace-id")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser("bootstrap", help="Create the workspace as its first/owner node.")
    _connection_arguments(bootstrap, include_workspace=False)
    bootstrap.add_argument("--name", default="Work Automation")
    bootstrap.set_defaults(handler=command_bootstrap)
    join = subparsers.add_parser("join", help="Store credentials for an added Windows or Mac node.")
    _connection_arguments(join, include_workspace=True)
    join.set_defaults(handler=command_join)
    join.add_argument("--transfer-file")
    status = subparsers.add_parser("status")
    status.set_defaults(handler=command_status)
    test = subparsers.add_parser("test")
    test.set_defaults(handler=command_test)
    add_member = subparsers.add_parser("add-member")
    add_member.add_argument("user_id")
    add_member.add_argument("--role", choices=("owner", "operator", "viewer"), default="operator")
    add_member.set_defaults(handler=command_add_member)
    gate = subparsers.add_parser("set-version-gate")
    gate.add_argument("--commit")
    gate.set_defaults(handler=command_set_gate)
    export = subparsers.add_parser("export-transfer")
    export.add_argument("output")
    export.set_defaults(handler=command_export_transfer)
    return parser


def main(argv=None):
    options = build_parser().parse_args(argv)
    return options.handler(options)


if __name__ == "__main__":
    raise SystemExit(main())
