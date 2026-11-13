"""In-memory RAM snapshot cache and lightweight HTTP server for go2rtc frames."""

from __future__ import annotations

import concurrent.futures
import http.client
import http.server
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import Camera

LOG = logging.getLogger("zosi-bridge.snapshot")

_SNAPSHOT_PATH_RE = re.compile(r"^/api/snapshot/([A-Za-z0-9_-]+?)(?:\.jpe?g)?$")


@dataclass
class SnapshotEntry:
    """Cached JPEG snapshot stored in RAM."""

    data: bytes
    fetched_monotonic: float
    fetched_at: float


class SnapshotCache:
    """Thread-safe RAM cache with stale-while-revalidate and staggered background refresh."""

    def __init__(
        self,
        go2rtc_url: str,
        get_cameras: Callable[[], list[Camera]],
        ttl: float = 10.0,
        fetch_timeout: float = 5.0,
        stagger_delay: float = 0.4,
        max_workers: int = 2,
        stop_event: threading.Event | None = None,
        get_runtime_status: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.go2rtc_url = go2rtc_url.rstrip("/")
        self.get_cameras = get_cameras
        self.ttl = max(0.1, float(ttl))
        self.fetch_timeout = max(0.5, float(fetch_timeout))
        self.stagger_delay = max(0.0, float(stagger_delay))
        self.stop_event = stop_event or threading.Event()
        self.get_runtime_status = get_runtime_status
        self.started_at = time.monotonic()

        self._cache_lock = threading.Lock()
        self._entries: dict[str, SnapshotEntry] = {}
        self._errors: dict[str, str] = {}

        self._cold_locks_guard = threading.Lock()
        self._cold_locks: dict[str, threading.Lock] = {}

        self._inflight_lock = threading.Lock()
        self._inflight: set[str] = set()
        self._closed = False

        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="snapshot-fetch",
        )
        self._bg_thread: threading.Thread | None = None

    def _cold_lock_for(self, cam_id: str) -> threading.Lock:
        with self._cold_locks_guard:
            lock = self._cold_locks.get(cam_id)
            if lock is None:
                lock = threading.Lock()
                self._cold_locks[cam_id] = lock
            return lock

    def _fetch_from_go2rtc(self, cam_id: str) -> bytes:
        src = urllib.parse.quote(f"{cam_id}_hd", safe="")
        url = f"{self.go2rtc_url}/api/frame.jpeg?src={src}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=self.fetch_timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise RuntimeError(f"go2rtc returned HTTP {status}")
            data = resp.read()
        if not data or not data.startswith(b"\xff\xd8"):
            raise ValueError("go2rtc response is not a valid JPEG payload")
        return data

    def refresh_now(self, cam_id: str) -> bool:
        """Synchronously fetch and update the RAM cache for *cam_id*."""
        try:
            data = self._fetch_from_go2rtc(cam_id)
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            OSError,
            ValueError,
            RuntimeError,
        ) as exc:
            with self._cache_lock:
                self._errors[cam_id] = str(exc)
            LOG.debug("[%s] Snapshot refresh failed: %s", cam_id, exc)
            return False

        now_mono = time.monotonic()
        now_wall = time.time()
        with self._cache_lock:
            self._entries[cam_id] = SnapshotEntry(
                data=data,
                fetched_monotonic=now_mono,
                fetched_at=now_wall,
            )
            self._errors.pop(cam_id, None)
        return True

    def _run_refresh_task(self, cam_id: str) -> None:
        try:
            if not self._closed and not self.stop_event.is_set():
                self.refresh_now(cam_id)
        finally:
            with self._inflight_lock:
                self._inflight.discard(cam_id)

    def trigger_refresh(self, cam_id: str) -> bool:
        """Schedule an asynchronous refresh for *cam_id*, deduplicated per camera."""
        with self._inflight_lock:
            if self._closed or self.stop_event.is_set() or cam_id in self._inflight:
                return False
            self._inflight.add(cam_id)
        try:
            self._executor.submit(self._run_refresh_task, cam_id)
            return True
        except RuntimeError:
            with self._inflight_lock:
                self._inflight.discard(cam_id)
            return False

    def get_snapshot(self, cam_id: str) -> tuple[bytes | None, str, float | None]:
        """Return (jpeg_bytes, cache_status, age_seconds) using stale-while-revalidate."""
        now_mono = time.monotonic()
        with self._cache_lock:
            entry = self._entries.get(cam_id)
        if entry is not None:
            age = max(0.0, now_mono - entry.fetched_monotonic)
            if age <= self.ttl:
                return entry.data, "HIT", age
            self.trigger_refresh(cam_id)
            return entry.data, "STALE", age

        cold_lock = self._cold_lock_for(cam_id)
        with cold_lock:
            with self._cache_lock:
                entry = self._entries.get(cam_id)
            if entry is not None:
                age = max(0.0, time.monotonic() - entry.fetched_monotonic)
                return entry.data, "HIT" if age <= self.ttl else "STALE", age

            if self.refresh_now(cam_id):
                with self._cache_lock:
                    entry = self._entries.get(cam_id)
                if entry is not None:
                    age = max(0.0, time.monotonic() - entry.fetched_monotonic)
                    return entry.data, "MISS", age
        return None, "MISS", None

    def is_known_camera(self, cam_id: str) -> bool:
        with self._cache_lock:
            if cam_id in self._entries:
                return True
        return any(c.id == cam_id for c in self.get_cameras())

    def get_camera_cache_info(self, cam_id: str) -> dict[str, Any]:
        now_mono = time.monotonic()
        with self._cache_lock:
            entry = self._entries.get(cam_id)
            err = self._errors.get(cam_id)
        if entry is None:
            return {
                "snapshot_cached": False,
                "snapshot_age_seconds": None,
                "snapshot_bytes": 0,
                "snapshot_error": err,
            }
        return {
            "snapshot_cached": True,
            "snapshot_age_seconds": round(max(0.0, now_mono - entry.fetched_monotonic), 2),
            "snapshot_bytes": len(entry.data),
            "snapshot_error": err,
        }

    def get_health_payload(self) -> dict[str, Any]:
        cams = self.get_cameras()
        with self._cache_lock:
            cached_count = len(self._entries)
        runtime = self.get_runtime_status() if self.get_runtime_status else {}
        return {
            "status": "ok",
            "uptime_seconds": round(max(0.0, time.monotonic() - self.started_at), 1),
            "cameras_total": len(cams),
            "cameras_online": sum(1 for c in cams if c.online),
            "cached_snapshots": cached_count,
            "cache_ttl_seconds": self.ttl,
            **runtime,
        }

    def get_cameras_payload(self) -> dict[str, Any]:
        cams = sorted(self.get_cameras(), key=lambda c: c.number)
        runtime = self.get_runtime_status() if self.get_runtime_status else {}
        motion_map = runtime.get("motion_states", {})
        items: list[dict[str, Any]] = []
        for cam in cams:
            item = {
                "id": cam.id,
                "number": cam.number,
                "name": cam.name,
                "ip": cam.ip,
                "mac": cam.mac,
                "model": cam.model,
                "firmware": cam.firmware,
                "online": cam.online,
                "onvif_ok": cam.onvif_ok,
                "rtsp_ok": cam.rtsp_ok,
                "motion": motion_map.get(cam.id),
                **self.get_camera_cache_info(cam.id),
            }
            items.append(item)
        return {"cameras": items}

    def _refresh_loop(self) -> None:
        while not self.stop_event.is_set() and not self._closed:
            cams = sorted(
                (c for c in self.get_cameras() if c.online),
                key=lambda c: c.number,
            )
            refresh_threshold = self.ttl * 0.8
            for cam in cams:
                if self.stop_event.is_set() or self._closed:
                    break
                with self._cache_lock:
                    entry = self._entries.get(cam.id)
                needs_refresh = entry is None or (time.monotonic() - entry.fetched_monotonic) >= refresh_threshold
                if needs_refresh:
                    self.refresh_now(cam.id)
                    if self.stagger_delay > 0 and self.stop_event.wait(self.stagger_delay):
                        break

            pass_wait = max(0.5, min(2.0, self.ttl / 4.0))
            if self.stop_event.wait(pass_wait):
                break

    def start_background_refresh(self) -> None:
        if self._bg_thread is not None and self._bg_thread.is_alive():
            return
        self._bg_thread = threading.Thread(
            target=self._refresh_loop,
            name="snapshot-refresher",
            daemon=True,
        )
        self._bg_thread.start()

    def close(self) -> None:
        self._closed = True
        if self._bg_thread is not None and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=3.0)
        self._executor.shutdown(wait=False, cancel_futures=True)


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(cache: SnapshotCache) -> type[http.server.BaseHTTPRequestHandler]:
    class SnapshotHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            LOG.debug("HTTP %s - %s", self.address_string(), format % args)

        def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
            body = (json.dumps(payload) + "\n").encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/health":
                self._send_json(200, cache.get_health_payload())
                return
            if path == "/api/cameras":
                self._send_json(200, cache.get_cameras_payload())
                return

            match = _SNAPSHOT_PATH_RE.match(path)
            if match:
                cam_id = match.group(1)
                if not cache.is_known_camera(cam_id):
                    self._send_json(404, {"error": f"Unknown camera {cam_id!r}"})
                    return
                data, cache_state, age = cache.get_snapshot(cam_id)
                if data is None:
                    self._send_json(503, {"error": f"Snapshot unavailable for {cam_id}"})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Cache", cache_state)
                if age is not None:
                    self.send_header("X-Snapshot-Age", f"{age:.2f}")
                self.end_headers()
                self.wfile.write(data)
                return

            self._send_json(404, {"error": "Not found"})

    return SnapshotHandler


class SnapshotHTTPServer:
    """Lifecycle wrapper around the threaded RAM snapshot HTTP server."""

    def __init__(self, cache: SnapshotCache, host: str = "0.0.0.0", port: int = 1985) -> None:
        self.cache = cache
        self.host = host
        self.port = int(port)
        self._httpd = _QuietThreadingHTTPServer((self.host, self.port), _make_handler(cache))
        self.server_port: int = int(self._httpd.server_address[1])
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.cache.start_background_refresh()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="snapshot-http",
            daemon=True,
        )
        self._thread.start()
        LOG.info("Snapshot HTTP server listening on %s:%d", self.host, self.server_port)

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self.cache.close()
