from __future__ import annotations

import ipaddress
import socket
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zosi_bridge.network import (
    arp_scan,
    parse_exclude_ips,
    resolve_exclude_ips,
    tcp_port_open,
    tcp_scan_onvif,
)


class TestNetwork(unittest.TestCase):
    def test_parse_exclude_ips(self) -> None:
        self.assertEqual(parse_exclude_ips(None), set())
        self.assertEqual(parse_exclude_ips("192.168.138.250, 192.168.138.251 ,"), {"192.168.138.250", "192.168.138.251"})
        with self.assertRaises(ValueError):
            parse_exclude_ips("192.168.138.250,not_an_ip")

    def test_resolve_exclude_ips_dynamic(self) -> None:
        subnet = ipaddress.IPv4Network("192.168.138.0/24")

        def fake_getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
            if host == "zosi-go2rtc":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.138.250", 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.5", 0)),
                ]
            raise socket.gaierror("unknown host")

        with (
            mock.patch("zosi_bridge.network.find_subnet_interface", return_value=("eth0", "192.168.138.251")),
            mock.patch("zosi_bridge.network.socket.getaddrinfo", side_effect=fake_getaddrinfo),
        ):
            excluded = resolve_exclude_ips(
                subnet,
                exclude_ips=["192.168.138.1"],
                go2rtc_url="http://zosi-go2rtc:1984",
            )

        self.assertEqual(excluded, {"192.168.138.251", "192.168.138.250", "192.168.138.1"})
        self.assertNotIn("172.18.0.5", excluded)

    def test_tcp_port_open_and_scan(self) -> None:
        subnet = ipaddress.IPv4Network("192.168.138.0/29")  # hosts .1 .. .6

        def fake_tcp(ip: str, port: int, timeout: float = 0.5) -> bool:
            return ip in {"192.168.138.2", "192.168.138.5"} and port == 8080

        with mock.patch("zosi_bridge.network.tcp_port_open", side_effect=fake_tcp):
            found = tcp_scan_onvif(subnet, exclude_ips={"192.168.138.2"}, port=8080)
        self.assertEqual(found, ["192.168.138.5"])

        with mock.patch("zosi_bridge.network.socket.create_connection", side_effect=OSError("refused")):
            self.assertFalse(tcp_port_open("192.168.138.99", 8080, timeout=0.1))

    def test_arp_scan_parses_reply(self) -> None:
        subnet = ipaddress.IPv4Network("192.168.138.0/30")  # hosts .1, .2
        cam_mac = bytes.fromhex("001122334455")
        cam_ip = socket.inet_aton("192.168.138.2")
        local_mac = bytes.fromhex("aabbccddeeff")
        local_ip = socket.inet_aton("192.168.138.1")

        arp_reply = (
            local_mac
            + cam_mac
            + b"\x08\x06"
            + struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, 2, cam_mac, cam_ip, local_mac, local_ip)
        )

        fake_sock = mock.MagicMock()
        fake_sock.getsockname.return_value = ("eth0", 0x0806, 0, 1, local_mac)
        fake_sock.recv.side_effect = [arp_reply, TimeoutError("done")]

        with (
            mock.patch("zosi_bridge.network.find_subnet_interface", return_value=("eth0", "192.168.138.1")),
            mock.patch("zosi_bridge.network.socket.socket", return_value=fake_sock),
            mock.patch("zosi_bridge.network.time.sleep"),
        ):
            result = arp_scan(subnet, passes=1)

        self.assertEqual(result, {"192.168.138.2": "00:11:22:33:44:55"})
        fake_sock.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
