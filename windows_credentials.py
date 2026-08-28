"""Native Windows Credential Manager access for local automation secrets."""

from __future__ import annotations

import ctypes
import json
import os
from dataclasses import dataclass
from ctypes import wintypes


PAYCOM_CREDENTIAL_TARGET = "WorkAutomation/Paycom"
CRM_CREDENTIAL_TARGET = "WorkAutomation/CRM"
SANMAR_CREDENTIAL_TARGET = "WorkAutomation/SanMar"
SLACK_CREDENTIAL_TARGET = "WorkAutomation/Slack"
SALESFORCE_CREDENTIAL_TARGET = "WorkAutomation/Salesforce"
GOOGLE_SHEETS_CREDENTIAL_TARGET = "WorkAutomation/GoogleSheets"
APP_SECURITY_CREDENTIAL_TARGET = "WorkAutomation/AppSecurity"

CREDENTIAL_TARGETS = {
    "paycom": PAYCOM_CREDENTIAL_TARGET,
    "crm": CRM_CREDENTIAL_TARGET,
    "sanmar": SANMAR_CREDENTIAL_TARGET,
    "slack": SLACK_CREDENTIAL_TARGET,
    "salesforce": SALESFORCE_CREDENTIAL_TARGET,
    "google_sheets": GOOGLE_SHEETS_CREDENTIAL_TARGET,
    "app_security": APP_SECURITY_CREDENTIAL_TARGET,
}

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_CRED_PERSIST_ENTERPRISE = 3
_ERROR_NOT_FOUND = 1168
_ERROR_NO_SUCH_LOGON_SESSION = 1312
_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
_MAX_DELETE_ATTEMPTS = 8
_UTF8_TAG = b"WorkAutomation.UTF8\0"


class WindowsCredentialError(RuntimeError):
    """Base error for Windows Credential Manager operations."""


class WindowsCredentialNotFoundError(WindowsCredentialError):
    """Raised when a required credential target does not exist."""


@dataclass(frozen=True)
class WindowsCredential:
    target: str
    username: str
    secret: str


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.c_void_p),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)


def _advapi32():
    if os.name != "nt":
        raise WindowsCredentialError("Windows Credential Manager is only available on Windows.")
    api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    api.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_PCREDENTIALW),
    ]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _validate_text(label: str, value: str) -> str:
    text = str(value or "")
    if not text.strip():
        raise WindowsCredentialError(f"{label} cannot be empty.")
    if "\0" in text:
        raise WindowsCredentialError(f"{label} cannot contain a null character.")
    return text


def _encode_secret(secret: str) -> bytes:
    payload = _UTF8_TAG + str(secret).encode("utf-8")
    if len(payload) > _MAX_CREDENTIAL_BLOB_SIZE:
        raise WindowsCredentialError(
            f"Credential payload is {len(payload)} bytes; Windows allows at most "
            f"{_MAX_CREDENTIAL_BLOB_SIZE} bytes."
        )
    return payload


def _decode_secret(payload: bytes) -> str:
    if payload.startswith(_UTF8_TAG):
        return payload[len(_UTF8_TAG) :].decode("utf-8")
    if not payload:
        return ""
    try:
        return payload.decode("utf-16-le").rstrip("\0")
    except UnicodeDecodeError:
        return payload.decode("utf-8")


def write_windows_credential(target: str, username: str, secret: str) -> None:
    """Authoritatively replace a persistent Generic Credential.

    Older releases used Python keyring, which writes enterprise-persistent
    credentials, while later native releases wrote local-machine credentials
    under the same target.  Windows can retain both backing records and expose
    the stale enterprise copy again after sign-in.  Delete every visible
    version first and write the replacement with the original enterprise
    persistence so the newest value remains authoritative after a restart.
    Local-only Windows accounts can reject enterprise persistence; those
    accounts safely fall back to local-machine persistence after cleanup.
    """
    target = _validate_text("Credential target", target).strip()
    username = _validate_text("Credential username", username)
    secret = _validate_text("Credential secret", secret)
    payload = _encode_secret(secret)
    blob = ctypes.create_string_buffer(payload, len(payload))
    credential = _CREDENTIALW()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.CredentialBlobSize = len(payload)
    credential.CredentialBlob = ctypes.cast(blob, ctypes.c_void_p)
    credential.Persist = _CRED_PERSIST_ENTERPRISE
    credential.UserName = username
    api = _advapi32()
    try:
        _delete_windows_credential_versions(api, target, missing_ok=True)
        if not api.CredWriteW(ctypes.byref(credential), 0):
            error_code = ctypes.get_last_error()
            if error_code == _ERROR_NO_SUCH_LOGON_SESSION:
                credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
                if api.CredWriteW(ctypes.byref(credential), 0):
                    return
                error_code = ctypes.get_last_error()
            raise WindowsCredentialError(
                f"Could not write Windows credential '{target}': {ctypes.WinError(error_code)}"
            )
    finally:
        ctypes.memset(blob, 0, len(payload))


def _delete_windows_credential_versions(api, target: str, *, missing_ok: bool) -> bool:
    """Remove all records Windows exposes for one credential target.

    Repeating the deletion matters for machines that have both an old roaming
    keyring record and a newer local-machine record for the same target.
    """
    removed = False
    for _attempt in range(_MAX_DELETE_ATTEMPTS):
        if api.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
            removed = True
            continue
        error_code = ctypes.get_last_error()
        if error_code == _ERROR_NOT_FOUND:
            if removed or missing_ok:
                return removed
            raise WindowsCredentialNotFoundError(
                f"Windows credential '{target}' was not found."
            )
        raise WindowsCredentialError(
            f"Could not delete Windows credential '{target}': {ctypes.WinError(error_code)}"
        )
    raise WindowsCredentialError(
        f"Could not fully remove duplicate Windows credential records for '{target}'."
    )


def read_windows_credential(target: str, *, required: bool = True) -> WindowsCredential | None:
    """Read a Generic Credential without logging or displaying its secret."""
    target = _validate_text("Credential target", target).strip()
    api = _advapi32()
    pointer = _PCREDENTIALW()
    if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error_code = ctypes.get_last_error()
        if error_code == _ERROR_NOT_FOUND:
            if required:
                raise WindowsCredentialNotFoundError(
                    f"Windows credential '{target}' was not found. "
                    "Run 'python manage_windows_credentials.py set <service>' to create it."
                )
            return None
        raise WindowsCredentialError(
            f"Could not read Windows credential '{target}': {ctypes.WinError(error_code)}"
        )

    try:
        credential = pointer.contents
        payload = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        return WindowsCredential(
            target=target,
            username=str(credential.UserName or ""),
            secret=_decode_secret(payload),
        )
    finally:
        api.CredFree(pointer)


def credential_exists(target: str) -> bool:
    return read_windows_credential(target, required=False) is not None


def delete_windows_credential(target: str, *, missing_ok: bool = True) -> bool:
    """Delete every Generic Credential record exposed for this target."""
    target = _validate_text("Credential target", target).strip()
    api = _advapi32()
    return _delete_windows_credential_versions(api, target, missing_ok=missing_ok)


def read_json_credential(target: str) -> dict:
    credential = read_windows_credential(target)
    try:
        value = json.loads(credential.secret)
    except json.JSONDecodeError as exc:
        raise WindowsCredentialError(
            f"Windows credential '{target}' does not contain valid JSON."
        ) from exc
    if not isinstance(value, dict):
        raise WindowsCredentialError(
            f"Windows credential '{target}' must contain a JSON object."
        )
    return value
