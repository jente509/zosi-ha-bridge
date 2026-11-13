from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zosi_bridge.models import Camera, atomic_write, load_cameras, normalize_mac, save_cameras


class TestNormalizeMac(unittest.TestCase):
    def test_valid_formats(self) -> None:
        self.assertEqual(normalize_mac("aa:bb:cc:dd:ee:ff"), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(normalize_mac("aa-bb-cc-dd-ee-ff"), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(normalize_mac("a:b:c:d:e:f"), "0A:0B:0C:0D:0E:0F")
        self.assertEqual(normalize_mac("aabbccddeeff"), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(normalize_mac("aabb.ccdd.eeff"), "AA:BB:CC:DD:EE:FF")

    def test_invalid_formats(self) -> None:
        self.assertEqual(normalize_mac(""), "")
        self.assertEqual(normalize_mac("ZZ:ZZ:ZZ:ZZ:ZZ:ZZ"), "")
        self.assertEqual(normalize_mac("aa:bb:cc"), "")
        self.assertEqual(normalize_mac("not-a-mac-address"), "")
        self.assertEqual(normalize_mac(None), "")  # type: ignore[arg-type]


class TestAtomicWriteAndPersistence(unittest.TestCase):
    def test_atomic_write_change_detection_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "sub" / "test.yaml"
            self.assertTrue(atomic_write(target, "hello: world\n"))
            self.assertFalse(atomic_write(target, "hello: world\n"))
            self.assertTrue(atomic_write(target, "hello: updated\n"))
            self.assertEqual(target.read_text(encoding="utf-8"), "hello: updated\n")
            mode = stat.S_IMODE(os.stat(target).st_mode)
            self.assertEqual(mode, 0o644)

    def test_atomic_write_thread_safety(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "concurrent.txt"
            errors: list[Exception] = []

            def worker(idx: int) -> None:
                try:
                    for j in range(15):
                        atomic_write(target, f"worker-{idx}-iter-{j}\n")
                except Exception as exc:  # pragma: no cover - failure capture
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            content = target.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("worker-") and content.endswith("\n"))
            leftovers = [p for p in Path(td).iterdir() if ".tmp." in p.name]
            self.assertEqual(leftovers, [])

    def test_save_and_load_cameras_resilient(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cameras.json"
            cams = [
                Camera(id="zosi_2", number=2, name="Cam 2", mac="00:11:22:33:44:02", ip="192.168.138.12", online=True),
                Camera(id="zosi_1", number=1, name="Cam 1", mac="00:11:22:33:44:01", ip="192.168.138.11", online=True),
            ]
            self.assertTrue(save_cameras(path, cams))
            loaded = load_cameras(path)
            self.assertEqual([c.id for c in loaded], ["zosi_1", "zosi_2"])

            # Malformed JSON returns empty list without crashing
            path.write_text("{broken json", encoding="utf-8")
            self.assertEqual(load_cameras(path), [])

            # Partial/bad entries are skipped while valid ones load and duplicate MACs deduplicate
            payload = {
                "cameras": [
                    "not-a-dict",
                    {"id": "bad_mac", "number": 9, "mac": "invalid", "ip": "192.168.138.99"},
                    {"id": "bad_ip", "number": 8, "mac": "00:11:22:33:44:08", "ip": "999.1.1.1"},
                    {"id": "zosi_1", "number": 1, "mac": "00:11:22:33:44:01", "ip": "192.168.138.11"},
                    {"id": "zosi_1_dup", "number": 1, "mac": "00-11-22-33-44-01", "ip": "192.168.138.15"},
                ]
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_cameras(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].ip, "192.168.138.15")


if __name__ == "__main__":
    unittest.main()
