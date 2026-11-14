"""Command-line interface and environment configuration for zosi-bridge."""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import sys
import urllib.request
from pathlib import Path
from typing import Any

from .go2rtc_config import DEFAULT_VAAPI_DEVICE, TRANSCODE_MODES, validate_transcode_mode
from .models import load_cameras
from .mqtt_bridge import ZosiDaemon
from .network import parse_exclude_ips
from .onvif import discover_cameras, probe_camera

LOG = logging.getLogger("zosi-bridge")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer (got {raw!r})") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number (got {raw!r})") from exc


def _validate_port(port: int, name: str, allow_zero: bool = False) -> int:
    low = 0 if allow_zero else 1
    if not (low <= port <= 65535):
        raise ValueError(f"{name} must be between {low} and 65535 (got {port})")
    return port


def _validate_subnet(subnet_str: str) -> str:
    try:
        return str(ipaddress.IPv4Network(subnet_str, strict=False))
    except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as exc:
        raise ValueError(f"Invalid IPv4 subnet {subnet_str!r}: {exc}") from exc


def cmd_discover(args: argparse.Namespace) -> int:
    cams, changed = discover_cameras(
        subnet_str=args.subnet,
        cameras_file=Path(args.cameras_file),
        go2rtc_file=Path(args.go2rtc_config),
        go2rtc_url=args.go2rtc_url if args.reload else None,
        exclude_ips=args.exclude_ip_set,
        onvif_port=args.onvif_port,
        rtsp_port=args.rtsp_port,
        transcode_mode=args.transcode_mode,
        vaapi_device=args.vaapi_device,
    )
    print(f"Discovered {len(cams)} camera(s); go2rtc.yaml updated={changed}")
    for c in cams:
        print(
            f"  {c.id:8s} ip={c.ip:15s} mac={c.mac} fw={c.firmware or '-':20s} "
            f"onvif={'OK' if c.onvif_ok else 'NO'} rtsp={'OK' if c.rtsp_ok else 'NO'} online={c.online}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cams = load_cameras(Path(args.cameras_file))
    if not cams:
        print(f"No cameras recorded in {args.cameras_file}. Run 'discover' first.")
        return 1

    go2rtc_streams: dict[str, Any] = {}
    if args.go2rtc_url:
        try:
            with urllib.request.urlopen(f"{args.go2rtc_url.rstrip('/')}/api/streams", timeout=3) as resp:
                go2rtc_streams = json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass

    print(f"{'ID':8s} {'IP':15s} {'MAC':17s} {'FIRMWARE':20s} {'ONVIF':5s} {'RTSP':5s} {'GO2RTC'}")
    for c in sorted(cams, key=lambda x: x.number):
        if args.live:
            info = probe_camera(c.ip, c.mac, onvif_port=args.onvif_port, rtsp_port=args.rtsp_port)
            onvif_s = "OK" if info and info["onvif_ok"] else "DOWN"
            rtsp_s = "OK" if info and info["rtsp_ok"] else "DOWN"
        else:
            onvif_s = "OK" if c.onvif_ok else "DOWN"
            rtsp_s = "OK" if c.rtsp_ok else "DOWN"
        g_status = "configured" if c.id in go2rtc_streams else ("unknown" if not go2rtc_streams else "missing")
        print(f"{c.id:8s} {c.ip:15s} {c.mac:17s} {(c.firmware or '-'):20s} {onvif_s:5s} {rtsp_s:5s} {g_status}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    daemon = ZosiDaemon(
        subnet=args.subnet,
        cameras_file=Path(args.cameras_file),
        go2rtc_file=Path(args.go2rtc_config),
        go2rtc_url=args.go2rtc_url,
        mqtt_host=args.mqtt_host,
        mqtt_port=args.mqtt_port,
        mqtt_user=args.mqtt_user,
        mqtt_password=args.mqtt_password,
        discover_interval=args.interval,
        exclude_ips=args.exclude_ip_set,
        onvif_port=args.onvif_port,
        rtsp_port=args.rtsp_port,
        motion_poll_interval=args.motion_poll_interval,
        motion_off_delay=args.motion_off_delay,
        transcode_mode=args.transcode_mode,
        vaapi_device=args.vaapi_device,
        snapshot_port=args.snapshot_port,
        snapshot_host=args.snapshot_host,
        snapshot_cache_ttl=args.snapshot_ttl,
        snapshot_idle_refresh_interval=args.snapshot_idle_ttl,
    )

    received_signal: list[int] = []

    def _handle_signal(signum: int, _frame: Any) -> None:
        received_signal.append(signum)
        daemon.stop_event.set()

    prev_term = signal.signal(signal.SIGTERM, _handle_signal)
    prev_int = signal.signal(signal.SIGINT, _handle_signal)
    try:
        rc = daemon.run()
        if received_signal:
            LOG.info("Stopped cleanly on signal %d", received_signal[0])
        return rc
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)


def build_parser() -> argparse.ArgumentParser:
    default_subnet = os.environ.get("SUBNET", "192.168.138.0/24")
    default_cameras_file = os.environ.get("CAMERAS_FILE", "/data/cameras.json")
    default_go2rtc_file = os.environ.get("GO2RTC_CONFIG", "/go2rtc/go2rtc.yaml")
    default_go2rtc_url = os.environ.get("GO2RTC_URL", "http://zosi-go2rtc:1984")
    default_exclude_ips = os.environ.get("EXCLUDE_IPS", "")
    default_transcode_mode = os.environ.get("TRANSCODE_MODE", "vaapi")
    default_vaapi_device = os.environ.get("VAAPI_DEVICE", DEFAULT_VAAPI_DEVICE)
    default_mqtt_host = os.environ.get("MQTT_HOST", "host.docker.internal")
    default_mqtt_port = _env_int("MQTT_PORT", 1883)
    default_mqtt_user = os.environ.get("MQTT_USER", "")
    default_mqtt_password = os.environ.get("MQTT_PASSWORD", "")
    default_interval = _env_int("DISCOVER_INTERVAL", 300)
    default_onvif_port = _env_int("ONVIF_PORT", 8080)
    default_rtsp_port = _env_int("RTSP_PORT", 554)
    default_poll_interval = _env_float("MOTION_POLL_INTERVAL", 1.5)
    default_off_delay = _env_float("MOTION_OFF_DELAY", 0.0)
    default_snapshot_port = _env_int("SNAPSHOT_SERVER_PORT", 1985)
    default_snapshot_host = os.environ.get("SNAPSHOT_SERVER_HOST", "0.0.0.0")
    default_snapshot_ttl = _env_float("SNAPSHOT_CACHE_TTL", 10.0)
    default_snapshot_idle_ttl = _env_float("SNAPSHOT_IDLE_REFRESH_INTERVAL", 300.0)

    p = argparse.ArgumentParser(prog="zosi-bridge", description="Zosi/Ansjer Camera Bridge & Discovery Tool")
    p.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    p_disc = sub.add_parser("discover", help="Discover Zosi cameras and write config files")
    p_disc.add_argument("--subnet", default=default_subnet)
    p_disc.add_argument("--cameras-file", default=default_cameras_file)
    p_disc.add_argument("--go2rtc-config", default=default_go2rtc_file)
    p_disc.add_argument("--go2rtc-url", default=default_go2rtc_url)
    p_disc.add_argument("--exclude-ips", default=default_exclude_ips, help="Comma-separated IPs to skip during scan")
    p_disc.add_argument("--transcode-mode", default=default_transcode_mode, choices=TRANSCODE_MODES)
    p_disc.add_argument("--vaapi-device", default=default_vaapi_device)
    p_disc.add_argument("--onvif-port", type=int, default=default_onvif_port)
    p_disc.add_argument("--rtsp-port", type=int, default=default_rtsp_port)
    p_disc.add_argument("--reload", action="store_true", help="Restart go2rtc if config changed")
    p_disc.set_defaults(func=cmd_discover)

    p_stat = sub.add_parser("status", help="Show camera inventory and status")
    p_stat.add_argument("--cameras-file", default=default_cameras_file)
    p_stat.add_argument("--go2rtc-url", default=default_go2rtc_url)
    p_stat.add_argument("--onvif-port", type=int, default=default_onvif_port)
    p_stat.add_argument("--rtsp-port", type=int, default=default_rtsp_port)
    p_stat.add_argument("--live", action="store_true", help="Actively probe each camera")
    p_stat.set_defaults(func=cmd_status)

    p_dae = sub.add_parser("daemon", help="Run discovery, MQTT HA bridge, snapshot cache, and ONVIF motion loop")
    p_dae.add_argument("--subnet", default=default_subnet)
    p_dae.add_argument("--cameras-file", default=default_cameras_file)
    p_dae.add_argument("--go2rtc-config", default=default_go2rtc_file)
    p_dae.add_argument("--go2rtc-url", default=default_go2rtc_url)
    p_dae.add_argument("--exclude-ips", default=default_exclude_ips, help="Comma-separated IPs to skip during scan")
    p_dae.add_argument("--transcode-mode", default=default_transcode_mode, choices=TRANSCODE_MODES)
    p_dae.add_argument("--vaapi-device", default=default_vaapi_device)
    p_dae.add_argument("--mqtt-host", default=default_mqtt_host)
    p_dae.add_argument("--mqtt-port", type=int, default=default_mqtt_port)
    p_dae.add_argument("--mqtt-user", default=default_mqtt_user)
    p_dae.add_argument("--mqtt-password", default=default_mqtt_password)
    p_dae.add_argument("--interval", type=int, default=default_interval)
    p_dae.add_argument("--onvif-port", type=int, default=default_onvif_port)
    p_dae.add_argument("--rtsp-port", type=int, default=default_rtsp_port)
    p_dae.add_argument("--motion-poll-interval", type=float, default=default_poll_interval)
    p_dae.add_argument("--motion-off-delay", type=float, default=default_off_delay)
    p_dae.add_argument("--snapshot-port", type=int, default=default_snapshot_port, help="0 disables HTTP snapshot server")
    p_dae.add_argument("--snapshot-host", default=default_snapshot_host)
    p_dae.add_argument("--snapshot-ttl", type=float, default=default_snapshot_ttl)
    p_dae.add_argument(
        "--snapshot-idle-ttl",
        type=float,
        default=default_snapshot_idle_ttl,
        help="Background refresh interval (seconds) when idle; 0 disables idle background refreshes",
    )
    p_dae.set_defaults(func=cmd_daemon)

    return p


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    try:
        if hasattr(args, "onvif_port"):
            _validate_port(args.onvif_port, "--onvif-port")
        if hasattr(args, "rtsp_port"):
            _validate_port(args.rtsp_port, "--rtsp-port")
        if hasattr(args, "subnet"):
            args.subnet = _validate_subnet(args.subnet)
        if hasattr(args, "exclude_ips"):
            args.exclude_ip_set = parse_exclude_ips(args.exclude_ips)
        if hasattr(args, "transcode_mode"):
            args.transcode_mode = validate_transcode_mode(args.transcode_mode)
        if args.command == "daemon":
            _validate_port(args.mqtt_port, "--mqtt-port")
            _validate_port(args.snapshot_port, "--snapshot-port", allow_zero=True)
            if args.interval < 5:
                raise ValueError("--interval must be at least 5 seconds")
            if args.motion_poll_interval < 0.1:
                raise ValueError("--motion-poll-interval must be at least 0.1 seconds")
            if args.motion_off_delay < 0.0:
                raise ValueError("--motion-off-delay must be >= 0")
            if args.snapshot_ttl <= 0.0:
                raise ValueError("--snapshot-ttl must be > 0")
            if args.snapshot_idle_ttl < 0.0:
                raise ValueError("--snapshot-idle-ttl must be >= 0")
    except ValueError as exc:
        parser.error(str(exc))


def main(argv: list[str] | None = None) -> int:
    try:
        parser = build_parser()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    args = parser.parse_args(argv)
    _validate_args(parser, args)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    return args.func(args)
