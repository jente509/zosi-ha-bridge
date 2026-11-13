"""Network scanning, interface lookup, and dynamic IP exclusion helpers."""

from __future__ import annotations

import concurrent.futures
import fcntl
import ipaddress
import logging
import socket
import struct
import time
import urllib.parse
from collections.abc import Iterable

LOG = logging.getLogger("zosi-bridge.network")


def _get_iface_ip(iface: str) -> str | None:
    """Return IPv4 address for a Linux network interface using SIOCGIFADDR."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        ifreq = struct.pack("256s", iface[:15].encode("utf-8"))
        res = fcntl.ioctl(sock.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def find_subnet_interface(subnet: ipaddress.IPv4Network) -> tuple[str, str] | None:
    """Find local (iface, ip) belonging to the given IPv4 subnet."""
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return None

    for _, name in interfaces:
        if name == "lo":
            continue
        ip_str = _get_iface_ip(name)
        if not ip_str:
            continue
        try:
            if ipaddress.IPv4Address(ip_str) in subnet:
                return name, ip_str
        except ValueError:
            continue
    return None


def parse_exclude_ips(raw: str | Iterable[str] | None) -> set[str]:
    """Parse and validate a comma-separated string or iterable of IPv4 addresses."""
    if raw is None:
        return set()
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    result: set[str] = set()
    for item in items:
        candidate = str(item).strip()
        if not candidate:
            continue
        try:
            result.add(str(ipaddress.IPv4Address(candidate)))
        except (ValueError, ipaddress.AddressValueError) as exc:
            raise ValueError(f"Invalid exclude IP address {candidate!r}") from exc
    return result


def resolve_exclude_ips(
    subnet: ipaddress.IPv4Network,
    exclude_ips: Iterable[str] | None = None,
    go2rtc_url: str | None = None,
) -> set[str]:
    """Build dynamic exclusion set from local interface IP, explicit IPs, and go2rtc host."""
    excluded = parse_exclude_ips(exclude_ips)

    iface_info = find_subnet_interface(subnet)
    if iface_info:
        excluded.add(iface_info[1])

    if go2rtc_url:
        parsed = urllib.parse.urlparse(go2rtc_url)
        hostname = parsed.hostname
        if hostname:
            try:
                infos = socket.getaddrinfo(hostname, None, family=socket.AF_INET)
                for info in infos:
                    ip_str = info[4][0]
                    try:
                        if ipaddress.IPv4Address(ip_str) in subnet:
                            excluded.add(ip_str)
                    except ValueError:
                        continue
            except OSError as exc:
                LOG.debug("Could not resolve go2rtc host %r for IP exclusion: %s", hostname, exc)

    return excluded


def arp_scan(
    subnet: ipaddress.IPv4Network,
    passes: int = 2,
    exclude_ips: set[str] | None = None,
) -> dict[str, str]:
    """Scan subnet using raw AF_PACKET ARP requests. Returns {ip: MAC}."""
    af_packet = getattr(socket, "AF_PACKET", None)
    if af_packet is None:
        LOG.debug("AF_PACKET is unavailable on this platform; skipping ARP scan")
        return {}

    iface_info = find_subnet_interface(subnet)
    if not iface_info:
        LOG.debug("No local interface in subnet %s; skipping AF_PACKET ARP scan", subnet)
        return {}

    iface, src_ip_str = iface_info
    skip_ips = set(exclude_ips or set())
    skip_ips.add(src_ip_str)
    found: dict[str, str] = {}

    try:
        sock = socket.socket(af_packet, socket.SOCK_RAW, socket.htons(0x0806))
        sock.bind((iface, 0))
    except OSError as exc:
        LOG.warning("AF_PACKET ARP scan unavailable on %s: %s", iface, exc)
        return {}

    try:
        sock_addr = sock.getsockname()
        src_mac = sock_addr[4] if len(sock_addr) > 4 else b""
        if len(src_mac) != 6:
            LOG.warning("Interface %s has unsupported MAC length (%d)", iface, len(src_mac))
            return {}

        src_ip = socket.inet_aton(src_ip_str)
        hosts = [ip for ip in subnet.hosts() if str(ip) not in skip_ips]

        for _ in range(max(1, passes)):
            for ip in hosts:
                eth = b"\xff\xff\xff\xff\xff\xff" + src_mac + b"\x08\x06"
                arp = struct.pack(
                    "!HHBBH6s4s6s4s",
                    1,
                    0x0800,
                    6,
                    4,
                    1,
                    src_mac,
                    src_ip,
                    b"\x00" * 6,
                    ip.packed,
                )
                try:
                    sock.send(eth + arp)
                except OSError:
                    pass
                time.sleep(0.0008)

            sock.settimeout(0.25)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    pkt = sock.recv(65535)
                except (socket.timeout, TimeoutError):
                    break
                except OSError:
                    break
                if len(pkt) >= 42 and pkt[12:14] == b"\x08\x06":
                    op = struct.unpack("!H", pkt[20:22])[0]
                    if op == 2:
                        smac = ":".join(f"{b:02X}" for b in pkt[22:28])
                        sip = socket.inet_ntoa(pkt[28:32])
                        if sip not in skip_ips and ipaddress.IPv4Address(sip) in subnet:
                            found[sip] = smac
    finally:
        sock.close()

    LOG.info("ARP scan on %s (%s) found %d hosts", iface, src_ip_str, len(found))
    return found


def tcp_port_open(ip: str, port: int, timeout: float = 0.6) -> bool:
    """Check whether TCP port is accepting connections on *ip*."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def tcp_scan_onvif(
    subnet: ipaddress.IPv4Network,
    exclude_ips: set[str],
    port: int = 8080,
    timeout: float = 0.5,
    max_workers: int = 64,
) -> list[str]:
    """Fast concurrent TCP scan on ONVIF port across the subnet."""
    candidates = [str(ip) for ip in subnet.hosts() if str(ip) not in exclude_ips]
    open_ips: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(tcp_port_open, ip, port, timeout): ip for ip in candidates}
        for fut in concurrent.futures.as_completed(future_map):
            try:
                if fut.result():
                    open_ips.append(future_map[fut])
            except Exception:
                continue
    return sorted(open_ips, key=ipaddress.IPv4Address)
