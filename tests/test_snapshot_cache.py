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


def _start_mock_go2rtc(state: _MockGo2rtcState) -> tuple[http.server.ThreadingHTTPServer, str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            with state.lock:
                state.calls[self.path] = state.calls.get(self.path, 0) + 1
                should_fail = state.fail_next
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
                self.assertEqual(resp.read(), b"\xff\xd8\xff\xe0MOCKJPEG")

            # Warm HIT does not re-fetch go2rtc
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

            with urllib.request.urlopen(f"{base_url}/api/cameras", timeout=3) as resp:
                cam_payload = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(len(cam_payload["cameras"]), 1)
                self.assertTrue(cam_payload["cameras"][0]["snapshot_cached"])

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


if __name__ == "__main__":
    unittest.main()
