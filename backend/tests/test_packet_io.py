"""Wire-format and native capture lifecycle regression tests; no sonar required."""
import ctypes as C
import socket
import struct
import threading
from types import SimpleNamespace

import dpkt
import pytest

import packet_io as io
from live_viewer.packet_logger import packet_addresses, udp_payload


def udp_frame(payload=b"sonar", *, ipv6=False):
    udp = dpkt.udp.UDP(sport=50010, dport=50011, ulen=8 + len(payload), data=payload)
    if ipv6:
        ip = dpkt.ip6.IP6(src=socket.inet_pton(socket.AF_INET6, "fe80::1"),
                          dst=socket.inet_pton(socket.AF_INET6, "fe80::2"),
                          nxt=17, plen=len(udp), data=udp)
        kind = dpkt.ethernet.ETH_TYPE_IP6
    else:
        ip = dpkt.ip.IP(src=socket.inet_aton("172.16.3.0"), dst=socket.inet_aton("172.16.6.0"), p=17, data=udp)
        ip.len = len(ip)
        kind = dpkt.ethernet.ETH_TYPE_IP
    return io.ethernet_frame("04:25:e8:68:ef:58", "04:23:22:9c:0a:78", kind, bytes(ip))


def test_presence_and_arp_match_existing_wire_format():
    payload = bytes.fromhex("040000054f400600") + bytes(38)
    frame = io.ethernet_frame("04:23:22:9c:0a:78", "ff:ff:ff:ff:ff:ff", 0x2003, payload)
    assert frame == bytes.fromhex("ffffffffffff0423229c0a782003") + payload
    arp = io.arp_request("04:23:22:9c:0a:78", "172.16.6.0", "172.16.3.0")
    assert arp.hex() == (
        "ffffffffffff0423229c0a7808060001080006040001"
        "0423229c0a78ac100600000000000000ac100300"
    )
    packet = io.decode_packet(arp)
    assert packet.arp.spa == socket.inet_aton("172.16.6.0")
    assert packet.udp is None


@pytest.mark.parametrize("ipv6", [False, True])
def test_sonar_datagram_payload_addresses_and_capture_timestamp(ipv6):
    packet = io.decode_packet(udp_frame(ipv6=ipv6), 1700000001.25)
    assert packet.time == 1700000001.25
    assert packet_addresses(packet) == (("fe80::1", "fe80::2") if ipv6 else ("172.16.3.0", "172.16.6.0"))
    assert udp_payload(packet) == b"sonar"
    assert (packet.udp.sport, packet.udp.dport) == (50010, 50011)


def test_mdns_query_has_expected_questions_and_valid_checksums():
    frame = io.mdns_query("04:23:22:9c:0a:78", "172.16.6.0")
    packet = io.decode_packet(frame)
    assert packet.ethernet.dst == bytes.fromhex("01005e0000fb")
    assert packet.ip.ttl == 255
    assert packet.udp.sport == packet.udp.dport == 5353
    assert packet.dns.id == 0 and packet.dns.op == 0
    assert [(q.name, q.type, q.cls) for q in packet.dns.qd] == [
        ("_garmin-ds-sink._udp.local", 12, 1),
        ("_garmin-data-sink._udp.local", 12, 1),
    ]
    assert dpkt.in_cksum(frame[14:34]) == 0
    pseudo = struct.pack("!4s4sBBH", packet.ip.src, packet.ip.dst, 0, 17, packet.udp.ulen)
    assert dpkt.in_cksum(pseudo + frame[34:]) == 0


def test_mdns_srv_response_and_unrelated_udp_are_distinguished():
    dns = dpkt.dns.DNS(id=0, qr=1, an=[dpkt.dns.DNS.RR(
        name="GLS10._garmin-ds-sink._udp.local", type=33, cls=1, ttl=120,
        priority=0, weight=0, port=50012, srvname="GLS10.local",
    )])
    frame = bytearray(udp_frame(bytes(dns)))
    # Set DNS port and zero the now-outdated UDP checksum.
    struct.pack_into("!H", frame, 34, 5353)
    struct.pack_into("!H", frame, 40, 0)
    packet = io.decode_packet(bytes(frame))
    assert packet.dns.an[0].port == 50012
    assert "_garmin-ds-sink._udp" in packet.dns.an[0].name
    assert io.decode_packet(udp_frame()).dns is None


def test_short_truncated_and_fragmented_packets_are_not_delivered_as_udp():
    assert io.decode_packet(b"\0" * 10) is None
    assert io.decode_packet(udp_frame()[:-1]) is None
    frame = bytearray(udp_frame())
    struct.pack_into("!H", frame, 20, 0x2000)  # IPv4 more-fragments bit.
    assert udp_payload(io.decode_packet(bytes(frame))) is None
    struct.pack_into("!H", frame, 20, 0)
    struct.pack_into("!H", frame, 38, 60000)  # Invalid UDP declared length.
    assert udp_payload(io.decode_packet(bytes(frame))) is None


@pytest.mark.parametrize("system,family_header", [("linux", b"\x02\x00"), ("win32", b"\x02\x00"), ("darwin", b"\x10\x02")])
def test_platform_sockaddr_ipv4_layout(system, family_header):
    address = C.create_string_buffer(family_header + b"\0\0" + socket.inet_aton("172.16.6.0") + bytes(8))
    assert io._sockaddr_ip(C.addressof(address), system) == (4, "172.16.6.0")


def test_windows_capture_identifier_maps_to_friendly_adapter(monkeypatch):
    name = r"\Device\NPF_{12345678-1234-1234-1234-123456789ABC}"
    device = io._Device(name=name.encode(), description=b"capture device", flags=2)
    freed = []
    def find(pointer, _error):
        C.cast(pointer, C.POINTER(C.POINTER(io._Device)))[0] = C.pointer(device)
        return 0
    monkeypatch.setattr(io, "_pcap", lambda: SimpleNamespace(pcap_findalldevs=find, pcap_freealldevs=freed.append))
    monkeypatch.setattr(io.sys, "platform", "win32")
    monkeypatch.setattr(io, "_windows_adapters", lambda: {
        "12345678-1234-1234-1234-123456789abc": {
            "Name": "Ethernet 2", "InterfaceDescription": "USB Ethernet", "Status": "Up", "MacAddress": "04-23-22-9C-0A-78",
        },
    })
    iface, = io.list_interfaces()
    assert iface.name == "Ethernet 2" and iface.network_name == name
    assert iface.mac == "04:23:22:9c:0a:78" and iface.flags == "UP"
    assert len(freed) == 1


def test_native_setup_failure_closes_handle(monkeypatch):
    closed = []
    fake = SimpleNamespace(pcap_open_live=lambda *_: 123, pcap_datalink=lambda *_: 113, pcap_close=closed.append)
    monkeypatch.setattr(io, "_pcap", lambda: fake)
    with pytest.raises(io.CaptureError, match="Ethernet"):
        io.CaptureSocket(r"\Device\NPF_test")
    assert closed == [123]


def test_idle_capture_stops_and_releases_native_handle(monkeypatch):
    ready, closed = threading.Event(), threading.Event()
    class Idle:
        def __init__(self, *_): pass
        def __enter__(self): return self
        def __exit__(self, *_): closed.set()
        def receive(self): return None
    monkeypatch.setattr(io, "CaptureSocket", Idle)
    capture = io.AsyncCapture(iface="fake", filter="udp", prn=lambda _: pytest.fail("idle capture delivered a packet"), started_callback=ready.set)
    capture.start()
    assert ready.wait(2)
    capture.stop()
    capture.thread.join(2)
    assert closed.is_set() and not capture.thread.is_alive()
    assert not capture.running and capture.exception is None


def test_callback_failure_is_propagated_and_closes_capture(monkeypatch):
    closed = threading.Event()
    class Source:
        def __init__(self, *_): pass
        def __enter__(self): return self
        def __exit__(self, *_): closed.set()
        def receive(self): return object()
    monkeypatch.setattr(io, "CaptureSocket", Source)
    def fail(_): raise RuntimeError("recording failed")
    capture = io.AsyncCapture(iface="fake", filter="udp", prn=fail)
    capture.start()
    capture.thread.join(2)
    assert str(capture.exception) == "recording failed"
    assert closed.is_set() and not capture.running


def test_cancel_during_native_open_cannot_leave_a_capture_running(monkeypatch):
    from live_viewer.pipeline import CaptureLoop
    opening, finish_open, closed = threading.Event(), threading.Event(), threading.Event()
    class SlowOpen:
        def __init__(self, *_):
            opening.set()
            assert finish_open.wait(3)
        def __enter__(self): return self
        def __exit__(self, *_): closed.set()
        def receive(self): pytest.fail("cancelled capture received traffic")
    monkeypatch.setattr(io, "CaptureSocket", SlowOpen)
    capture = CaptureLoop(lambda _: pytest.fail("cancelled callback"), iface="fake", bpf="udp")
    capture._sniffer.start()
    assert opening.wait(2)
    capture.close()
    finish_open.set()
    capture._sniffer.thread.join(2)
    assert closed.is_set() and not capture._sniffer.thread.is_alive()


def test_packet_transmission_failure_is_reported_and_handle_closed(monkeypatch):
    closed, sent = [], []
    def send(handle, data, size):
        sent.append((handle, data, size))
        return -1
    fake = SimpleNamespace(pcap_open_live=lambda *_: 123, pcap_datalink=lambda *_: 1,
                           pcap_setnonblock=lambda *_: 0, pcap_sendpacket=send,
                           pcap_geterr=lambda _: b"adapter disconnected", pcap_close=closed.append)
    monkeypatch.setattr(io, "_pcap", lambda: fake)
    with pytest.raises(io.CaptureError, match="adapter disconnected"):
        io.send_packet(b"\x00\x01\x00", iface=r"\Device\NPF_test")
    assert sent == [(123, b"\x00\x01\x00", 3)]
    assert closed == [123]


def test_native_libpcap_reads_saved_ethernet_frame(tmp_path):
    """Exercise the real ctypes header/data ABI without capture privileges."""
    try:
        lib = io._pcap()
    except io.CaptureError as exc:
        pytest.skip(str(exc))
    path = tmp_path / "fixture.pcap"
    with path.open("wb") as stream:
        writer = dpkt.pcap.Writer(stream)
        writer.writepkt(udp_frame(), ts=1700000001.25)
    lib.pcap_open_offline.argtypes = [C.c_char_p, C.c_char_p]
    lib.pcap_open_offline.restype = C.c_void_p
    error = C.create_string_buffer(256)
    capture = io.CaptureSocket.__new__(io.CaptureSocket)
    capture.lib, capture.handle = lib, lib.pcap_open_offline(str(path).encode(), error)
    assert capture.handle, error.value
    with capture:
        packet = capture.receive()
        assert packet.time == 1700000001.25
        assert udp_payload(packet) == b"sonar"
