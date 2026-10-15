from __future__ import annotations

from typing import Tuple

import cv2
import numpy as np

from live_viewer.state import MotionState


WINDOW_NAME = "OpenScope · Sonar"
ECHOGRAM_WINDOW_NAME = "OpenScope · Echogram"
CONTROL_BAR_HEIGHT = 176
CONTROL_BAR_MIN_WIDTH = 540
RECORD_BUTTON = (10, 42, 126, 78)
VIEW_BUTTON = (136, 42, 252, 78)
MOTION_BUTTON = (262, 42, 390, 78)
SAVE_FRAME_BUTTON = (400, 42, 526, 78)
GAIN_DOWN_BUTTON = (10, 86, 48, 122)
GAIN_VALUE_RECT = (48, 86, 112, 122)
GAIN_UP_BUTTON = (112, 86, 150, 122)
SONAR_TOGGLE_BUTTON = (160, 86, 424, 122)
SONAR_RECOVERY_BUTTON = (220, 304, 420, 342)
ZOOM_IN_BUTTON = (10, 130, 112, 166)
ZOOM_OUT_BUTTON = (122, 130, 230, 166)
MOTION_GAIN_STEP = 0.5


def make_control_bar(content_width: int) -> np.ndarray:
    """Create the fixed panel displayed above a live viewer."""
    width = max(content_width, CONTROL_BAR_MIN_WIDTH)
    bar = np.full((CONTROL_BAR_HEIGHT, width, 3), (24, 24, 24), dtype=np.uint8)
    cv2.line(
        bar,
        (0, CONTROL_BAR_HEIGHT - 1),
        (width, CONTROL_BAR_HEIGHT - 1),
        (90, 90, 90),
        1,
    )
    return bar


def stack_control_bar(control_bar: np.ndarray, content: np.ndarray) -> np.ndarray:
    """Place a control panel above content without covering the live image."""
    width = max(control_bar.shape[1], content.shape[1])
    if control_bar.shape[1] < width:
        control_bar = cv2.copyMakeBorder(
            control_bar, 0, 0, 0, width - control_bar.shape[1],
            cv2.BORDER_CONSTANT, value=(24, 24, 24),
        )
    if content.shape[1] < width:
        content = cv2.copyMakeBorder(
            content, 0, 0, 0, width - content.shape[1],
            cv2.BORDER_CONSTANT, value=(0, 0, 0),
        )
    return np.vstack((control_bar, content))


def put_centered_text(
    display: np.ndarray,
    text: str,
    rect: Tuple[int, int, int, int],
    font_scale: float,
    thickness: int = 2,
) -> None:
    """Draw text centered inside a rectangle."""
    x1, y1, x2, y2 = rect
    (text_w, text_h), baseline = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness)
    x = x1 + max(0, (x2 - x1 - text_w) // 2)
    y = y1 + max(text_h, (y2 - y1 + text_h) // 2) - baseline // 2
    cv2.putText(
        display,
        text,
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        font_scale,
        (255, 255, 255),
        thickness,
    )


def put_fitted_text(
    display: np.ndarray,
    text: str,
    origin: tuple[int, int],
    max_width: int,
    max_scale: float = 0.44,
    color: tuple[int, int, int] = (255, 255, 255),
) -> None:
    """Draw one status line, shrinking it when the window is narrow."""
    base_width = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 1
    )[0][0]
    scale = min(max_scale, max_width / max(base_width, 1))
    cv2.putText(
        display, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
        max(scale, 0.25), color, 1,
    )


def format_recording_duration(seconds: float) -> str:
    elapsed = max(0.0, seconds)
    total_seconds = int(elapsed)
    minutes, whole_seconds = divmod(total_seconds, 60)
    centiseconds = min(99, int((elapsed - total_seconds) * 100))
    return f"{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def draw_record_button(
    display: np.ndarray,
    recording_enabled: bool,
    recording: bool,
    elapsed_seconds: float | None = None,
) -> None:
    """Draw a simple clickable recording button onto the OpenCV frame."""
    if not recording_enabled:
        return

    x1, y1, x2, y2 = RECORD_BUTTON
    fill = (32, 32, 180) if recording else (40, 120, 40)
    label = (
        format_recording_duration(elapsed_seconds)
        if recording and elapsed_seconds is not None
        else ("STOP REC" if recording else "START REC")
    )

    cv2.rectangle(display, (x1, y1), (x2, y2), fill, thickness=-1)
    cv2.rectangle(display, (x1, y1), (x2, y2), (255, 255, 255), thickness=1)
    put_centered_text(display, label, RECORD_BUTTON, font_scale=0.43)


def draw_save_frame_button(display: np.ndarray, frame_available: bool) -> None:
    """Draw the button used to save the currently displayed frame."""
    x1, y1, x2, y2 = SAVE_FRAME_BUTTON
    fill = (45, 105, 45) if frame_available else (45, 45, 45)
    cv2.rectangle(display, (x1, y1), (x2, y2), fill, thickness=-1)
    cv2.rectangle(display, (x1, y1), (x2, y2), (255, 255, 255), thickness=1)
    put_centered_text(
        display, "SAVE FRAME", SAVE_FRAME_BUTTON,
        font_scale=0.40, thickness=1,
    )


def draw_motion_button(display: np.ndarray, motion_enabled: bool) -> None:
    """Draw a clickable motion/background subtraction toggle."""
    x1, y1, x2, y2 = MOTION_BUTTON
    fill = (32, 90, 150) if motion_enabled else (80, 80, 80)
    label = "MOTION OFF" if motion_enabled else "MOTION ON"

    cv2.rectangle(display, (x1, y1), (x2, y2), fill, thickness=-1)
    cv2.rectangle(display, (x1, y1), (x2, y2), (255, 255, 255), thickness=1)
    put_centered_text(display, label, MOTION_BUTTON, font_scale=0.45)


def draw_gain_buttons(display: np.ndarray, gain: float) -> None:
    """Draw +/- controls for the motion gain."""
    for rect, label in ((GAIN_DOWN_BUTTON, "-"), (GAIN_UP_BUTTON, "+")):
        x1, y1, x2, y2 = rect
        cv2.rectangle(display, (x1, y1), (x2, y2), (70, 70, 70), thickness=-1)
        cv2.rectangle(display, (x1, y1), (x2, y2), (255, 255, 255), thickness=1)
        put_centered_text(display, label, rect, font_scale=0.75)

    put_centered_text(display, f"{gain:.1f}", GAIN_VALUE_RECT, font_scale=0.55)


def prompt_motion_gain(motion: MotionState) -> None:
    """Prompt in the terminal for an exact motion gain value."""
    try:
        value = input(f"Enter motion gain [{motion.gain:.1f}]: ").strip()
    except EOFError:
        print("Motion gain unchanged.")
        return

    if not value:
        print("Motion gain unchanged.")
        return

    try:
        motion.set_gain(float(value))
    except ValueError:
        print(f"Invalid motion gain: {value!r}")


def draw_view_button(display: np.ndarray, warp_enabled: bool) -> None:
    """Draw a clickable raw/warped view toggle onto the OpenCV frame."""
    x1, y1, x2, y2 = VIEW_BUTTON
    fill = (110, 70, 30) if warp_enabled else (80, 80, 80)
    label = "RAW VIEW" if warp_enabled else "WARP VIEW"

    cv2.rectangle(display, (x1, y1), (x2, y2), fill, thickness=-1)
    cv2.rectangle(display, (x1, y1), (x2, y2), (255, 255, 255), thickness=1)
    put_centered_text(display, label, VIEW_BUTTON, font_scale=0.48)


def draw_control_buttons(display: np.ndarray, control) -> None:
    """Draw GLS 10 sonar/range controls and locally tracked state."""
    controls = [
        (ZOOM_IN_BUTTON, "ZOOM IN", control.current_range not in (None, "min")),
        (ZOOM_OUT_BUTTON, "ZOOM OUT", control.current_range not in (None, 200)),
    ]
    if getattr(control, "supports_sonar", True):
        sonar_enabled = getattr(control, "current_sonar_enabled", None)
        sonar_pending = getattr(control, "pending_sonar_enabled", None)
        if sonar_pending is False:
            sonar_label, sonar_action_enabled = "SONAR DISABLING…", False
        elif sonar_pending is True:
            sonar_label, sonar_action_enabled = "SONAR ENABLING…", False
        elif sonar_enabled is True:
            sonar_label, sonar_action_enabled = "DISABLE SONAR", True
        else:
            sonar_label, sonar_action_enabled = "ENABLE SONAR", True
        controls[:0] = [
            (
                SONAR_TOGGLE_BUTTON,
                sonar_label,
                sonar_action_enabled,
            ),
        ]
    for rect, label, enabled in controls:
        x1, y1, x2, y2 = rect
        fill = (45, 105, 45) if enabled else (45, 45, 45)
        cv2.rectangle(display, (x1, y1), (x2, y2), fill, thickness=-1)
        cv2.rectangle(display, (x1, y1), (x2, y2), (255, 255, 255), thickness=1)
        put_centered_text(display, label, rect, font_scale=0.40, thickness=1)

    connection = "DRY RUN" if control.dry_run else ("CONNECTED" if control.connected else "DISCONNECTED")
    requested_value = getattr(control, "requested_range", control.current_range)
    actual_value = getattr(control, "actual_range", requested_value)
    requested = "UNKNOWN" if requested_value is None else ("MIN" if requested_value == "min" else f"{requested_value} ft")
    actual = "UNKNOWN" if actual_value is None else ("MIN" if actual_value == "min" else f"{actual_value} ft")
    applying = bool(getattr(control, "applying_range", False))
    pending_ranges = int(getattr(control, "pending_range_count", 0))
    status_x = 240
    status_width = max(1, display.shape[1] - status_x - 10)
    put_fitted_text(
        display, f"Requested: {requested} | Actual: {actual}",
        (status_x, 137), status_width,
    )
    put_fitted_text(
        display,
        (
            f"QUEUED UNTIL SONAR ENABLED ({pending_ranges})"
            if pending_ranges
            else "APPLYING RANGE..." if applying
            else f"Range settled | {connection}"
        ),
        (status_x, 155), status_width,
        color=(0, 220, 255) if applying or pending_ranges else (255, 255, 255),
    )


def draw_sonar_recovery_button(display: np.ndarray) -> None:
    """Draw the explicit sonar-enable action used before the first frame."""
    x1, y1, x2, y2 = SONAR_RECOVERY_BUTTON
    cv2.rectangle(display, (x1, y1), (x2, y2), (45, 105, 45), thickness=-1)
    cv2.rectangle(
        display, (x1, y1), (x2, y2), (255, 255, 255), thickness=1
    )
    put_centered_text(
        display, "ENABLE SONAR", SONAR_RECOVERY_BUTTON,
        font_scale=0.46, thickness=1,
    )
