from __future__ import annotations

import select
import socket
import time
from typing import Optional, Union


RangeValue = Union[str, int]

SUPPORTED_RANGES: tuple[RangeValue, ...] = (
    "min", 7, 9, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65,
    70, 75, 80, 90, 100, 110, 120, 135, 150, 175, 200,
)

RANGE_VALUE_BYTES = {
    "min": bytes.fromhex("81 65 09 81"),
    7: bytes.fromhex("f9 df 4e 82"), 9: bytes.fromhex("a9 e3 75 82"),
    10: bytes.fromhex("81 65 89 82"), 15: bytes.fromhex("e5 a0 d8 82"),
    20: bytes.fromhex("81 65 09 82"), 25: bytes.fromhex("1c 2a 39 82"),
    30: bytes.fromhex("e5 a0 58 83"), 35: bytes.fromhex("33 03 70 83"),
    40: bytes.fromhex("81 65 89 83"), 45: bytes.fromhex("cf c7 a1 83"),
    50: bytes.fromhex("1c 2a b9 83"), 55: bytes.fromhex("be 6f cc 83"),
    60: bytes.fromhex("e5 a0 d8 83"), 65: bytes.fromhex("0c d1 e4 83"),
    70: bytes.fromhex("33 03 f0 83"), 75: bytes.fromhex("5a 34 fc 83"),
    80: bytes.fromhex("81 65 09 83"), 90: bytes.fromhex("cf c6 20 83"),
    100: bytes.fromhex("1c 2a 39 83"), 110: bytes.fromhex("be 6f 4c 84"),
    120: bytes.fromhex("e5 a0 58 84"), 135: bytes.fromhex("9f ea 6a 84"),
    150: bytes.fromhex("5a 34 7c 84"), 175: bytes.fromhex("3b af 9b 84"),
    200: bytes.fromhex("1c 2a b9 84"),
}

RANGE_PACKET_PREFIX = bytes.fromhex(
    "04 09 ef be 14 00 00 00 10 00 02 00 14 53 46 42 47 53 46 42"
)
RANGE_PACKET_SUFFIX = bytes.fromhex("16 53 46 42")
SONAR_PACKET_PREFIX = bytes.fromhex("d2 07 ef be 0a 00 00 00 01 07 07 01 02 01 00 a9 01")
ENABLE_SONAR_PACKET = SONAR_PACKET_PREFIX + b"\x00"
DISABLE_SONAR_PACKET = SONAR_PACKET_PREFIX + b"\x01"


def build_range_packet(range_value: RangeValue) -> bytes:
    try:
        encoded = RANGE_VALUE_BYTES[range_value]
    except (KeyError, TypeError):
        raise ValueError(f"unsupported sonar range: {range_value!r}") from None
    packet = RANGE_PACKET_PREFIX + encoded + RANGE_PACKET_SUFFIX
    assert len(packet) == 28
    return packet


class SonarStateTracker:
    """Track requested sonar state separately from state inferred from frames."""

    SONAR_CONFIRM_SECONDS = 2.0
    SONAR_RETRY_INTERVAL_SECONDS = 1.0
    SONAR_MAX_ATTEMPTS = 5
    SONAR_REQUEST_TIMEOUT_SECONDS = 5.0
    FIRST_ENABLE_REQUEST_TIMEOUT_SECONDS = 15.0

    def _init_sonar_state(self) -> None:
        self.current_sonar_enabled: Optional[bool] = None
        self.last_requested_sonar_enabled: Optional[bool] = None
        self.sonar_transition_state = "unknown"
        self._pending_sonar_enabled: Optional[bool] = None
        self._sonar_request_started: Optional[float] = None
        self._sonar_request_timeout_seconds: Optional[float] = None
        self._last_sonar_frame_at: Optional[float] = None
        self._frame_at_sonar_request: Optional[float] = None
        self._sonar_attempts = 0
        self._last_sonar_attempt_at: Optional[float] = None
        self._sonar_enable_request_count = 0

    @property
    def pending_sonar_enabled(self) -> Optional[bool]:
        return self._pending_sonar_enabled

    @property
    def requested_sonar_enabled(self) -> Optional[bool]:
        if self._pending_sonar_enabled is not None:
            return self._pending_sonar_enabled
        return self.current_sonar_enabled

    def _accept_sonar_request(self, enabled: bool) -> None:
        self.last_requested_sonar_enabled = enabled
        self.sonar_transition_state = "pending"
        self._pending_sonar_enabled = enabled
        self._sonar_request_started = time.monotonic()
        if enabled and self._sonar_enable_request_count == 0:
            self._sonar_request_timeout_seconds = (
                self.FIRST_ENABLE_REQUEST_TIMEOUT_SECONDS
            )
        else:
            self._sonar_request_timeout_seconds = self.SONAR_REQUEST_TIMEOUT_SECONDS
        if enabled:
            self._sonar_enable_request_count += 1
        self._last_sonar_attempt_at = self._sonar_request_started
        self._sonar_attempts = 1
        self._frame_at_sonar_request = self._last_sonar_frame_at

    def _finish_sonar_request(self, enabled: bool) -> None:
        self.current_sonar_enabled = enabled
        self._pending_sonar_enabled = None
        self._sonar_request_started = None
        self._sonar_request_timeout_seconds = None
        self._frame_at_sonar_request = None
        self._last_sonar_attempt_at = None
        self._sonar_attempts = 0
        self.sonar_transition_state = "confirmed"
        if enabled:
            self._flush_pending_ranges()

    def _resend_pending_sonar(self, enabled: bool) -> bool:
        """Subclasses resend one transition command without resetting state."""
        raise NotImplementedError

    def _retry_pending_sonar(self, now: float) -> None:
        if (
            self._pending_sonar_enabled is None
            or self._last_sonar_attempt_at is None
            or self._sonar_attempts >= self.SONAR_MAX_ATTEMPTS
            or now - self._last_sonar_attempt_at < self.SONAR_RETRY_INTERVAL_SECONDS
        ):
            return
        enabled = self._pending_sonar_enabled
        self._sonar_attempts += 1
        self._last_sonar_attempt_at = now
        action = "enable" if enabled else "disable"
        print(
            f"Sonar {action} still pending; retrying "
            f"({self._sonar_attempts}/{self.SONAR_MAX_ATTEMPTS})"
        )
        self._resend_pending_sonar(enabled)

    def observe_sonar_frame(self) -> Optional[str]:
        """Incorporate a decoded frame and return a user-facing state event."""
        now = time.monotonic()
        self._last_sonar_frame_at = now
        if self._pending_sonar_enabled is True:
            self._finish_sonar_request(True)
            return "enabled_confirmed"
        if self._pending_sonar_enabled is False:
            assert self._sonar_request_started is not None
            request_timeout = (
                self._sonar_request_timeout_seconds
                or self.SONAR_REQUEST_TIMEOUT_SECONDS
            )
            if now - self._sonar_request_started >= request_timeout:
                self._finish_sonar_request(True)
                self.sonar_transition_state = "failed"
                return "disable_failed"
            self._retry_pending_sonar(now)
            return None
        if (
            self.sonar_transition_state == "failed"
            and self.last_requested_sonar_enabled is True
        ):
            # The unit may recover after the retry deadline even when our
            # pre-request state was already "enabled". A decoded frame proves
            # the requested end state regardless of that stale prior value.
            self.current_sonar_enabled = True
            self.sonar_transition_state = "confirmed"
            return "enabled_confirmed"
        if self.current_sonar_enabled is False:
            self.current_sonar_enabled = True
            if self.last_requested_sonar_enabled is True:
                # A power-cycled GLS10 can resume after our bounded retry
                # window. Live images are definitive evidence that enable
                # eventually took effect, so clear the stale failure.
                self.sonar_transition_state = "confirmed"
                return "enabled_confirmed"
            self.sonar_transition_state = "failed"
            return "unexpected_stream"
        if self.current_sonar_enabled is None:
            self.current_sonar_enabled = True
            return "stream_detected"
        return None

    def observe_sonar_silence(self) -> Optional[str]:
        """Confirm disable only after a recently live stream actually stops."""
        if self._pending_sonar_enabled is True:
            assert self._sonar_request_started is not None
            now = time.monotonic()
            request_timeout = (
                self._sonar_request_timeout_seconds
                or self.SONAR_REQUEST_TIMEOUT_SECONDS
            )
            if now - self._sonar_request_started >= request_timeout:
                self._pending_sonar_enabled = None
                self._sonar_request_started = None
                self._sonar_request_timeout_seconds = None
                self._last_sonar_attempt_at = None
                self._frame_at_sonar_request = None
                self._sonar_attempts = 0
                self.sonar_transition_state = "failed"
                return "enable_failed"
            self._retry_pending_sonar(now)
            return None
        if self._pending_sonar_enabled is not False:
            return None
        assert self._sonar_request_started is not None
        now = time.monotonic()
        request_age = now - self._sonar_request_started
        had_recent_stream = (
            self._frame_at_sonar_request is not None
            and self._sonar_request_started - self._frame_at_sonar_request
            <= self.SONAR_CONFIRM_SECONDS
        )
        quiet_for = (
            None if self._last_sonar_frame_at is None
            else now - self._last_sonar_frame_at
        )
        if (
            had_recent_stream
            and quiet_for is not None
            and quiet_for >= self.SONAR_CONFIRM_SECONDS
        ):
            self._finish_sonar_request(False)
            return "disabled_confirmed"
        request_timeout = (
            self._sonar_request_timeout_seconds
            or self.SONAR_REQUEST_TIMEOUT_SECONDS
        )
        if request_age >= request_timeout:
            self._finish_sonar_request(True)
            self.sonar_transition_state = "failed"
            return "disable_unconfirmed"
        self._retry_pending_sonar(now)
        return None


class SonarControlClient(SonarStateTracker):
    """Persistent TCP control connections to a GLS 10."""

    RANGE_PORT = 50224
    SONAR_PORT = 50227

    def __init__(
        self,
        gls10_ip: str,
        *,
        dry_run: bool = False,
        initial_range: Optional[RangeValue] = None,
        connect_timeout: float = 2.0,
    ) -> None:
        if initial_range is not None and initial_range not in RANGE_VALUE_BYTES:
            raise ValueError(f"unsupported sonar range: {initial_range!r}")
        self.gls10_ip = gls10_ip
        self.dry_run = dry_run
        self.current_range = initial_range
        self._init_sonar_state()
        self.connect_timeout = connect_timeout
        self._sockets: dict[int, socket.socket] = {}
        self._pending_ranges: list[RangeValue] = []

    @property
    def connected(self) -> bool:
        return self.dry_run or len(self._sockets) == 2

    @property
    def pending_range_count(self) -> int:
        return len(self._pending_ranges)

    @property
    def requested_range(self) -> Optional[RangeValue]:
        """Expose the selected range using the browser control contract."""
        return self.current_range

    @property
    def actual_range(self) -> Optional[RangeValue]:
        # The direct client has no frame-feedback tracker of its own. A
        # successfully transmitted selection is therefore its best-known
        # active range, and must remain visible across browser refreshes.
        return self.current_range

    @property
    def applying_range(self) -> bool:
        return False

    @property
    def range_change_pending(self) -> bool:
        return False

    def _log(self, operation: str, port: int, packet: Optional[bytes] = None, detail: str = "") -> None:
        stamp = time.strftime("%H:%M:%S")
        lines = [f"[{stamp}]", operation]
        if detail:
            lines.append(detail)
        lines.append(f"Destination {self.gls10_ip}:{port}")
        if packet is not None:
            lines.append(packet.hex(" "))
        print("\n".join(lines) + "\n")

    def connect(self) -> bool:
        if self.dry_run:
            self._log("DRY RUN CONNECT", self.RANGE_PORT, detail="No TCP connection opened")
            return True
        ok = True
        for port in (self.RANGE_PORT, self.SONAR_PORT):
            if port in self._sockets:
                continue
            try:
                sock = socket.create_connection((self.gls10_ip, port), timeout=self.connect_timeout)
                sock.settimeout(self.connect_timeout)
                self._sockets[port] = sock
                self._log("CONNECTED", port)
            except OSError as exc:
                ok = False
                self._log("CONNECTION FAILURE", port, detail=repr(exc))
        return ok

    def close(self) -> None:
        for sock in self._sockets.values():
            try:
                sock.close()
            except OSError as exc:
                self._log("CLOSE EXCEPTION", 0, detail=repr(exc))
        self._sockets.clear()

    def send_packet(self, packet: bytes) -> bool:
        if packet.startswith(RANGE_PACKET_PREFIX):
            port, operation = self.RANGE_PORT, "SET RANGE"
        elif packet in (ENABLE_SONAR_PACKET, DISABLE_SONAR_PACKET):
            port = self.SONAR_PORT
            operation = "ENABLE SONAR" if packet == ENABLE_SONAR_PACKET else "DISABLE SONAR"
        else:
            raise ValueError("unknown sonar control packet")

        prefix = "DRY RUN " if self.dry_run else ""
        self._log(prefix + operation, port, packet)
        if self.dry_run:
            return True
        if port not in self._sockets:
            self.connect()
        sock = self._sockets.get(port)
        if sock is None:
            return False
        try:
            sock.sendall(packet)
            readable, _, _ = select.select([sock], [], [], 0.05)
            if readable:
                ack = sock.recv(4096)
                self._log("ACK" if ack else "CONNECTION CLOSED", port, ack)
                if not ack:
                    sock.close()
                    self._sockets.pop(port, None)
            return True
        except OSError as exc:
            self._log("SEND/ACK EXCEPTION", port, packet, repr(exc))
            try:
                sock.close()
            finally:
                self._sockets.pop(port, None)
            return False

    def set_sonar_enabled(self, enabled: bool) -> bool:
        packet = ENABLE_SONAR_PACKET if enabled else DISABLE_SONAR_PACKET
        if self.send_packet(packet):
            self._accept_sonar_request(enabled)
            return True
        return False

    def _resend_pending_sonar(self, enabled: bool) -> bool:
        packet = ENABLE_SONAR_PACKET if enabled else DISABLE_SONAR_PACKET
        return self.send_packet(packet)

    def toggle_sonar(self) -> bool:
        if self.pending_sonar_enabled is not None:
            return False
        return self.set_sonar_enabled(self.current_sonar_enabled is not True)

    def set_range(self, range_value: RangeValue) -> bool:
        packet = build_range_packet(range_value)
        if self.requested_sonar_enabled is False:
            # Preserve the result of every zoom step in current_range, but
            # only transmit the final target after sonar is enabled. Bursting
            # intermediate range packets can leave the GLS10 rejecting the
            # entire replay and later range changes.
            self._pending_ranges[:] = [range_value]
            self.current_range = range_value
            self._log(
                "QUEUED RANGE",
                self.RANGE_PORT,
                packet,
                detail="Sonar transmission is disabled",
            )
            return True
        if self.send_packet(packet):
            self.current_range = range_value
            return True
        return False

    def _flush_pending_ranges(self) -> None:
        while self._pending_ranges:
            range_value = self._pending_ranges[0]
            if not self.send_packet(build_range_packet(range_value)):
                return
            self.current_range = range_value
            self._pending_ranges.pop(0)

    def zoom_in(self) -> bool:
        if self.current_range is None:
            return False
        index = SUPPORTED_RANGES.index(self.current_range)
        return False if index == 0 else self.set_range(SUPPORTED_RANGES[index - 1])

    def zoom_out(self) -> bool:
        if self.current_range is None:
            return False
        index = SUPPORTED_RANGES.index(self.current_range)
        return False if index == len(SUPPORTED_RANGES) - 1 else self.set_range(SUPPORTED_RANGES[index + 1])


class EmulatorControlClient(SonarStateTracker):
    """Range-control proxy for the viewer embedded in the head-unit emulator."""

    RANGE_APPLY_SECONDS = 1.5
    QUEUED_REPLAY_GRACE_SECONDS = 1.0
    RANGE_RETRY_INTERVAL_SECONDS = 1.0
    RANGE_MAX_ATTEMPTS = 5
    dry_run = False
    supports_sonar = True

    def __init__(self, port: int, initial_range: Optional[RangeValue] = None) -> None:
        if initial_range is not None and initial_range not in RANGE_VALUE_BYTES:
            raise ValueError(f"unsupported sonar range: {initial_range!r}")
        self.current_range = initial_range
        self.current_gain = 21
        # ``initial_range`` is a startup request, not evidence of the GLS10's
        # active setting.  Wait for a frame before showing an actual range.
        self._actual_range: Optional[RangeValue] = None
        self._apply_started_at: Optional[float] = None
        self.reported_range_ft: Optional[float] = None
        self.reported_range: Optional[RangeValue] = None
        self._has_requested_range = False
        self._pending_since: Optional[float] = None
        self._pending_warned = False
        self._reported_at_request: Optional[RangeValue] = None
        self._range_attempts = 0
        self._last_range_attempt_at: Optional[float] = None
        self._pending_ranges: list[RangeValue] = []
        self._init_sonar_state()
        self._address = ("127.0.0.1", port)
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.settimeout(0.75)

    @property
    def connected(self) -> bool:
        return self._socket is not None

    @property
    def pending_range_count(self) -> int:
        return len(self._pending_ranges)

    def _finish_range_application_if_ready(self) -> None:
        if (self._apply_started_at is not None and
                time.monotonic() - self._apply_started_at >= self.RANGE_APPLY_SECONDS):
            self._apply_started_at = None

    @property
    def requested_range(self) -> Optional[RangeValue]:
        return self.current_range

    @property
    def actual_range(self) -> Optional[RangeValue]:
        self._finish_range_application_if_ready()
        return self._actual_range

    @property
    def applying_range(self) -> bool:
        self._finish_range_application_if_ready()
        return self._apply_started_at is not None

    @property
    def range_change_pending(self) -> bool:
        return self._pending_since is not None and not self._pending_warned

    def connect(self) -> bool:
        return self.connected

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def set_sonar_enabled(self, enabled: bool) -> bool:
        if self._socket is None:
            return False
        value = "enable" if enabled else "disable"
        try:
            self._socket.sendto(f"sonar:{value}".encode("ascii"), self._address)
            reply, _ = self._socket.recvfrom(64)
        except OSError:
            print(f"Sonar {value} command did not reach the emulator")
            return False
        if reply != b"OK":
            print(
                f"Sonar {value} command rejected: "
                f"{reply.decode('ascii', 'replace')}"
            )
            return False
        self._accept_sonar_request(enabled)
        print(f"Sonar {value} request accepted; waiting for stream confirmation")
        return True

    def _resend_pending_sonar(self, enabled: bool) -> bool:
        if self._socket is None:
            return False
        value = "enable" if enabled else "disable"
        try:
            self._socket.sendto(f"sonar:{value}".encode("ascii"), self._address)
            reply, _ = self._socket.recvfrom(64)
        except OSError:
            return False
        return reply == b"OK"

    def toggle_sonar(self) -> bool:
        if self.pending_sonar_enabled is not None:
            return False
        return self.set_sonar_enabled(self.current_sonar_enabled is not True)

    def set_range(self, range_value: RangeValue) -> bool:
        if range_value not in RANGE_VALUE_BYTES or self._socket is None:
            return False
        if self.requested_sonar_enabled is False:
            # Zoom steps are reflected in current_range as they happen. Only
            # the final resulting target needs to reach the hardware.
            self._pending_ranges[:] = [range_value]
            self.current_range = range_value
            self._has_requested_range = True
            print(
                f"Range command queued until sonar is enabled: "
                f"{range_value} ft"
            )
            return True
        return self._send_range(range_value)

    def set_gain(self, gain_percent: int) -> bool:
        if (
            isinstance(gain_percent, bool)
            or not isinstance(gain_percent, int)
            or not 0 <= gain_percent <= 100
        ):
            raise ValueError("gain must be an integer from 0 through 100")
        self.current_gain = gain_percent
        print(f"Head-unit sonar gain set to {gain_percent}%")
        return True

    def _send_range(self, range_value: RangeValue) -> bool:
        command = f"range:{range_value}".encode("ascii")
        try:
            self._socket.sendto(command, self._address)
            reply, _ = self._socket.recvfrom(64)
        except OSError:
            print(f"Range command {range_value!r} did not reach the emulator")
            return False
        if reply not in {b"OK", b"QUEUED", b"NOT_READY"}:
            print(f"Range command {range_value!r} rejected: {reply.decode('ascii', 'replace')}")
            return False
        self.current_range = range_value
        self._apply_started_at = time.monotonic()
        self._has_requested_range = True
        self._pending_since = time.monotonic()
        self._last_range_attempt_at = self._pending_since
        self._range_attempts = 1
        self._pending_warned = False
        self._reported_at_request = self.reported_range
        if reply == b"OK":
            print(f"Range command sent to GLS10: {range_value} ft")
        else:
            print(
                f"Range {range_value} ft queued for the GLS10 control session"
            )
        if range_value == self.reported_range:
            print(
                f"GLS10 already reported {range_value} before this command; "
                "this does not confirm that control was accepted"
            )
        return True

    def _flush_pending_ranges(self) -> None:
        replayed = False
        while self._pending_ranges:
            if not self._send_range(self._pending_ranges[0]):
                print(
                    f"{len(self._pending_ranges)} queued range command(s) "
                    "remain pending"
                )
                return
            self._pending_ranges.pop(0)
            replayed = True
        if replayed:
            # The emulator acknowledges a queued range immediately, then
            # transmits it after the GLS10's enable transition has settled.
            grace = self.QUEUED_REPLAY_GRACE_SECONDS
            if self._apply_started_at is not None:
                self._apply_started_at += grace
            if self._pending_since is not None:
                self._pending_since += grace

    def observe_reported_range(self, range_ft: Optional[float]) -> Optional[str]:
        """Track frame range and report a pending command's outcome."""
        if range_ft is None:
            return
        numeric_ranges = tuple(value for value in SUPPORTED_RANGES if isinstance(value, int))
        reported: RangeValue = min(numeric_ranges, key=lambda value: abs(value - range_ft))
        if range_ft < 6.5:
            reported = "min"
        if reported != self.reported_range:
            print(f"GLS10 frame-reported range: {range_ft:.3f} ft (nearest setting: {reported})")
        self.reported_range_ft = range_ft
        self.reported_range = reported

        # Frame metadata is useful for drawing and diagnostics, but the linear
        # estimate becomes inaccurate at longer ranges.  Use it to discover
        # the initial persisted setting only; after this client issues a range
        # command, retain the exact requested setting for subsequent button
        # steps instead of allowing the estimate to jump to (for example) 200.
        if self._pending_since is None and not self._has_requested_range:
            self.current_range = reported
            self._actual_range = reported
            return
        if self._pending_since is None:
            return
        target_applied = (
            (self.current_range == "min" and reported == "min")
            or (
                isinstance(self.current_range, int)
                and reported == self.current_range
            )
        )
        if target_applied:
            self._actual_range = self.current_range
            self._apply_started_at = None
            if self._reported_at_request != self.current_range:
                print(f"GLS10 confirmed requested range change: {self.current_range}")
            self._pending_since = None
            self._last_range_attempt_at = None
            self._range_attempts = 0
            self._reported_at_request = None
            return "confirmed"
        now = time.monotonic()
        if (
            self._last_range_attempt_at is not None
            and now - self._last_range_attempt_at >= self.RANGE_RETRY_INTERVAL_SECONDS
            and self._range_attempts < self.RANGE_MAX_ATTEMPTS
        ):
            self._range_attempts += 1
            self._last_range_attempt_at = now
            command = f"range:{self.current_range}".encode("ascii")
            try:
                self._socket.sendto(command, self._address)
                reply, _ = self._socket.recvfrom(64)
            except OSError:
                reply = b"NOT_READY"
            print(
                f"Range {self.current_range} ft still pending; retrying "
                f"({self._range_attempts}/{self.RANGE_MAX_ATTEMPTS})"
            )
            return "retrying"
        elif (
            not self._pending_warned
            and self._range_attempts >= self.RANGE_MAX_ATTEMPTS
            and self._last_range_attempt_at is not None
            and now - self._last_range_attempt_at >= self.RANGE_RETRY_INTERVAL_SECONDS
        ):
            print(
                f"GLS10 failed to apply requested range {self.current_range}; "
                f"frames still report {range_ft:.3f} ft"
            )
            # The requested value is not the active value when the bounded
            # retry window expires. Restore the last confirmed range so
            # callers (including the web dropdown) do not display a failed
            # request as if it had succeeded.
            self._actual_range = self.reported_range
            self.current_range = self._actual_range
            self._pending_since = None
            self._apply_started_at = None
            self._pending_warned = True
            return "failed"
        return None

    def zoom_in(self) -> bool:
        index = SUPPORTED_RANGES.index(self.current_range)
        return False if index == 0 else self.set_range(SUPPORTED_RANGES[index - 1])

    def zoom_out(self) -> bool:
        index = SUPPORTED_RANGES.index(self.current_range)
        return False if index == len(SUPPORTED_RANGES) - 1 else self.set_range(SUPPORTED_RANGES[index + 1])
