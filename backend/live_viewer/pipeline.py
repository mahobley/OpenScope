"""Continuous capture with a bounded, latest-frame-only display handoff."""

import threading
import time
from dataclasses import dataclass

import numpy as np
from packet_io import AsyncCapture, CaptureError
from live_viewer.permissions import drop_invoking_user_privileges


@dataclass(frozen=True)
class CapturedFrame:
    frame_id: int
    image: np.ndarray
    prejpg: bytes
    captured_at: float
    capture_fps: float


class LatestFrame:
    """Only display frames may be replaced; saving happens before this handoff."""

    def __init__(self):
        self._condition = threading.Condition()
        self._frame = None
        self.skipped = 0

    def publish(self, frame: CapturedFrame) -> None:
        with self._condition:
            if self._frame is not None:
                self.skipped += 1
            self._frame = frame
            self._condition.notify()

    def take(self, timeout: float = 0.03) -> CapturedFrame | None:
        with self._condition:
            if self._frame is None:
                self._condition.wait_for(lambda: self._frame is not None, timeout)
            frame, self._frame = self._frame, None
            return frame


class CaptureLoop:
    """Own one persistent capture socket and finish its callback before closing."""

    def __init__(self, callback, *, iface, bpf, sniffer_factory=AsyncCapture, drop_privileges=False):
        self._ready = threading.Event()
        self._permissions_ready = threading.Event()
        self._drop_privileges = drop_privileges
        self._closing = threading.Event()
        self._failure = None

        def receive(packet):
            self._permissions_ready.wait()
            if self._closing.is_set():
                return
            try:
                callback(packet)
            except Exception as exc:
                # Preserve callback failures even when capture is stopped.
                self._failure = exc
                self._sniffer.stop(join=False)

        self._sniffer = sniffer_factory(
            iface=iface, filter=bpf, prn=receive, store=False,
            started_callback=self._ready.set,
        )

    def start(self, timeout: float = 10.0) -> None:
        self._sniffer.start()
        deadline = time.monotonic() + timeout
        while not self._ready.wait(0.02):
            self.check()
            if time.monotonic() >= deadline:
                raise CaptureError("Timed out opening the packet capture socket")
        self.check()
        try:
            if self._drop_privileges:
                drop_invoking_user_privileges()
        except (OSError, KeyError, ValueError) as exc:
            self._closing.set()
            raise CaptureError(f"Could not switch recording permissions to the desktop user: {exc}") from exc
        finally:
            self._permissions_ready.set()

    def check(self) -> None:
        failure = self._failure or self._sniffer.exception
        if failure is not None:
            raise CaptureError(f"Capture worker failed: {failure}") from failure
        if self._sniffer.thread is not None and not self._sniffer.thread.is_alive():
            raise CaptureError("Packet capture stopped unexpectedly")

    def close(self) -> None:
        self._closing.set()
        self._permissions_ready.set()
        if isinstance(self._sniffer, AsyncCapture) or (self._sniffer.running and self._ready.is_set()):
            try:
                self._sniffer.stop(join=False)
            except CaptureError:
                # A socket may have ended between the running check and stop.
                if self._sniffer.thread.is_alive():
                    raise
        # Wait for an in-progress raw save before releasing its video writer.
        if self._sniffer.thread is not None:
            self._sniffer.thread.join(timeout=None if self._ready.is_set() else 1.0)
