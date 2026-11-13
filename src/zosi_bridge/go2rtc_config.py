"""go2rtc configuration renderer and API reload helper."""

from __future__ import annotations

import http.client
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import yaml

from .models import Camera

LOG = logging.getLogger("zosi-bridge.go2rtc")

TRANSCODE_MODES = ("vaapi", "libx264", "copy")
DEFAULT_VAAPI_DEVICE = "/dev/dri/renderD128"
DEFAULT_FFMPEG_RTSP_TEMPLATE = (
    "-fflags nobuffer -flags low_delay -probesize 150000 -analyzeduration 100000 "
    "-timeout {timeout} -user_agent go2rtc/ffmpeg -rtsp_flags prefer_tcp -i {input}"
)


def validate_transcode_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in TRANSCODE_MODES:
        raise ValueError(f"Unsupported transcode mode {mode!r}; expected one of {TRANSCODE_MODES}")
    return normalized


def render_go2rtc_yaml(
    cameras: list[Camera],
    transcode_mode: str = "vaapi",
    vaapi_device: str = DEFAULT_VAAPI_DEVICE,
    rtsp_port: int = 554,
    api_listen: str = ":1984",
    rtsp_listen: str = ":8554",
) -> str:
    """Render go2rtc.yaml with configurable transcode mode and low-latency ffmpeg flags."""
    mode = validate_transcode_mode(transcode_mode)
    streams: dict[str, str] = {}

    for cam in sorted(cameras, key=lambda c: c.number):
        hd_name = f"{cam.id}_hd"
        sd_name = f"{cam.id}_sd"
        streams[hd_name] = f"rtsp://{cam.ip}:{rtsp_port}/video1"
        if mode == "vaapi":
            streams[cam.id] = f"ffmpeg:{hd_name}#video=h264#hardware=vaapi#audio=aac#audio=copy"
        elif mode == "libx264":
            streams[cam.id] = f"ffmpeg:{hd_name}#video=h264#audio=aac#audio=copy"
        else:
            streams[cam.id] = f"ffmpeg:{hd_name}#video=copy#audio=aac#audio=copy"
        streams[sd_name] = f"rtsp://{cam.ip}:{rtsp_port}/video2"

    ffmpeg_cfg: dict[str, str] = {
        "rtsp": DEFAULT_FFMPEG_RTSP_TEMPLATE,
    }
    if mode == "vaapi":
        ffmpeg_cfg["global"] = f"-vaapi_device {vaapi_device}"

    cfg: dict[str, Any] = {
        "api": {"listen": api_listen},
        "rtsp": {"listen": rtsp_listen},
        "webrtc": {"listen": ""},
        "ffmpeg": ffmpeg_cfg,
        "streams": streams,
    }
    return yaml.safe_dump(cfg, sort_keys=False)


def reload_go2rtc(
    go2rtc_url: str,
    quiet_if_down: bool = False,
    retries: int = 3,
    retry_delay: float = 2.0,
    stop_event: threading.Event | None = None,
) -> bool:
    """Trigger go2rtc restart via HTTP API with retry backoff."""
    url = f"{go2rtc_url.rstrip('/')}/api/restart"
    attempts = 1 if quiet_if_down else max(1, retries)

    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                LOG.info("Triggered go2rtc restart (%s -> HTTP %s)", url, resp.status)
                return True
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            if quiet_if_down:
                LOG.debug("go2rtc not yet reachable at %s during startup (%s)", url, exc)
                return False
            if attempt < attempts:
                LOG.warning(
                    "go2rtc restart attempt %d/%d failed at %s (%s); retrying in %.1fs",
                    attempt,
                    attempts,
                    url,
                    exc,
                    retry_delay,
                )
                if stop_event is not None:
                    if stop_event.wait(retry_delay):
                        return False
                else:
                    time.sleep(retry_delay)
            else:
                LOG.warning("Could not trigger go2rtc restart at %s after %d attempts: %s", url, attempts, exc)
    return False
