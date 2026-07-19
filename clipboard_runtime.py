"""Private, non-persistent cross-platform clipboard synchronization."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import io
import json
import platform
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from PIL import Image, ImageGrab


CLIPBOARD_PROTOCOL_VERSION = 1
MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
PEER_PATH_PREFIX = "/api/clipboard/peer/"


class ClipboardError(RuntimeError):
    pass


class ClipboardUnavailable(ClipboardError):
    pass


def _validate_image(image: Image.Image, *, require_png: bool = False):
    width, height = image.size
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise ClipboardError("Clipboard image dimensions exceed the 25 megapixel limit.")
    if require_png and str(image.format or "").upper() != "PNG":
        raise ClipboardError("Clipboard image data is not PNG.")


@dataclass(frozen=True)
class ClipboardItem:
    kind: str
    data: bytes
    mime_type: str

    @classmethod
    def text(cls, value: str) -> "ClipboardItem":
        return cls("text", str(value).encode("utf-8"), "text/plain; charset=utf-8")

    @classmethod
    def png(cls, value: bytes) -> "ClipboardItem":
        return cls("image", bytes(value), "image/png")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.kind.encode("ascii") + b"\0" + self.data).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        if self.kind == "text":
            return {
                "kind": "text",
                "mime_type": self.mime_type,
                "text": self.data.decode("utf-8"),
                "digest": self.digest,
            }
        return {
            "kind": "image",
            "mime_type": "image/png",
            "data_base64": base64.b64encode(self.data).decode("ascii"),
            "digest": self.digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClipboardItem":
        if not isinstance(payload, Mapping):
            raise ClipboardError("Clipboard payload must be an object.")
        kind = str(payload.get("kind") or "").strip().lower()
        if kind == "text":
            value = str(payload.get("text") or "")
            item = cls.text(value)
            if len(item.data) > MAX_TEXT_BYTES:
                raise ClipboardError("Clipboard text exceeds the 1 MB limit.")
        elif kind == "image":
            if str(payload.get("mime_type") or "image/png").lower() != "image/png":
                raise ClipboardError("Only PNG clipboard images are supported.")
            try:
                raw = base64.b64decode(str(payload.get("data_base64") or ""), validate=True)
            except (ValueError, TypeError) as exc:
                raise ClipboardError("Clipboard image is not valid base64.") from exc
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise ClipboardError("Clipboard image is empty or exceeds the 8 MB limit.")
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    _validate_image(image, require_png=True)
                    image.verify()
            except Exception as exc:
                raise ClipboardError("Clipboard image is not a valid PNG.") from exc
            item = cls.png(raw)
        else:
            raise ClipboardError("Clipboard item must contain text or a PNG image.")
        supplied_digest = str(payload.get("digest") or "").strip().lower()
        if supplied_digest and not hmac.compare_digest(supplied_digest, item.digest):
            raise ClipboardError("Clipboard content hash did not match.")
        return item


class UnsupportedClipboardAdapter:
    available = False

    def change_token(self):
        return None

    def read(self):
        raise ClipboardUnavailable("Clipboard transfer is supported only on Windows and macOS.")

    def write(self, _item):
        raise ClipboardUnavailable("Clipboard transfer is supported only on Windows and macOS.")


class WindowsClipboardAdapter:
    available = True
    CF_UNICODETEXT = 13
    CF_DIB = 8
    GMEM_MOVEABLE = 0x0002

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        self.kernel32.GlobalAlloc.restype = ctypes.c_void_p
        self.kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        self.kernel32.GlobalLock.restype = ctypes.c_void_p
        self.kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        self.kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        self.user32.GetClipboardData.argtypes = [ctypes.c_uint]
        self.user32.GetClipboardData.restype = ctypes.c_void_p
        self.user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        self.user32.SetClipboardData.restype = ctypes.c_void_p
        self.png_format = self.user32.RegisterClipboardFormatW("PNG")

    def change_token(self):
        return int(self.user32.GetClipboardSequenceNumber())

    def _open(self):
        for _ in range(10):
            if self.user32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise ClipboardUnavailable("The Windows clipboard is busy.")

    def _read_text(self) -> Optional[ClipboardItem]:
        self._open()
        try:
            handle = self.user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return None
            pointer = self.kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                value = ctypes.wstring_at(pointer)
            finally:
                self.kernel32.GlobalUnlock(handle)
            return ClipboardItem.text(value)
        finally:
            self.user32.CloseClipboard()

    @staticmethod
    def _image_to_png(image: Image.Image) -> bytes:
        _validate_image(image)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()

    def read(self) -> Optional[ClipboardItem]:
        try:
            value = ImageGrab.grabclipboard()
        except Exception:
            value = None
        if isinstance(value, Image.Image):
            raw = self._image_to_png(value)
            if len(raw) > MAX_IMAGE_BYTES:
                raise ClipboardError("Clipboard image exceeds the 8 MB limit.")
            return ClipboardItem.png(raw)
        item = self._read_text()
        if item and len(item.data) > MAX_TEXT_BYTES:
            raise ClipboardError("Clipboard text exceeds the 1 MB limit.")
        return item

    def _set_bytes(self, clipboard_format: int, raw: bytes):
        handle = self.kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(raw))
        if not handle:
            raise ClipboardUnavailable("Windows could not allocate clipboard memory.")
        pointer = self.kernel32.GlobalLock(handle)
        if not pointer:
            self.kernel32.GlobalFree(handle)
            raise ClipboardUnavailable("Windows could not lock clipboard memory.")
        ctypes.memmove(pointer, raw, len(raw))
        self.kernel32.GlobalUnlock(handle)
        if not self.user32.SetClipboardData(clipboard_format, handle):
            self.kernel32.GlobalFree(handle)
            raise ClipboardUnavailable("Windows rejected clipboard data.")

    def write(self, item: ClipboardItem):
        self._open()
        try:
            if not self.user32.EmptyClipboard():
                raise ClipboardUnavailable("Windows could not clear the clipboard.")
            if item.kind == "text":
                raw = item.data.decode("utf-8").encode("utf-16-le") + b"\0\0"
                self._set_bytes(self.CF_UNICODETEXT, raw)
            elif item.kind == "image":
                with Image.open(io.BytesIO(item.data)) as image:
                    png_output = io.BytesIO()
                    image.save(png_output, format="PNG")
                    bmp_output = io.BytesIO()
                    image.convert("RGB").save(bmp_output, format="BMP")
                self._set_bytes(self.CF_DIB, bmp_output.getvalue()[14:])
                if self.png_format:
                    self._set_bytes(self.png_format, png_output.getvalue())
            else:
                raise ClipboardError("Unsupported clipboard item type.")
        finally:
            self.user32.CloseClipboard()


class MacClipboardAdapter:
    available = True

    def __init__(self):
        try:
            import AppKit
            import Foundation
            import objc
        except ImportError as exc:
            raise ClipboardUnavailable("PyObjC is required for the macOS clipboard.") from exc
        self.AppKit = AppKit
        self.Foundation = Foundation
        self.objc = objc
        self.pasteboard = AppKit.NSPasteboard.generalPasteboard()

    def change_token(self):
        with self.objc.autorelease_pool():
            return int(self.pasteboard.changeCount())

    @staticmethod
    def _nsdata_bytes(value) -> bytes:
        return bytes(value) if value is not None else b""

    def read(self) -> Optional[ClipboardItem]:
        with self.objc.autorelease_pool():
            png_type = self.AppKit.NSPasteboardTypePNG
            tiff_type = self.AppKit.NSPasteboardTypeTIFF
            raw = self._nsdata_bytes(self.pasteboard.dataForType_(png_type))
            if not raw:
                tiff = self._nsdata_bytes(self.pasteboard.dataForType_(tiff_type))
                if tiff:
                    with Image.open(io.BytesIO(tiff)) as image:
                        _validate_image(image)
                        output = io.BytesIO()
                        image.save(output, format="PNG", optimize=True)
                        raw = output.getvalue()
            if raw:
                if len(raw) > MAX_IMAGE_BYTES:
                    raise ClipboardError("Clipboard image exceeds the 8 MB limit.")
                return ClipboardItem.png(raw)
            value = self.pasteboard.stringForType_(self.AppKit.NSPasteboardTypeString)
            if value is None:
                return None
            item = ClipboardItem.text(str(value))
            if len(item.data) > MAX_TEXT_BYTES:
                raise ClipboardError("Clipboard text exceeds the 1 MB limit.")
            return item

    def write(self, item: ClipboardItem):
        with self.objc.autorelease_pool():
            self.pasteboard.clearContents()
            if item.kind == "text":
                if not self.pasteboard.setString_forType_(
                    item.data.decode("utf-8"), self.AppKit.NSPasteboardTypeString
                ):
                    raise ClipboardUnavailable("macOS rejected clipboard text.")
            elif item.kind == "image":
                png_value = self.Foundation.NSData.dataWithBytes_length_(item.data, len(item.data))
                with Image.open(io.BytesIO(item.data)) as image:
                    _validate_image(image, require_png=True)
                    tiff_output = io.BytesIO()
                    image.save(tiff_output, format="TIFF")
                tiff_raw = tiff_output.getvalue()
                tiff_value = self.Foundation.NSData.dataWithBytes_length_(tiff_raw, len(tiff_raw))
                png_ok = self.pasteboard.setData_forType_(png_value, self.AppKit.NSPasteboardTypePNG)
                tiff_ok = self.pasteboard.setData_forType_(tiff_value, self.AppKit.NSPasteboardTypeTIFF)
                if not png_ok and not tiff_ok:
                    raise ClipboardUnavailable("macOS rejected the clipboard image.")
            else:
                raise ClipboardError("Unsupported clipboard item type.")


def create_platform_clipboard_adapter(system_name: Optional[str] = None):
    normalized = str(system_name or platform.system()).strip().lower()
    try:
        if normalized == "windows":
            return WindowsClipboardAdapter()
        if normalized in {"darwin", "macos"}:
            return MacClipboardAdapter()
    except ClipboardError:
        raise
    except Exception as exc:
        raise ClipboardUnavailable(str(exc)) from exc
    return UnsupportedClipboardAdapter()


class PeerRequestAuthenticator:
    """HMAC request authentication with timestamp and replay protection."""

    def __init__(self, secret_provider: Callable[[], str], *, max_clock_skew_seconds: int = 60):
        self.secret_provider = secret_provider
        self.max_clock_skew_seconds = int(max_clock_skew_seconds)
        self._nonces: dict[str, float] = {}
        self._lock = threading.RLock()

    def _key(self) -> bytes:
        secret = str(self.secret_provider() or "")
        if len(secret) < 32:
            raise ClipboardUnavailable("Shared app security is not configured on this computer.")
        return hashlib.sha256(b"automation-clipboard-v1\0" + secret.encode("utf-8")).digest()

    @staticmethod
    def _canonical(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> bytes:
        body_hash = hashlib.sha256(body).hexdigest()
        return "\n".join((method.upper(), path, timestamp, nonce, body_hash)).encode("utf-8")

    def headers(self, method: str, path: str, body: bytes = b"", *, now: Optional[int] = None, nonce: Optional[str] = None):
        timestamp = str(int(time.time() if now is None else now))
        nonce = str(nonce or secrets.token_urlsafe(18))
        signature = hmac.new(
            self._key(), self._canonical(method, path, timestamp, nonce, body), hashlib.sha256
        ).hexdigest()
        return {
            "X-Clipboard-Timestamp": timestamp,
            "X-Clipboard-Nonce": nonce,
            "X-Clipboard-Signature": signature,
            "X-Clipboard-Protocol": str(CLIPBOARD_PROTOCOL_VERSION),
        }

    def verify(self, method: str, path: str, body: bytes, headers: Mapping[str, Any], *, now: Optional[int] = None):
        if str(headers.get("X-Clipboard-Protocol") or "") != str(CLIPBOARD_PROTOCOL_VERSION):
            raise ClipboardError("Clipboard protocol version mismatch.")
        timestamp = str(headers.get("X-Clipboard-Timestamp") or "")
        nonce = str(headers.get("X-Clipboard-Nonce") or "")
        supplied = str(headers.get("X-Clipboard-Signature") or "").lower()
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise ClipboardError("Clipboard request timestamp is invalid.") from exc
        current = int(time.time() if now is None else now)
        if abs(current - timestamp_value) > self.max_clock_skew_seconds:
            raise ClipboardError("Clipboard request expired.")
        if len(nonce) < 16 or len(supplied) != 64:
            raise ClipboardError("Clipboard request authentication is incomplete.")
        expected = hmac.new(
            self._key(), self._canonical(method, path, timestamp, nonce, body), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise ClipboardError("Clipboard request authentication failed.")
        with self._lock:
            cutoff = current - self.max_clock_skew_seconds
            self._nonces = {key: stamp for key, stamp in self._nonces.items() if stamp >= cutoff}
            if nonce in self._nonces:
                raise ClipboardError("Clipboard request was already used.")
            self._nonces[nonce] = current
        return True


class ClipboardPeerClient:
    def __init__(
        self,
        peer_url: str,
        authenticator: PeerRequestAuthenticator,
        *,
        timeout_seconds: float = 6.0,
        opener: Optional[Callable[..., Any]] = None,
    ):
        raw_peer_url = str(peer_url or "").strip().rstrip("/")
        self._configuration_error = None
        if raw_peer_url:
            parsed = urllib.parse.urlsplit(raw_peer_url)
            valid_host = bool(parsed.hostname and parsed.hostname.lower().endswith(".ts.net"))
            valid_path = parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            if parsed.scheme.lower() != "https" or not valid_host or not valid_path:
                self._configuration_error = (
                    "AUTOMATION_CLIPBOARD_PEER_URL must be a device-specific "
                    "https://...ts.net URL without a path."
                )
                raw_peer_url = ""
        self.peer_url = raw_peer_url
        self.authenticator = authenticator
        self.timeout_seconds = float(timeout_seconds)
        self.opener = opener or urllib.request.urlopen

    @property
    def configured(self):
        return bool(self.peer_url)

    @property
    def configuration_error(self):
        return self._configuration_error

    def _request(self, path: str, *, method: str = "GET", payload: Optional[Mapping[str, Any]] = None):
        if not self.peer_url:
            raise ClipboardUnavailable(
                self._configuration_error or "Set AUTOMATION_CLIPBOARD_PEER_URL in config.py first."
            )
        body = b"" if payload is None else json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        headers = self.authenticator.headers(method, path, body)
        headers["Accept"] = "application/json"
        if body:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            urllib.parse.urljoin(self.peer_url + "/", path.lstrip("/")),
            data=body if method.upper() != "GET" else None,
            headers=headers,
            method=method.upper(),
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
            result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message")
            except Exception:
                detail = None
            raise ClipboardUnavailable(detail or f"Other computer returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ClipboardUnavailable(f"Other computer is unavailable: {exc}") from exc
        if not isinstance(result, dict) or not result.get("success"):
            raise ClipboardUnavailable(str((result or {}).get("message") or "Other computer rejected the clipboard request."))
        return result

    def status(self):
        return self._request(f"{PEER_PATH_PREFIX}status")

    def send(self, item: ClipboardItem, *, automatic: bool):
        return self._request(
            f"{PEER_PATH_PREFIX}receive",
            method="POST",
            payload={"automatic": bool(automatic), "item": item.to_payload()},
        )

    def read(self):
        result = self._request(f"{PEER_PATH_PREFIX}read")
        return ClipboardItem.from_payload(result.get("item") or {})


class ClipboardRuntime:
    def __init__(
        self,
        adapter,
        peer_client: ClipboardPeerClient,
        *,
        enabled: bool = False,
        preference_updater: Optional[Callable[[bool], Any]] = None,
        poll_interval_seconds: float = 0.5,
        peer_status_interval_seconds: float = 3.0,
    ):
        self.adapter = adapter
        self.peer_client = peer_client
        self.preference_updater = preference_updater
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.peer_status_interval_seconds = float(peer_status_interval_seconds)
        self._enabled = bool(enabled)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_seen_token = None
        self._lock = threading.RLock()
        self._io_lock = threading.RLock()
        self._state = {
            "success": True,
            "available": bool(getattr(adapter, "available", False)),
            "enabled": self._enabled,
            "peer_configured": peer_client.configured,
            "peer_online": False,
            "peer_enabled": False,
            "last_sync_at": None,
            "last_direction": None,
            "last_kind": None,
            "last_error": getattr(peer_client, "configuration_error", None),
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        try:
            with self._io_lock:
                self._last_seen_token = self.adapter.change_token()
        except Exception:
            self._last_seen_token = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="clipboard-sync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def state(self):
        with self._lock:
            return dict(self._state)

    @property
    def enabled(self):
        with self._lock:
            return self._enabled

    def _update_state(self, **updates):
        with self._lock:
            self._state.update(updates)
            self._state["enabled"] = self._enabled

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if self.preference_updater:
            self.preference_updater(enabled)
        with self._lock:
            self._enabled = enabled
            self._state["enabled"] = enabled
            self._state["last_error"] = None
        try:
            with self._io_lock:
                self._last_seen_token = self.adapter.change_token()
        except Exception:
            self._last_seen_token = None
        self._wake.set()
        return self.state()

    def _read_local(self):
        with self._io_lock:
            item = self.adapter.read()
        if item is None:
            raise ClipboardUnavailable("The clipboard does not contain text or an image.")
        return item

    def manual_send(self):
        item = self._read_local()
        self.peer_client.send(item, automatic=False)
        self._record_sync("sent", item)
        return item

    def manual_pull(self):
        item = self.peer_client.read()
        self.apply_remote(item, automatic=False)
        return item

    def read_for_peer(self):
        return self._read_local()

    def apply_remote(self, item: ClipboardItem, *, automatic: bool):
        if automatic and not self.enabled:
            raise ClipboardUnavailable("Automatic clipboard sync is disabled on this computer.")
        with self._io_lock:
            self.adapter.write(item)
            try:
                self._last_seen_token = self.adapter.change_token()
            except Exception:
                self._last_seen_token = None
        self._record_sync("received", item)

    def _record_sync(self, direction: str, item: ClipboardItem):
        self._update_state(
            last_sync_at=time.time(),
            last_direction=direction,
            last_kind=item.kind,
            last_error=None,
        )

    def _refresh_peer_status(self):
        try:
            result = self.peer_client.status()
            self._update_state(
                peer_online=True,
                peer_enabled=bool(result.get("enabled")),
                last_error=None,
            )
        except ClipboardError as exc:
            self._update_state(peer_online=False, peer_enabled=False, last_error=str(exc))

    def _send_automatic_change(self, expected_token=None):
        try:
            with self._io_lock:
                if expected_token is not None and self.adapter.change_token() != expected_token:
                    return False
                item = self.adapter.read()
                if item is None:
                    self._last_seen_token = expected_token
                    return False
            self.peer_client.send(item, automatic=True)
            with self._io_lock:
                current_token = self.adapter.change_token()
                if expected_token is None or current_token == expected_token:
                    self._last_seen_token = current_token
            self._record_sync("sent", item)
            return True
        except ClipboardError as exc:
            self._update_state(last_error=str(exc))
        except Exception as exc:
            self._update_state(last_error=f"Clipboard sync failed: {exc}")
        return False

    def _run(self):
        next_peer_status = 0.0
        while not self._stop.is_set():
            if not self.enabled or not getattr(self.adapter, "available", False):
                self._wake.wait(self.poll_interval_seconds)
                self._wake.clear()
                continue
            now = time.monotonic()
            if now >= next_peer_status:
                self._refresh_peer_status()
                next_peer_status = now + self.peer_status_interval_seconds
            try:
                with self._io_lock:
                    token = self.adapter.change_token()
                    changed = token is not None and token != self._last_seen_token
                if changed:
                    state = self.state()
                    if state.get("peer_online") and state.get("peer_enabled"):
                        self._send_automatic_change(token)
            except ClipboardError as exc:
                self._update_state(last_error=str(exc))
            except Exception as exc:
                self._update_state(last_error=f"Clipboard monitor failed: {exc}")
            self._wake.wait(self.poll_interval_seconds)
            self._wake.clear()
