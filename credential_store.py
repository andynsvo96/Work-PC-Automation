"""Portable secret storage backed by Keychain/Credential Manager via keyring.

Windows keeps a read fallback for the existing native credential targets so an
upgrade does not invalidate credentials that are already installed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from windows_credentials import (
    CREDENTIAL_TARGETS,
    CRM_CREDENTIAL_TARGET,
    GOOGLE_SHEETS_CREDENTIAL_TARGET,
    PAYCOM_CREDENTIAL_TARGET,
    SALESFORCE_CREDENTIAL_TARGET,
    SANMAR_CREDENTIAL_TARGET,
    SHARED_QUEUE_CREDENTIAL_TARGET,
    APP_SECURITY_CREDENTIAL_TARGET,
)

try:
    import keyring
    from keyring.errors import KeyringError
except Exception:  # pragma: no cover - depends on optional platform package
    keyring = None

    class KeyringError(Exception):
        pass


KEYRING_ACCOUNT = "automation"


class CredentialStoreError(RuntimeError):
    pass


class CredentialNotFoundError(CredentialStoreError):
    pass


@dataclass(frozen=True)
class StoredCredential:
    target: str
    username: str
    secret: str


def _encode_payload(username, secret):
    username = str(username or "")
    secret = str(secret or "")
    if not username.strip():
        raise CredentialStoreError("Credential username cannot be empty.")
    if not secret:
        raise CredentialStoreError("Credential secret cannot be empty.")
    return json.dumps({"username": username, "secret": secret}, separators=(",", ":"))


def _decode_payload(target, payload):
    try:
        data = json.loads(str(payload or ""))
    except json.JSONDecodeError as exc:
        raise CredentialStoreError(f"Credential '{target}' has an invalid portable payload.") from exc
    if not isinstance(data, dict) or not str(data.get("username") or "").strip() or not str(data.get("secret") or ""):
        raise CredentialStoreError(f"Credential '{target}' has an incomplete portable payload.")
    return StoredCredential(target=target, username=str(data["username"]), secret=str(data["secret"]))


def _legacy_windows_read(target, required=True):
    if os.name != "nt":
        return None
    try:
        from windows_credentials import read_windows_credential as read_legacy

        credential = read_legacy(target, required=required)
    except Exception:
        if required:
            raise
        return None
    if credential is None:
        return None
    return StoredCredential(target=target, username=credential.username, secret=credential.secret)


def read_credential(target, *, required=True):
    target = str(target or "").strip()
    if not target:
        raise CredentialStoreError("Credential target cannot be empty.")
    if keyring is not None:
        try:
            payload = keyring.get_password(target, KEYRING_ACCOUNT)
        except KeyringError as exc:
            if os.name != "nt":
                raise CredentialStoreError(f"Could not read '{target}' from the OS keychain: {exc}") from exc
            payload = None
        if payload:
            return _decode_payload(target, payload)

    legacy = _legacy_windows_read(target, required=False)
    if legacy is not None:
        return legacy
    if required:
        raise CredentialNotFoundError(
            f"Credential '{target}' was not found in the OS keychain. "
            "Run 'python manage_credentials.py set <service>' to create it."
        )
    return None


def write_credential(target, username, secret):
    target = str(target or "").strip()
    if not target:
        raise CredentialStoreError("Credential target cannot be empty.")
    payload = _encode_payload(username, secret)
    if keyring is None:
        if os.name == "nt":
            from windows_credentials import write_windows_credential

            write_windows_credential(target, username, secret)
            return
        raise CredentialStoreError("The 'keyring' package is required for macOS Keychain access.")
    try:
        keyring.set_password(target, KEYRING_ACCOUNT, payload)
    except KeyringError as exc:
        raise CredentialStoreError(f"Could not write '{target}' to the OS keychain: {exc}") from exc


def delete_credential(target, *, missing_ok=True):
    target = str(target or "").strip()
    removed = False
    if keyring is not None:
        try:
            keyring.delete_password(target, KEYRING_ACCOUNT)
            removed = True
        except Exception:
            if not missing_ok:
                raise
    if os.name == "nt":
        try:
            from windows_credentials import delete_windows_credential

            removed = delete_windows_credential(target, missing_ok=True) or removed
        except Exception:
            if not missing_ok:
                raise
    return removed


def credential_exists(target):
    return read_credential(target, required=False) is not None


def read_json_credential(target):
    credential = read_credential(target)
    try:
        payload = json.loads(credential.secret)
    except json.JSONDecodeError as exc:
        raise CredentialStoreError(f"Credential '{target}' does not contain valid JSON.") from exc
    if not isinstance(payload, dict):
        raise CredentialStoreError(f"Credential '{target}' must contain a JSON object.")
    return payload


# Compatibility name used by the current workers while their call sites are
# migrated incrementally.
read_windows_credential = read_credential
