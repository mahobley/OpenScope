#!/usr/bin/env python3
"""Sonar head-unit emulator for a directly connected GLS10.

Observed physical identities:
  GLS10:     172.16.3.0 / 04:25:e8:68:ef:58
  Head unit: 172.16.6.0 / 04:23:22:9c:0a:78 (emulated by this host)

Unlike v2.2, this program is the active client in the captured negotiation.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field, replace
import hashlib
import logging
import os
import platform
from pathlib import Path
import queue
import re
import secrets
import select
import signal
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from packet_io import (
    CaptureError, arp_request, ethernet_frame, get_if_hwaddr, list_interfaces,
    mdns_query, resolve_iface, send_packet, sniff,
)

from live_viewer.control import (
    DISABLE_SONAR_PACKET,
    ENABLE_SONAR_PACKET,
    RANGE_VALUE_BYTES,
    build_range_packet,
)
from live_viewer.packets import SonarFrameAssembler, jpeg_dimensions
from packaging_runtime import FROZEN, bundle_executable, user_data_path
from network_recovery import create_network_recovery

try:
    from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf
except ImportError:
    Zeroconf = None
    ServiceListener = object

HEAD_IP = "172.16.6.0"
GLS_IP = "172.16.3.0"
GLS_MAC = "04:25:e8:68:ef:58"  # legacy static-ARP fallback; default learns it
LOG = logging.getLogger("head-unit-v3")
VIEWER_SCRIPT = Path(__file__).resolve().parent / "live_viewer" / "live_viewer.py"
ACTIVE_PROTOCOL_EXPERIMENT: str | None = None
MAXIMAL_PORTABLE_PROFILE = False
PORTABLE_V2_PROFILE = False
CONSOLIDATED_TEST_PROFILE = False
NETWORK_RECOVERY = None


def multicast_ttl() -> int:
    """Return the qualified direct-link multicast TTL."""
    return 1


def experiment_is(name: str) -> bool:
    maximal_changes = {
        "time-01-50900-delay-zero", "time-02-50224-delay-zero",
        "time-03-50227-delay-zero", "time-04-identity-delay-zero",
        "time-05-active-delay-zero", "time-06-transition-delay-zero",
        "order-01-identity-single", "ctrl-02-no-50227-heartbeat",
        "sink-01-ephemeral-source", "sink-03-freeze-counter",
        "sink-04-maintenance-1s", "sink-05-no-type-01",
        "sink-05-no-type-02", "sink-05-no-type-04",
        "sink-05-no-type-05", "sink-05-no-type-06",
        "sink-05-no-type-0b", "token-01-port-50900",
        "token-01-port-50224", "token-01-port-50227",
    }
    return (
        ACTIVE_PROTOCOL_EXPERIMENT == name
        or (MAXIMAL_PORTABLE_PROFILE and name in maximal_changes)
        or (
            (PORTABLE_V2_PROFILE or CONSOLIDATED_TEST_PROFILE)
            and name in maximal_changes | {
                "id-07b-software-string", "id-07c-ilitek-string",
                "id-07d-map-string", "id-07e-versmap-string",
                "sink-02-initial-counter", "sink-06-reverse-order",
                "sink-07-counter-suffix", "sink-08-active-payload",
            }
            | (
                {"token-02-port-50900", "token-02-port-50224"}
                if PORTABLE_V2_PROFILE else set()
            )
        )
        or (
            CONSOLIDATED_TEST_PROFILE
            and name in {
                "pres-04-periodic-only", "pres-04-periodic-30s",
                "token-01-remove-50900", "token-01-remove-50224",
                "token-01-remove-50227",
            }
        )
    )


@dataclass
class EmulatorRuntime:
    """Mutable synchronization state owned by one emulator run."""

    stop: threading.Event = field(default_factory=threading.Event)
    gls_seen: threading.Event = field(default_factory=threading.Event)
    neighbor_ready: threading.Event = field(default_factory=threading.Event)
    tcp_50224_ready: threading.Event = field(default_factory=threading.Event)
    tcp_50900_ready: threading.Event = field(default_factory=threading.Event)
    tcp_50227_ready: threading.Event = field(default_factory=threading.Event)
    data_sink_active: threading.Event = field(default_factory=threading.Event)
    head_identity_ready: threading.Event = field(default_factory=threading.Event)
    data_sink_maintenance: threading.Event = field(default_factory=threading.Event)
    stream_transition_sent: threading.Event = field(default_factory=threading.Event)
    range_commands: queue.Queue[
        tuple[str | int, bytes, threading.Event]
    ] = field(default_factory=queue.Queue)
    sonar_state_commands: queue.Queue[
        tuple[bool, threading.Event]
    ] = field(default_factory=queue.Queue)
    sonar_commands: queue.Queue[
        tuple[bool, bytes, threading.Event]
    ] = field(default_factory=queue.Queue)
    sonar_requests: queue.Queue[bool] = field(default_factory=queue.Queue)


HEAD_50220 = bytes.fromhex(
    "d1070000100000000501040daa effa910d12d42219052101".replace(" ", "")
)
HEAD_50030 = bytes.fromhex(
    "2c0a000072000000ffffffffffff" +
    ("148f21304377" * 16) +
    "474d4e310053595300323000"
)
HEAD_50050 = bytes.fromhex(
    "8e030000590000000200541109000601100000004e0001001900000054000100"
    "1a0001005000010051000100590001005e000000030001001600010008000300"
    "5700010043000100170000005f0000005a000000505bb70b0204030000000a0000"
)
HEAD_50050_ACTIVE = bytes.fromhex(
    "8e030000690000000200541100000601144150204e0001001900000054000100"
    "1a0001005000010051000100590001005e000000030001001600010008000300"
    "5700010043000100170000005f0000005a000000440001005b00010045000100"
    "15000000505bb70b0204c60100000a0000"
)
HEAD_50050_STATUS = bytes.fromhex(
    "8e0300001e000000028d8e0b0000030102000000170000004f00010090e1ba01"
    "0104c6010000"
)
HEAD_STREAM_TRANSITION = bytes.fromhex("7408000000000000")
HEAD_51000_PRESENCE = bytes.fromhex(
    "e70800000b0000000301030d98bb82f5051101"
)
HEAD_51400_SERVICE = bytes.fromhex(
    "050a00001800000003090211051f1110fa41d0a59e1753fab0a329acd7d7ce21"
)
HEAD_51950_SHORT_PREFIX = bytes.fromhex(
    "481900001200000003030101030f08ff0f00aab7a81c0112"
)
HEAD_51950_LONG_PREFIX = bytes.fromhex(
    "471900001c00000006030101030f08ff0f00aab7a81c0111001c02509b0122904e2a"
)
HEAD_50615_BURST = (
    bytes.fromhex("fd090000080000000200010003000000"),
    bytes.fromhex("fd090000080000000100000008000000"),
    bytes.fromhex("fd09000008000000040000000a000000"),
)
HEAD_8322_CAPABILITIES = bytes.fromhex(
    "000000000100000a0102b7c504010109011711100eb1dcff576c5386a2de708e"
    "3e1ccc801fdb011f03010109041104030104090a110a03010509031103030109"
    "0964110103010c0901110103010d0900110003010e0909110703010f09091108"
    "0301100901110103011109011100030113090511050301140902110103011509"
    "001100030117090111010301180900110003011c0903110203011d0902110203"
    "011e0902110103011f0900110003012009021101030121090111010301220901"
    "1100030124090311000301270900110003012809031101030129090511020301"
    "2a0902110103012b0901110003012c0901110003012d0901110103012e0a8020"
    "1100"
)
HEAD_DISCOVERY_124 = bytes.fromhex(
    "92030000740000000200000001005411"
    "4543484f4d415020556c747261322031"
    "30367376000001001900000054000100"
    "43484f4d415020556c74726132203130"
    "36737600030001001600010008000300"
    "0e000910000053797374656d20436f64"
    "65000000505bb70b0204030000000a00"
    "0000000000000000505bb70b"
)
HEAD_DISCOVERY_179 = bytes.fromhex(
    "9b030000ab000000505bb70b541109109c0000004543484f4d415020556c747261"
    "3220313036737620536f6674776172652056657273696f6e2034312e30350a696c"
    "6974656b5f74733a207665722d696c6974656b206669726d776172652076657273"
    "696f6e20697320372e302e302e332e302e302e302e300a564552424d4150204255"
    "533030314c202d20552e532e202620436f617374616c2043616e616461200a5645"
    "52534d4150204e6f6e6500010107"
)
HEAD_DISCOVERY_12 = bytes.fromhex("9703000004000000aab73ed2")

CAPTURED_MODEL_NAME = b"ECHOMAP Ultra2 106sv"
CAPTURED_SECONDARY_MODEL_NAME = b"CHOMAP Ultra2 106sv"
ID_07A_MODEL_NAMES = {
    "id-07a-neutral": "TESTHEAD MODEL 00000",
    "id-07a-ultra2-126sv": "ECHOMAP Ultra2 126sv",
    "id-07a-ultra2-166sv": "ECHOMAP Ultra2 166sv",
    "id-07a-uhd2-92sv": "ECHOMAP\N{TRADE MARK SIGN} UHD2 92sv",
    "id-07a-uhd2-74sv": "ECHOMAP\N{TRADE MARK SIGN} UHD2 74sv",
    "id-07a-uhd2-64sv": "ECHOMAP\N{TRADE MARK SIGN} UHD2 64sv",
    "id-07a-uhd2-54cv": "ECHOMAP\N{TRADE MARK SIGN} UHD2 54cv",
}
ID_07A_LENGTH_NAMES = {
    "id-07a-length-0": "",
    "id-07a-length-1": "X",
    "id-07a-length-8": "MODEL008",
    "id-07a-length-30": "GENERIC HEAD UNIT MODEL 000000",
    "id-07a-length-40": "GENERIC HEAD UNIT MODEL STRING 000000000",
}
EXTENDED_MUTATION_EXPERIMENTS = (
    "time-01-50900-delay-zero", "time-02-50224-delay-zero",
    "time-03-50227-delay-zero", "time-04-identity-delay-zero",
    "time-05-active-delay-zero", "time-06-transition-delay-zero",
    "order-01-identity-single", "ctrl-01-no-50224-repeats",
    "ctrl-02-no-50227-heartbeat", "sink-01-ephemeral-source",
    "sink-03-freeze-counter", "sink-04-maintenance-1s",
    "sink-05-no-type-01", "sink-05-no-type-02", "sink-05-no-type-04",
    "sink-05-no-type-05", "sink-05-no-type-06", "sink-05-no-type-0b",
    "token-01-port-50900", "token-01-port-50224", "token-01-port-50227",
)

REMAINING_OVERNIGHT_EXPERIMENTS = (
    "id-07b-software-string", "id-07c-ilitek-string",
    "id-07d-map-string", "id-07e-versmap-string",
    "sink-02-initial-counter", "sink-06-reverse-order",
    "sink-07-counter-suffix", "sink-08-active-payload",
    "token-02-port-50900", "token-02-port-50224",
    "token-02-port-50227",
)

NEXT_SEVEN_EXPERIMENTS = (
    "pres-04-startup-only", "pres-04-periodic-only",
    "pres-04-one-startup", "pres-04-periodic-30s",
    "ctrl-03-command-first", "ctrl-03-state-delay-500ms",
    "sink-init-no-type-01", "sink-init-no-type-02",
    "sink-init-no-type-04", "sink-init-no-type-05",
    "sink-init-no-type-06", "sink-init-no-type-0b",
    "sink-init-none",
    "token-01-zero-50900", "token-01-zero-50224",
    "token-01-zero-50227", "token-02-zero-50900",
    "token-02-zero-50224", "token-02-zero-50227",
)

IDENTITY_MINIMIZATION_EXPERIMENTS = (
    "id-10-no-mrn-snr-advertisement",
    "id-07b-remove-software", "id-07c-remove-ilitek",
    "id-07d-remove-map", "id-07e-remove-versmap",
    "id-07f-neutral-system-code", "id-11-zero-505bb70b",
    "id-07f-zero-system-code-and-505bb70b",
    "id-12-zero-discovery-12-tail",
    *(f"token-01-remove-{port}" for port in (50900, 50224, 50227)),
    *(f"token-02-remove-{port}" for port in (50900, 50224, 50227)),
)

# These candidates are deliberately permitted as one-at-a-time overrides of the
# consolidated profile.  They are used only by the follow-up qualification
# suite; the normal consolidated profile remains conservative.
CONSOLIDATED_MINIMIZATION_FOLLOWUP_EXPERIMENTS = frozenset((
    "id-07f-neutral-system-code",
    "id-11-zero-505bb70b",
    "id-07f-zero-system-code-and-505bb70b",
    "token-02-remove-50224",
    "token-02-remove-50227",
))


def discovery_records_for_remaining_string(experiment: str) -> tuple[bytes, bytes, bytes]:
    """Apply one length-preserving mutation to the extended identity record."""
    replacements = {
        "id-07b-software-string": (b"Software Version 41.05", b"Software Version 99.99"),
        "id-07c-ilitek-string": (b"firmware version is 7.0.0.3.0.0.0.0", b"firmware version is 8.0.0.3.0.0.0.0"),
        "id-07d-map-string": (b"VERBMAP BUS001L", b"TESTMAP BUS001L"),
        "id-07e-versmap-string": (b"VERSMAP None", b"VERSMAP Test"),
    }
    old, new = replacements[experiment]
    record = HEAD_DISCOVERY_179.replace(old, new)
    if old in record or len(record) != len(HEAD_DISCOVERY_179):
        raise ValueError(f"failed length-preserving identity mutation: {experiment}")
    return HEAD_DISCOVERY_124, record, HEAD_DISCOVERY_12


def randomize_extended_identity_strings(
    records: tuple[bytes, bytes, bytes]
) -> tuple[bytes, bytes, bytes]:
    """Randomize tested same-length identity fields without changing framing."""
    record_124, record_179, record_12 = records
    digits = lambda count: "".join(secrets.choice(string.digits) for _ in range(count))
    letters = lambda count: "".join(secrets.choice(string.ascii_uppercase) for _ in range(count))
    replacements = (
        (b"Software Version 41.05", f"Software Version {digits(2)}.{digits(2)}".encode()),
        (b"firmware version is 7.0.0.3.0.0.0.0", f"firmware version is {digits(1)}.0.0.3.0.0.0.0".encode()),
        (b"VERBMAP BUS001L", f"{letters(7)} BUS001L".encode()),
        (b"VERSMAP None", f"VERSMAP {letters(4)}".encode()),
    )
    for old, new in replacements:
        record_179 = record_179.replace(old, new)
    LOG.info(
        "Portable v2 randomized extended identity: software=%s ilitek=%s "
        "map=%s versmap=%s",
        replacements[0][1].decode(), replacements[1][1].decode(),
        replacements[2][1].decode(), replacements[3][1].decode(),
    )
    if len(record_179) != len(records[1]):
        raise ValueError("portable-v2 identity randomization changed record length")
    return record_124, record_179, record_12


def discovery_records_for_model(
    model_name: str, *, allow_length_change: bool = False
) -> tuple[bytes, bytes, bytes]:
    """Build the three identity records, repairing outer record lengths."""
    replacement = model_name.encode("utf-8")
    if not allow_length_change and len(replacement) != len(CAPTURED_MODEL_NAME):
        raise ValueError(
            f"model name must encode to {len(CAPTURED_MODEL_NAME)} bytes, "
            f"got {len(replacement)}: {model_name!r}"
        )
    secondary_replacement = replacement[1:] if replacement else replacement
    record_124 = HEAD_DISCOVERY_124.replace(
        CAPTURED_MODEL_NAME, replacement
    ).replace(CAPTURED_SECONDARY_MODEL_NAME, secondary_replacement)
    record_179 = HEAD_DISCOVERY_179.replace(CAPTURED_MODEL_NAME, replacement)
    if (
        CAPTURED_MODEL_NAME in record_124
        or CAPTURED_SECONDARY_MODEL_NAME in record_124
        or CAPTURED_MODEL_NAME in record_179
    ):
        raise ValueError("one or more captured model fields were not replaced")
    if allow_length_change:
        record_124 = record_124[:4] + (len(record_124) - 8).to_bytes(4, "little") + record_124[8:]
        record_179 = record_179[:4] + (len(record_179) - 8).to_bytes(4, "little") + record_179[8:]
    return record_124, record_179, HEAD_DISCOVERY_12


def discovery_records_for_identity_minimization(
    experiment: str,
    records: tuple[bytes, bytes, bytes] | None = None,
) -> tuple[bytes, bytes, bytes]:
    """Apply one neutralization/removal candidate to identity records."""
    record_124, record_179, record_12 = records or (
        HEAD_DISCOVERY_124, HEAD_DISCOVERY_179, HEAD_DISCOVERY_12
    )
    removals = {
        "id-07b-remove-software": b" Software Version 41.05",
        "id-07c-remove-ilitek": (
            b"\nilitek_ts: ver-ilitek firmware version is 7.0.0.3.0.0.0.0"
        ),
        "id-07d-remove-map": b"\nVERBMAP BUS001L - U.S. & Coastal Canada ",
        "id-07e-remove-versmap": b"\nVERSMAP None",
    }
    if experiment in removals:
        old = removals[experiment]
        if record_179.count(old) != 1:
            raise ValueError(f"identity field not found exactly once: {experiment}")
        record_179 = record_179.replace(old, b"")
        record_179 = (
            record_179[:4]
            + (len(record_179) - 8).to_bytes(4, "little")
            + record_179[8:]
        )
    elif experiment == "id-07f-neutral-system-code":
        if record_124.count(b"System Code") != 1:
            raise ValueError("System Code field not found exactly once")
        record_124 = record_124.replace(b"System Code", b"Client Code")
    elif experiment == "id-11-zero-505bb70b":
        token = bytes.fromhex("505bb70b")
        record_124 = record_124.replace(token, bytes(4))
        record_179 = record_179.replace(token, bytes(4))
    elif experiment == "id-07f-zero-system-code-and-505bb70b":
        system_code = b"System Code"
        if record_124.count(system_code) != 1:
            raise ValueError("System Code field not found exactly once")
        token = bytes.fromhex("505bb70b")
        record_124 = record_124.replace(system_code, bytes(len(system_code)))
        record_124 = record_124.replace(token, bytes(4))
        record_179 = record_179.replace(token, bytes(4))
    elif experiment == "id-12-zero-discovery-12-tail":
        record_12 = record_12[:8] + bytes(4)
    else:
        raise ValueError(f"unknown identity-minimization experiment: {experiment}")
    return record_124, record_179, record_12


def identity_records_for_consolidated_profile() -> tuple[bytes, bytes, bytes]:
    """Build the minimized identity used by the consolidated profile."""
    record_124, record_179, record_12 = discovery_records_for_model(
        "", allow_length_change=True
    )
    for old in (
        b" Software Version 41.05",
        b"\nilitek_ts: ver-ilitek firmware version is 7.0.0.3.0.0.0.0",
        b"\nVERBMAP BUS001L - U.S. & Coastal Canada ",
        b"\nVERSMAP None",
    ):
        if record_179.count(old) != 1:
            raise ValueError(
                f"consolidated identity field not found exactly once: {old!r}"
            )
        record_179 = record_179.replace(old, b"")
    record_179 = (
        record_179[:4]
        + (len(record_179) - 8).to_bytes(4, "little")
        + record_179[8:]
    )
    record_12 = record_12[:8] + bytes(4)
    return discovery_records_for_identity_minimization(
        "id-07f-zero-system-code-and-505bb70b",
        (record_124, record_179, record_12),
    )


TCP_50224_MESSAGES = (
    bytes.fromhex("3f08efbe020000005bdf"),
    bytes.fromhex("a90aefbe08000000ecf7601ca9c046e8"),
    bytes.fromhex("0409efbe0400000000000400"),
    # The head unit sends the inactive 50224 state record before the GLS10
    # returns its six-record initialization response.  Earlier captures began
    # after session setup, which made this look like a post-response heartbeat.
    bytes.fromhex(
        "0409efbe180000001400010030594137d0107f097a6a4137265943373a5ca446"
    ),
)
TCP_50900_MESSAGES = (
    bytes.fromhex("3f08efbe02000000d4c6"),
    bytes.fromhex("a90aefbe0800000090910075c5292f85"),
    bytes.fromhex("c508efbe00000000"),
)
TCP_50227_MESSAGES = (
    bytes.fromhex("3f08efbe020000002dc4"),
    bytes.fromhex("a90aefbe08000000e47c30483a89abb6"),
    bytes.fromhex("d307efbe00000000"),
)
TCP_50227_HEARTBEAT = bytes.fromhex(
    "d107efbe0e0000000401040daaeffa910d12d4222101"
)
DATA_SINK_INIT = (
    bytes.fromhex("c505000003000000010100"),
    bytes.fromhex("c505000009000000020701058ce794a803"),
    bytes.fromhex("c505000003000000040100"),
    bytes.fromhex("c505000003000000050100"),
    bytes.fromhex("c505000003000000060100"),
    bytes.fromhex("c5050000030000000b0100"),
)
DATA_SINK_ACTIVE_COMMAND = bytes.fromhex(
    "c50500000e000000050c011709ff9f80d0fa96aa8e01"
)
DATA_SINK_COUNTER_PREFIX = bytes.fromhex("c50500000900000002070105")
DATA_SINK_COUNTER_SUFFIX = bytes.fromhex("94a803")
DATA_SINK_COUNTER_INITIAL = 0xE78D
DATA_SINK_SOURCE_PORT = 51401
HEAD_DISCOVERY_FINALIZE = bytes.fromhex("9a03000004000000009b760d")
TCP_50224_KEEPALIVE = bytes.fromhex(
    "0409efbe180000001400010030594137d0107f097a6a4137265943373a5ca446"
)
TCP_50224_STREAM_KEEPALIVE = bytes.fromhex(
    "0409efbe180000001400010030594137d0107f097a6a41372a5943373a5ca446"
)

# The captured static range records are linked against this GSFB base. The live
# session base is the first word in the GLS10's 50224 initialization response.
RANGE_PACKET_STATIC_BASE = 0x42465312


@dataclass(frozen=True)
class EmulatorConfig:
    """Validated settings used to start the emulator."""

    interface: str | None
    debug: bool
    viewer_enabled: bool
    viewer_args: tuple[str, ...]
    startup_range: str | int | None
    auto_configure_interface: bool = False
    portable_profile: bool = False
    maximal_portable_profile: bool = False
    portable_v2_profile: bool = False
    consolidated_test_profile: bool = False


@dataclass(frozen=True)
class EmulatorStatus:
    """Read-only snapshot of the emulator's existing runtime signals."""

    stopping: bool
    gls_detected: bool
    control_50224_ready: bool
    control_50227_ready: bool
    stream_active: bool

    @property
    def control_ready(self) -> bool:
        return self.control_50224_ready and self.control_50227_ready


def get_emulator_status(
    runtime: EmulatorRuntime,
) -> EmulatorStatus:
    """Return current state without waiting for or changing any signal."""
    return EmulatorStatus(
        stopping=runtime.stop.is_set(),
        gls_detected=runtime.gls_seen.is_set(),
        control_50224_ready=runtime.tcp_50224_ready.is_set(),
        control_50227_ready=runtime.tcp_50227_ready.is_set(),
        stream_active=runtime.data_sink_active.is_set(),
    )


def build_session_range_packet(
    range_value: str | int,
    session_base: int,
) -> bytes:
    packet = bytearray(build_range_packet(range_value))
    relocation = (session_base - RANGE_PACKET_STATIC_BASE) & 0xFFFFFFFF
    for offset in range(12, 28, 4):
        static_word = int.from_bytes(packet[offset:offset + 4], "little")
        if offset == 20 and range_value != "min":
            # Range selectors are banked 8:24 addresses. The low 24 bits
            # and bank byte relocate independently. A normal 32-bit carry or
            # borrow made the same numeric range select a different scale for
            # some session bases. The special "min" selector retains the
            # captured legacy relocation used by the working implementation.
            relocated_word = (
                (
                    (
                        (session_base >> 24)
                        + (static_word >> 24)
                        - (RANGE_PACKET_STATIC_BASE >> 24)
                    ) & 0xFF
                ) << 24
                | (
                    (session_base & 0xFFFFFF)
                    + (static_word & 0xFFFFFF)
                    - (RANGE_PACKET_STATIC_BASE & 0xFFFFFF)
                ) & 0xFFFFFF
            )
        else:
            relocated_word = (static_word + relocation) & 0xFFFFFFFF
        packet[offset:offset + 4] = relocated_word.to_bytes(4, "little")
    return bytes(packet)


def build_session_sonar_state_packet(enabled: bool, session_base: int) -> bytes:
    """Build the 50224 state write paired with a 50227 sonar command."""
    words = (
        session_base,
        (session_base + 12) & 0xFFFFFFFF,
        (session_base + (0 if enabled else 1)) & 0xFFFFFFFF,
        (session_base + 1) & 0xFFFFFFFF,
    )
    return (
        bytes.fromhex("0409efbe1400000010000200")
        + b"".join(word.to_bytes(4, "little") for word in words)
    )


def build_session_perspective_packet(session_base: int) -> bytes:
    """Build the captured 50224 orientation write that selects Perspective."""
    words = (
        session_base,
        (session_base + 0x2F) & 0xFFFFFFFF,
        (session_base + 0x20004) & 0xFFFFFFFF,
        (session_base + 4) & 0xFFFFFFFF,
    )
    return (
        bytes.fromhex("0409efbe1400000010000200")
        + b"".join(word.to_bytes(4, "little") for word in words)
    )


def interface_details(iface: str):
    """Resolve a user-facing name or capture name to a capture interface."""
    try:
        return resolve_iface(iface)
    except (ValueError, CaptureError):
        return None


def interface_addresses(iface: str) -> set[str]:
    details = interface_details(iface)
    if details is None:
        return set()
    ips = getattr(details, "ips", {})
    return set(ips.get(4, ()))


_IRRELEVANT_INTERFACE_WORDS = (
    "loopback", "wi-fi", "wifi", "wireless", "wlan", "bluetooth",
    "tunnel", "vpn", "virtual", "hyper-v", "vmware", "virtualbox",
    "docker", "wsl", "teredo", "isatap", "miniport", "bridge",
    "awdl", "llw", "utun", "gif", "stf", "anpi",
)


def _interface_capture_name(iface) -> str:
    """Return the native capture device identifier."""
    return str(getattr(iface, "network_name", None) or iface.name)


def _interface_is_usable_ethernet(iface) -> bool:
    name = str(getattr(iface, "name", ""))
    description = str(getattr(iface, "description", ""))
    combined = f"{name} {description}".lower()
    if any(word in combined for word in _IRRELEVANT_INTERFACE_WORDS):
        return False
    if re.match(
            r"^(lo|tun|tap|wg|ppp|sl|ap|wl|wlan|veth|br)\w*$",
            name.lower()):
        return False

    flags = str(getattr(iface, "flags", "")).upper()
    if "LOOPBACK" in flags or "POINTOPOINT" in flags:
        return False
    # Capture providers expose UP when link state is available. An empty flags
    # value is retained because some Windows/Npcap versions omit this metadata.
    if flags and "UP" not in flags:
        return False

    mac = str(getattr(iface, "mac", "")).lower()
    if not mac or mac == "00:00:00:00:00:00":
        return False
    return True


def _darwin_wired_link_active(iface) -> bool:
    """Use macOS media state to refine the generic capture device list."""
    name = str(getattr(iface, "name", ""))
    try:
        result = subprocess.run(
            ["ifconfig", name], capture_output=True, text=True)
    except OSError:
        return True
    if result.returncode:
        return False
    active = re.search(
        r"^\s*status:\s*active\s*$", result.stdout,
        re.MULTILINE | re.IGNORECASE)
    ethernet_media = re.search(
        r"^\s*media:.*\([^)]*\b\d+(?:\.\d+)?(?:g)?base-?t(?:x)?\b",
        result.stdout, re.MULTILINE | re.IGNORECASE)
    return bool(active and ethernet_media)


def _select_ethernet_interfaces(ifaces, system: str, *, prefer_configured: bool = True) -> list[str]:
    usable = [iface for iface in ifaces if _interface_is_usable_ethernet(iface)]
    configured = [
        iface for iface in usable
        if HEAD_IP in set(getattr(iface, "ips", {}).get(4, ()))
    ]
    if configured and prefer_configured:
        return [_interface_capture_name(iface) for iface in configured]

    # BPF doesn't distinguish Wi-Fi from Ethernet by link type. Preserve the
    # prior macOS negotiated-wired-media check for unconfigured adapters.
    if system == "Darwin":
        usable = [iface for iface in usable if _darwin_wired_link_active(iface)]
    return [_interface_capture_name(iface) for iface in usable]


def detect_ethernet_interfaces(*, prefer_configured: bool = True) -> list[str]:
    """Find usable wired interfaces using native adapter metadata.

    On Windows, ``network_name`` is deliberately returned because it is the
    Npcap device identifier accepted by native capture and layer-2 sending.
    """
    try:
        ifaces = list_interfaces()
    except (OSError, CaptureError) as exc:
        LOG.error("Packet adapter could not enumerate network interfaces: %s", exc)
        return []
    return _select_ethernet_interfaces(ifaces, platform.system(), prefer_configured=prefer_configured)


def normalize_viewer_args(viewer_args: list[str]) -> list[str]:
    """Accept PowerShell's commonly quoted group of flag-only viewer options."""
    if (len(viewer_args) == 1 and " " in viewer_args[0] and
            all(part.startswith("-") for part in viewer_args[0].split())):
        return viewer_args[0].split()
    return viewer_args


def _powershell_command(script: str) -> list[str]:
    # Pass code separately from Windows command-line quoting, including Unicode.
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


def configure_interface_addresses(
    iface: str,
    addresses: set[str],
    system: str | None = None,
) -> set[str] | None:
    """Repair the dedicated sonar adapter's two known IPv4 assignments."""
    system = platform.system() if system is None else system
    details = interface_details(iface)
    device = str(getattr(details, "name", iface))
    quoted_device = "'" + device.replace("'", "''") + "'"
    updated = set(addresses)

    commands: list[tuple[str, list[str]]] = []
    if GLS_IP in updated:
        if system == "Windows":
            commands.append(("remove conflicting GLS10 address", _powershell_command(
                f"Remove-NetIPAddress -InterfaceAlias {quoted_device} "
                f"-IPAddress '{GLS_IP}' -Confirm:$false -ErrorAction Stop"
            )))
        elif system == "Linux":
            prefix = (NETWORK_RECOVERY.data["original"].get(GLS_IP, 16)
                      if NETWORK_RECOVERY is not None else 16)
            commands.append(("remove conflicting GLS10 address", [
                "ip", "address", "delete", f"{GLS_IP}/{prefix}", "dev", device,
            ]))
        else:
            commands.append(("remove conflicting GLS10 address", [
                "ifconfig", device, "-alias", GLS_IP,
            ]))
    if HEAD_IP not in updated:
        if system == "Windows":
            commands.append(("assign emulated head-unit address", _powershell_command(
                f"New-NetIPAddress -InterfaceAlias {quoted_device} "
                f"-IPAddress '{HEAD_IP}' -PrefixLength 16 -ErrorAction Stop"
            )))
        elif system == "Linux":
            commands.append(("assign emulated head-unit address", [
                "ip", "address", "add", f"{HEAD_IP}/16", "dev", device,
            ]))
        else:
            commands.append(("assign emulated head-unit address", [
                "ifconfig", device, "inet", HEAD_IP,
                "netmask", "255.255.0.0", "alias",
            ]))

    for label, command in commands:
        try:
            if system in {"Darwin", "Linux"} and NETWORK_RECOVERY is not None:
                NETWORK_RECOVERY.mark_address("gls_removed" if label.startswith("remove") else "head_added")
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("Could not %s on %s: %s", label, device, exc)
            return None
        if result.returncode:
            LOG.error(
                "Could not %s on %s: %s",
                label,
                device,
                (result.stderr or result.stdout).strip(),
            )
            return None
        LOG.info("Automatically completed interface repair: %s on %s",
                 label, device)
        if label.startswith("remove"):
            updated.discard(GLS_IP)
        else:
            updated.add(HEAD_IP)
    return updated


def install_gls_arp(iface: str) -> bool:
    if NETWORK_RECOVERY is not None:
        with NETWORK_RECOVERY.lock:
            return _install_gls_arp(iface)
    return _install_gls_arp(iface)


def _install_gls_arp(iface: str) -> bool:
    """Pin the directly-connected GLS10 MAC using the host OS."""
    system = platform.system()
    if system == "Windows":
        # arp.exe selects the correct adapter by its configured local address;
        # it does not accept Npcap's capture GUID.
        arp_command = [
            "arp", "-s", GLS_IP, GLS_MAC.replace(":", "-"), HEAD_IP]
        try:
            result = subprocess.run(
                arp_command, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("Could not run Windows arp.exe: %s", exc)
            return False
        if result.returncode:
            LOG.error(
                "Could not install the GLS10 ARP entry. Run PowerShell as "
                "Administrator and verify %s is assigned to the adapter: %s",
                HEAD_IP, (result.stderr or result.stdout).strip())
            return False
        LOG.info("Installed static ARP: %s is-at %s", GLS_IP, GLS_MAC)
        return True

    if system == "Linux":
        details = interface_details(iface)
        device = str(getattr(details, "name", iface))
        try:
            if NETWORK_RECOVERY is not None:
                NETWORK_RECOVERY.prepare_neighbor(device, GLS_MAC)
            result = subprocess.run(
                ["ip", "neigh", "replace", GLS_IP, "lladdr", GLS_MAC,
                 "nud", "permanent", "dev", device],
                capture_output=True, text=True, timeout=5)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            LOG.error("Could not run Linux ip command: %s", exc)
            return False
        if result.returncode:
            LOG.error("Could not install GLS10 neighbor entry: %s",
                      (result.stderr or result.stdout).strip())
            return False
        LOG.info("Installed static neighbor: %s is-at %s on %s",
                 GLS_IP, GLS_MAC, device)
        return True

    if NETWORK_RECOVERY is not None:
        try:
            NETWORK_RECOVERY.prepare_neighbor(iface, GLS_MAC)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            LOG.error("Could not journal sonar neighbor: %s", exc)
            return False

    # macOS needs a scoped entry and occasionally an explicit connected route.
    arp_command = ["arp", "-s", GLS_IP, GLS_MAC, "ifscope", iface]
    try:
        result = subprocess.run(arp_command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOG.error("Could not run macOS arp command: %s", exc)
        return False
    if result.returncode and "No such process" in result.stderr:
        # macOS can configure the /16 address without installing its connected
        # route (notably when the local device identity itself ends in .0).
        # A scoped ARP entry cannot be created until that route exists.
        if NETWORK_RECOVERY is not None:
            NETWORK_RECOVERY.prepare_route(iface)
        route = subprocess.run(
            ["route", "-n", "add", "-net", "172.16.0.0/16",
             "-interface", iface, "-ifscope", iface],
            capture_output=True, text=True, timeout=5)
        if route.returncode and NETWORK_RECOVERY is not None:
            NETWORK_RECOVERY.cancel_route()
        if route.returncode and "File exists" not in route.stderr:
            LOG.error("Could not install sonar network route on %s: %s",
                      iface, route.stderr.strip())
            return False
        LOG.info("Installed sonar 172.16.0.0/16 route on %s", iface)
        result = subprocess.run(arp_command, capture_output=True, text=True, timeout=5)
    if result.returncode:
        LOG.error("Could not install GLS10 ARP entry: %s", result.stderr.strip())
        return False
    LOG.info("Installed static ARP: %s is-at %s on %s", GLS_IP, GLS_MAC, iface)
    return True


def passive_gls_mac_learning_loop(
    runtime: EmulatorRuntime, iface: str, captured_mac: str
) -> None:
    """Learn the peer MAC from its first IP/ARP frame after presence starts."""
    global GLS_MAC

    def is_gls_frame(packet) -> bool:
        return bool(
            (packet.ip is not None and socket.inet_ntop(
                socket.AF_INET if len(packet.ip.src) == 4 else socket.AF_INET6,
                packet.ip.src) == GLS_IP)
            or (packet.arp is not None and packet.arp.spa == socket.inet_aton(GLS_IP))
        )

    deadline = time.monotonic() + 45
    while not runtime.stop.is_set() and time.monotonic() < deadline:
        packets = sniff(
            iface=iface, timeout=1, count=1, store=True, lfilter=is_gls_frame
        )
        if runtime.stop.is_set():
            return
        if not packets:
            continue
        learned = ":".join(f"{byte:02x}" for byte in packets[0].ethernet.src)
        GLS_MAC = learned
        LOG.info(
            "Passively learned GLS10 MAC from first peer frame: %s is-at %s "
            "(captured constant was %s)",
            GLS_IP, learned, captured_mac,
        )
        if install_gls_arp(iface):
            runtime.neighbor_ready.set()
        else:
            runtime.stop.set()
        return
    LOG.error("No GLS10 Ethernet frame observed during passive MAC-learning window")
    runtime.stop.set()


def join(group: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                    socket.inet_aton(group) + socket.inet_aton(HEAD_IP))
    sock.settimeout(1)
    return sock


def presence_loop(
    runtime: EmulatorRuntime,
    iface: str,
    head_mac: str,
    send_ethernet_presence: bool = True,
    send_arp_probe: bool = True,
) -> None:
    payload = bytes.fromhex("040000054f400600") + bytes(38)
    packet = ethernet_frame(head_mac, "ff:ff:ff:ff:ff:ff", 0x2003, payload)
    arp_probe = arp_request(head_mac, HEAD_IP, GLS_IP)
    while not runtime.stop.is_set():
        if send_ethernet_presence:
            send_packet(packet, iface=iface)
        if send_arp_probe:
            send_packet(arp_probe, iface=iface)
        runtime.stop.wait(5)


def announcement_loop(
    runtime: EmulatorRuntime,
    send_50220: bool = True,
    send_50050: bool = True,
) -> None:
    s50220 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s50220.bind((HEAD_IP, 0))
    s50050 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s50050.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s50050.bind((HEAD_IP, 50050))
    for sock in (s50220, s50050):
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(HEAD_IP))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl())
    announcement_sequence = 3
    active_sequence = 0xc6
    active_schedule_started: float | None = None
    next_status = next_capability = 0.0
    while not runtime.stop.is_set():
        try:
            if send_50220:
                s50220.sendto(HEAD_50220, ("239.254.2.1", 50220))
            now = time.monotonic()
            if not send_50050:
                pass
            elif runtime.data_sink_active.is_set():
                if active_schedule_started is None:
                    active_schedule_started = now
                    next_status = now
                    next_capability = now + 2.85
                if now >= next_status:
                    status = bytearray(HEAD_50050_STATUS)
                    status[-4] = active_sequence
                    s50050.sendto(status, ("239.254.2.2", 50050))
                    next_status += 5.0
                if now >= next_capability:
                    capability = bytearray(HEAD_50050_ACTIVE)
                    capability[-7] = active_sequence
                    s50050.sendto(capability, ("239.254.2.2", 50050))
                    active_sequence = (active_sequence + 1) & 0xff
                    next_capability += 5.0
            else:
                # Preserve the existing startup advertisement until the
                # active data-sink state begins.
                head_50050 = bytearray(HEAD_50050)
                head_50050[-7] = announcement_sequence
                s50050.sendto(head_50050, ("239.254.2.2", 50050))
                announcement_sequence = (announcement_sequence + 1) & 0xff
        except OSError as exc:
            LOG.debug("Multicast announcement deferred: %s", exc)
        runtime.stop.wait(.1 if runtime.data_sink_active.is_set() else 1)


def system_presence_loop(runtime: EmulatorRuntime, head_mac: str) -> None:
    """Send the head-unit system record observed throughout healthy sessions."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HEAD_IP, 50030))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(HEAD_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl())
    message = HEAD_50030
    startup_messages = 0 if experiment_is("pres-04-periodic-only") else 10
    messages_sent = 0
    while not runtime.stop.is_set():
        try:
            sock.sendto(message, ("224.0.0.1", 50030))
            messages_sent += 1
        except OSError as exc:
            LOG.debug("UDP 50030 system presence deferred: %s", exc)
        if experiment_is("pres-04-one-startup") and messages_sent >= 1:
            return
        if experiment_is("pres-04-startup-only") and startup_messages <= 1:
            return
        interval = 1.0 if startup_messages > 0 else (
            30.0 if experiment_is("pres-04-periodic-30s") else 11.75
        )
        startup_messages -= 1
        runtime.stop.wait(interval)


def head_service_51000_loop(runtime: EmulatorRuntime) -> None:
    """Publish the one-second head-unit presence record from healthy sessions."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HEAD_IP, 51000))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(HEAD_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl())
    try:
        while not runtime.stop.is_set():
            try:
                sock.sendto(HEAD_51000_PRESENCE, ("239.254.2.11", 51000))
            except OSError as exc:
                LOG.debug("UDP 51000 head presence deferred: %s", exc)
            runtime.stop.wait(1)
    finally:
        sock.close()


def head_service_51400_loop(runtime: EmulatorRuntime) -> None:
    """Publish the two-second head-unit system-service record."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HEAD_IP, 51400))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(HEAD_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl())
    try:
        while not runtime.stop.is_set():
            try:
                sock.sendto(HEAD_51400_SERVICE, ("239.254.2.18", 51400))
            except OSError as exc:
                LOG.debug("UDP 51400 system service deferred: %s", exc)
            runtime.stop.wait(2)
    finally:
        sock.close()


def head_service_51950_loop(runtime: EmulatorRuntime) -> None:
    """Publish the two counter-bearing head-unit state records."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HEAD_IP, 51950))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(HEAD_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl())
    short_counter = 0x08ae
    long_counter = 0x06d7
    started = time.monotonic()
    next_short = started
    next_long = started + 1.0
    try:
        while not runtime.stop.is_set():
            now = time.monotonic()
            try:
                if now >= next_short:
                    message = (HEAD_51950_SHORT_PREFIX +
                               short_counter.to_bytes(2, "little"))
                    sock.sendto(message, ("239.254.2.22", 51950))
                    short_counter = (short_counter + 1) & 0xffff
                    next_short += 2.0
                if now >= next_long:
                    message = (HEAD_51950_LONG_PREFIX +
                               long_counter.to_bytes(2, "little"))
                    sock.sendto(message, ("239.254.2.22", 51950))
                    long_counter = (long_counter + 1) & 0xffff
                    next_long += 2.5
            except OSError as exc:
                LOG.debug("UDP 51950 head state deferred: %s", exc)
            runtime.stop.wait(.05)
    finally:
        sock.close()


def head_service_50615_loop(
    runtime: EmulatorRuntime,
    send_50615: bool = True,
    send_8322: bool = True,
) -> None:
    """Publish the healthy three-record service burst every ten seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HEAD_IP, 50615))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(HEAD_IP))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl())
    capability_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    capability_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    capability_sock.bind((HEAD_IP, 8322))
    capability_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                               socket.inet_aton(HEAD_IP))
    capability_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, multicast_ttl())
    try:
        # Healthy capture places the first burst about five seconds into the
        # observed session, then repeats it every ten seconds.
        if runtime.stop.wait(5):
            return
        while not runtime.stop.is_set():
            try:
                if send_50615:
                    for message in HEAD_50615_BURST:
                        sock.sendto(message, ("239.254.2.14", 50615))
                if send_8322:
                    capability_sock.sendto(
                        HEAD_8322_CAPABILITIES, ("239.254.2.14", 8322))
            except OSError as exc:
                LOG.debug("UDP 50615 service burst deferred: %s", exc)
            runtime.stop.wait(10)
    finally:
        sock.close()
        capability_sock.close()


def discovery_50051_loop(
    runtime: EmulatorRuntime,
    identity_records: tuple[bytes, bytes, bytes] = (
        HEAD_DISCOVERY_124, HEAD_DISCOVERY_179, HEAD_DISCOVERY_12
    ),
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((HEAD_IP, 50051))
    sock.settimeout(1)
    last_probe = 0.0
    last_wait_log = 0.0
    finalize_sent = False
    head_records_sent = False
    while not runtime.stop.is_set():
        if runtime.data_sink_maintenance.is_set() and not head_records_sent:
            # Healthy trace sends 124/179/12/124/179 head identity records
            # about 307 ms after the first sink-maintenance cycle.
            runtime.stop.wait(0 if experiment_is("time-04-identity-delay-zero") else .3)
            record_124, record_179, record_12 = identity_records
            records = (
                (record_124, record_179, record_12)
                if experiment_is("order-01-identity-single")
                else (record_124, record_179, record_12, record_124, record_179)
            )
            for record in records:
                sock.sendto(record, (GLS_IP, 50051))
            head_records_sent = True
            LOG.info("Sent post-maintenance UDP 50051 head identity transition")
        if (not runtime.tcp_50224_ready.is_set() and
                time.monotonic() - last_probe > 2):
            sent = False
            try:
                for command in (0x391, 0x398, 0x396, 0x391):
                    sock.sendto(command.to_bytes(4, "little") + bytes(4),
                                (GLS_IP, 50051))
                sent = True
            except OSError as exc:
                now = time.monotonic()
                if now - last_wait_log >= 10:
                    LOG.info("Waiting for GLS10 route/ARP: %s", exc)
                    last_wait_log = now
            if sent:
                LOG.debug("Sent UDP 50051 discovery sequence")
            last_probe = time.monotonic()
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if addr[0] != GLS_IP:
            continue
        runtime.gls_seen.set()
        LOG.info("UDP 50051 from GLS10: %d bytes type=%s", len(data), data[:4].hex())
        if data.startswith(bytes.fromhex("9703")) and not finalize_sent:
            sock.sendto(HEAD_DISCOVERY_FINALIZE, (GLS_IP, 50051))
            finalize_sent = True
            LOG.info("Sent UDP 50051 head-unit session finalizer: %s",
                     HEAD_DISCOVERY_FINALIZE.hex())
        if data.startswith(bytes.fromhex("9103")):
            replies = identity_records
        elif data.startswith(bytes.fromhex("9603")):
            replies = (identity_records[2],)
        else:
            replies = ()
        for reply in replies:
            sock.sendto(reply, (GLS_IP, 50051))


def tcp_exchange(
    runtime: EmulatorRuntime,
    port: int,
    messages: tuple[bytes, ...],
    expected: int,
    startup_range: str | int | None = None,
) -> None:
    while (not runtime.stop.is_set() and
           not runtime.neighbor_ready.wait(1)):
        pass
    if port == 50900:
        while (not runtime.stop.is_set() and
               not runtime.head_identity_ready.wait(1)):
            pass
        # Healthy trace advertises mudskipper's mrn-snr identity roughly
        # 560 ms before opening the first TCP session.
        runtime.stop.wait(0 if experiment_is("time-01-50900-delay-zero") else .55)
    if port == 50224:
        while (not runtime.stop.is_set() and
               not runtime.tcp_50900_ready.wait(1)):
            pass
        # Healthy capture leaves roughly 620 ms between the completed 50900
        # exchange and opening 50224.
        runtime.stop.wait(0 if experiment_is("time-02-50224-delay-zero") else .62)
    if port == 50227:
        while (not runtime.stop.is_set() and
               not runtime.data_sink_active.wait(1)):
            pass
        # Healthy capture: first active sink record at 78.484561 and the
        # 50227 SYN at 78.680349 (about 196 ms later).
        runtime.stop.wait(0 if experiment_is("time-03-50227-delay-zero") else .195)
    while not runtime.stop.is_set():
        runtime.gls_seen.wait(1)
        if not runtime.gls_seen.is_set():
            continue
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.bind((HEAD_IP, 0))
            sock.connect((GLS_IP, port))
            LOG.info("Connected to GLS10 TCP %d", port)
            send_messages = messages
            token_experiment = f"token-01-port-{port}"
            if experiment_is(token_experiment):
                changed = bytearray(messages[0])
                changed[-1] ^= 0x01
                send_messages = (bytes(changed), *messages[1:])
                LOG.info("TOKEN-01 one-bit mutation active on TCP %d", port)
            token_02_experiment = f"token-02-port-{port}"
            if experiment_is(token_02_experiment):
                changed = bytearray(send_messages[1])
                changed[-1] ^= 0x01
                send_messages = (send_messages[0], bytes(changed), *send_messages[2:])
                LOG.info("TOKEN-02 one-bit a90a mutation active on TCP %d", port)
            if experiment_is(f"token-01-zero-{port}"):
                changed = bytearray(send_messages[0])
                changed[-2:] = b"\x00\x00"
                send_messages = (bytes(changed), *send_messages[1:])
                LOG.info("TOKEN-01 zero token active on TCP %d", port)
            if experiment_is(f"token-02-zero-{port}"):
                changed = bytearray(send_messages[1])
                changed[-8:] = bytes(8)
                send_messages = (send_messages[0], bytes(changed), *send_messages[2:])
                LOG.info("TOKEN-02 zero a90a token active on TCP %d", port)
            if experiment_is(f"token-01-remove-{port}"):
                send_messages = send_messages[1:]
                LOG.info("TOKEN-01 removed initial 3f08 record on TCP %d", port)
            if experiment_is(f"token-02-remove-{port}"):
                send_messages = (send_messages[0], *send_messages[2:])
                LOG.info("TOKEN-02 removed initial a90a record on TCP %d", port)
            for message in send_messages:
                sock.sendall(message)
            received = bytearray()
            while len(received) < expected and not runtime.stop.is_set():
                chunk = sock.recv(65535)
                if not chunk:
                    break
                received.extend(chunk)
            response_digest = hashlib.sha256(received).hexdigest()[:16]
            LOG.info(
                "TCP %d initialization response: %d bytes sha256=%s prefix=%s",
                port, len(received), response_digest, received[:32].hex(),
            )
            LOG.debug("TCP %d full initialization response: %s",
                      port, received.hex())
            if port == 50224:
                runtime.tcp_50224_ready.set()
                session_base = int.from_bytes(received[12:16], "little")
                LOG.info("TCP 50224 session relocation base: 0x%08x",
                         session_base)
                if startup_range is not None:
                    sock.sendall(build_session_range_packet(
                        startup_range, session_base))
                    LOG.info("Requested startup GLS10 range: %s ft",
                             startup_range)
                # Once initialization completes, the healthy head immediately
                # transitions from the pre-response 0x26 state to 0x2a and
                # maintains that active state once per second.
                sock.sendall(TCP_50224_STREAM_KEEPALIVE)
                last_keepalive = time.monotonic()
                stream_keepalive_sent = True
                pending_sonar_state_ack = None
                pending_sonar_state_deadline = 0.0
                runtime.stream_transition_sent.set()
                LOG.info("Sent immediate TCP 50224 active-state transition")
                while not runtime.stop.wait(.01):
                    now = time.monotonic()
                    if (
                        pending_sonar_state_ack is not None
                        and now >= pending_sonar_state_deadline
                    ):
                        LOG.info(
                            "No immediate TCP 50224 state response; "
                            "continuing sonar enable"
                        )
                        pending_sonar_state_ack.set()
                        pending_sonar_state_ack = None
                    try:
                        range_value, range_packet, sent = (
                            runtime.range_commands.get_nowait()
                        )
                    except queue.Empty:
                        pass
                    else:
                        range_packet = build_session_range_packet(
                            range_value, session_base)
                        sock.sendall(range_packet)
                        sent.set()
                        LOG.info(
                            "Set GLS10 range to %s ft using base=0x%08x "
                            "packet=%s",
                            range_value,
                            session_base,
                            range_packet.hex(),
                        )
                    try:
                        sonar_enabled, sent = (
                            runtime.sonar_state_commands.get_nowait()
                        )
                    except queue.Empty:
                        pass
                    else:
                        sock.sendall(build_session_sonar_state_packet(
                            sonar_enabled, session_base))
                        if sonar_enabled:
                            # In the capture the first GLS10 state response
                            # precedes the head unit's 50227 enable write.
                            pending_sonar_state_ack = sent
                            pending_sonar_state_deadline = now + 0.05
                        else:
                            sent.set()
                        LOG.info(
                            "Sent TCP 50224 sonar %s state",
                            "enabled" if sonar_enabled else "disabled",
                        )
                    if (runtime.stream_transition_sent.is_set() and
                            not stream_keepalive_sent):
                        sock.sendall(TCP_50224_STREAM_KEEPALIVE)
                        stream_keepalive_sent = True
                        last_keepalive = now
                        LOG.info("Sent immediate TCP 50224 stream-state transition")
                    elif (
                        not experiment_is("ctrl-01-no-50224-repeats")
                        and now - last_keepalive >= 1
                    ):
                        message = (TCP_50224_STREAM_KEEPALIVE
                                   if stream_keepalive_sent
                                   else TCP_50224_KEEPALIVE)
                        sock.sendall(message)
                        last_keepalive = now
                    readable, _, _ = select.select([sock], [], [], 0)
                    if readable:
                        follow_up = sock.recv(65535)
                        if not follow_up:
                            raise ConnectionError("GLS10 closed TCP 50224")
                        LOG.info("TCP 50224 follow-up: %d bytes type=%s",
                                 len(follow_up), follow_up[:8].hex())
                        LOG.debug("TCP 50224 full follow-up: %s",
                                  follow_up.hex())
                        if pending_sonar_state_ack is not None:
                            pending_sonar_state_ack.set()
                            pending_sonar_state_ack = None
            elif port == 50227:
                runtime.tcp_50227_ready.set()
                LOG.info("Started TCP 50227 identity heartbeat")
                last_heartbeat = time.monotonic()
                while not runtime.stop.wait(.01):
                    try:
                        enabled, packet, sent = (
                            runtime.sonar_commands.get_nowait()
                        )
                    except queue.Empty:
                        pass
                    else:
                        sock.sendall(packet)
                        sent.set()
                        LOG.info(
                            "%s GLS10 sonar transmission",
                            "Enabled" if enabled else "Disabled",
                        )
                    now = time.monotonic()
                    if (
                        not experiment_is("ctrl-02-no-50227-heartbeat")
                        and now - last_heartbeat >= 1
                    ):
                        sock.sendall(TCP_50227_HEARTBEAT)
                        last_heartbeat = now
                    readable, _, _ = select.select([sock], [], [], 0)
                    if readable:
                        response = sock.recv(65535)
                        if not response:
                            raise ConnectionError("GLS10 closed TCP 50227")
                        LOG.info(
                            "TCP 50227 response: %d bytes type=%s",
                            len(response), response[:8].hex(),
                        )
                        LOG.debug(
                            "TCP 50227 full response: %s", response.hex()
                        )
            else:
                if port == 50900:
                    runtime.tcp_50900_ready.set()
                # Keep 50900 open. Closing it immediately is interpreted as
                # loss of the head-unit session by the GLS10.
                sock.settimeout(1)
                while not runtime.stop.is_set():
                    try:
                        chunk = sock.recv(65535)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError("GLS10 closed TCP 50900")
                    LOG.debug("TCP 50900 follow-up: %d bytes", len(chunk))
            sock.close()
            return
        except OSError as exc:
            if port == 50224:
                runtime.tcp_50224_ready.clear()
            elif port == 50227:
                runtime.tcp_50227_ready.clear()
            LOG.debug("TCP %d not ready: %s", port, exc)
            runtime.stop.wait(2)


def sonar_command_coordinator(runtime: EmulatorRuntime) -> None:
    """Deliver accepted sonar requests once both control sessions are ready."""
    while not runtime.stop.is_set():
        try:
            enabled = runtime.sonar_requests.get(timeout=1)
        except queue.Empty:
            continue
        while not runtime.stop.is_set():
            if (runtime.tcp_50224_ready.wait(.25) and
                    runtime.tcp_50227_ready.is_set()):
                break
        if runtime.stop.is_set():
            return

        packet = ENABLE_SONAR_PACKET if enabled else DISABLE_SONAR_PACKET
        def send_state() -> bool:
            sent = threading.Event()
            runtime.sonar_state_commands.put((enabled, sent))
            while not runtime.stop.is_set() and not sent.wait(.25):
                pass
            return not runtime.stop.is_set()

        def send_command() -> bool:
            sent = threading.Event()
            runtime.sonar_commands.put((enabled, packet, sent))
            while not runtime.stop.is_set() and not sent.wait(.25):
                pass
            return not runtime.stop.is_set()

        if experiment_is("ctrl-03-command-first"):
            if not send_command():
                return
            if not send_state():
                return
        else:
            if not send_state():
                return
            if experiment_is("ctrl-03-state-delay-500ms"):
                runtime.stop.wait(.5)
            if not send_command():
                return


def viewer_control_loop(
    runtime: EmulatorRuntime,
    sock: socket.socket,
) -> None:
    """Accept validated range and sonar commands from the local viewer."""
    sock.settimeout(1)
    while not runtime.stop.is_set():
        try:
            command, address = sock.recvfrom(64)
        except socket.timeout:
            continue
        try:
            operation, raw_value = command.decode("ascii").split(":", 1)
            if operation == "range":
                range_value = "min" if raw_value == "min" else int(raw_value)
                if range_value not in RANGE_VALUE_BYTES:
                    raise ValueError
                # Retain the newest requested target across a GLS10 power
                # cycle. The TCP 50224 worker consumes it only after its new
                # session has supplied a fresh relocation base.
                while True:
                    try:
                        runtime.range_commands.get_nowait()
                    except queue.Empty:
                        break
                sent = threading.Event()
                runtime.range_commands.put((
                    range_value, build_range_packet(range_value), sent
                ))
                sock.sendto(b"QUEUED", address)
                continue
            elif operation == "sonar":
                enabled = {"enable": True, "disable": False}[raw_value]
                # Keep the captured 50224-state then 50227-command ordering,
                # but let a control click survive session startup/reconnect.
                runtime.sonar_requests.put(enabled)
                sock.sendto(b"OK", address)
                continue
            else:
                raise ValueError
        except (KeyError, UnicodeDecodeError, ValueError):
            sock.sendto(b"ERROR", address)
        else:
            sock.sendto(b"OK" if sent.wait(.5) else b"NOT_READY", address)
    sock.close()


def sonar_multicast_loop(runtime: EmulatorRuntime, jpeg_sample_every: int = 0) -> None:
    sock = join("239.254.2.16", 50223)
    count = total = large_packets = 0
    completed_frames = 0
    assembler = SonarFrameAssembler() if jpeg_sample_every else None
    last = time.monotonic()
    while not runtime.stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        if addr[0] != GLS_IP:
            continue
        runtime.gls_seen.set()
        count += 1
        total += len(data)
        if len(data) > 1000:
            large_packets += 1
        if assembler is not None:
            assembled = assembler.add_payload(data)
            if assembled is not None:
                frame_id, jpg, _ = assembled
                completed_frames += 1
                if completed_frames % jpeg_sample_every == 0:
                    dimensions = jpeg_dimensions(jpg)
                    if dimensions is None:
                        LOG.warning(
                            "GLS10 JPEG sample: frame=%d bytes=%d dimensions=unknown",
                            frame_id, len(jpg),
                        )
                    else:
                        width, height = dimensions
                        LOG.info(
                            "GLS10 JPEG sample: frame=%d bytes=%d dimensions=%dx%d",
                            frame_id, len(jpg), width, height,
                        )
        now = time.monotonic()
        if now - last >= 1:
            LOG.info("GLS10 50223 stream: %d packets, %d bytes/s, %d image-sized",
                     count, total, large_packets)
            count = total = large_packets = 0
            last = now


class SinkListener(ServiceListener):
    def __init__(self, runtime: EmulatorRuntime) -> None:
        self.runtime = runtime
        self.started: set[int] = set()

    def add_service(self, zc, service_type: str, name: str) -> None:
        info = zc.get_service_info(service_type, name, timeout=3000)
        if info is None or info.port in self.started:
            return
        if GLS_IP not in info.parsed_addresses():
            return
        self.started.add(info.port)
        self.runtime.gls_seen.set()
        LOG.info("GLS10 data sink discovered on UDP %d", info.port)
        threading.Thread(target=self._initialize_sink, args=(info.port,),
                         daemon=True).start()

    def _initialize_sink(self, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sink_source_port = (
            0 if experiment_is("sink-01-ephemeral-source")
            else DATA_SINK_SOURCE_PORT
        )
        sock.bind((HEAD_IP, sink_source_port))

        try:
            while (not self.runtime.stop.is_set() and
                   not self.runtime.tcp_50224_ready.wait(1)):
                pass
            while not self.runtime.stop.is_set():
                try:
                    initial_messages = list(DATA_SINK_INIT)
                    if experiment_is("sink-02-initial-counter"):
                        initial_messages[1] = (
                            DATA_SINK_COUNTER_PREFIX
                            + ((DATA_SINK_COUNTER_INITIAL + 1) & 0xffff).to_bytes(2, "little")
                            + DATA_SINK_COUNTER_SUFFIX
                        )
                    if experiment_is("sink-07-counter-suffix"):
                        initial_messages[1] = (
                            initial_messages[1][:-1]
                            + bytes([initial_messages[1][-1] ^ 0x01])
                        )
                    if experiment_is("sink-06-reverse-order"):
                        initial_messages.reverse()
                    initial_omissions = {
                        "sink-init-no-type-01": 0, "sink-init-no-type-02": 1,
                        "sink-init-no-type-04": 2, "sink-init-no-type-05": 3,
                        "sink-init-no-type-06": 4, "sink-init-no-type-0b": 5,
                    }
                    if experiment_is("sink-init-none"):
                        initial_messages = []
                    else:
                        initial_messages = [
                            message for index, message in enumerate(initial_messages)
                            if not any(
                                experiment_is(name) and index == omitted
                                for name, omitted in initial_omissions.items()
                            )
                        ]
                    for message in initial_messages:
                        sock.sendto(message, (GLS_IP, port))
                    LOG.info("Started %d-message data-sink maintenance on UDP %d",
                             len(initial_messages), port)
                    LOG.info("GLS10 data-sink endpoint: %s:%d -> %s:%d",
                             HEAD_IP, sock.getsockname()[1], GLS_IP, port)
                    self.runtime.data_sink_maintenance.set()
                    break
                except OSError as exc:
                    LOG.debug("Data-sink initialization deferred: %s", exc)
                    self.runtime.stop.wait(2)
            started = time.monotonic()
            counter = DATA_SINK_COUNTER_INITIAL
            counter_repeats = 0
            active_logged = False
            maintenance_interval = (
                1.0 if experiment_is("sink-04-maintenance-1s") else .1
            )
            while not self.runtime.stop.wait(maintenance_interval):
                messages = list(DATA_SINK_INIT)
                # In the healthy capture this field is repeated for ten
                # 100-ms maintenance cycles, then advances once per second.
                messages[1] = (DATA_SINK_COUNTER_PREFIX +
                               counter.to_bytes(2, "little") +
                               DATA_SINK_COUNTER_SUFFIX)
                if experiment_is("sink-07-counter-suffix"):
                    messages[1] = messages[1][:-1] + bytes([messages[1][-1] ^ 0x01])
                active_delay = 0 if experiment_is("time-05-active-delay-zero") else 5.5
                if time.monotonic() - started >= active_delay:
                    # Type 05 changes from its short inactive record to the
                    # active sonar-data request. Type 06 remains present.
                    messages[3] = DATA_SINK_ACTIVE_COMMAND
                    if experiment_is("sink-08-active-payload"):
                        messages[3] = messages[3][:-1] + bytes([messages[3][-1] ^ 0x01])
                    if not active_logged:
                        LOG.info("Enabled active GLS10 data request and live "
                                 "maintenance counter on UDP %d", port)
                        active_logged = True
                omission_indexes = {
                    "sink-05-no-type-01": 0,
                    "sink-05-no-type-02": 1,
                    "sink-05-no-type-04": 2,
                    "sink-05-no-type-05": 3,
                    "sink-05-no-type-06": 4,
                    "sink-05-no-type-0b": 5,
                }
                if experiment_is("sink-06-reverse-order"):
                    messages.reverse()
                    omission_indexes = {}
                for index, message in enumerate(messages):
                    if any(
                        omitted_index == index and experiment_is(experiment)
                        for experiment, omitted_index in omission_indexes.items()
                    ):
                        continue
                    sock.sendto(message, (GLS_IP, port))
                if active_logged:
                    self.runtime.data_sink_active.set()
                counter_repeats += 1
                if counter_repeats == 10:
                    if experiment_is("sink-03-freeze-counter"):
                        counter_repeats = 0
                        continue
                    counter = (counter + 1) & 0xffff
                    counter_repeats = 0
        finally:
            sock.close()

    def update_service(self, zc, service_type: str, name: str) -> None:
        self.add_service(zc, service_type, name)

    def remove_service(self, zc, service_type: str, name: str) -> None:
        pass


def stream_transition_loop(runtime: EmulatorRuntime) -> None:
    """Broadcast the final head-unit transition seen immediately before images."""
    while (not runtime.stop.is_set() and
           not runtime.data_sink_active.wait(1)):
        pass
    # New healthy trace: 5.733 s after the first active sink record and
    # 153 ms before the first image packet (11 ms in the older trace).
    transition_delay = 0 if experiment_is("time-06-transition-delay-zero") else 5.73
    if runtime.stop.wait(transition_delay):
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HEAD_IP, 50010))
        sock.sendto(HEAD_STREAM_TRANSITION, ("172.16.255.255", 50010))
        LOG.info("Sent UDP 50010 stream transition: %s",
                 HEAD_STREAM_TRANSITION.hex())
        runtime.stream_transition_sent.set()
    finally:
        sock.close()


def native_sink_discovery_loop(
    runtime: EmulatorRuntime,
    iface: str,
) -> None:
    """Discover the GLS10 SRV port when the optional zeroconf package is absent."""
    listener = SinkListener(runtime)

    def query_loop() -> None:
        head_mac = get_if_hwaddr(iface)
        query = mdns_query(head_mac, HEAD_IP)
        while not runtime.stop.is_set() and not listener.started:
            try:
                send_packet(query, iface=iface)
                LOG.debug("Queried for GLS10 mDNS data-sink service")
            except OSError as exc:
                LOG.debug("Native mDNS query deferred: %s", exc)
            runtime.stop.wait(1)

    def handle(packet) -> None:
        dns = packet.dns
        if packet.ip is None or packet.ip.src != socket.inet_aton(GLS_IP) or dns is None:
            return
        for section in (dns.an, dns.ns, dns.ar):
            records = section if isinstance(section, list) else [section]
            for record in records:
                name = getattr(record, "name", "")
                if (getattr(record, "type", None) == 33 and
                        "_garmin-ds-sink._udp" in name):
                    port = int(record.port)
                    if port in listener.started:
                        continue
                    listener.started.add(port)
                    runtime.gls_seen.set()
                    LOG.info("GLS10 data sink discovered natively on UDP %d", port)
                    threading.Thread(target=listener._initialize_sink,
                                     args=(port,), daemon=True).start()

    # Passive listening alone is insufficient: the GLS10 may not repeat its
    # SRV record unless a head unit explicitly asks for the service.
    threading.Thread(target=query_loop, daemon=True).start()
    while not runtime.stop.is_set() and not listener.started:
        try:
            sniff(iface=iface, filter="udp port 5353", prn=handle, store=False,
                  timeout=1)
        except (OSError, CaptureError) as exc:
            if platform.system() == "Windows":
                LOG.error("Packet capture failed on Windows: %s. Install "
                          "Npcap manually (with WinPcap API compatibility), "
                          "then restart the emulator.", exc)
            else:
                LOG.error("Packet capture failed on %s: %s",
                          platform.system(), exc)
            runtime.stop.set()
            return


def mdns_loop(
    runtime: EmulatorRuntime,
    iface: str,
    server_name: str,
    instance_name: str,
    identity_port: int,
    service_type: str,
    advertise_identity: bool = True,
) -> None:
    if Zeroconf is None:
        LOG.warning("zeroconf unavailable; using native mDNS sink discovery")
        # Native fallback cannot register ServiceInfo, but must not deadlock
        # protocol startup on systems where zeroconf is intentionally absent.
        runtime.head_identity_ready.set()
        native_sink_discovery_loop(runtime, iface)
        return
    zc = Zeroconf(interfaces=[HEAD_IP], ip_version=IPVersion.V4Only)
    address = socket.inet_aton(HEAD_IP)
    identity = ServiceInfo(
        service_type,
        f"{instance_name}.{service_type}",
        addresses=[address], port=identity_port, server=server_name, properties={})
    listener = SinkListener(runtime)
    browser = ServiceBrowser(zc, "_garmin-ds-sink._udp.local.", listener)
    registered = False
    try:
        if advertise_identity:
            zc.register_service(identity, allow_name_change=False)
            registered = True
            LOG.info("Advertised emulated head-unit identity: instance=%s server=%s "
                     "port=%d", instance_name, server_name, identity_port)
        else:
            LOG.info("Omitted emulated head-unit MRN/SNR service advertisement")
        runtime.head_identity_ready.set()
        runtime.stop.wait()
    finally:
        browser.cancel()
        if registered:
            try:
                zc.unregister_service(identity)
            except Exception:
                pass
        zc.close()


def generate_portable_identity() -> tuple[str, str]:
    """Return independently randomized, length-preserving mDNS identity names."""
    platform_token = "".join(
        secrets.choice(string.ascii_lowercase) for _ in range(10)
    )
    instance_suffix = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
    server_suffix = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
    instance = f"device-j6-{platform_token}-{instance_suffix}"
    server = f"device-j6-{platform_token}-{server_suffix}.local."
    return server, instance


def run_engine(runtime: EmulatorRuntime) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--iface",
        help="sonar-facing Ethernet interface (auto-detected when omitted)")
    parser.add_argument("--network-state", type=Path, default=user_data_path("network-recovery.json"))
    parser.add_argument("--restore-network", action="store_true", help="Restore network changes from an interrupted session and exit")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--no-viewer", action="store_true",
        help="Run the head-unit protocol emulator without the integrated live viewer")
    parser.add_argument(
        "--web-viewer", action="store_true",
        help="Also serve the integrated viewer at http://127.0.0.1:8765")
    parser.add_argument(
        "--viewer-args", nargs=argparse.REMAINDER, default=[], metavar="ARG",
        help="Pass all remaining arguments to the live viewer (place this option last)")
    parser.add_argument(
        "--auto-configure-interface", action="store_true",
        help="Repair sonar adapter IPv4 addresses before startup")
    parser.add_argument(
        "--startup-range", default="15",
        help="Range requested immediately after TCP 50224 initialization; "
             "default: 15; use 'none' to leave the GLS10 unchanged")
    parser.add_argument(
        "--portable-profile",
        action="store_true",
        help=(
            "use the composite experimental portable identity/minimal-presence "
            "profile; intended for the separate portable emulator entry point"
        ),
    )
    parser.add_argument(
        "--maximal-portable-profile",
        action="store_true",
        help=(
            "combine all screened portable changes while retaining UDP 50030 "
            "and repeated TCP 50224 active-state keepalives"
        ),
    )
    parser.add_argument(
        "--portable-v2-profile",
        action="store_true",
        help="use all qualified portability changes through 2026-09-04",
    )
    parser.add_argument(
        "--test-consolidated-profile",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--jpeg-sample-every",
        type=int,
        default=0,
        metavar="N",
        help="log dimensions and encoded size for every Nth completed JPEG",
    )
    parser.add_argument(
        "--emulator-control-port", type=int, default=0,
        help=argparse.SUPPRESS)
    args = parser.parse_args()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: runtime.stop.set())
    global NETWORK_RECOVERY
    if platform.system() in {"Darwin", "Linux"}:
        NETWORK_RECOVERY = create_network_recovery(args.network_state)
        if args.restore_network:
            NETWORK_RECOVERY.restore()
            return 0
        if platform.system() == "Linux" and NETWORK_RECOVERY is not None and args.network_state.exists():
            NETWORK_RECOVERY.restore()
            list_interfaces()
    elif args.restore_network:
        parser.error("Network recovery currently supports macOS and Linux")
    global ACTIVE_PROTOCOL_EXPERIMENT, MAXIMAL_PORTABLE_PROFILE, PORTABLE_V2_PROFILE
    global CONSOLIDATED_TEST_PROFILE
    global HEAD_IP, GLS_MAC
    ACTIVE_PROTOCOL_EXPERIMENT = None
    MAXIMAL_PORTABLE_PROFILE = args.maximal_portable_profile
    PORTABLE_V2_PROFILE = args.portable_v2_profile
    # The minimized consolidated behavior is the normal emulator behavior.
    # Explicit protocol experiments retain the legacy baseline unless an
    # allow-listed composed test asks for the hidden test profile.
    CONSOLIDATED_TEST_PROFILE = (
        args.test_consolidated_profile
        or (
            True
            and not args.portable_profile
            and not args.maximal_portable_profile
            and not args.portable_v2_profile
        )
    )
    if PORTABLE_V2_PROFILE or CONSOLIDATED_TEST_PROFILE:
        HEAD_IP = "172.16.6.1"
    selected_profiles = (
        int(args.portable_profile) + int(args.maximal_portable_profile)
        + int(args.portable_v2_profile)
    )
    if selected_profiles > 1 or args.test_consolidated_profile:
        parser.error(
            "profiles cannot be combined with an experiment, except for an "
            "allow-listed consolidated minimization follow-up"
        )
    if args.jpeg_sample_every < 0:
        parser.error("--jpeg-sample-every cannot be negative")
    args.viewer_args = normalize_viewer_args(args.viewer_args)
    if args.startup_range.lower() == "none":
        args.startup_range = None
    else:
        try:
            args.startup_range = (
                "min" if args.startup_range.lower() == "min"
                else int(args.startup_range)
            )
        except ValueError:
            parser.error(f"invalid --startup-range: {args.startup_range}")
        if args.startup_range not in RANGE_VALUE_BYTES:
            parser.error(f"unsupported --startup-range: {args.startup_range}")
    config = EmulatorConfig(
        interface=args.iface,
        debug=args.debug,
        viewer_enabled=not args.no_viewer,
        viewer_args=tuple(args.viewer_args),
        startup_range=args.startup_range,
        auto_configure_interface=args.auto_configure_interface,
        portable_profile=args.portable_profile,
        maximal_portable_profile=args.maximal_portable_profile,
        portable_v2_profile=args.portable_v2_profile,
        consolidated_test_profile=CONSOLIDATED_TEST_PROFILE,
    )
    logging.basicConfig(level=logging.DEBUG if config.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if config.portable_profile:
        LOG.warning("PORTABLE COMPOSITE PROFILE ACTIVE")
    if config.maximal_portable_profile:
        LOG.warning("MAXIMAL PORTABLE COMPOSITE PROFILE ACTIVE")
    if config.portable_v2_profile:
        LOG.warning("PORTABLE V2 COMPOSITE PROFILE ACTIVE")
    if config.consolidated_test_profile:
        LOG.warning("CONSOLIDATED LONG-RUN TEST PROFILE ACTIVE")
    if config.interface is None:
        candidates = detect_ethernet_interfaces()
        if not candidates:
            LOG.error("Could not auto-detect an active wired Ethernet interface; "
                      "connect the GLS10 adapter or specify --iface")
            return 2
        if len(candidates) > 1:
            LOG.error("Multiple active wired Ethernet interfaces found (%s); "
                      "open Advanced → Connection and logging and select the "
                      "Sonar Ethernet interface connected to the GLS10", ", ".join(candidates))
            return 2
        config = replace(config, interface=candidates[0])
        LOG.info("Auto-detected sonar-facing Ethernet interface: %s",
                 config.interface)
    else:
        details = interface_details(config.interface)
        if details is None:
            LOG.error("Interface %s does not exist; omit --iface to auto-detect it",
                      config.interface)
            return 2
        # Canonicalize display names to the exact identifier the packet adapter's capture
        # backend requires (notably \\Device\\NPF_{GUID} on Windows).
        config = replace(config, interface=_interface_capture_name(details))
    assert config.interface is not None
    if interface_details(config.interface) is None:
        LOG.error("Interface %s does not exist; omit --iface to auto-detect it",
                  config.interface)
        return 2
    if NETWORK_RECOVERY is not None:
        NETWORK_RECOVERY.begin(config.interface, HEAD_IP)
    addresses = interface_addresses(config.interface)
    if config.auto_configure_interface:
        repaired_addresses = configure_interface_addresses(
            config.interface, addresses
        )
        if repaired_addresses is None:
            return 2
        addresses = repaired_addresses
    if GLS_IP in addresses:
        if platform.system() == "Windows":
            LOG.error("Remove conflicting address %s from the sonar adapter "
                      "with Remove-NetIPAddress in an Administrator PowerShell",
                      GLS_IP)
        elif platform.system() == "Darwin":
            LOG.error("Remove conflicting GLS10 address first: "
                      "sudo ifconfig %s -alias %s",
                      config.interface, GLS_IP)
        else:
            LOG.error("Remove conflicting GLS10 address first: "
                      "sudo ip address delete %s/16 dev %s",
                      GLS_IP, getattr(interface_details(config.interface),
                                      "name", config.interface))
        return 2
    if HEAD_IP not in addresses:
        details = interface_details(config.interface)
        if platform.system() == "Windows":
            adapter = getattr(details, "name", config.interface)
            LOG.error(
                "Assign %s/16 to the sonar adapter first in an Administrator "
                "PowerShell: New-NetIPAddress -InterfaceAlias %r "
                "-IPAddress %s -PrefixLength 16",
                HEAD_IP, adapter, HEAD_IP)
        elif platform.system() == "Linux":
            LOG.error("Assign %s/16 to %s first (for example: "
                      "sudo ip address add %s/16 dev %s)",
                      HEAD_IP, config.interface, HEAD_IP,
                      getattr(details, "name", config.interface))
        else:
            LOG.error("Assign %s first: sudo ifconfig %s inet %s "
                      "netmask 255.255.0.0 up",
                      HEAD_IP, config.interface, HEAD_IP)
        return 2
    if platform.system() == "Linux" and NETWORK_RECOVERY is not None:
        NETWORK_RECOVERY.validate_route()
    try:
        get_if_hwaddr(config.interface)
    except (OSError, CaptureError) as exc:
        if platform.system() == "Windows":
            LOG.error("Packet adapter cannot access interface %s: %s. Install Npcap "
                          "manually (with WinPcap API compatibility) and run "
                          "PowerShell as Administrator.", config.interface, exc)
        else:
            LOG.error("Packet adapter cannot access interface %s: %s",
                      config.interface, exc)
        return 2
    head_mac = get_if_hwaddr(config.interface)
    captured_gls_mac = GLS_MAC
    passive_mac_experiment = (
        config.portable_v2_profile
        or config.consolidated_test_profile
    )
    if not passive_mac_experiment:
        if not install_gls_arp(config.interface):
            return 2
        runtime.neighbor_ready.set()
    LOG.info("Emulating head unit %s / %s; GLS10 is %s / %s",
             HEAD_IP, head_mac, GLS_IP, GLS_MAC)
    if runtime.stop.is_set():
        return 0
    viewer_process = None
    viewer_ready_file = None
    viewer_control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    viewer_control_socket.bind(("127.0.0.1", args.emulator_control_port))
    viewer_control_port = viewer_control_socket.getsockname()[1]
    LOG.info("Local emulator control endpoint: 127.0.0.1:%d",
             viewer_control_port)
    identity_server, identity_instance = generate_portable_identity()
    identity_port = 50222
    identity_service_type = "_device-mrn-snr._tcp.local."
    identity_records = (HEAD_DISCOVERY_124, HEAD_DISCOVERY_179, HEAD_DISCOVERY_12)
    portable_mode = (
        config.portable_profile or config.maximal_portable_profile
        or config.portable_v2_profile or config.consolidated_test_profile
    )
    if portable_mode:
        identity_server, identity_instance = generate_portable_identity()
        identity_port = 50222
        if (config.maximal_portable_profile or config.portable_v2_profile
                or config.consolidated_test_profile):
            identity_service_type = "_device-mrn-snr._tcp.local."
            model_length = secrets.randbelow(40) + 1
            model_name = "".join(
                secrets.choice(string.ascii_uppercase + string.digits)
                for _ in range(model_length)
            )
            identity_records = discovery_records_for_model(
                model_name, allow_length_change=True
            )
            if config.consolidated_test_profile:
                identity_records = identity_records_for_consolidated_profile()
            elif config.portable_v2_profile:
                identity_records = randomize_extended_identity_strings(identity_records)
        LOG.info(
            "Generated portable identity: instance=%s server=%s port=%d",
            identity_instance,
            identity_server,
            identity_port,
        )
        if (config.maximal_portable_profile or config.portable_v2_profile
                or config.consolidated_test_profile):
            if config.consolidated_test_profile:
                LOG.info(
                    "Consolidated identity removes model/software/Ilitek/map/"
                    "VERSMAP and omits the MRN/SNR advertisement"
                )
            else:
                LOG.info(
                    "Maximal profile identity model=%s (%d bytes); omitting all "
                    "screened optional presence traffic including UDP 8322; "
                    "retaining required UDP 50030 and TCP 50224 active keepalives",
                    model_name, model_length,
                )
            if config.portable_v2_profile:
                LOG.info(
                    "Portable network/sink extensions: head=%s; "
                    "passive GLS MAC; randomized extended identity strings; "
                    "tested sink mutations",
                    HEAD_IP,
                )
            if config.consolidated_test_profile:
                LOG.info(
                    "Consolidated additions: UDP 50030 immediately then every "
                    "30 seconds without a startup burst; removed model and "
                    "qualified extended identity fields; zero type-9703 tail; "
                    "removed 3f08 records on all TCP ports while retaining a90a; "
                    "MRN/SNR advertisement omitted"
                )
        else:
            LOG.info(
                "Portable omissions: Ethernet 0x2003, ARP probe, UDP 50220, "
                "50050, 51000, 51400, 51950, 50615, and 50010; retaining "
                "required UDP 50030 and control-candidate UDP 8322"
            )
    if config.viewer_enabled:
        if not FROZEN and not VIEWER_SCRIPT.is_file():
            LOG.error("Integrated viewer not found: %s", VIEWER_SCRIPT)
            return 2
        if FROZEN:
            viewer_command = bundle_executable("SonarHeadUnitViewer")
        else:
            viewer_command = [sys.executable, str(VIEWER_SCRIPT)]
        # The viewer becomes the desktop user after opening its capture socket.
        # An administrator's private temporary directory is inaccessible then.
        ready_directory = Path("/tmp") if os.name == "posix" else Path(tempfile.gettempdir())
        viewer_ready_file = (ready_directory /
                             f"viewer-ready-{uuid.uuid4().hex}")
        viewer_command.extend([
            "--iface", config.interface,
            "--stream", "all",
            "--ready-file", str(viewer_ready_file),
            "--emulator-control-port", str(viewer_control_port),
        ])
        if config.startup_range is not None:
            viewer_command.extend(("--initial-range", str(config.startup_range)))
        if args.web_viewer:
            viewer_command.extend(("--web", "--web-only"))
        viewer_command.extend(config.viewer_args)
        LOG.info("Starting integrated Sonar viewer")
        viewer_process = subprocess.Popen(viewer_command)
        runtime.viewer_process = viewer_process
        LOG.info("Waiting for Sonar viewer packet capture to become ready")
        while not runtime.stop.wait(.05):
            if viewer_ready_file.is_file():
                LOG.info("Sonar viewer is ready; starting GLS10 negotiation")
                # Give the child one scheduler turn to enter its first sniff
                # call after publishing the readiness marker.
                runtime.stop.wait(.25)
                break
            if viewer_process.poll() is not None:
                LOG.error(
                    "Sonar viewer exited before packet capture was ready "
                    "(status %s); check viewer startup logs for the cause",
                    viewer_process.returncode,
                )
                return viewer_process.returncode or 2
    threads = [
        threading.Thread(
            target=passive_gls_mac_learning_loop,
            args=(runtime, config.interface, captured_gls_mac), daemon=True,
        ) if passive_mac_experiment else None,
        threading.Thread(target=presence_loop,
                         args=(
                             runtime,
                             config.interface,
                             head_mac, not portable_mode, not portable_mode,
                         ), daemon=True),
        threading.Thread(target=announcement_loop,
                         args=(
                             runtime,
                             not portable_mode, not portable_mode,
                         ), daemon=True),
        threading.Thread(target=system_presence_loop,
                         args=(runtime, head_mac), daemon=True),
        threading.Thread(target=head_service_51000_loop,
                         args=(runtime,), daemon=True)
        if not portable_mode else None,
        threading.Thread(target=head_service_51400_loop,
                         args=(runtime,), daemon=True)
        if not portable_mode else None,
        threading.Thread(target=head_service_51950_loop,
                         args=(runtime,), daemon=True)
        if not portable_mode else None,
        threading.Thread(target=head_service_50615_loop,
                         args=(
                             runtime,
                             not portable_mode,
                             False,
                         ), daemon=True),
        threading.Thread(target=discovery_50051_loop,
                         args=(runtime, identity_records), daemon=True),
        threading.Thread(target=sonar_multicast_loop,
                         args=(runtime, args.jpeg_sample_every), daemon=True),
        threading.Thread(target=stream_transition_loop,
                         args=(runtime,), daemon=True)
        if not portable_mode else None,
        threading.Thread(target=tcp_exchange,
                         args=(runtime, 50224, TCP_50224_MESSAGES, 268 * 6,
                               config.startup_range), daemon=True),
        threading.Thread(target=tcp_exchange,
                         args=(runtime, 50900, TCP_50900_MESSAGES, 171),
                         daemon=True),
        threading.Thread(target=tcp_exchange,
                         args=(runtime, 50227, TCP_50227_MESSAGES, 313),
                         daemon=True),
        threading.Thread(target=mdns_loop,
                         args=(runtime, config.interface, identity_server,
                               identity_instance, identity_port,
                               identity_service_type,
                               not config.consolidated_test_profile), daemon=True),
        threading.Thread(target=viewer_control_loop,
                         args=(runtime, viewer_control_socket), daemon=True),
        threading.Thread(target=sonar_command_coordinator,
                         args=(runtime,), daemon=True),
    ]
    for thread in threads:
        if thread is None:
            continue
        thread.start()
    LOG.info("Head-unit emulator running")
    while not runtime.stop.wait(.5):
        if viewer_process is not None and viewer_process.poll() is not None:
            LOG.info("Sonar viewer closed; stopping emulator")
            runtime.stop.set()
    LOG.info("Stopping")
    if passive_mac_experiment and threads[0] is not None:
        threads[0].join(timeout=6)
    if viewer_process is not None and viewer_process.poll() is None:
        viewer_process.terminate()
        try:
            viewer_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            viewer_process.kill()
            viewer_process.wait()
    if viewer_ready_file is not None:
        viewer_ready_file.unlink(missing_ok=True)
    return 0


def main() -> int:
    global NETWORK_RECOVERY
    NETWORK_RECOVERY = None
    runtime = EmulatorRuntime()
    try:
        return run_engine(runtime)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        LOG.error("OpenScope network/startup failure: %s", exc)
        return 2
    finally:
        runtime.stop.set()
        viewer = getattr(runtime, "viewer_process", None)
        if viewer is not None and viewer.poll() is None:
            viewer.terminate()
            try:
                viewer.wait(timeout=10)
            except subprocess.TimeoutExpired:
                viewer.kill()
                viewer.wait()
        if NETWORK_RECOVERY is not None:
            try:
                NETWORK_RECOVERY.restore()
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                LOG.error("Network recovery pending: %s. Reconnect the dedicated adapter and use Restore network settings.", exc)


if __name__ == "__main__":
    raise SystemExit(main())
