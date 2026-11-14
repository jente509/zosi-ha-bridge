from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zosi_bridge.cli import main
from zosi_bridge.models import Camera, save_cameras


class TestCli(unittest.TestCase):
    def test_validation_rejects_invalid_args_and_env(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            main(["discover", "--subnet", "not-a-subnet"])
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx:
            main(["daemon", "--motion-poll-interval", "0.01"])
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx:
            main(["discover", "--exclude-ips", "192.168.138.999"])
        self.assertEqual(ctx.exception.code, 2)

        with mock.patch.dict(os.environ, {"MQTT_PORT": "badint"}):
            self.assertEqual(main(["status"]), 2)

        with self.assertRaises(SystemExit) as ctx:
            main(["daemon", "--snapshot-idle-ttl", "-1"])
        self.assertEqual(ctx.exception.code, 2)

    def test_status_and_discover_commands(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cams_path = Path(td) / "cameras.json"
            go2rtc_path = Path(td) / "go2rtc.yaml"
            cam = Camera(
                id="zosi_1",
                number=1,
                name="Zosi Camera 1",
                mac="00:11:22:33:44:01",
                ip="192.168.138.11",
                onvif_ok=True,
                rtsp_ok=True,
                online=True,
            )
            save_cameras(cams_path, [cam])

            with mock.patch("zosi_bridge.cli.discover_cameras", return_value=([cam], True)) as m_disc:
                rc = main([
                    "discover",
                    "--cameras-file",
                    str(cams_path),
                    "--go2rtc-config",
                    str(go2rtc_path),
                    "--transcode-mode",
                    "copy",
                    "--exclude-ips",
                    "192.168.138.250",
                ])
            self.assertEqual(rc, 0)
            m_disc.assert_called_once()

            rc_status = main(["status", "--cameras-file", str(cams_path), "--go2rtc-url", ""])
            self.assertEqual(rc_status, 0)


if __name__ == "__main__":
    unittest.main()
