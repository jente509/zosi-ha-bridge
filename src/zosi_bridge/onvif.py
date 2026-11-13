"""ONVIF discovery, SOAP client, RTSP probing, and PullPoint motion watcher."""

from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

from .go2rtc_config import DEFAULT_VAAPI_DEVICE, reload_go2rtc, render_go2rtc_yaml
from .models import Camera, atomic_write, load_cameras, normalize_mac, save_cameras
from .network import arp_scan, resolve_exclude_ips, tcp_port_open, tcp_scan_onvif

LOG = logging.getLogger("zosi-bridge.onvif")

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
WSA_NS = "http://www.w3.org/2005/08/addressing"
WSNT_NS = "http://docs.oasis-open.org/wsn/b-2"
TT_NS = "http://www.onvif.org/ver10/schema"


def soap_request(
    url: str,
    action: str,
    body_xml: str,
    to_addr: str | None = None,
    timeout: float = 5.0,
) -> ET.Element:
    """Send an ONVIF SOAP 1.2 request and parse the XML response safely."""
    safe_action = xml_escape(action)
    header_parts = [f"<wsa:Action>{safe_action}</wsa:Action>"]
    if to_addr:
        header_parts.append(f"<wsa:To>{xml_escape(to_addr)}</wsa:To>")

    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:wsa="http://www.w3.org/2005/08/addressing"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:tev="http://www.onvif.org/ver10/events/wsdl"'
        ' xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">'
        f"<s:Header>{''.join(header_parts)}</s:Header>"
        f"<s:Body>{body_xml}</s:Body>"
        "</s:Envelope>"
    )
    header_action = action.replace('"', "").replace("\r", "").replace("\n", "")
    req = urllib.request.Request(
        url,
        data=envelope.encode("utf-8"),
        headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{header_action}"',
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()

    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ET.ParseError("Disallowed XML DOCTYPE/ENTITY declaration in SOAP response")
    return ET.fromstring(data)


def _find_text_by_localname(root: ET.Element, localname: str) -> str:
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == localname and elem.text:
            return elem.text.strip()
    return ""


def probe_rtsp(ip: str, path: str = "/video1", port: int = 554, timeout: float = 2.0) -> bool:
    """Verify RTSP service responds with 200 OK to OPTIONS."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            req = f"OPTIONS rtsp://{ip}:{port}{path} RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            sock.sendall(req.encode("ascii"))
            resp = sock.recv(512).decode("ascii", errors="ignore")
            first_line = resp.splitlines()[0] if resp else ""
            return first_line.startswith("RTSP/1.0 200")
    except OSError:
        return False


def probe_camera(
    ip: str,
    arp_mac: str | None = None,
    onvif_port: int = 8080,
    rtsp_port: int = 554,
) -> dict[str, Any] | None:
    """Query ONVIF GetDeviceInformation + GetNetworkInterfaces and probe RTSP."""
    if not tcp_port_open(ip, onvif_port, timeout=0.8):
        return None

    device_url = f"http://{ip}:{onvif_port}/onvif/device_service"
    try:
        dev_root = soap_request(
            device_url,
            "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
            "<tds:GetDeviceInformation/>",
            timeout=4.0,
        )
        net_root = soap_request(
            device_url,
            "http://www.onvif.org/ver10/device/wsdl/GetNetworkInterfaces",
            "<tds:GetNetworkInterfaces/>",
            timeout=4.0,
        )
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        ET.ParseError,
        OSError,
    ) as exc:
        LOG.debug("ONVIF probe failed for %s: %s", ip, exc)
        return None

    hw_addr = _find_text_by_localname(net_root, "HwAddress")
    mac = normalize_mac(hw_addr or (arp_mac or ""))
    if not mac:
        return None

    manufacturer = _find_text_by_localname(dev_root, "Manufacturer") or "Ansjer"
    model = _find_text_by_localname(dev_root, "Model") or "IPCamera"
    firmware = _find_text_by_localname(dev_root, "FirmwareVersion")
    serial = _find_text_by_localname(dev_root, "SerialNumber")
    hardware_id = _find_text_by_localname(dev_root, "HardwareId")
    rtsp_ok = probe_rtsp(ip, "/video1", port=rtsp_port) and probe_rtsp(ip, "/video2", port=rtsp_port)

    return {
        "ip": ip,
        "mac": mac,
        "manufacturer": manufacturer,
        "model": model,
        "firmware": firmware,
        "serial": serial,
        "hardware_id": hardware_id,
        "onvif_ok": True,
        "rtsp_ok": rtsp_ok,
    }


def discover_cameras(
    subnet_str: str = "192.168.138.0/24",
    cameras_file: Path = Path("/data/cameras.json"),
    go2rtc_file: Path = Path("/go2rtc/go2rtc.yaml"),
    go2rtc_url: str | None = None,
    quiet_reload_if_down: bool = False,
    exclude_ips: Iterable[str] | None = None,
    onvif_port: int = 8080,
    rtsp_port: int = 554,
    transcode_mode: str = "vaapi",
    vaapi_device: str = DEFAULT_VAAPI_DEVICE,
) -> tuple[list[Camera], bool]:
    """Discover Zosi/Ansjer cameras and persist cameras.json + go2rtc.yaml.

    Returns (cameras, go2rtc_changed).
    """
    subnet = ipaddress.IPv4Network(subnet_str, strict=False)
    existing = load_cameras(cameras_file)
    by_mac: dict[str, Camera] = {c.mac: c for c in existing}

    excluded = resolve_exclude_ips(subnet, exclude_ips=exclude_ips, go2rtc_url=go2rtc_url)

    arp_hosts = arp_scan(subnet, passes=2, exclude_ips=excluded)
    candidate_ips = set(arp_hosts.keys()) | {c.ip for c in existing}
    tcp_hosts = tcp_scan_onvif(subnet, excluded, port=onvif_port)
    candidate_ips.update(tcp_hosts)
    candidate_ips -= excluded

    probed: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futs = {
            pool.submit(probe_camera, ip, arp_hosts.get(ip), onvif_port, rtsp_port): ip
            for ip in sorted(candidate_ips, key=ipaddress.IPv4Address)
        }
        for fut in concurrent.futures.as_completed(futs):
            ip = futs[fut]
            try:
                res = fut.result()
            except Exception as exc:
                LOG.warning("Unexpected error probing %s: %s", ip, exc)
                continue
            if res:
                probed.append(res)

    now = time.time()
    seen_macs: set[str] = set()
    new_devices_by_mac: dict[str, dict[str, Any]] = {}

    for info in sorted(probed, key=lambda d: ipaddress.IPv4Address(d["ip"])):
        mac = info["mac"]
        if mac in seen_macs:
            LOG.warning("Duplicate MAC %s responded from multiple IPs (ignoring %s)", mac, info["ip"])
            continue
        seen_macs.add(mac)
        if mac in by_mac:
            cam = by_mac[mac]
            cam.ip = info["ip"]
            cam.firmware = info["firmware"] or cam.firmware
            cam.serial = info["serial"] or cam.serial
            cam.hardware_id = info["hardware_id"] or cam.hardware_id
            cam.model = info["model"] or cam.model
            cam.onvif_ok = info["onvif_ok"]
            cam.rtsp_ok = info["rtsp_ok"]
            cam.online = True
            cam.last_seen = now
        else:
            new_devices_by_mac[mac] = info

    for mac, cam in by_mac.items():
        if mac not in seen_macs:
            cam.online = False
            cam.onvif_ok = False
            cam.rtsp_ok = False

    used_numbers = {c.number for c in by_mac.values()}
    next_num = 1
    for mac in sorted(new_devices_by_mac):
        info = new_devices_by_mac[mac]
        while next_num in used_numbers:
            next_num += 1
        used_numbers.add(next_num)
        cam_id = f"zosi_{next_num}"
        by_mac[mac] = Camera(
            id=cam_id,
            number=next_num,
            name=f"Zosi Camera {next_num}",
            mac=mac,
            ip=info["ip"],
            manufacturer="Zosi (Ansjer)",
            model=info["model"] or "IPCamera",
            firmware=info["firmware"],
            serial=info["serial"],
            hardware_id=info["hardware_id"],
            rtsp_ok=info["rtsp_ok"],
            onvif_ok=info["onvif_ok"],
            online=True,
            last_seen=now,
        )

    cameras = sorted(by_mac.values(), key=lambda c: c.number)
    save_cameras(cameras_file, cameras)

    go2rtc_yaml = render_go2rtc_yaml(
        cameras,
        transcode_mode=transcode_mode,
        vaapi_device=vaapi_device,
        rtsp_port=rtsp_port,
    )
    go2rtc_changed = atomic_write(go2rtc_file, go2rtc_yaml)
    if go2rtc_changed:
        LOG.info("Updated %s with %d cameras (mode=%s)", go2rtc_file, len(cameras), transcode_mode)
        if go2rtc_url:
            reload_go2rtc(go2rtc_url, quiet_if_down=quiet_reload_if_down)

    return cameras, go2rtc_changed


class MotionWatcher(threading.Thread):
    """Per-camera ONVIF PullPointSubscription loop publishing motion events via callback."""

    def __init__(
        self,
        camera: Camera,
        on_motion: Callable[[str, bool], None],
        on_availability: Callable[[str, bool], None],
        stop_event: threading.Event,
        onvif_port: int = 8080,
        poll_interval: float = 1.5,
        off_delay: float = 0.0,
    ) -> None:
        super().__init__(name=f"motion-{camera.id}", daemon=True)
        self.camera = camera
        self._lock = threading.Lock()
        self.on_motion = on_motion
        self.on_availability = on_availability
        self.stop_event = stop_event
        self.onvif_port = onvif_port
        self.poll_interval = max(0.0, float(poll_interval))
        self.off_delay = max(0.0, float(off_delay))
        self.last_motion: bool | None = None
        self._pending_off_at: float | None = None
        self._reported_online: bool | None = None
        self._reset_sub = threading.Event()

    def update_camera(self, camera: Camera) -> None:
        with self._lock:
            ip_changed = self.camera.ip != camera.ip
            self.camera = camera
        if ip_changed:
            LOG.info("[%s] IP changed to %s; resetting ONVIF subscription", camera.id, camera.ip)
            self._reset_sub.set()

    def _current_cam(self) -> Camera:
        with self._lock:
            return self.camera

    def _emit_motion(self, cam_id: str, state: bool) -> None:
        self.last_motion = state
        LOG.info("[%s] Motion -> %s", cam_id, "ON" if state else "OFF")
        try:
            self.on_motion(cam_id, state)
        except Exception as exc:
            LOG.exception("[%s] Motion callback failed: %s", cam_id, exc)

    def _emit_availability(self, cam_id: str, online: bool) -> None:
        if self._reported_online is online:
            return
        self._reported_online = online
        try:
            self.on_availability(cam_id, online)
        except Exception as exc:
            LOG.exception("[%s] Availability callback failed: %s", cam_id, exc)

    def _create_subscription(self, ip: str) -> str:
        event_url = f"http://{ip}:{self.onvif_port}/onvif/event_service"
        root = soap_request(
            event_url,
            "http://www.onvif.org/ver10/events/wsdl/CreatePullPointSubscription",
            "<tev:CreatePullPointSubscription>"
            "<tev:InitialTerminationTime>PT60S</tev:InitialTerminationTime>"
            "</tev:CreatePullPointSubscription>",
            timeout=6.0,
        )
        addr = _find_text_by_localname(root, "Address")
        if not addr:
            raise RuntimeError("Missing SubscriptionReference Address in CreatePullPointSubscriptionResponse")
        return addr

    def _renew(self, sub_addr: str) -> None:
        soap_request(
            sub_addr,
            "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest",
            "<wsnt:Renew><wsnt:TerminationTime>PT60S</wsnt:TerminationTime></wsnt:Renew>",
            to_addr=sub_addr,
            timeout=5.0,
        )

    def _pull_messages(self, sub_addr: str) -> list[bool]:
        root = soap_request(
            sub_addr,
            "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest",
            "<tev:PullMessages><tev:Timeout>PT10S</tev:Timeout><tev:MessageLimit>20</tev:MessageLimit></tev:PullMessages>",
            to_addr=sub_addr,
            timeout=15.0,
        )
        states: list[bool] = []
        for msg in root.findall(f".//{{{WSNT_NS}}}NotificationMessage"):
            topic_el = msg.find(f"{{{WSNT_NS}}}Topic")
            topic = (topic_el.text or "") if topic_el is not None else ""
            if "CellMotionDetector/Motion" not in topic:
                continue
            for item in msg.findall(f".//{{{TT_NS}}}SimpleItem"):
                if item.attrib.get("Name") == "IsMotion":
                    val = item.attrib.get("Value", "").strip().lower()
                    if val in ("true", "1", "yes"):
                        states.append(True)
                    elif val in ("false", "0", "no"):
                        states.append(False)
        return states

    def _process_states(self, cam_id: str, states: list[bool], now: float | None = None) -> None:
        now_ts = time.monotonic() if now is None else now
        for state in states:
            if state:
                self._pending_off_at = None
                if self.last_motion is not True:
                    self._emit_motion(cam_id, True)
            else:
                if self.off_delay > 0.0 and self.last_motion is True:
                    if self._pending_off_at is None:
                        self._pending_off_at = now_ts + self.off_delay
                else:
                    self._pending_off_at = None
                    if self.last_motion is not False:
                        self._emit_motion(cam_id, False)

        if self._pending_off_at is not None and now_ts >= self._pending_off_at:
            self._pending_off_at = None
            if self.last_motion is not False:
                self._emit_motion(cam_id, False)

    def _wait_next_cycle(self, cam_id: str, next_poll_at: float) -> None:
        while not self.stop_event.is_set():
            now = time.monotonic()
            if self._pending_off_at is not None and now >= self._pending_off_at:
                self._pending_off_at = None
                if self.last_motion is not False:
                    self._emit_motion(cam_id, False)

            target = next_poll_at
            if self._pending_off_at is not None:
                target = min(target, self._pending_off_at)

            remaining = target - now
            if remaining <= 0:
                if now >= next_poll_at:
                    return
                continue
            if self.stop_event.wait(remaining):
                return

    def run(self) -> None:
        sub_addr: str | None = None
        last_renew = 0.0
        failures = 0

        while not self.stop_event.is_set():
            cycle_start = time.monotonic()
            cam = self._current_cam()
            if self._reset_sub.is_set():
                self._reset_sub.clear()
                sub_addr = None

            try:
                if not sub_addr:
                    sub_addr = self._create_subscription(cam.ip)
                    last_renew = time.monotonic()
                    LOG.info("[%s] Subscribed ONVIF PullPoint: %s", cam.id, sub_addr)

                if time.monotonic() - last_renew >= 35.0:
                    self._renew(sub_addr)
                    last_renew = time.monotonic()

                states = self._pull_messages(sub_addr)
                if failures > 0 or not cam.online or self._reported_online is not True:
                    self._emit_availability(cam.id, True)
                failures = 0

                self._process_states(cam.id, states)
                self._wait_next_cycle(cam.id, cycle_start + self.poll_interval)

            except Exception as exc:
                failures += 1
                sub_addr = None
                if failures >= 2:
                    self._emit_availability(cam.id, False)
                backoff = min(30.0, 3.0 * failures)
                LOG.warning("[%s] ONVIF PullPoint error (%s), retrying in %.0fs", cam.id, exc, backoff)
                self.stop_event.wait(backoff)
