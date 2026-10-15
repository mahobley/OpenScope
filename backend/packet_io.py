"""Ethernet packet I/O using libpcap/Npcap and BSD-licensed dpkt.

Native capture libraries are loaded lazily: controller startup and source tests
do not require capture privileges. Windows uses separately installed Npcap.
"""
from __future__ import annotations

import base64
import ctypes as C
import ctypes.util
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

import dpkt


class CaptureError(OSError):
    """Interface, capture, or transmission failure."""


@dataclass
class Packet:
    ethernet: dpkt.ethernet.Ethernet
    time: float

    @property
    def ip(self):
        value = self.ethernet.data
        return value if isinstance(value, (dpkt.ip.IP, dpkt.ip6.IP6)) else None

    @property
    def udp(self):
        value = self.ip
        # Do not interpret a fragment as a complete UDP datagram.
        if isinstance(value, dpkt.ip.IP) and (value.offset or value.mf):
            return None
        if isinstance(value, dpkt.ip6.IP6) and dpkt.ip.IP_PROTO_FRAGMENT in value.extension_hdrs:
            return None
        udp = value.data if value is not None and isinstance(value.data, dpkt.udp.UDP) else None
        if udp is not None and 8 <= udp.ulen <= len(udp):
            return udp
        return None

    @property
    def arp(self):
        value = self.ethernet.data
        return value if isinstance(value, dpkt.arp.ARP) else None

    @property
    def dns(self):
        udp = self.udp
        if udp is None or not ({udp.sport, udp.dport} & {53, 5353}):
            return None
        try:
            return dpkt.dns.DNS(udp.data)
        except (dpkt.UnpackError, ValueError, IndexError):
            return None


def decode_packet(data: bytes, timestamp: float | None = None) -> Packet | None:
    try:
        packet = Packet(dpkt.ethernet.Ethernet(data), time.time() if timestamp is None else timestamp)
        ip = packet.ip
        if isinstance(ip, dpkt.ip.IP) and (ip.len < ip.hl * 4 or ip.len > len(ip)):
            return None
        if isinstance(ip, dpkt.ip6.IP6) and ip.plen > len(ip) - 40:
            return None
        if packet.udp is not None:
            packet.udp.data = packet.udp.data[:packet.udp.ulen - 8]
        return packet
    except (dpkt.UnpackError, ValueError, IndexError):
        return None


def _mac(value: str) -> bytes:
    address = bytes.fromhex(value.replace(":", "").replace("-", ""))
    if len(address) != 6:
        raise ValueError("Expected a six-byte Ethernet address")
    return address


def ethernet_frame(src: str, dst: str, ethertype: int, payload: bytes) -> bytes:
    return bytes(dpkt.ethernet.Ethernet(src=_mac(src), dst=_mac(dst), type=ethertype, data=payload))


def arp_request(src: str, source_ip: str, target_ip: str) -> bytes:
    arp = dpkt.arp.ARP(op=dpkt.arp.ARP_OP_REQUEST, sha=_mac(src),
                       spa=socket.inet_aton(source_ip), tha=bytes(6), tpa=socket.inet_aton(target_ip))
    return ethernet_frame(src, "ff:ff:ff:ff:ff:ff", dpkt.ethernet.ETH_TYPE_ARP, bytes(arp))


def mdns_query(src: str, source_ip: str) -> bytes:
    dns = dpkt.dns.DNS(id=0, op=0, qd=[
        dpkt.dns.DNS.Q(name=name, type=dpkt.dns.DNS_PTR)
        for name in ("_garmin-ds-sink._udp.local", "_garmin-data-sink._udp.local")
    ])
    payload = bytes(dns)
    udp = dpkt.udp.UDP(sport=5353, dport=5353, ulen=8 + len(payload), data=payload)
    ip = dpkt.ip.IP(src=socket.inet_aton(source_ip), dst=socket.inet_aton("224.0.0.251"),
                    p=socket.IPPROTO_UDP, ttl=255, id=1, data=udp)
    ip.len = len(ip)
    # dpkt computes IPv4 and UDP checksums when serializing the IPv4 packet.
    return ethernet_frame(src, "01:00:5e:00:00:fb", dpkt.ethernet.ETH_TYPE_IP, bytes(ip))


class _Timeval(C.Structure):
    _fields_ = [("seconds", C.c_long), ("microseconds", C.c_long)]


class _Header(C.Structure):
    _fields_ = [("ts", _Timeval), ("caplen", C.c_uint32), ("length", C.c_uint32)]


class _Address(C.Structure):
    pass


_Address._fields_ = [("next", C.POINTER(_Address)), ("addr", C.c_void_p),
                     ("netmask", C.c_void_p), ("broadaddr", C.c_void_p), ("dstaddr", C.c_void_p)]


class _Device(C.Structure):
    pass


_Device._fields_ = [("next", C.POINTER(_Device)), ("name", C.c_char_p),
                    ("description", C.c_char_p), ("addresses", C.POINTER(_Address)), ("flags", C.c_uint)]


class _Program(C.Structure):
    _fields_ = [("length", C.c_uint), ("instructions", C.c_void_p)]


@lru_cache(maxsize=1)
def _pcap():
    candidates = []
    if sys.platform == "win32":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Npcap"
        candidates = [str(system / "wpcap.dll")]
    elif sys.platform == "darwin":
        candidates = ["/usr/lib/libpcap.A.dylib"]
    else:
        bundle = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
        candidates = [str(p) for p in sorted(bundle.glob("libpcap.so*"))]
        found = ctypes.util.find_library("pcap")
        if found:
            candidates.append(found)
    for candidate in candidates:
        try:
            # Npcap dependencies must resolve from its installation directory.
            if sys.platform == "win32":
                with os.add_dll_directory(str(Path(candidate).parent)):
                    lib = C.CDLL(candidate)
            else:
                lib = C.CDLL(candidate)
            break
        except OSError:
            continue
    else:
        prerequisite = "Npcap with WinPcap API compatibility" if sys.platform == "win32" else "libpcap"
        raise CaptureError(f"Packet capture library unavailable; install {prerequisite}")
    signatures = {
        "pcap_findalldevs": ([C.POINTER(C.POINTER(_Device)), C.c_char_p], C.c_int),
        "pcap_freealldevs": ([C.POINTER(_Device)], None),
        "pcap_open_live": ([C.c_char_p, C.c_int, C.c_int, C.c_int, C.c_char_p], C.c_void_p),
        "pcap_close": ([C.c_void_p], None),
        "pcap_datalink": ([C.c_void_p], C.c_int),
        "pcap_setnonblock": ([C.c_void_p, C.c_int, C.c_char_p], C.c_int),
        "pcap_next_ex": ([C.c_void_p, C.POINTER(C.POINTER(_Header)), C.POINTER(C.POINTER(C.c_ubyte))], C.c_int),
        "pcap_geterr": ([C.c_void_p], C.c_char_p),
        "pcap_compile": ([C.c_void_p, C.POINTER(_Program), C.c_char_p, C.c_int, C.c_uint32], C.c_int),
        "pcap_setfilter": ([C.c_void_p, C.POINTER(_Program)], C.c_int),
        "pcap_freecode": ([C.POINTER(_Program)], None),
        "pcap_sendpacket": ([C.c_void_p, C.c_void_p, C.c_int], C.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    if hasattr(lib, "pcap_init"):
        lib.pcap_init.argtypes, lib.pcap_init.restype = [C.c_uint, C.c_char_p], C.c_int
        error = C.create_string_buffer(256)
        if lib.pcap_init(1, error) != 0:  # UTF-8, including Windows adapter descriptions.
            raise CaptureError(error.value.decode(errors="replace"))
    return lib


@dataclass
class Interface:
    name: str
    network_name: str
    description: str
    ips: dict
    flags: str
    mac: str


def _windows_adapters() -> dict:
    script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-NetAdapter | Select-Object Name,InterfaceGuid,InterfaceDescription,MacAddress,Status | ConvertTo-Json -Compress"
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
    rows = json.loads(result.stdout or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    return {str(row["InterfaceGuid"]).strip("{}").lower(): row for row in rows}


def _interface_mac(name: str) -> str:
    if sys.platform == "linux":
        try:
            return (Path("/sys/class/net") / name / "address").read_text().strip()
        except OSError:
            return ""
    if sys.platform == "darwin":
        result = subprocess.run(["/sbin/ifconfig", name], capture_output=True, text=True, timeout=5)
        match = re.search(r"\bether\s+([0-9a-f:]{17})", result.stdout, re.I)
        return match.group(1).lower() if match else ""
    return ""


def _sockaddr_ip(address, system: str):
    if not address:
        return None
    header = C.string_at(address, 2)
    family = header[1] if system == "darwin" else int.from_bytes(header, sys.byteorder)
    if family == socket.AF_INET:
        return 4, socket.inet_ntop(socket.AF_INET, C.string_at(address, 8)[4:8])
    if family == socket.AF_INET6:
        return 6, socket.inet_ntop(socket.AF_INET6, C.string_at(address, 24)[8:24])
    return None


def list_interfaces() -> list[Interface]:
    lib = _pcap()
    devices = C.POINTER(_Device)()
    error = C.create_string_buffer(256)
    if lib.pcap_findalldevs(C.byref(devices), error) != 0:
        raise CaptureError(error.value.decode(errors="replace"))
    interfaces = []
    try:
        windows = _windows_adapters() if sys.platform == "win32" else {}
        cursor = devices
        while cursor:
            device = cursor.contents
            capture_name = device.name.decode(errors="replace")
            description = (device.description or b"").decode(errors="replace")
            name, mac = capture_name, ""
            flags = "UP" if device.flags & 2 else "DOWN"
            if device.flags & 1:
                flags += " LOOPBACK"
            ips = {4: [], 6: []}
            address = device.addresses
            while address:
                value = _sockaddr_ip(address.contents.addr, sys.platform)
                if value:
                    ips[value[0]].append(value[1])
                address = address.contents.next
            if sys.platform == "win32":
                guid = capture_name.rsplit("{", 1)[-1].rstrip("}").lower()
                row = windows.get(guid)
                if row:
                    name = row["Name"]
                    description = row["InterfaceDescription"]
                    mac = str(row.get("MacAddress") or "").replace("-", ":").lower()
                    flags = "UP" if row.get("Status") == "Up" else "DOWN"
            else:
                mac = _interface_mac(name)
            interfaces.append(Interface(name, capture_name, description, ips, flags, mac))
            cursor = device.next
    except (subprocess.SubprocessError, ValueError) as exc:
        raise CaptureError(f"Could not read adapter metadata: {exc}") from exc
    finally:
        lib.pcap_freealldevs(devices)
    return interfaces


def resolve_iface(name: str) -> Interface:
    for interface in list_interfaces():
        if name in (interface.name, interface.network_name):
            return interface
    raise ValueError(f"Network interface not found: {name}")


def get_if_hwaddr(name: str) -> str:
    mac = resolve_iface(name).mac
    if not mac:
        raise CaptureError(f"No Ethernet address for {name}")
    return mac


class CaptureSocket:
    def __init__(self, iface: str, bpf: str = ""):
        self.lib = _pcap()
        self.handle = None
        if sys.platform == "win32" and not iface.startswith("\\Device\\"):
            iface = resolve_iface(iface).network_name
        error = C.create_string_buffer(256)
        self.handle = self.lib.pcap_open_live(iface.encode(), 65535, 1, 50, error)
        if not self.handle:
            raise CaptureError(error.value.decode(errors="replace"))
        try:
            if self.lib.pcap_datalink(self.handle) != 1:
                raise CaptureError(f"{iface} does not provide Ethernet frames")
            if self.lib.pcap_setnonblock(self.handle, 1, error) != 0:
                raise CaptureError(error.value.decode(errors="replace"))
            if bpf:
                program = _Program()
                if self.lib.pcap_compile(self.handle, C.byref(program), bpf.encode(), 1, 0xffffffff) != 0:
                    raise CaptureError(self.error())
                try:
                    if self.lib.pcap_setfilter(self.handle, C.byref(program)) != 0:
                        raise CaptureError(self.error())
                finally:
                    self.lib.pcap_freecode(C.byref(program))
        except BaseException:
            self.close()
            raise

    def error(self):
        return (self.lib.pcap_geterr(self.handle) or b"Packet I/O failed").decode(errors="replace")

    def receive(self):
        header = C.POINTER(_Header)()
        data = C.POINTER(C.c_ubyte)()
        status = self.lib.pcap_next_ex(self.handle, C.byref(header), C.byref(data))
        if status == 0:
            return None
        if status != 1:
            raise CaptureError(self.error())
        h = header.contents
        if h.caplen != h.length:
            return None
        return decode_packet(C.string_at(data, h.caplen), h.ts.seconds + h.ts.microseconds / 1e6)

    def send(self, data: bytes):
        if self.lib.pcap_sendpacket(self.handle, data, len(data)) != 0:
            raise CaptureError(self.error())

    def close(self):
        if self.handle:
            self.lib.pcap_close(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def send_packet(data: bytes, *, iface: str):
    with CaptureSocket(iface) as capture:
        capture.send(data)


def sniff(*, iface, timeout, filter="", count=0, store=True, lfilter=None, prn=None):
    packets = []
    deadline = time.monotonic() + timeout
    seen = 0
    with CaptureSocket(iface, filter) as capture:
        while time.monotonic() < deadline:
            packet = capture.receive()
            if packet is None:
                time.sleep(0.005)
                continue
            if lfilter is not None and not lfilter(packet):
                continue
            seen += 1
            if store:
                packets.append(packet)
            if prn is not None:
                prn(packet)
            if count and seen >= count:
                break
    return packets


class AsyncCapture:
    """Persistent capture whose stop never waits on an idle native read."""
    def __init__(self, *, iface, filter, prn, store=False, started_callback=None):
        if store:
            raise ValueError("AsyncCapture does not retain packets")
        self.iface, self.filter, self.prn = iface, filter, prn
        self.started_callback = started_callback
        self.running = False
        self.exception = None
        self.thread = None
        self._stop = threading.Event()

    def start(self):
        if self.thread is not None:
            raise CaptureError("Capture already started")
        def run():
            try:
                with CaptureSocket(self.iface, self.filter) as capture:
                    self.running = True
                    if self.started_callback:
                        self.started_callback()
                    while not self._stop.is_set():
                        packet = capture.receive()
                        if packet is None:
                            self._stop.wait(0.005)
                        else:
                            self.prn(packet)
            except Exception as exc:
                self.exception = exc
            finally:
                self.running = False
        self.thread = threading.Thread(target=run, name="sonar-capture", daemon=True)
        self.thread.start()

    def stop(self, join=False):
        self._stop.set()
        if join and self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join()
