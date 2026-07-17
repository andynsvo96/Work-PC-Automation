"""Shared PIN and session-secret handling for Tailscale web access."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from werkzeug.security import check_password_hash, generate_password_hash

from credential_store import APP_SECURITY_CREDENTIAL_TARGET, read_credential, write_credential


class AppSecurityError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppSecurityConfig:
    pin_hash: str
    session_secret: str

    def verify_pin(self, pin: str) -> bool:
        try:
            return check_password_hash(self.pin_hash, str(pin or ""))
        except (TypeError, ValueError):
            return False

    def to_dict(self):
        return {"pin_hash": self.pin_hash, "session_secret": self.session_secret}


def validate_pin(pin: str) -> str:
    value = str(pin or "").strip()
    if not value.isdigit() or not 6 <= len(value) <= 12:
        raise AppSecurityError("App PIN must contain 6 to 12 digits.")
    return value


def create_app_security(pin: str) -> AppSecurityConfig:
    value = validate_pin(pin)
    return AppSecurityConfig(
        pin_hash=generate_password_hash(value, method="scrypt"),
        session_secret=secrets.token_urlsafe(48),
    )


def load_app_security() -> AppSecurityConfig:
    credential = read_credential(APP_SECURITY_CREDENTIAL_TARGET)
    try:
        values = json.loads(credential.secret)
    except json.JSONDecodeError as exc:
        raise AppSecurityError("App security keychain entry is invalid JSON.") from exc
    if not isinstance(values, dict):
        raise AppSecurityError("App security keychain entry must be an object.")
    pin_hash = str(values.get("pin_hash") or "").strip()
    session_secret = str(values.get("session_secret") or "").strip()
    if not pin_hash or len(session_secret) < 32:
        raise AppSecurityError("App security keychain entry is incomplete.")
    return AppSecurityConfig(pin_hash=pin_hash, session_secret=session_secret)


def store_app_security(config: AppSecurityConfig) -> None:
    write_credential(
        APP_SECURITY_CREDENTIAL_TARGET,
        "shared-web-access",
        json.dumps(config.to_dict(), separators=(",", ":")),
    )
