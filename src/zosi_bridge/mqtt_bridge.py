"""MQTT Home Assistant Discovery bridge and daemon orchestrator."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .go2rtc_config import DEFAULT_VAAPI_DEVICE
from .models import Camera
from .onvif import MotionWatcher, discover_cameras
from .snapshot_cache import SnapshotCache, SnapshotHTTPServer

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None  # type: ignore[assignment]

LOG = logging.getLogger("zosi-bridge.mqtt")

BRIDGE_STATUS_TOPIC = "zosi/bridge/status"


class ZosiDaemon:
    """Orchestrates discovery, go2rtc config reload, MQTT HA discovery, snapshot cache, and motion watchers."""

    def __init__(
        self,
        subnet: str,
        cameras_file: Path,
        go2rtc_file: Path,
        go2rtc_url: str,
        mqtt_host: str,
        mqtt_port: int,
        mqtt_user: str = "",
        mqtt_password: str = "",
        discover_interval: int = 300,
        exclude_ips: Iterable[str] | None = None,
        onvif_port: int = 8080,
        rtsp_port: int = 554,
        motion_poll_interval: float = 1.5,
        motion_off_delay: float = 0.0,
        transcode_mode: str = "vaapi",
        vaapi_device: str = DEFAULT_VAAPI_DEVICE,
        snapshot_port: int = 1985,
        snapshot_host: str = "0.0.0.0",
        snapshot_cache_ttl: float = 10.0,
        snapshot_idle_refresh_interval: float = 300.0,
        mqtt_client: Any = None,
    ) -> None:
        if mqtt_client is None and mqtt is None:
            raise RuntimeError("paho-mqtt is required for daemon mode")

        self.subnet = subnet
        self.cameras_file = cameras_file
        self.go2rtc_file = go2rtc_file
        self.go2rtc_url = go2rtc_url
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_user = mqtt_user
        self.mqtt_password = mqtt_password
        self.discover_interval = discover_interval
        self.exclude_ips = list(exclude_ips) if exclude_ips is not None else []
        self.onvif_port = onvif_port
        self.rtsp_port = rtsp_port
        self.motion_poll_interval = motion_poll_interval
        self.motion_off_delay = motion_off_delay
        self.transcode_mode = transcode_mode
        self.vaapi_device = vaapi_device
        self.snapshot_port = int(snapshot_port)
        self.snapshot_host = snapshot_host
        self.snapshot_cache_ttl = float(snapshot_cache_ttl)
        self.snapshot_idle_refresh_interval = float(snapshot_idle_refresh_interval)

        self.stop_event = threading.Event()
        self.cameras_lock = threading.Lock()
        self.cameras: dict[str, Camera] = {}
        self.watchers: dict[str, MotionWatcher] = {}
        self.mqtt_connected = threading.Event()

        if mqtt_client is not None:
            self.client = mqtt_client
        else:
            self.client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"zosi-bridge-{os.getpid()}",
            )
        if mqtt_user:
            self.client.username_pw_set(mqtt_user, mqtt_password)

        self.client.will_set(BRIDGE_STATUS_TOPIC, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_mqtt_connect
        self.client.on_disconnect = self._on_mqtt_disconnect
        self.client.on_message = self._on_mqtt_message
        if hasattr(self.client, "reconnect_delay_set"):
            self.client.reconnect_delay_set(min_delay=2, max_delay=30)

        self.snapshot_cache: SnapshotCache | None = None
        self.snapshot_server: SnapshotHTTPServer | None = None
        if self.snapshot_port > 0:
            self.snapshot_cache = SnapshotCache(
                go2rtc_url=self.go2rtc_url,
                get_cameras=self._snapshot_get_cameras,
                ttl=self.snapshot_cache_ttl,
                idle_refresh_interval=self.snapshot_idle_refresh_interval,
                stop_event=self.stop_event,
                get_runtime_status=self._snapshot_runtime_status,
            )

    def _snapshot_get_cameras(self) -> list[Camera]:
        with self.cameras_lock:
            return list(self.cameras.values())

    def _snapshot_runtime_status(self) -> dict[str, Any]:
        with self.cameras_lock:
            motion_states = {
                cam_id: watcher.last_motion
                for cam_id, watcher in self.watchers.items()
            }
        return {
            "mqtt_connected": self.mqtt_connected.is_set(),
            "motion_states": motion_states,
        }

    def _on_mqtt_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if reason_code == 0 or str(reason_code) == "Success":
            LOG.info("Connected to MQTT broker %s:%d", self.mqtt_host, self.mqtt_port)
            self.mqtt_connected.set()
            client.publish(BRIDGE_STATUS_TOPIC, "online", qos=1, retain=True)
            client.subscribe("homeassistant/status", qos=1)
            self.publish_all_discovery()
        else:
            LOG.warning("MQTT connect failed: %s", reason_code)

    def _on_mqtt_disconnect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        self.mqtt_connected.clear()
        LOG.warning("Disconnected from MQTT broker (%s)", reason_code)

    def _on_mqtt_message(self, client: Any, userdata: Any, msg: Any) -> None:
        payload = msg.payload.decode("utf-8", errors="ignore").strip().lower()
        if msg.topic == "homeassistant/status" and payload == "online":
            LOG.info("Home Assistant came online; republishing MQTT discovery")
            self.publish_all_discovery()

    def _publish(self, topic: str, payload: str, retain: bool = True) -> Any:
        if not self.mqtt_connected.is_set():
            return None
        return self.client.publish(topic, payload, qos=1, retain=retain)

    def publish_camera_discovery(self, cam: Camera) -> None:
        device = {
            "identifiers": [f"zosi_{cam.mac_clean}"],
            "name": cam.name,
            "manufacturer": "Zosi (Ansjer)",
            "model": cam.model or "IPCamera",
            "sw_version": cam.firmware or "unknown",
        }
        cam_avail_topic = f"zosi/{cam.id}/status"
        availability = [
            {
                "topic": BRIDGE_STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            {
                "topic": cam_avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ]

        motion_cfg = {
            "name": "Motion",
            "unique_id": f"{cam.id}_motion",
            "object_id": f"{cam.id}_motion",
            "device_class": "motion",
            "state_topic": f"zosi/{cam.id}/motion",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability": availability,
            "availability_mode": "all",
            "device": device,
        }
        ip_cfg = {
            "name": "IP Address",
            "unique_id": f"{cam.id}_ip",
            "object_id": f"{cam.id}_ip",
            "entity_category": "diagnostic",
            "icon": "mdi:ip-network",
            "state_topic": f"zosi/{cam.id}/ip",
            "availability": availability,
            "availability_mode": "all",
            "device": device,
        }
        fw_cfg = {
            "name": "Firmware",
            "unique_id": f"{cam.id}_firmware",
            "object_id": f"{cam.id}_firmware",
            "entity_category": "diagnostic",
            "icon": "mdi:chip",
            "state_topic": f"zosi/{cam.id}/firmware",
            "availability": availability,
            "availability_mode": "all",
            "device": device,
        }

        self._publish(f"homeassistant/binary_sensor/{cam.id}_motion/config", json.dumps(motion_cfg))
        self._publish(f"homeassistant/sensor/{cam.id}_ip/config", json.dumps(ip_cfg))
        self._publish(f"homeassistant/sensor/{cam.id}_firmware/config", json.dumps(fw_cfg))

        self._publish(cam_avail_topic, "online" if cam.online else "offline")
        self._publish(f"zosi/{cam.id}/ip", cam.ip)
        self._publish(f"zosi/{cam.id}/firmware", cam.firmware or "unknown")

        watcher = self.watchers.get(cam.id)
        if watcher is None or watcher.last_motion is None:
            self._publish(f"zosi/{cam.id}/motion", "OFF")
        else:
            self._publish(f"zosi/{cam.id}/motion", "ON" if watcher.last_motion else "OFF")

    def publish_all_discovery(self) -> None:
        with self.cameras_lock:
            cams = list(self.cameras.values())
        for cam in cams:
            self.publish_camera_discovery(cam)

    def _on_motion_event(self, cam_id: str, is_motion: bool) -> None:
        self._publish(f"zosi/{cam_id}/motion", "ON" if is_motion else "OFF")
        if is_motion and self.snapshot_cache is not None:
            self.snapshot_cache.trigger_refresh(cam_id, respect_backoff=True)

    def _on_availability_event(self, cam_id: str, online: bool) -> None:
        with self.cameras_lock:
            if cam_id in self.cameras:
                self.cameras[cam_id].online = online
        self._publish(f"zosi/{cam_id}/status", "online" if online else "offline")

    def _sync_watchers(self, cams: list[Camera]) -> None:
        for cam in cams:
            watcher = self.watchers.get(cam.id)
            if watcher is None:
                watcher = MotionWatcher(
                    camera=cam,
                    on_motion=self._on_motion_event,
                    on_availability=self._on_availability_event,
                    stop_event=self.stop_event,
                    onvif_port=self.onvif_port,
                    poll_interval=self.motion_poll_interval,
                    off_delay=self.motion_off_delay,
                )
                self.watchers[cam.id] = watcher
                watcher.start()
            else:
                watcher.update_camera(cam)

    def run_discovery_cycle(self, initial: bool = False) -> None:
        LOG.info("Running camera discovery on %s", self.subnet)
        cams, _ = discover_cameras(
            subnet_str=self.subnet,
            cameras_file=self.cameras_file,
            go2rtc_file=self.go2rtc_file,
            go2rtc_url=self.go2rtc_url,
            quiet_reload_if_down=initial,
            exclude_ips=self.exclude_ips,
            onvif_port=self.onvif_port,
            rtsp_port=self.rtsp_port,
            transcode_mode=self.transcode_mode,
            vaapi_device=self.vaapi_device,
        )
        with self.cameras_lock:
            self.cameras = {c.id: c for c in cams}
        self._sync_watchers(cams)
        self.publish_all_discovery()

    def _start_snapshot_server(self) -> None:
        if self.snapshot_cache is None or self.snapshot_port <= 0:
            return
        self.snapshot_server = SnapshotHTTPServer(
            self.snapshot_cache,
            host=self.snapshot_host,
            port=self.snapshot_port,
        )
        self.snapshot_server.start()

    def _connect_mqtt(self) -> bool:
        while not self.stop_event.is_set():
            try:
                self.client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)
                self.client.loop_start()
                return True
            except Exception as exc:
                LOG.warning(
                    "Waiting for MQTT broker %s:%d (%s); retrying in 5s",
                    self.mqtt_host,
                    self.mqtt_port,
                    exc,
                )
                self.stop_event.wait(5.0)
        return False

    def shutdown(self) -> None:
        LOG.info("Shutting down zosi-bridge daemon")
        self.stop_event.set()

        if self.snapshot_server is not None:
            self.snapshot_server.stop()
            self.snapshot_server = None
        elif self.snapshot_cache is not None:
            self.snapshot_cache.close()

        for watcher in list(self.watchers.values()):
            if watcher.is_alive():
                watcher.join(timeout=5.0)

        with self.cameras_lock:
            cam_ids = list(self.cameras.keys())

        pending_msgs = []
        for cam_id in cam_ids:
            info = self._publish(f"zosi/{cam_id}/status", "offline")
            if info is not None:
                pending_msgs.append(info)
        bridge_info = self._publish(BRIDGE_STATUS_TOPIC, "offline")
        if bridge_info is not None:
            pending_msgs.append(bridge_info)

        for info in pending_msgs:
            if hasattr(info, "wait_for_publish"):
                try:
                    info.wait_for_publish(timeout=1.0)
                except Exception:
                    pass
        if pending_msgs:
            time.sleep(0.1)

        try:
            self.client.disconnect()
        except Exception:
            pass
        try:
            self.client.loop_stop()
        except Exception:
            pass

    def run(self) -> int:
        try:
            self.run_discovery_cycle(initial=True)
            if self.stop_event.is_set():
                return 0

            self._start_snapshot_server()
            if not self._connect_mqtt():
                return 0

            while not self.stop_event.wait(self.discover_interval):
                try:
                    self.run_discovery_cycle(initial=False)
                except Exception as exc:
                    LOG.exception("Periodic discovery failed: %s", exc)
        finally:
            self.shutdown()
        return 0
