"""Portable secret storage backed by Keychain/Credential Manager.

Windows keeps a read fallback for the existing native credential targets so an
upgrade does not invalidate credentials that are already installed. macOS uses
Apple's built-in Security framework, so its system Python does not need the
optional third-party ``keyring`` package.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from dataclasses import dataclass

from windows_credentials import (
    CREDENTIAL_TARGETS,
    CRM_CREDENTIAL_TARGET,
    GOOGLE_SHEETS_CREDENTIAL_TARGET,
    PAYCOM_CREDENTIAL_TARGET,
    SALESFORCE_CREDENTIAL_TARGET,
    SANMAR_CREDENTIAL_TARGET,
    SLACK_CREDENTIAL_TARGET,
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
MACOS_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
CORE_FOUNDATION_FRAMEWORK = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
ERR_SEC_ITEM_NOT_FOUND = -25300


class CredentialStoreError(RuntimeError):
    pass


class CredentialNotFoundError(CredentialStoreError):
    pass


@dataclass(frozen=True)
class StoredCredential:
    target: str
    username: str
    secret: str


@dataclass(frozen=True)
class PaycomCredential:
    """The complete credential set needed by Paycom's login form."""

    username: str
    password: str
    pin: str


def _is_macos():
    return sys.platform == "darwin"


class _MacOSKeychainAPI:
    """Small ctypes bridge to Apple's native generic-password API."""

    def __init__(self):
        try:
            self.security = ctypes.CDLL(MACOS_SECURITY_FRAMEWORK)
            self.core_foundation = ctypes.CDLL(CORE_FOUNDATION_FRAMEWORK)
        except OSError as exc:
            raise CredentialStoreError("Could not load Apple Keychain services.") from exc

        uint32_pointer = ctypes.POINTER(ctypes.c_uint32)
        void_pointer = ctypes.POINTER(ctypes.c_void_p)
        self.security.SecKeychainFindGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            uint32_pointer,
            void_pointer,
            void_pointer,
        ]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            void_pointer,
        ]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.security.SecKeychainItemFreeContent.restype = ctypes.c_int32
        self.security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
        self.core_foundation.CFRelease.restype = None

    @staticmethod
    def _identifier_bytes(value):
        return str(value).encode("utf-8")

    def _find(self, target):
        service = self._identifier_bytes(target)
        account = self._identifier_bytes(KEYRING_ACCOUNT)
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        item = ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(
            None,
            len(service),
            service,
            len(account),
            account,
            ctypes.byref(password_length),
            ctypes.byref(password_data),
            ctypes.byref(item),
        )
        return status, password_length, password_data, item

    def read(self, target):
        status, password_length, password_data, item = self._find(target)
        try:
            if status == ERR_SEC_ITEM_NOT_FOUND:
                return None
            if status != 0:
                raise CredentialStoreError(
                    f"Could not read '{target}' from Apple Keychain (status {status})."
                )
            try:
                return ctypes.string_at(password_data, password_length.value).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CredentialStoreError(
                    f"Credential '{target}' contains invalid Keychain data."
                ) from exc
        finally:
            if password_data.value:
                self.security.SecKeychainItemFreeContent(None, password_data)
            if item.value:
                self.core_foundation.CFRelease(item)

    def write(self, target, payload):
        status, _password_length, password_data, item = self._find(target)
        if password_data.value:
            self.security.SecKeychainItemFreeContent(None, password_data)

        payload_bytes = str(payload).encode("utf-8")
        payload_buffer = ctypes.create_string_buffer(payload_bytes)
        payload_pointer = ctypes.cast(payload_buffer, ctypes.c_void_p)
        try:
            if status == ERR_SEC_ITEM_NOT_FOUND:
                service = self._identifier_bytes(target)
                account = self._identifier_bytes(KEYRING_ACCOUNT)
                status = self.security.SecKeychainAddGenericPassword(
                    None,
                    len(service),
                    service,
                    len(account),
                    account,
                    len(payload_bytes),
                    payload_pointer,
                    None,
                )
            elif status == 0:
                status = self.security.SecKeychainItemModifyAttributesAndData(
                    item,
                    None,
                    len(payload_bytes),
                    payload_pointer,
                )
            if status != 0:
                raise CredentialStoreError(
                    f"Could not write '{target}' to Apple Keychain (status {status})."
                )
        finally:
            if item.value:
                self.core_foundation.CFRelease(item)

    def delete(self, target):
        status, _password_length, password_data, item = self._find(target)
        if password_data.value:
            self.security.SecKeychainItemFreeContent(None, password_data)
        try:
            if status == ERR_SEC_ITEM_NOT_FOUND:
                return False
            if status != 0:
                raise CredentialStoreError(
                    f"Could not find '{target}' in Apple Keychain (status {status})."
                )
            status = self.security.SecKeychainItemDelete(item)
            if status != 0:
                raise CredentialStoreError(
                    f"Could not delete '{target}' from Apple Keychain (status {status})."
                )
            return True
        finally:
            if item.value:
                self.core_foundation.CFRelease(item)


_macos_keychain_api_instance = None


def _get_macos_keychain_api():
    global _macos_keychain_api_instance
    if _macos_keychain_api_instance is None:
        _macos_keychain_api_instance = _MacOSKeychainAPI()
    return _macos_keychain_api_instance


def _macos_keychain_read(target, *, required=True):
    payload = _get_macos_keychain_api().read(target)
    if payload is None:
        if required:
            raise CredentialNotFoundError(
                f"Credential '{target}' was not found in Apple Keychain. "
                "Run 'python3 manage_credentials.py set <service>' to create it."
            )
        return None
    return _decode_payload(target, payload)


def _macos_keychain_write(target, payload):
    _get_macos_keychain_api().write(target, payload)


def _macos_keychain_delete(target, *, missing_ok=True):
    removed = _get_macos_keychain_api().delete(target)
    if removed or missing_ok:
        return removed
    raise CredentialNotFoundError(f"Credential '{target}' was not found in Apple Keychain.")


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

    # Do not access Windows Credential Manager through both keyring and the
    # native ctypes implementation.  They use the same target names but store
    # different payload formats, which allowed a restart using another Python
    # environment to misread a valid native credential as an incomplete
    # keyring payload.  Windows therefore has one authoritative format.
    if os.name == "nt":
        native = _legacy_windows_read(target, required=False)
        if native is not None:
            return native
        if required:
            raise CredentialNotFoundError(
                f"Credential '{target}' was not found in Windows Credential Manager. "
                "Run 'python manage_windows_credentials.py set <service>' to create it."
            )
        return None

    if _is_macos():
        return _macos_keychain_read(target, required=required)

    if keyring is not None:
        try:
            payload = keyring.get_password(target, KEYRING_ACCOUNT)
        except KeyringError as exc:
            raise CredentialStoreError(f"Could not read '{target}' from the OS keychain: {exc}") from exc
        if payload:
            return _decode_payload(target, payload)

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

    if os.name == "nt":
        from windows_credentials import write_windows_credential

        write_windows_credential(target, username, secret)
        return

    payload = _encode_payload(username, secret)
    if _is_macos():
        _macos_keychain_write(target, payload)
        return

    if keyring is None:
        raise CredentialStoreError("The 'keyring' package is required for OS keychain access.")
    try:
        keyring.set_password(target, KEYRING_ACCOUNT, payload)
    except KeyringError as exc:
        raise CredentialStoreError(f"Could not write '{target}' to the OS keychain: {exc}") from exc


def delete_credential(target, *, missing_ok=True):
    target = str(target or "").strip()
    if os.name == "nt":
        from windows_credentials import delete_windows_credential

        return delete_windows_credential(target, missing_ok=missing_ok)

    if _is_macos():
        return _macos_keychain_delete(target, missing_ok=missing_ok)

    removed = False
    if keyring is not None:
        try:
            keyring.delete_password(target, KEYRING_ACCOUNT)
            removed = True
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


def build_paycom_secret(password, pin):
    """Return the structured secret stored alongside the Paycom username."""
    password = str(password or "")
    pin = str(pin or "").strip()
    if not password:
        raise CredentialStoreError("Paycom password cannot be empty.")
    if not pin.isdigit() or len(pin) != 4:
        raise CredentialStoreError("Paycom PIN must be exactly four digits.")
    return json.dumps({"password": password, "pin": pin}, separators=(",", ":"))


def _parse_paycom_credential(credential):
    """Validate a Paycom credential without exposing its secret in errors."""
    if credential.username == "PIN":
        raise CredentialStoreError(
            "The saved Paycom credential contains only a legacy PIN. "
            "Run 'python manage_windows_credentials.py set paycom' to save the username, password, and PIN."
        )
    try:
        payload = json.loads(credential.secret)
    except json.JSONDecodeError as exc:
        raise CredentialStoreError(
            "The saved Paycom credential is incomplete. "
            "Run 'python manage_windows_credentials.py set paycom' to replace it."
        ) from exc
    username = str(credential.username or "").strip()
    password = str(payload.get("password") or "") if isinstance(payload, dict) else ""
    pin = str(payload.get("pin") or "").strip() if isinstance(payload, dict) else ""
    if not username or not password or not pin.isdigit() or len(pin) != 4:
        raise CredentialStoreError(
            "The saved Paycom credential must include a username, password, and four-digit PIN. "
            "Run 'python manage_windows_credentials.py set paycom' to replace it."
        )
    return PaycomCredential(username=username, password=password, pin=pin)


def read_paycom_credential():
    """Read Paycom credentials, recovering from a stale portable Windows entry.

    Older releases could leave a PIN-only or otherwise incomplete portable
    credential behind.  The Windows setup command writes the native Windows
    Credential Manager entry, so prefer that complete fallback when the
    portable entry cannot satisfy Paycom's full login requirements.
    """
    credential = read_credential(PAYCOM_CREDENTIAL_TARGET)
    try:
        return _parse_paycom_credential(credential)
    except CredentialStoreError as primary_error:
        # On Windows, a stale keyring value may take precedence over the
        # credential most recently saved with manage_windows_credentials.py.
        # Only use the native value when it is distinct and fully valid.
        native = _legacy_windows_read(PAYCOM_CREDENTIAL_TARGET, required=False)
        if native is not None and native != credential:
            try:
                return _parse_paycom_credential(native)
            except CredentialStoreError:
                pass
        raise primary_error


# Compatibility name used by the current workers while their call sites are
# migrated incrementally.
read_windows_credential = read_credential
