from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np


JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MAX_FRAME_BYTES = 16 * 1024 * 1024


@dataclass
class ChunkHeader:
    magic: int
    len_after8: int
    stream_id: int
    frame_id: int
    total_len: int
    offset: int
    chunk_len: int


def parse_chunk_header(payload: bytes) -> Optional[ChunkHeader]:
    if len(payload) < 28:
        return None

    vals = np.frombuffer(payload[:28], dtype="<u4", count=7)
    h = ChunkHeader(*map(int, vals))

    if h.magic != 0x00000904:
        return None
    if not 0 < h.total_len <= MAX_FRAME_BYTES or h.chunk_len <= 0:
        return None
    if h.chunk_len > len(payload) - 28:
        return None
    if h.offset < 0 or h.offset + h.chunk_len > h.total_len + 4096:
        return None

    return h


def find_jpeg_span(data: bytes) -> Optional[Tuple[int, int]]:
    start = data.find(JPEG_SOI)
    if start < 0:
        return None

    end = data.find(JPEG_EOI, start + 2)
    if end < 0:
        return None

    return start, end + 2


def jpeg_dimensions(jpg: bytes) -> Optional[Tuple[int, int]]:
    """Return JPEG width/height from a Start Of Frame marker without decoding."""
    if not jpg.startswith(JPEG_SOI):
        return None
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 3 < len(jpg):
        if jpg[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(jpg) and jpg[offset] == 0xFF:
            offset += 1
        if offset >= len(jpg):
            return None
        marker = jpg[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(jpg):
            return None
        segment_length = int.from_bytes(jpg[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(jpg):
            return None
        if marker in sof_markers:
            if segment_length < 7:
                return None
            height = int.from_bytes(jpg[offset + 3:offset + 5], "big")
            width = int.from_bytes(jpg[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    return None


class SonarFrameAssembler:
    def __init__(self, stream_filter: Optional[int] = 0x00060044, *,
                 max_frames: int = 64, max_bytes: int = 32 * 1024 * 1024,
                 expiry_seconds: float = 5.0, clock=time.monotonic):
        if max_frames < 1 or max_bytes < 1 or expiry_seconds <= 0:
            raise ValueError("Assembler limits must be positive")
        self.stream_filter = stream_filter
        self.parts: Dict[Tuple[int, int, int], Dict[int, bytes]] = {}
        self._created = {}
        self._sizes = {}
        self.buffered_bytes = 0
        self.max_frames, self.max_bytes = max_frames, max_bytes
        self.expiry_seconds, self.clock = expiry_seconds, clock
        self._last_ids = {}

    def _discard(self, key):
        self.parts.pop(key, None)
        self._created.pop(key, None)
        self.buffered_bytes -= self._sizes.pop(key, 0)

    def add_payload(self, payload: bytes) -> Optional[Tuple[int, bytes, bytes]]:
        self.cleanup_old_frames()
        h = parse_chunk_header(payload)
        if h is None:
            return None

        if self.stream_filter is not None and h.stream_id != self.stream_filter:
            return None

        previous_id = self._last_ids.get(h.stream_id)
        if previous_id is not None and previous_id - h.frame_id > 30:
            for old_key in list(self.parts):
                if old_key[0] == h.stream_id:
                    self._discard(old_key)
            self._last_ids[h.stream_id] = h.frame_id
        else:
            self._last_ids[h.stream_id] = max(h.frame_id, previous_id or 0)
        while len(self._last_ids) > self.max_frames:
            del self._last_ids[next(iter(self._last_ids))]

        key = (h.stream_id, h.frame_id, h.total_len)
        if h.chunk_len > self.max_bytes:
            return None
        if key not in self.parts:
            self.parts[key] = {}
            self._created[key] = self.clock()
            self._sizes[key] = 0
        previous = len(self.parts[key].get(h.offset, b""))
        self.parts[key][h.offset] = payload[28:28 + h.chunk_len]
        self._sizes[key] += h.chunk_len - previous
        self.buffered_bytes += h.chunk_len - previous
        while len(self.parts) > self.max_frames or self.buffered_bytes > self.max_bytes:
            self._discard(next(iter(self.parts)))
        if key not in self.parts:
            return None

        # Fast completeness check
        received = self._sizes[key]
        if received < h.total_len:
            return None

        # Check coverage before allocating a full image: overlapping duplicate
        # chunks do not imply completeness and must not trigger large copies.
        covered = 0
        for offset, chunk in sorted(self.parts[key].items()):
            if offset > covered:
                return None
            covered = max(covered, offset + len(chunk))
        if covered < h.total_len:
            return None
        buf = bytearray(h.total_len)

        for offset, chunk in self.parts[key].items():
            end = min(h.total_len, offset + len(chunk))
            if 0 <= offset < h.total_len:
                buf[offset:end] = chunk[:end - offset]

        self._discard(key)
        frame_data = bytes(buf)
        span = find_jpeg_span(frame_data)
        if span is None:
            return None
        start, end = span
        prejpg = frame_data[:start]
        jpg = frame_data[start:end]

        return h.frame_id, jpg, prejpg

    def cleanup_old_frames(self, newest_frame_id: int | None = None) -> None:
        # Age rather than frame-ID ordering also handles device restarts/wrap.
        now = self.clock()
        for key in list(self.parts):
            if now - self._created[key] >= self.expiry_seconds:
                self._discard(key)
