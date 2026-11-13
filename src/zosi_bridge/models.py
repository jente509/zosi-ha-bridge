"""Data models, MAC normalization, and thread-safe persistence helpers."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

LOG = logging.getLogger("zosi-bridge.models")

_HEX_CHARS = frozenset("0123456789ABCDEF")
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.Lock] = {}


def _lock_for_path(path: Path) -> threading.Lock:
    resolved = path.resolve()
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[resolved] = lock
        return lock


@dataclass
class Camera:
    """Represents a discovered Zosi/Ansjer camera."""

    id: str
    number: int
    name: str
    mac: str
    ip: str
    manufacturer: str = "Zosi (Ansjer)"
    model: str = "IPCamera"
    firmware: str = ""
    serial: str = ""
    hardware_id: str = ""
    rtsp_ok: bool = False
    onvif_ok: bool = False
    online: bool = False
    last_seen: float = 0.0

    @property
    def mac_clean(self) -> str:
        return self.mac.replace(":", "").replace("-", "").lower()


def normalize_mac(mac: str) -> str:
    """Normalize a MAC address to uppercase colon-separated form, or '' if invalid."""
    if not isinstance(mac, str):
        return ""
    stripped = mac.strip().upper()
    if not stripped:
        return ""

    cleaned = stripped.replace("-", ":")
    if ":" in cleaned:
        raw_parts = cleaned.split(":")
        if len(raw_parts) == 6 and all(
            1 <= len(part) <= 2 and all(ch in _HEX_CHARS for ch in part)
            for part in raw_parts
        ):
            return ":".join(part.zfill(2) for part in raw_parts)

    hex_only = "".join(ch for ch in stripped if ch in _HEX_CHARS)
    if len(hex_only) == 12 and all(ch in _HEX_CHARS or ch in ":-. " for ch in stripped):
        return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
    return ""


def atomic_write(path: Path, content: str, mode: int = 0o644) -> bool:
    """Atomically write UTF-8 text to *path*. Returns True if contents changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_for_path(path)
    with lock:
        if path.exists():
            try:
                if path.read_text(encoding="utf-8") == content:
                    return False
            except OSError:
                pass

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.tmp.",
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
                tmp.write(content)
                tmp.flush()
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return True


def load_cameras(path: Path) -> list[Camera]:
    """Load camera inventory from JSON, skipping malformed records safely."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        LOG.warning("Failed to parse %s: %s", path, exc)
        return []

    if isinstance(raw, dict):
        items = raw.get("cameras", [])
    elif isinstance(raw, list):
        items = raw
    else:
        LOG.warning("Unexpected JSON root type in %s: %s", path, type(raw).__name__)
        return []

    if not isinstance(items, list):
        LOG.warning("Unexpected 'cameras' field type in %s: %s", path, type(items).__name__)
        return []

    cameras_by_mac: dict[str, Camera] = {}
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            LOG.warning("Skipping non-object camera entry #%d in %s", idx, path)
            continue

        cam_id = item.get("id")
        raw_mac = item.get("mac")
        raw_ip = item.get("ip")
        if not isinstance(cam_id, str) or not cam_id.strip():
            LOG.warning("Skipping camera entry #%d with missing id in %s", idx, path)
            continue
        mac = normalize_mac(raw_mac or "")
        if not mac:
            LOG.warning("Skipping camera %s with invalid MAC %r", cam_id, raw_mac)
            continue
        try:
            ip = str(ipaddress.IPv4Address(str(raw_ip).strip()))
        except (ValueError, ipaddress.AddressValueError):
            LOG.warning("Skipping camera %s with invalid IP %r", cam_id, raw_ip)
            continue

        try:
            number = int(item.get("number", cam_id.split("_")[-1]))
        except (TypeError, ValueError):
            LOG.warning("Skipping camera %s with invalid number", cam_id)
            continue

        if mac in cameras_by_mac:
            LOG.warning("Duplicate MAC %s in %s; keeping latest entry %s", mac, path, cam_id)

        try:
            last_seen = float(item.get("last_seen", 0.0))
        except (TypeError, ValueError):
            last_seen = 0.0

        cameras_by_mac[mac] = Camera(
            id=cam_id.strip(),
            number=number,
            name=str(item.get("name") or f"Zosi Camera {number}").strip(),
            mac=mac,
            ip=ip,
            manufacturer=str(item.get("manufacturer") or "Zosi (Ansjer)"),
            model=str(item.get("model") or "IPCamera"),
            firmware=str(item.get("firmware") or ""),
            serial=str(item.get("serial") or ""),
            hardware_id=str(item.get("hardware_id") or ""),
            rtsp_ok=bool(item.get("rtsp_ok", False)),
            onvif_ok=bool(item.get("onvif_ok", False)),
            online=bool(item.get("online", False)),
            last_seen=last_seen,
        )

    return sorted(cameras_by_mac.values(), key=lambda c: c.number)


def save_cameras(path: Path, cameras: list[Camera]) -> bool:
    """Persist camera inventory sorted by camera number using atomic write."""
    payload = {
        "updated_at": int(time.time()),
        "cameras": [asdict(c) for c in sorted(cameras, key=lambda c: c.number)],
    }
    return atomic_write(path, json.dumps(payload, indent=2) + "\n")
