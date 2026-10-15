from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


BRIGHTER_MOTION_COLOR_BGR = np.array((0.0, 0.55, 1.0), dtype=np.float32)
DARKER_MOTION_COLOR_BGR = np.array((1.0, 0.25, 0.0), dtype=np.float32)
DEFAULT_SONAR_GAIN_PERCENT = 21
MOTION_BACKGROUND_SCALE = 0.25


def apply_sonar_gain(
    img: np.ndarray,
    gain_percent: int,
    baseline_percent: int = DEFAULT_SONAR_GAIN_PERCENT,
) -> np.ndarray:
    """Apply head-unit sonar intensity gain while preserving the raw stream."""
    if isinstance(gain_percent, bool) or not isinstance(gain_percent, int):
        raise ValueError("gain must be an integer from 0 through 100")
    if not 0 <= gain_percent <= 100:
        raise ValueError("gain must be an integer from 0 through 100")
    if baseline_percent <= 0:
        raise ValueError("baseline gain must be positive")
    scale = gain_percent / baseline_percent
    return np.clip(img.astype(np.float32) * scale, 0, 255).astype(np.uint8)


def apply_intensity_window(
    image: np.ndarray,
    minimum: int,
    maximum: int,
) -> np.ndarray:
    """Map the selected intensity window to the full display range.

    Values at/below ``minimum`` become black, values at/above ``maximum``
    become white, and values between them are linearly stretched.  This makes
    the two histogram handles conventional black- and white-point controls.
    """
    if (
        isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, (int, np.integer))
        or not isinstance(maximum, (int, np.integer))
        or not 0 <= minimum <= maximum <= 255
    ):
        raise ValueError("intensity window must satisfy 0 <= minimum <= maximum <= 255")
    if minimum == maximum:
        return np.where(image >= maximum, 255, 0).astype(image.dtype)
    scaled = (image.astype(np.float32) - minimum) * (255.0 / (maximum - minimum))
    return np.clip(scaled, 0, 255).astype(image.dtype)


@dataclass
class ViewState:
    warp_enabled: bool

    def toggle(self) -> None:
        self.warp_enabled = not self.warp_enabled
        mode = "warped X/Y" if self.warp_enabled else "raw sonar JPEG"
        print(f"Display mode: {mode}")


@dataclass
class MotionState:
    enabled: bool
    alpha: float
    threshold: int
    gain: float
    background: Optional[np.ndarray] = None

    def toggle(self) -> None:
        self.enabled = not self.enabled
        self.reset_background()
        mode = "on" if self.enabled else "off"
        print(f"Motion filter: {mode}")

    def reset_background(self) -> None:
        """Discard history when the input coordinate system changes."""
        self.background = None

    def adjust_gain(self, delta: float) -> None:
        self.gain = max(0.0, self.gain + delta)
        print(f"Motion gain: {self.gain:.1f}")

    def set_gain(self, gain: float) -> None:
        self.gain = max(0.0, gain)
        print(f"Motion gain: {self.gain:.1f}")

    def apply(self, img: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return img

        motion = self.motion_difference(img)
        if motion is None:
            return np.zeros_like(img)

        return np.clip(np.abs(motion) * self.gain, 0, 255).astype(np.uint8)

    def apply_signed_color(
        self, img: np.ndarray, input_scale: float = 1.0
    ) -> np.ndarray:
        if not self.enabled:
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

        motion = self.motion_difference(img, input_scale=input_scale)
        background = self.background if self.background is not None else img
        context = cv2.cvtColor(
            np.clip(
                background * MOTION_BACKGROUND_SCALE, 0, 255
            ).astype(np.uint8),
            cv2.COLOR_GRAY2BGR,
        )
        if motion is None:
            return context

        magnitude = np.clip(np.abs(motion) * self.gain, 0, 255).astype(np.uint8)
        display = np.zeros((*img.shape, 3), dtype=np.uint8)
        brighter = motion > 0
        darker = motion < 0

        # BGR colors: brighter-than-background is orange, darker is blue.
        display[brighter] = (
            magnitude[brighter].astype(np.float32)[:, None] * BRIGHTER_MOTION_COLOR_BGR
        ).astype(np.uint8)
        display[darker] = (
            magnitude[darker].astype(np.float32)[:, None] * DARKER_MOTION_COLOR_BGR
        ).astype(np.uint8)
        return np.maximum(context, display)

    def motion_difference(
        self, img: np.ndarray, input_scale: float = 1.0
    ) -> Optional[np.ndarray]:
        if not np.isfinite(input_scale) or input_scale < 0:
            raise ValueError("motion input scale must be finite and non-negative")
        current = img.astype(np.float32)
        if self.background is None or self.background.shape != current.shape:
            self.background = current.copy()
            return None

        # Gain both operands equally before subtraction. The background model
        # stays in raw units, so changing display gain cannot contaminate its
        # history or reveal static returns.
        diff = current * input_scale - self.background * input_scale
        cv2.accumulateWeighted(current, self.background, self.alpha)

        magnitude = np.maximum(np.abs(diff) - float(self.threshold), 0.0)
        return np.sign(diff) * magnitude
