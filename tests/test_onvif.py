from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zosi_bridge.models import Camera, load_cameras
from zosi_bridge.onvif import MotionWatcher, discover_cameras, soap_request


class TestSoapRequest(unittest.TestCase):
    def test_escapes_to_addr_and_action(self) -> None:
        captured: dict[str, bytes] = {}

        class FakeResp:
            def read(self) -> bytes:
                return b'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"><s:Body/></s:Envelope>'

            def __enter__(self) -> "FakeResp":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

        def fake_urlopen(req: object, timeout: float = 5.0) -> FakeResp:
            captured["data"] = req.data  # type: ignore[attr-defined]
            return FakeResp()

        with mock.patch("zosi_bridge.onvif.urllib.request.urlopen", side_effect=fake_urlopen):
            root = soap_request(
                "http://192.168.138.11:8080/onvif/event",
                "http://example.com/action?a=1&b=2",
                "<tev:PullMessages/>",
                to_addr="http://192.168.138.11:8080/sub?id=1&token=<abc>",
            )

        self.assertIsInstance(root, ET.Element)
        body = captured["data"].decode("utf-8")
        self.assertIn("id=1&amp;token=&lt;abc&gt;", body)
        self.assertIn("a=1&amp;b=2", body)
        ET.fromstring(captured["data"])

    def test_rejects_doctype(self) -> None:
        class FakeResp:
            def read(self) -> bytes:
                return b"<!DOCTYPE foo><root/>"

            def __enter__(self) -> "FakeResp":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

        with mock.patch("zosi_bridge.onvif.urllib.request.urlopen", return_value=FakeResp()):
            with self.assertRaises(ET.ParseError):
                soap_request("http://192.168.138.11:8080/onvif", "urn:test", "<x/>")


class TestDiscovery(unittest.TestCase):
    def test_discover_updates_ip_and_ignores_duplicate_mac(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cams_file = Path(td) / "cameras.json"
            go2rtc_file = Path(td) / "go2rtc.yaml"

            def fake_probe(ip: str, arp_mac: str | None = None, onvif_port: int = 8080, rtsp_port: int = 554):
                if ip == "192.168.138.11":
                    return {
                        "ip": ip,
                        "mac": "00:11:22:33:44:01",
                        "manufacturer": "Ansjer",
                        "model": "IPC",
                        "firmware": "1.0.0",
                        "serial": "SN1",
                        "hardware_id": "HW1",
                        "onvif_ok": True,
                        "rtsp_ok": True,
                    }
                if ip == "192.168.138.12":
                    # Duplicate MAC should be skipped cleanly
                    return {
                        "ip": ip,
                        "mac": "00:11:22:33:44:01",
                        "manufacturer": "Ansjer",
                        "model": "IPC",
                        "firmware": "1.0.0",
                        "serial": "SN1",
                        "hardware_id": "HW1",
                        "onvif_ok": True,
                        "rtsp_ok": True,
                    }
                return None

            with (
                mock.patch("zosi_bridge.onvif.arp_scan", return_value={"192.168.138.11": "00:11:22:33:44:01"}),
                mock.patch("zosi_bridge.onvif.tcp_scan_onvif", return_value=["192.168.138.11", "192.168.138.12"]),
                mock.patch("zosi_bridge.onvif.resolve_exclude_ips", return_value={"192.168.138.250"}),
                mock.patch("zosi_bridge.onvif.probe_camera", side_effect=fake_probe),
                mock.patch("zosi_bridge.onvif.reload_go2rtc", return_value=True) as m_reload,
            ):
                cams, changed = discover_cameras(
                    subnet_str="192.168.138.0/24",
                    cameras_file=cams_file,
                    go2rtc_file=go2rtc_file,
                    go2rtc_url="http://zosi-go2rtc:1984",
                )

            self.assertTrue(changed)
            self.assertEqual(len(cams), 1)
            self.assertEqual(cams[0].id, "zosi_1")
            self.assertEqual(cams[0].ip, "192.168.138.11")
            m_reload.assert_called_once()
            self.assertEqual(len(load_cameras(cams_file)), 1)


class TestMotionWatcher(unittest.TestCase):
    def test_poll_interval_prevents_tight_loop(self) -> None:
        cam = Camera(id="zosi_1", number=1, name="Cam 1", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True)
        stop = threading.Event()
        events: list[tuple[str, bool]] = []
        poll_count = 0

        def fake_pull(_sub: str) -> list[bool]:
            nonlocal poll_count
            poll_count += 1
            if poll_count == 1:
                return [True]
            stop.set()
            return [False]

        watcher = MotionWatcher(
            camera=cam,
            on_motion=lambda cid, state: events.append((cid, state)),
            on_availability=lambda _cid, _on: None,
            stop_event=stop,
            poll_interval=0.15,
            off_delay=0.0,
        )
        with (
            mock.patch.object(watcher, "_create_subscription", return_value="http://192.168.138.11:8080/sub"),
            mock.patch.object(watcher, "_pull_messages", side_effect=fake_pull),
        ):
            t0 = time.monotonic()
            watcher.run()
            elapsed = time.monotonic() - t0

        self.assertGreaterEqual(elapsed, 0.12)
        self.assertEqual(events, [("zosi_1", True), ("zosi_1", False)])

    def test_off_delay_debounce_hold_and_cancel(self) -> None:
        cam = Camera(id="zosi_1", number=1, name="Cam 1", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True)
        stop = threading.Event()
        events: list[tuple[str, bool]] = []

        watcher = MotionWatcher(
            camera=cam,
            on_motion=lambda cid, state: events.append((cid, state)),
            on_availability=lambda _cid, _on: None,
            stop_event=stop,
            poll_interval=1.0,
            off_delay=2.0,
        )

        watcher._process_states("zosi_1", [True], now=10.0)
        self.assertEqual(events, [("zosi_1", True)])

        # False starts debounce timer at 10.5 -> expires at 12.5
        watcher._process_states("zosi_1", [False], now=10.5)
        self.assertEqual(events, [("zosi_1", True)])

        # Another False at 11.0 must NOT reset deadline past 12.5
        watcher._process_states("zosi_1", [False], now=11.0)
        self.assertEqual(events, [("zosi_1", True)])

        # Another True at 11.5 cancels pending off
        watcher._process_states("zosi_1", [True], now=11.5)
        watcher._process_states("zosi_1", [], now=13.0)
        self.assertEqual(events, [("zosi_1", True)])

        # New False at 13.0 expires at 15.0
        watcher._process_states("zosi_1", [False], now=13.0)
        watcher._process_states("zosi_1", [], now=14.9)
        self.assertEqual(events, [("zosi_1", True)])
        watcher._process_states("zosi_1", [], now=15.0)
        self.assertEqual(events, [("zosi_1", True), ("zosi_1", False)])


if __name__ == "__main__":
    unittest.main()
