from __future__ import annotations

import sys
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zosi_bridge.go2rtc_config import reload_go2rtc, render_go2rtc_yaml
from zosi_bridge.models import Camera


class TestGo2rtcConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.cameras = [
            Camera(id="zosi_1", number=1, name="Cam 1", mac="00:11:22:33:44:01", ip="192.168.138.11"),
        ]

    def test_render_vaapi_mode(self) -> None:
        rendered = render_go2rtc_yaml(self.cameras, transcode_mode="vaapi", vaapi_device="/dev/dri/renderD128")
        parsed = yaml.safe_load(rendered)
        self.assertEqual(parsed["ffmpeg"]["global"], "-vaapi_device /dev/dri/renderD128")
        self.assertIn("-fflags nobuffer -flags low_delay -probesize 150000 -analyzeduration 100000", parsed["ffmpeg"]["rtsp"])
        self.assertEqual(parsed["streams"]["zosi_1_hd"], "rtsp://192.168.138.11:554/video1")
        self.assertEqual(parsed["streams"]["zosi_1"], "ffmpeg:zosi_1_hd#video=h264#hardware=vaapi#audio=aac#audio=copy")
        self.assertEqual(parsed["streams"]["zosi_1_sd"], "rtsp://192.168.138.11:554/video2")

    def test_render_libx264_and_copy_modes(self) -> None:
        libx = yaml.safe_load(render_go2rtc_yaml(self.cameras, transcode_mode="libx264"))
        self.assertNotIn("global", libx["ffmpeg"])
        self.assertEqual(libx["streams"]["zosi_1"], "ffmpeg:zosi_1_hd#video=h264#audio=aac#audio=copy")

        copy_cfg = yaml.safe_load(render_go2rtc_yaml(self.cameras, transcode_mode="copy"))
        self.assertEqual(copy_cfg["streams"]["zosi_1"], "ffmpeg:zosi_1_hd#video=copy#audio=aac#audio=copy")

        with self.assertRaises(ValueError):
            render_go2rtc_yaml(self.cameras, transcode_mode="nvenc")

    def test_reload_go2rtc_retries_and_stop_event(self) -> None:
        ok_resp = mock.MagicMock()
        ok_resp.status = 200
        ok_resp.__enter__.return_value = ok_resp
        ok_resp.__exit__.return_value = False

        with (
            mock.patch(
                "zosi_bridge.go2rtc_config.urllib.request.urlopen",
                side_effect=[urllib.error.URLError("booting"), urllib.error.URLError("booting"), ok_resp],
            ) as m_open,
            mock.patch("zosi_bridge.go2rtc_config.time.sleep"),
        ):
            self.assertTrue(reload_go2rtc("http://zosi-go2rtc:1984", retries=3, retry_delay=0.1))
            self.assertEqual(m_open.call_count, 3)

        stop = threading.Event()
        stop.set()
        with mock.patch(
            "zosi_bridge.go2rtc_config.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ):
            self.assertFalse(reload_go2rtc("http://zosi-go2rtc:1984", retries=3, retry_delay=10.0, stop_event=stop))


if __name__ == "__main__":
    unittest.main()
