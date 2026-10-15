from __future__ import annotations

import time
import threading
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

import cv2
import numpy as np

from live_viewer.permissions import make_user_editable
from live_viewer.outputs import unique_file, write_image_atomic


def make_video_writer(path: Path, fps: float, frame_size: Tuple[int, int]) -> cv2.VideoWriter:
    """Create an OpenCV video writer, preferring low-loss codecs when possible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".avi":
        codecs = ("FFV1", "HFYU", "MJPG")
    elif suffix in {".mkv"}:
        codecs = ("FFV1", "MJPG")
    elif suffix in {".mp4", ".m4v"}:
        codecs = ("avc1", "H264", "mp4v")
    else:
        codecs = ("FFV1", "MJPG", "mp4v")

    for codec in codecs:
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), fps, frame_size)
        if writer.isOpened():
            print(f"Video codec for {path}: {codec}")
            return writer

    raise RuntimeError(f"could not open video writer for {path} with codecs: {', '.join(codecs)}")


def video_clip_path(base_path: Path, clip_index: int) -> Path:
    """Return the path for a recording clip, preserving the exact first path."""
    if clip_index == 1:
        return base_path
    suffix = base_path.suffix or ".mp4"
    return base_path.with_name(f"{base_path.stem}_{clip_index:03d}{suffix}")


def echogram_image_path(base_path: Path, image_index: int) -> Path:
    """Return a PNG path for one accumulated echogram image."""
    png_path = base_path.with_suffix(".png")
    if image_index == 1:
        return png_path
    return png_path.with_name(f"{png_path.stem}_{image_index:03d}.png")


def recording_locked(method):
    """Serialize writer lifecycle changes against the capture thread."""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return locked


@dataclass
class RecordingState:
    base_path: Path
    fps: float
    label: str
    auto_segment_seconds: Optional[float] = None
    writer: Optional[cv2.VideoWriter] = None
    active_path: Optional[Path] = None
    writer_path: Optional[Path] = None
    frame_size: Optional[Tuple[int, int]] = None
    segment_started_at: Optional[float] = None
    next_video_frame_at: Optional[float] = None
    clip_index: int = 0
    autosaved_clips: int = 0
    requested: bool = False
    video_enabled: bool = True
    png_enabled: bool = True
    active: bool = False
    lock: object = field(default_factory=threading.RLock, init=False, repr=False)

    @recording_locked
    def elapsed_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """Return elapsed wall time for the active recording segment."""
        if not self.active or self.segment_started_at is None:
            return None
        return max(0.0, (time.time() if now is None else now) - self.segment_started_at)

    @recording_locked
    def toggle_requested(self) -> None:
        if self.active:
            self.requested = False
            self.stop()
            return
        self.requested = not self.requested

    @recording_locked
    def finish(self) -> Optional[str]:
        """Stop recording without allowing output failures to end capture."""
        self.requested = False
        self.auto_segment_seconds = None
        try:
            self.stop()
        except (OSError, RuntimeError, ValueError, cv2.error) as exc:
            return f"{self.label} finalization failed: {exc}. The unfinished video is retained."
        return None

    @recording_locked
    def sync(self, frame_size: Tuple[int, int]) -> None:
        now = time.time()
        auto_active = self.auto_segment_seconds is not None and self.auto_segment_seconds > 0

        if auto_active and self.active and self.segment_started_at is not None:
            if now - self.segment_started_at >= self.auto_segment_seconds:
                self.stop()
                self.autosaved_clips += 1

        if (self.requested or auto_active) and not self.active:
            self.start(frame_size, now)
        elif not self.requested and not auto_active and self.active:
            self.stop()

    @recording_locked
    def start(self, frame_size: Tuple[int, int], started_at: Optional[float] = None) -> None:
        if not self.video_enabled and not self.png_enabled:
            raise ValueError("Select video, PNGs with metadata, or both")
        self.clip_index += 1
        self.active_path = unique_file(video_clip_path(self.base_path, self.clip_index))
        self.frame_size = frame_size
        self.segment_started_at = time.time() if started_at is None else started_at
        self.next_video_frame_at = self.segment_started_at
        if self.video_enabled:
            self.writer_path = unique_file(self.active_path.with_name(
                f"{self.active_path.stem}.inprogress{self.active_path.suffix}"
            ))
            self.writer = make_video_writer(self.writer_path, self.fps, frame_size)
            make_user_editable(self.active_path.parent)
            make_user_editable(self.writer_path)
        self.active = True
        print(
            f"Recording {self.label} to {self.active_path}"
            if self.video_enabled else f"Recording {self.label} as PNGs with metadata"
        )

    @recording_locked
    def write(self, frame: np.ndarray) -> None:
        if self.writer is None:
            return

        if self.frame_size is not None:
            width, height = self.frame_size
            if frame.shape[1] != width or frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)

        now = time.time()
        frame_interval = 1.0 / max(self.fps, 0.001)
        if self.next_video_frame_at is None:
            self.next_video_frame_at = now

        # OpenCV writes constant-FPS video. Fill the timeline with repeats so
        # playback duration tracks real capture time when incoming FPS is lower.
        max_fill_frames = max(1, int(self.fps * 2))
        written = 0
        while self.next_video_frame_at <= now and written < max_fill_frames:
            self.writer.write(frame)
            self.next_video_frame_at += frame_interval
            written += 1

        if self.next_video_frame_at < now:
            self.next_video_frame_at = now + frame_interval

    @recording_locked
    def record(self, frame: np.ndarray) -> Optional[str]:
        """Record one frame, disabling sonar recording if encoding fails."""
        try:
            self.sync((frame.shape[1], frame.shape[0]))
            self.write(frame)
        except (OSError, RuntimeError, ValueError, cv2.error) as exc:
            self.requested = False
            self.auto_segment_seconds = None
            try:
                self.stop()
            except (OSError, RuntimeError, cv2.error):
                pass
            self.writer = None
            self.active = False
            self.frame_size = None
            self.segment_started_at = None
            self.next_video_frame_at = None
            return f"{self.label} recording failed: {exc}"
        return None

    @recording_locked
    def stop(self) -> None:
        if not self.active and self.writer is None:
            return
        writer, self.writer = self.writer, None
        video_was_active = writer is not None
        self.active = False
        self.frame_size = None
        self.segment_started_at = None
        self.next_video_frame_at = None
        if writer is not None:
            writer.release()
            if self.writer_path is not None and self.writer_path.exists():
                self.active_path = unique_file(self.active_path)
                self.writer_path.replace(self.active_path)
            if self.active_path is not None:
                make_user_editable(self.active_path)
        self.writer_path = None
        print(f"Video saved: {self.active_path}" if video_was_active else "PNG recording finished")

    @recording_locked
    def set_formats(self, video: bool, png: bool) -> None:
        if type(video) is not bool or type(png) is not bool or not (video or png):
            raise ValueError("Select video, PNGs with metadata, or both")
        if self.active or self.requested or self.auto_segment_seconds is not None:
            raise ValueError("Stop sonar recording and autosave before changing formats")
        self.video_enabled, self.png_enabled = video, png


@dataclass
class EchogramImageRecordingState:
    """Accumulate one rendered echogram column per sonar frame into a PNG."""

    base_path: Path
    label: str = "echogram image"
    auto_segment_seconds: Optional[float] = None
    active_path: Optional[Path] = None
    segment_started_at: Optional[float] = None
    clip_index: int = 0
    autosaved_clips: int = 0
    requested: bool = False
    active: bool = False
    columns: Optional[list[np.ndarray]] = None
    max_buffer_bytes: int = 32 * 1024 * 1024
    max_columns: int = 8192
    buffered_bytes: int = 0

    @property
    def writer(self) -> object | None:
        """Match the video recording state interface used by the viewer."""
        return self if self.active else None

    def elapsed_seconds(self, now: Optional[float] = None) -> Optional[float]:
        if not self.active or self.segment_started_at is None:
            return None
        return max(
            0.0,
            (time.time() if now is None else now) - self.segment_started_at,
        )

    def toggle_requested(self) -> Optional[str]:
        if self.active:
            self.requested = False
            return self.stop()
        self.requested = not self.requested
        return None

    def sync(self) -> Optional[str]:
        now = time.time()
        auto_active = (
            self.auto_segment_seconds is not None
            and self.auto_segment_seconds > 0
        )
        if (
            auto_active
            and self.active
            and self.segment_started_at is not None
            and now - self.segment_started_at >= self.auto_segment_seconds
        ):
            error = self.stop()
            if error is not None:
                self.requested = False
                self.auto_segment_seconds = None
                return error
            self.autosaved_clips += 1
        if (self.requested or auto_active) and not self.active:
            self.start(now)
        elif not self.requested and not auto_active and self.active:
            return self.stop()
        return None

    def start(self, started_at: Optional[float] = None) -> None:
        self.clip_index += 1
        self.active_path = unique_file(echogram_image_path(self.base_path, self.clip_index))
        self.segment_started_at = (
            time.time() if started_at is None else started_at
        )
        self.columns = []
        self.buffered_bytes = 0
        self.active = True
        print(f"Capturing {self.label} to {self.active_path}")

    def record(self, frame: np.ndarray) -> Optional[str]:
        """Append the newest echogram column, saving a PNG when stopped."""
        try:
            sync_error = self.sync()
            if sync_error is not None:
                return sync_error
            if self.active:
                if frame.ndim != 3 or frame.shape[1] < 1:
                    raise ValueError("echogram frame must be a color image")
                assert self.columns is not None
                column = frame[:, -1:, :]
                if column.nbytes > self.max_buffer_bytes:
                    raise ValueError("echogram column exceeds the recording memory limit")
                if self.columns and (
                    self.buffered_bytes + column.nbytes > self.max_buffer_bytes
                    or len(self.columns) >= self.max_columns
                    or self.columns[0].shape != column.shape
                ):
                    error = self.stop()
                    if error is not None:
                        self.requested = False
                        self.auto_segment_seconds = None
                        return error
                    self.autosaved_clips += 1
                    self.start()
                self.columns.append(column.copy())
                self.buffered_bytes += column.nbytes
        except (OSError, RuntimeError, ValueError, cv2.error) as exc:
            self.requested = False
            self.auto_segment_seconds = None
            self.active = False
            self.columns = None
            self.buffered_bytes = 0
            self.segment_started_at = None
            return f"{self.label} capture failed: {exc}"
        return None

    def stop(self) -> Optional[str]:
        if not self.active:
            return None
        output = self.active_path
        columns = self.columns or []
        self.active = False
        self.columns = None
        self.buffered_bytes = 0
        self.segment_started_at = None
        if output is None or not columns:
            return None
        try:
            image = np.concatenate(columns, axis=1)
            output = write_image_atomic(output, image)
        except (OSError, RuntimeError, cv2.error) as exc:
            self.requested = False
            self.auto_segment_seconds = None
            return f"{self.label} capture failed: {exc}"
        print(f"Echogram PNG saved: {output}")
        return None
