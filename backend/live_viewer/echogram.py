from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np


@dataclass
class EchogramState:
    width: int
    height: int
    background_alpha: float = 0.04
    threshold: float = 0.0
    gain: float = 1.0
    brightness_power: float = 1.5
    background_warmup_frames: int = 64
    image: Optional[np.ndarray] = None
    color_image: Optional[np.ndarray] = None
    background: Optional[np.ndarray] = None
    full_coverage_boundary: Optional[float] = None
    _warmup_frames: Optional[list[np.ndarray]] = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        self.width = max(1, self.width)
        self.height = max(1, self.height)
        self.image = np.zeros((self.height, self.width), dtype=np.uint8)
        self.color_image = np.zeros((self.height, self.width, 3), dtype=np.uint8)

    def add_frame(self, warped_img: np.ndarray) -> np.ndarray:
        """Append brightness/angle columns from a background-subtracted warp."""
        if self.image is None:
            self.image = np.zeros((self.height, self.width), dtype=np.uint8)
        if self.color_image is None:
            self.color_image = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        current = warped_img.astype(np.float32)
        if self.background is None or self.background.shape != current.shape:
            self.background = current.copy()
            self._warmup_frames = []
            self.image.fill(0)
            self.color_image.fill(0)

        if self._warmup_frames is not None:
            self._warmup_frames.append(current.copy())
            warmup_limit = max(1, self.background_warmup_frames)
            estimate = np.mean(self._warmup_frames, axis=0, dtype=np.float32)
            self._rebuild_warmup_history(estimate)
            if len(self._warmup_frames) >= warmup_limit:
                self.background = estimate
                self._warmup_frames = None
            return self.image.copy()

        difference = np.clip(current - self.background - self.threshold, 0, 255)
        cv2.accumulateWeighted(
            current, self.background,
            float(np.clip(self.background_alpha, 0, 1)),
        )
        self._append_difference_column(difference)
        return self.image.copy()

    def _rebuild_warmup_history(self, background: np.ndarray) -> None:
        """Recalculate calibration columns against the current mean baseline."""
        assert self.image is not None and self.color_image is not None
        self.image.fill(0)
        self.color_image.fill(0)
        frames = (self._warmup_frames or [])[-self.width:]
        start = self.width - len(frames)
        for index, frame in enumerate(frames):
            self._write_difference_column(
                start + index,
                np.clip(frame - background - self.threshold, 0, 255),
            )

    def _append_difference_column(self, difference: np.ndarray) -> None:
        assert self.image is not None and self.color_image is not None
        self.image[:, :-1] = self.image[:, 1:]
        self.color_image[:, :-1, :] = self.color_image[:, 1:, :]
        self._write_difference_column(self.width - 1, difference)

    def _write_difference_column(self, column_index: int, difference: np.ndarray) -> None:
        """Reduce one range/theta difference image into an echogram column."""
        assert self.image is not None and self.color_image is not None
        brightness = np.max(difference, axis=1).astype(np.uint8)
        angle = np.argmax(difference, axis=1).astype(np.float32)
        angle /= max(difference.shape[1] - 1, 1)
        column = cv2.resize(brightness[:, None], (1, self.height), interpolation=cv2.INTER_AREA)[:, 0]
        angle_column = cv2.resize(angle[:, None], (1, self.height), interpolation=cv2.INTER_NEAREST)[:, 0]
        color_column = self._rdbu_colors_bgr(angle_column)
        self.image[:, column_index] = column
        self.color_image[:, column_index, :] = color_column

    def render(self, intensity: Optional[np.ndarray] = None) -> np.ndarray:
        """Render the complete history at the current display gain."""
        if self.image is None:
            self.image = np.zeros((self.height, self.width), dtype=np.uint8)
        if self.color_image is None:
            self.color_image = np.zeros((self.height, self.width, 3), dtype=np.uint8)

        value = self.image if intensity is None else intensity
        scale = value.astype(np.float32)[..., None]
        peak = float(np.max(scale))
        if peak > 0:
            scale /= peak
        scale *= max(0.0, self.gain)
        scale = np.clip(scale, 0.0, 1.0)
        scale = np.power(scale, max(0.0, self.brightness_power))
        rendered = np.clip(
            self.color_image.astype(np.float32) * scale, 0, 255
        ).astype(np.uint8)
        if self.full_coverage_boundary is not None:
            row = int(round(self.full_coverage_boundary * (self.height - 1)))
            # The line sits immediately before the first center-beam row.
            row = max(0, row - 1)
            cv2.line(rendered, (0, row), (self.width - 1, row), (180, 255, 180), 1)
        return rendered

    def crop_at_range_boundary(self, rendered: np.ndarray) -> np.ndarray:
        """Return only rows below the full-coverage marker line."""
        if self.full_coverage_boundary is None:
            return rendered
        row = int(round(self.full_coverage_boundary * (self.height - 1)))
        start = min(max(0, rendered.shape[0] - 1), row + 1)
        return rendered[start:].copy()

    @staticmethod
    def _rdbu_colors_bgr(angle: np.ndarray) -> np.ndarray:
        """Color normalized lateral position with Matplotlib-like RdBu stops."""
        stops_rgb = np.array([
            (103, 0, 31), (178, 24, 43), (214, 96, 77),
            (244, 165, 130), (253, 219, 199), (247, 247, 247),
            (209, 229, 240), (146, 197, 222), (67, 147, 195),
            (33, 102, 172), (5, 48, 97),
        ], dtype=np.float32)
        position = np.clip(angle, 0.0, 1.0) * (len(stops_rgb) - 1)
        left = np.floor(position).astype(int)
        right = np.minimum(left + 1, len(stops_rgb) - 1)
        fraction = (position - left)[..., None]
        rgb = stops_rgb[left] * (1.0 - fraction) + stops_rgb[right] * fraction
        return np.clip(rgb[..., ::-1], 0, 255).astype(np.uint8)
