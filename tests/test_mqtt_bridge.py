from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zosi_bridge.models import Camera
from zosi_bridge.mqtt_bridge import BRIDGE_STATUS_TOPIC, ZosiDaemon


class FakeMqttClient:
    def __init__(self) -> None:
        self.will: tuple[str, str, int, bool] | None = None
        self.connected_args: tuple[str, int, int] | None = None
        self.published: list[tuple[str, str, int, bool]] = []
        self.subscribed: list[tuple[str, int]] = []
        self.disconnected = False
        self.loop_started = False
        self.loop_stopped = False
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def username_pw_set(self, _user: str, _pw: str) -> None:
        pass

    def will_set(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.will = (topic, payload, qos, retain)

    def reconnect_delay_set(self, **_kwargs: object) -> None:
        pass

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_args = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_started = True

    def loop_stop(self) -> None:
        self.loop_stopped = True

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> SimpleNamespace:
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(wait_for_publish=lambda timeout=1.0: None)


class TestMqttBridge(unittest.TestCase):
    def test_lwt_discovery_motion_and_clean_shutdown(self) -> None:
        fake_client = FakeMqttClient()
        with tempfile.TemporaryDirectory() as td:
            daemon = ZosiDaemon(
                subnet="192.168.138.0/24",
                cameras_file=Path(td) / "cameras.json",
                go2rtc_file=Path(td) / "go2rtc.yaml",
                go2rtc_url="http://127.0.0.1:1984",
                mqtt_host="localhost",
                mqtt_port=1883,
                snapshot_port=1985,
                mqtt_client=fake_client,
            )

            # LWT configured before connect
            self.assertEqual(fake_client.will, (BRIDGE_STATUS_TOPIC, "offline", 1, True))

            cam = Camera(
                id="zosi_1",
                number=1,
                name="Zosi Camera 1",
                mac="00:11:22:33:44:01",
                ip="192.168.138.11",
                firmware="1.2.3",
                online=True,
            )
            daemon.cameras = {cam.id: cam}

            # Simulate connect callback
            daemon._on_mqtt_connect(fake_client, None, None, 0)
            self.assertIn((BRIDGE_STATUS_TOPIC, "online", 1, True), fake_client.published)
            self.assertIn(("homeassistant/status", 1), fake_client.subscribed)

            # Verify HA discovery includes combined bridge + camera availability
            motion_disc = [p for t, p, _, _ in fake_client.published if t == "homeassistant/binary_sensor/zosi_1_motion/config"]
            self.assertEqual(len(motion_disc), 1)
            cfg = json.loads(motion_disc[0])
            self.assertEqual(cfg["availability_mode"], "all")
            topics = [entry["topic"] for entry in cfg["availability"]]
            self.assertEqual(topics, [BRIDGE_STATUS_TOPIC, "zosi/zosi_1/status"])

            # Motion ON triggers MQTT publish + async snapshot refresh
            assert daemon.snapshot_cache is not None
            with mock.patch.object(daemon.snapshot_cache, "trigger_refresh", return_value=True) as m_refresh:
                daemon._on_motion_event("zosi_1", True)
                m_refresh.assert_called_once_with("zosi_1", respect_backoff=True)
            self.assertIn(("zosi/zosi_1/motion", "ON", 1, True), fake_client.published)

            # Shutdown publishes offline for camera and bridge, then disconnects
            daemon.shutdown()
            self.assertIn(("zosi/zosi_1/status", "offline", 1, True), fake_client.published)
            self.assertIn((BRIDGE_STATUS_TOPIC, "offline", 1, True), fake_client.published)
            self.assertTrue(fake_client.disconnected)
            self.assertTrue(fake_client.loop_stopped)


if __name__ == "__main__":
    unittest.main()
