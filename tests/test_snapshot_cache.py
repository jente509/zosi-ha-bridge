from __future__ import annotations

import http.server
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zosi_bridge.models import Camera
from zosi_bridge.snapshot_cache import SnapshotCache, SnapshotHTTPServer


class _MockGo2rtcState:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.lock = threading.Lock()
        self.payload = b"\xff\xd8\xff\xe0MOCKJPEG"
        self.fail_next = False
        self.fail_paths: set[str] = set()


def _start_mock_go2rtc(state: _MockGo2rtcState) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            with state.lock:
                state.calls[self.path] = state.calls.get(self.path, 0) + 1
                should_fail = state.fail_next or (self.path in state.fail_paths)
                body = state.payload
            if should_fail:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class TestSnapshotCacheAndServer(unittest.TestCase):
    def test_ram_cache_hit_stale_and_endpoints(self) -> None:
        mock_state = _MockGo2rtcState()
        mock_go2rtc, go2rtc_url = _start_mock_go2rtc(mock_state)
        cams = [
            Camera(id="zosi_1", number=1, name="Front", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True),
        ]
        stop_event = threading.Event()
        cache = SnapshotCache(
            go2rtc_url=go2rtc_url,
            get_cameras=lambda: cams,
            ttl=0.25,
            stagger_delay=0.05,
            stop_event=stop_event,
            get_runtime_status=lambda: {"mqtt_connected": True, "motion_states": {"zosi_1": False}},
        )
        server = SnapshotHTTPServer(cache, host="127.0.0.1", port=0)
        # Start HTTP server only (without background refresher loop interfering with deterministic HIT/STALE assertions)
        http_thread = threading.Thread(target=server._httpd.serve_forever, daemon=True)
        http_thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        try:
            # Cold MISS fetches once
            with urllib.request.urlopen(f"{base_url}/api/snapshot/zosi_1.jpg", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Cache"), "MISS")
                etag1 = resp.headers.get("ETag")
                self.assertTrue(etag1)
                self.assertEqual(resp.read(), b"\xff\xd8\xff\xe0MOCKJPEG")

            # Warm HIT with matching If-None-Match returns 304 Not Modified
            req_304 = urllib.request.Request(
                f"{base_url}/api/snapshot/zosi_1.jpg",
                headers={"If-None-Match": etag1},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx_304:
                urllib.request.urlopen(req_304, timeout=3)
            self.assertEqual(ctx_304.exception.code, 304)
            self.assertEqual(ctx_304.exception.headers.get("ETag"), etag1)
            self.assertEqual(ctx_304.exception.headers.get("X-Cache"), "HIT")
            ctx_304.exception.close()

            # Warm HIT without If-None-Match returns 200 and does not re-fetch go2rtc
            with urllib.request.urlopen(f"{base_url}/api/snapshot/zosi_1.jpg", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Cache"), "HIT")
            self.assertEqual(mock_state.calls.get("/api/frame.jpeg?src=zosi_1_hd"), 1)

            # Wait past TTL -> STALE serves cached bytes immediately and refreshes async
            time.sleep(0.3)
            with mock_state.lock:
                mock_state.payload = b"\xff\xd8\xff\xe0UPDATEDJPEG"
            with urllib.request.urlopen(f"{base_url}/api/snapshot/zosi_1.jpg", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Cache"), "STALE")
                self.assertEqual(resp.read(), b"\xff\xd8\xff\xe0MOCKJPEG")

            # Wait briefly for async refresh to complete
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                with urllib.request.urlopen(f"{base_url}/api/snapshot/zosi_1.jpg", timeout=3) as resp:
                    if resp.headers.get("X-Cache") == "HIT":
                        self.assertNotEqual(resp.headers.get("ETag"), etag1)
                        self.assertEqual(resp.read(), b"\xff\xd8\xff\xe0UPDATEDJPEG")
                        break
                time.sleep(0.05)
            else:
                self.fail("Async stale-while-revalidate did not refresh cache in time")

            # /health and /api/cameras endpoints
            with urllib.request.urlopen(f"{base_url}/health", timeout=3) as resp:
                health = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(health["status"], "ok")
                self.assertEqual(health["cameras_total"], 1)
                self.assertTrue(health["mqtt_connected"])
                self.assertIn("cache_stats", health)
                self.assertGreaterEqual(health["cache_stats"]["not_modified"], 1)
                self.assertGreaterEqual(health["cache_stats"]["refreshes_ok"], 2)

            with urllib.request.urlopen(f"{base_url}/api/cameras", timeout=3) as resp:
                cam_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(cam_payload["cameras"]), 1)
                cam_info = cam_payload["cameras"][0]
                self.assertTrue(cam_info["snapshot_cached"])
                self.assertIsNotNone(cam_info["snapshot_fetch_ms"])
                self.assertTrue(cam_info["snapshot_etag"])

            # HEAD returns 200 with ETag and empty body
            head_req = urllib.request.Request(f"{base_url}/api/snapshot/zosi_1.jpg", method="HEAD")
            with urllib.request.urlopen(head_req, timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.headers.get("ETag"))
                self.assertEqual(resp.read(), b"")

            # Unknown camera -> 404
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{base_url}/api/snapshot/zosi_999.jpg", timeout=3)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()
        finally:
            stop_event.set()
            server.stop()
            mock_go2rtc.shutdown()
            mock_go2rtc.server_close()

    def test_trigger_refresh_deduplicates_and_handles_bad_jpeg(self) -> None:
        mock_state = _MockGo2rtcState()
        mock_state.payload = b"NOT_A_JPEG"
        mock_go2rtc, go2rtc_url = _start_mock_go2rtc(mock_state)
        cams = [Camera(id="zosi_1", number=1, name="Front", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True)]
        cache = SnapshotCache(go2rtc_url=go2rtc_url, get_cameras=lambda: cams, ttl=10.0)
        try:
            data, state, _ = cache.get_snapshot("zosi_1")
            self.assertIsNone(data)
            self.assertEqual(state, "MISS")
            info = cache.get_camera_cache_info("zosi_1")
            self.assertFalse(info["snapshot_cached"])
            self.assertIn("JPEG", info["snapshot_error"])
        finally:
            cache.close()
            mock_go2rtc.shutdown()
            mock_go2rtc.server_close()

    def test_hd_failure_falls_back_to_sd(self) -> None:
        mock_state = _MockGo2rtcState()
        mock_state.fail_paths.add("/api/frame.jpeg?src=zosi_1_hd")
        mock_go2rtc, go2rtc_url = _start_mock_go2rtc(mock_state)
        cams = [Camera(id="zosi_1", number=1, name="Front", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True)]
        cache = SnapshotCache(go2rtc_url=go2rtc_url, get_cameras=lambda: cams, ttl=10.0)
        try:
            data, state, _ = cache.get_snapshot("zosi_1")
            self.assertEqual(data, b"\xff\xd8\xff\xe0MOCKJPEG")
            self.assertEqual(state, "MISS")
            self.assertEqual(mock_state.calls.get("/api/frame.jpeg?src=zosi_1_hd"), 1)
            self.assertEqual(mock_state.calls.get("/api/frame.jpeg?src=zosi_1_sd"), 1)
        finally:
            cache.close()
            mock_go2rtc.shutdown()
            mock_go2rtc.server_close()

    def test_warm_cache_prewarms_only_online_cameras(self) -> None:
        mock_state = _MockGo2rtcState()
        mock_go2rtc, go2rtc_url = _start_mock_go2rtc(mock_state)
        cams = [
            Camera(id="zosi_1", number=1, name="Cam 1", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True),
            Camera(id="zosi_2", number=2, name="Cam 2", mac="00:11:22:33:44:02", ip="192.168.138.12", online=False),
            Camera(id="zosi_3", number=3, name="Cam 3", mac="00:11:22:33:44:03", ip="192.168.138.13", online=True),
        ]
        cache = SnapshotCache(go2rtc_url=go2rtc_url, get_cameras=lambda: cams, ttl=10.0)
        try:
            queued = cache.warm_cache()
            self.assertEqual(queued, 2)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if cache.get_camera_cache_info("zosi_1")["snapshot_cached"] and cache.get_camera_cache_info("zosi_3")["snapshot_cached"]:
                    break
                time.sleep(0.02)
            else:
                self.fail("warm_cache did not populate online cameras in time")
            self.assertFalse(cache.get_camera_cache_info("zosi_2")["snapshot_cached"])
            self.assertEqual(cache.warm_cache(), 0)
        finally:
            cache.close()
            mock_go2rtc.shutdown()
            mock_go2rtc.server_close()

    def test_idle_throttling_and_active_transition(self) -> None:
        mock_state = _MockGo2rtcState()
        mock_go2rtc, go2rtc_url = _start_mock_go2rtc(mock_state)
        cams = [Camera(id="zosi_1", number=1, name="Front", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True)]
        stop_event = threading.Event()
        cache = SnapshotCache(
            go2rtc_url=go2rtc_url,
            get_cameras=lambda: cams,
            ttl=0.15,
            stagger_delay=0.0,
            idle_refresh_interval=0.0,
            active_window=1.5,
            stop_event=stop_event,
        )
        try:
            cache.start_background_refresh()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if cache.get_camera_cache_info("zosi_1")["snapshot_cached"]:
                    break
                time.sleep(0.02)
            else:
                self.fail("Initial cache warmup did not run")

            # While idle with idle_refresh_interval=0.0, background loop must NOT keep re-fetching
            time.sleep(0.75)
            with mock_state.lock:
                idle_calls = mock_state.calls.get("/api/frame.jpeg?src=zosi_1_hd", 0)
            self.assertEqual(idle_calls, 1)
            self.assertEqual(cache.get_health_payload()["refresh_mode"], "idle")

            # Interactive snapshot access activates aggressive ttl*0.8 background refreshes
            data, _, _ = cache.get_snapshot("zosi_1")
            self.assertEqual(data, b"\xff\xd8\xff\xe0MOCKJPEG")
            self.assertEqual(cache.get_health_payload()["refresh_mode"], "active")

            time.sleep(0.75)
            with mock_state.lock:
                active_calls = mock_state.calls.get("/api/frame.jpeg?src=zosi_1_hd", 0)
            self.assertGreater(active_calls, 1)
        finally:
            stop_event.set()
            cache.close()
            mock_go2rtc.shutdown()
            mock_go2rtc.server_close()

    def test_trigger_refresh_respects_backoff(self) -> None:
        mock_state = _MockGo2rtcState()
        mock_state.fail_next = True
        mock_go2rtc, go2rtc_url = _start_mock_go2rtc(mock_state)
        cams = [Camera(id="zosi_1", number=1, name="Front", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True)]
        cache = SnapshotCache(go2rtc_url=go2rtc_url, get_cameras=lambda: cams, ttl=10.0)
        try:
            self.assertFalse(cache.refresh_now("zosi_1"))
            self.assertFalse(cache.trigger_refresh("zosi_1", respect_backoff=True))
        finally:
            cache.close()
            mock_go2rtc.shutdown()
            mock_go2rtc.server_close()


if __name__ == "__main__":
    unittest.main()
