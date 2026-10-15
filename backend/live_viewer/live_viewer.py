#!/usr/bin/env python3
"""
Live GLS 10 / Sonar JPEG viewer.

What it does:
  - Listens for sonar UDP chunk packets on a network interface.
  - Reassembles chunks from stream 0x00060044 into frames.
  - Extracts the embedded JPEG.
  - Displays it live with OpenCV.
  - Optional: saves JPEG frames to disk.
  - Optional: records the displayed view to video.

Install:
  pip install dpkt opencv-python numpy

Run from the backend directory:
  sudo python3 -m live_viewer.live_viewer --iface en0

Notes:
  - On macOS/Linux, packet sniffing usually requires sudo.
  - Connect the GLS 10 to the computer's selected network adapter.
  - If the stream id differs, try --stream all first.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import platform
import queue
import signal
import threading
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

if __package__ in {None, ""}:
    # Retain direct-script compatibility while using unambiguous package imports.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packet_io import CaptureError

from live_viewer.echogram import EchogramState
from live_viewer.pipeline import CapturedFrame, CaptureLoop, LatestFrame
from live_viewer.control import (
    EmulatorControlClient,
    SonarControlClient,
    RANGE_VALUE_BYTES,
)
from live_viewer.outputs import (
    RANGE_SCALE_OFFSET,
    DiskSpaceGuard,
    write_image_atomic,
    append_timestamp_to_path,
    choose_save_root,
    prompt_output_path,
    estimate_selected_range_feet,
    feet_to_meters,
    read_float32_from_prejpg,
    safe_name,
    save_decoded_frame,
    intensity_histogram,
    preflight_output_paths,
    under_save_root,
    unique_dir,
    unique_file,
)
from live_viewer.packet_logger import (
    OtherPacketLogger,
    extract_roll_degrees,
    extract_temperature_c,
    extract_tilt_degrees,
    relative_angle_degrees,
    udp_payload,
)
from live_viewer.permissions import make_user_editable
from live_viewer.packets import SonarFrameAssembler, parse_chunk_header
from live_viewer.recording import EchogramImageRecordingState, RecordingState
from live_viewer.rate import RecentFrameRate
from live_viewer.state import (
    MotionState,
    ViewState,
    apply_intensity_window,
    apply_sonar_gain,
)
from live_viewer.ui import (
    CONTROL_BAR_HEIGHT,
    ECHOGRAM_WINDOW_NAME,
    GAIN_DOWN_BUTTON,
    GAIN_UP_BUTTON,
    GAIN_VALUE_RECT,
    MOTION_BUTTON,
    MOTION_GAIN_STEP,
    RECORD_BUTTON,
    SAVE_FRAME_BUTTON,
    VIEW_BUTTON,
    WINDOW_NAME,
    SONAR_TOGGLE_BUTTON,
    SONAR_RECOVERY_BUTTON,
    ZOOM_IN_BUTTON,
    ZOOM_OUT_BUTTON,
    draw_control_buttons,
    draw_gain_buttons,
    draw_motion_button,
    draw_record_button,
    draw_save_frame_button,
    draw_sonar_recovery_button,
    draw_view_button,
    make_control_bar,
    prompt_motion_gain,
    stack_control_bar,
)
from live_viewer.view import (
    echogram_full_coverage_boundary,
    COLOR_SCHEMES,
    add_polar_grid_and_outer_data,
    cartesian_vertical_crop,
    crop_cartesian_to_selected_range,
    crop_raw_view_to_selected_range,
    add_raw_range_scale,
    colorize_for_cv2,
    polar_image_to_xy,
    prepare_raw_view_and_record_frame,
    rotate_raw_view,
)
from live_viewer.web import BrowserViewerServer


def is_image_chunk(payload: bytes, stream_filter: Optional[int]) -> bool:
    header = parse_chunk_header(payload)
    if header is None:
        return False
    return stream_filter is None or header.stream_id == stream_filter


def decode_packet_frame(payload: bytes, assembler: SonarFrameAssembler) -> Optional[tuple[int, np.ndarray, bytes]]:
    result = assembler.add_payload(payload)
    if result is None:
        return None

    frame_id, jpg, prejpg = result
    arr = np.frombuffer(jpg, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None

    return frame_id, img, prejpg


def update_echogram_window(
    img: np.ndarray,
    geometry_img: np.ndarray,
    prejpg: bytes,
    echogram: EchogramState,
    echogram_recording: EchogramImageRecordingState,
    args: argparse.Namespace,
    control=None,
    show_window: bool = True,
    crop_at_range: bool = False,
) -> tuple[Optional[np.ndarray], Optional[str]]:
    # An echogram is range vs. time, so its rows must come directly from the
    # original range/theta raster.  Reducing rows of the Cartesian X/Y warp
    # loses off-axis returns and does not preserve one row per range bin.
    range_theta = rotate_raw_view(img)
    if args.flip_theta:
        range_theta = range_theta[:, ::-1]
    if args.flip_range:
        range_theta = range_theta[::-1, :]
    echogram.full_coverage_boundary = echogram_full_coverage_boundary(
        geometry_img, args.flip_theta, args.flip_range
    )
    full_display = echogram.render(echogram.add_frame(range_theta))
    recording_error = echogram_recording.record(full_display)
    echogram_display = (
        echogram.crop_at_range_boundary(full_display)
        if crop_at_range else full_display
    )
    if show_window:
        control_bar = make_control_bar(echogram_display.shape[1])
        draw_record_button(
            control_bar,
            recording_enabled=True,
            recording=echogram_recording.active,
            elapsed_seconds=echogram_recording.elapsed_seconds(),
        )
        cv2.imshow(
            ECHOGRAM_WINDOW_NAME,
            stack_control_bar(control_bar, echogram_display),
        )
    return echogram_display, recording_error


def render_main_display(
    img: np.ndarray,
    geometry_img: np.ndarray,
    prejpg: bytes,
    raw_view_img: np.ndarray,
    view: ViewState,
    motion: MotionState,
    args: argparse.Namespace,
    frame_id: int,
    control=None,
    distance_grid_enabled: bool = True,
    sonar_gain_percent: int = 21,
    crop_at_range: bool = False,
) -> Optional[np.ndarray]:
    selected_range_m = control_range_meters(control, args.max_range)
    if view.warp_enabled:
        try:
            warped = polar_image_to_xy(
                geometry_img if motion.enabled else img,
                prejpg,
                geometry_img=geometry_img,
                out_width=args.width,
                out_height=args.height,
                max_range=selected_range_m,
                range_offset_bins=args.range_offset_bins,
                theta_offset_deg=args.theta_offset_deg,
                flip_theta=args.flip_theta,
                flip_range=args.flip_range,
                forward_is_up=not args.forward_down,
                output_scale=args.warp_output_scale,
                interpolation_mode=args.warp_interpolation_mode,
            )
        except ValueError as exc:
            print(f"frame {frame_id}: cannot warp: {exc}")
            return None
        if motion.enabled:
            display = motion.apply_signed_color(
                warped, input_scale=sonar_gain_percent / 21.0
            )
        else:
            display = colorize_for_cv2(warped, args.colorscheme)
        try:
            display = add_polar_grid_and_outer_data(
                display,
                img,
                prejpg,
                geometry_img=geometry_img,
                max_range=selected_range_m,
                theta_offset_deg=args.theta_offset_deg,
                flip_theta=args.flip_theta,
                flip_range=args.flip_range,
                forward_is_up=not args.forward_down,
                grid_spacing_ft=args.grid_spacing_ft,
                color_scheme=args.colorscheme,
                show_grid=distance_grid_enabled,
            )
            # Crop after drawing the grid so its physical positions are kept.
            # The known polar Y extent avoids scanning Cartesian returns and
            # keeps the height stable through sparse or empty frames.
            display = display[cartesian_vertical_crop(
                geometry_img, display.shape[0], args.flip_theta,
                args.flip_range, not args.forward_down, args.range_offset_bins,
                args.warp_interpolation_mode,
            ), :]
            return crop_cartesian_to_selected_range(
                display, geometry_img, args.flip_theta, args.flip_range
            ) if crop_at_range else display
        except ValueError as exc:
            print(f"frame {frame_id}: cannot draw warp grid: {exc}")
            return None

    if motion.enabled:
        display = motion.apply_signed_color(
            rotate_raw_view(geometry_img),
            input_scale=sonar_gain_percent / 21.0,
        )
    else:
        display = colorize_for_cv2(raw_view_img, args.colorscheme)
    if not distance_grid_enabled:
        return display
    try:
        display = add_raw_range_scale(
            display,
            raw_view_img,
            prejpg,
            max_range=selected_range_m,
            spacing_ft=args.grid_spacing_ft,
            geometry_img=geometry_img,
        )
        return crop_raw_view_to_selected_range(display, geometry_img) if crop_at_range else display
    except ValueError as exc:
        print(f"frame {frame_id}: cannot draw raw range scale: {exc}")
        return crop_raw_view_to_selected_range(display, geometry_img) if crop_at_range else display


def control_range_meters(control, configured_max_range: Optional[float]) -> Optional[float]:
    """Prefer the exact selected control setting over the metadata estimate."""
    if configured_max_range is not None:
        return configured_max_range
    actual_range = getattr(control, "actual_range", None) if control is not None else None
    if isinstance(actual_range, int):
        return feet_to_meters(float(actual_range))
    return None


def record_frame(recording: RecordingState, frame: np.ndarray) -> Optional[str]:
    return recording.record(frame)


def write_snapshot(output: Path, frame: np.ndarray) -> None:
    """Write a snapshot or raise a recoverable output error."""
    write_image_atomic(output, frame)


def draw_main_overlays(
    control_bar: np.ndarray,
    frame_id: int,
    fps: float,
    recording: RecordingState,
    view: ViewState,
    motion: MotionState,
    control: Optional[SonarControlClient],
) -> None:
    cv2.putText(
        control_bar,
        f"Frame {frame_id} | {fps:.1f} capture FPS",
        (10, 24),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
    )
    draw_record_button(
        control_bar,
        recording_enabled=True,
        recording=recording.active,
        elapsed_seconds=recording.elapsed_seconds(),
    )
    draw_view_button(control_bar, warp_enabled=view.warp_enabled)
    draw_motion_button(control_bar, motion_enabled=motion.enabled)
    draw_save_frame_button(control_bar, frame_available=True)
    draw_gain_buttons(control_bar, gain=motion.gain)
    if control is not None:
        draw_control_buttons(control_bar, control)


def compose_main_window(
    display: np.ndarray,
    frame_id: int,
    fps: float,
    recording: RecordingState,
    view: ViewState,
    motion: MotionState,
    control: Optional[SonarControlClient],
) -> np.ndarray:
    """Render controls above, rather than on top of, the live viewer."""
    control_bar = make_control_bar(display.shape[1])
    draw_main_overlays(
        control_bar, frame_id, fps, recording, view, motion, control
    )
    return stack_control_bar(control_bar, display)


def render_no_signal_display(seconds_since_frame: float, waiting_for_first_frame: bool) -> np.ndarray:
    display = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.rectangle(display, (28, 70), (612, 282), (32, 32, 32), thickness=-1)
    cv2.rectangle(display, (28, 70), (612, 282), (0, 165, 255), thickness=2)

    title = "NO INCOMING SONAR SIGNAL"
    detail = "No decoded frames received yet." if waiting_for_first_frame else f"No decoded frames for {seconds_since_frame:.1f} seconds."
    hint = "Re-establishing network connection; check power, Ethernet, and adapter."
    quit_hint = "Press q to quit. Waiting for frames..."

    for text, y, scale, color, thickness in (
        (title, 128, 0.78, (0, 165, 255), 2),
        (detail, 174, 0.58, (255, 255, 255), 1),
        (hint, 215, 0.45, (210, 210, 210), 1),
        (quit_hint, 250, 0.45, (180, 180, 180), 1),
    ):
        (text_w, _), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
        cv2.putText(
            display,
            text,
            (max(20, (display.shape[1] - text_w) // 2), y),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            thickness,
        )

    return display


def draw_frozen_frame_status(
    display: np.ndarray,
    seconds_since_frame: float,
    sonar_disabled: bool,
) -> None:
    """Mark a retained image, with prominent bands when sonar is disabled."""
    label = (
        "SONAR DISABLED - LAST RECEIVED FRAME"
        if sonar_disabled
        else f"NO NEW SONAR FRAME ({seconds_since_frame:.1f}s) - LAST FRAME"
    )
    band_height = min(38, max(24, display.shape[0] // 8))
    bands = (0, display.shape[0] - band_height) if sonar_disabled else (0,)
    background = (0, 105, 255) if sonar_disabled else (25, 25, 25)
    foreground = (255, 255, 255) if sonar_disabled else (0, 210, 255)
    for top in bands:
        cv2.rectangle(
            display,
            (0, top),
            (display.shape[1], top + band_height),
            background,
            thickness=-1,
        )
        (text_width, _), _ = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.53, 2
        )
        cv2.putText(
            display,
            label,
            (max(8, (display.shape[1] - text_width) // 2), top + 25),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.53,
            foreground,
            2,
        )


def prompt_control_range(control: SonarControlClient) -> None:
    try:
        value = input(f"Set range {tuple(RANGE_VALUE_BYTES)}: ").strip().lower()
        parsed = "min" if value == "min" else int(value)
        control.set_range(parsed)
    except (EOFError, ValueError) as exc:
        print(f"Range unchanged: {exc}")


def handle_keyboard(key: int, view: ViewState, motion: MotionState, control: Optional[SonarControlClient] = None) -> None:
    if key == ord("w"):
        view.toggle()
        motion.reset_background()
    if key == ord("m"):
        motion.toggle()
    if key == ord("["):
        motion.adjust_gain(-MOTION_GAIN_STEP)
    if key == ord("]"):
        motion.adjust_gain(MOTION_GAIN_STEP)
    if key == ord("g"):
        prompt_motion_gain(motion)
    if control is not None:
        if key == ord("s"):
            control.toggle_sonar()
        if key == ord("e"):
            control.set_sonar_enabled(True)
        if key == ord("d"):
            control.set_sonar_enabled(False)
        if key == ord("z"):
            control.zoom_in()
        if key == ord("x"):
            control.zoom_out()
        if key == ord("r"):
            prompt_control_range(control)
    if key == ord("q"):
        raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", required=True, help="Network interface, e.g. en0, eth0, Ethernet")
    parser.add_argument("--ready-file", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--settings-file", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--gls10-ip", help="GLS 10 IP address; enables TCP sonar/range controls")
    parser.add_argument("--control-dry-run", action="store_true", help="Simulate controls and print packets without TCP sends")
    parser.add_argument("--initial-range", default=None, help="Locally known starting range: min or a supported number")
    parser.add_argument("--control-timeout", type=float, default=2.0, help="TCP connect/send timeout; default: 2 seconds")
    parser.add_argument("--emulator-control-port", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--web", action="store_true", help="Serve the browser viewer on localhost")
    parser.add_argument("--web-only", action="store_true", help="Use the browser UI without opening OpenCV windows")
    parser.add_argument("--web-host", default="127.0.0.1", help="Browser viewer bind address; default: 127.0.0.1")
    parser.add_argument("--web-port", type=int, default=8765, help="Browser viewer port; default: 8765")
    parser.add_argument("--stream", default="0x00060044", help="Stream ID hex, or 'all'")
    parser.add_argument("--save", type=Path, default=None, help="Optional folder to save display-oriented frame images")
    parser.add_argument("--frames-dir", type=Path, default=Path("frames"), help="Folder for rotated raw PNG frames saved while raw recording is active; default: frames")
    parser.add_argument("--no-save-frames", action="store_true", help="Disable saving decoded PNG frames alongside raw recordings")
    parser.add_argument("--no-save-video", action="store_true", help="Record PNGs with metadata without encoding video")
    parser.add_argument("--record-video", type=Path, default=Path("sonar.mp4"), help="Output path for the START REC button; default: sonar.mp4")
    parser.add_argument("--record-echogram", type=Path, default=Path("echogram.png"), help="Base PNG path for accumulated echogram columns; default: echogram.png")
    parser.add_argument("--video-fps", type=float, default=20.0, help="FPS to write into recorded video; default: 20")
    parser.add_argument("--save-root", type=Path, default=None, help="Root folder for relative output paths; prompts by default")
    parser.add_argument("--session-name", default=None, help="Optional prefix for default output names")
    parser.add_argument("--no-save-prompt", action="store_true", help="Do not ask for an output folder at startup")
    parser.add_argument("--paths-confirmed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--autosave-raw", action="store_true", help="Automatically record raw footage in rolling segments")
    parser.add_argument("--autosave-echogram", action="store_true", help="Automatically save accumulated echogram columns as rolling PNG images")
    parser.add_argument("--autosave-minutes", type=float, default=10.0, help="Autosave segment length in minutes; default: 10")
    parser.add_argument("--log-other-packets", action="store_true", help="Log non-image UDP packets for reverse engineering")
    parser.add_argument("--other-packets-dir", type=Path, default=Path("other_packets"), help="Folder for --log-other-packets CSV output")
    parser.add_argument("--udp-port", type=int, default=None, help="Optional UDP port filter")
    parser.add_argument("--no-signal-seconds", type=float, default=5.0, help="Seconds without decoded frames before showing a no-signal warning; default: 5")
    parser.set_defaults(warp_xy=True)
    parser.add_argument("--warp-xy", action="store_true", help="Start in warped theta/range X/Y fan view; default")
    parser.add_argument("--polar", dest="warp_xy", action="store_false", help="Start in unwarped polar/raw view")
    parser.add_argument("--width", type=int, default=None, help="override the physically derived warped output width")
    parser.add_argument("--height", type=int, default=None, help="override the physically derived warped output height")
    parser.add_argument("--max-range", type=float, default=None, help="override selected range in meters; defaults to frame metadata")
    parser.add_argument("--warp-output-scale", type=float, default=1.0, help="scale physically derived warp dimensions; default: 1")
    parser.add_argument("--warp-interpolation-mode", choices=("nearest", "linear", "cubic", "area", "lanczos4"), default="linear", help="OpenCV interpolation used by the warp; default: linear")
    parser.add_argument("--grid-spacing-ft", type=float, default=2.0, help="warped grid and raw-view range scale spacing in feet; default: 2")
    parser.add_argument("--range-offset-bins", type=float, default=0.0, help="add this source-row offset to the range mapping")
    parser.add_argument("--theta-offset-deg", type=float, default=0.0, help="rotate theta table by this many degrees")
    parser.add_argument("--flip-theta", action="store_true", help="reverse theta columns if left/right is backwards")
    parser.add_argument("--flip-range", action="store_true", help="reverse range rows if near/far is backwards")
    parser.add_argument("--forward-down", action="store_true", help="put forward/far range at bottom instead of top")
    parser.add_argument("--motion", action="store_true", help="Start with background-subtraction motion view enabled")
    parser.add_argument("--motion-alpha", type=float, default=0.04, help="Background learning rate for motion view; default: 0.04")
    parser.add_argument("--motion-threshold", type=int, default=14, help="Minimum brightness change shown in motion view; default: 14")
    parser.add_argument("--motion-gain", type=float, default=4.0, help="Brightness gain applied to motion differences; default: 4.0")
    parser.add_argument("--no-echogram", action="store_true", help="Disable the second scrolling echogram window")
    parser.add_argument("--echogram-width", type=int, default=700, help="Scrolling echogram window width; default: 700")
    parser.add_argument("--echogram-height", type=int, default=512, help="Scrolling echogram window height; default: 512")
    parser.add_argument("--echogram-mode", choices=("max", "mean"), default="max", help=argparse.SUPPRESS)
    parser.add_argument("--echogram-motion-alpha", type=float, default=0.04, help="Echogram background learning rate; default: 0.04")
    parser.add_argument("--echogram-motion-threshold", type=int, default=0, help="Minimum positive warped-frame change shown; default: 0")
    parser.add_argument("--echogram-motion-gain", type=float, default=1.0, help="Brightness gain for positive warped-frame differences; default: 1")
    parser.add_argument("--echogram-brightness-power", type=float, default=1.5, help="Power curve applied to normalized echogram brightness; default: 1.5")
    parser.add_argument(
        "--colorscheme",
        "--colourscheme",
        "--color-scheme",
        "--colour-scheme",
        default="orange",
        choices=sorted(COLOR_SCHEMES),
        help="warped display color ramp; default: orange",
    )
    args = parser.parse_args()
    if args.no_save_video and args.no_save_frames:
        parser.error("Select at least one sonar recording format")
    if args.web_only:
        args.web = True
    if args.control_dry_run and not args.gls10_ip:
        args.gls10_ip = "DRY-RUN"
    if args.initial_range is not None:
        args.initial_range = "min" if args.initial_range.lower() == "min" else int(args.initial_range)
        if args.initial_range not in RANGE_VALUE_BYTES:
            parser.error(f"unsupported --initial-range: {args.initial_range}")
    run_timestamp = time.strftime("%Y-%m-%d_%H%M%S")

    stream_filter = None if args.stream.lower() == "all" else int(args.stream, 0)
    assembler = SonarFrameAssembler(stream_filter=stream_filter)

    save_root = args.save_root
    if save_root is None and not args.no_save_prompt and not args.web_only:
        save_root = choose_save_root()
    if save_root is not None:
        save_root.mkdir(parents=True, exist_ok=True)
        make_user_editable(save_root)
        if args.session_name:
            prefix = safe_name(args.session_name)
            args.record_video = Path(f"{prefix}_raw_{run_timestamp}{args.record_video.suffix or '.mp4'}")
            args.record_echogram = Path(f"{prefix}_echogram_{run_timestamp}.png")
            args.frames_dir = Path(f"{prefix}_frames_{run_timestamp}")
            args.other_packets_dir = Path(f"{prefix}_other_packets_{run_timestamp}")
        elif not args.no_save_prompt and not args.web_only:
            args.record_video = prompt_output_path("Raw MP4 filename", args.record_video)
            args.record_echogram = prompt_output_path("Echogram PNG filename", args.record_echogram)
            args.frames_dir = prompt_output_path("Frames folder name", args.frames_dir, is_dir=True)
            args.record_video = append_timestamp_to_path(args.record_video, run_timestamp)
            args.record_echogram = append_timestamp_to_path(args.record_echogram, run_timestamp)
            args.frames_dir = append_timestamp_to_path(args.frames_dir, run_timestamp)
            args.other_packets_dir = append_timestamp_to_path(args.other_packets_dir, run_timestamp)
        else:
            args.record_video = append_timestamp_to_path(args.record_video, run_timestamp)
            args.record_echogram = append_timestamp_to_path(args.record_echogram, run_timestamp)
            args.frames_dir = append_timestamp_to_path(args.frames_dir, run_timestamp)
            args.other_packets_dir = append_timestamp_to_path(args.other_packets_dir, run_timestamp)

        args.save = under_save_root(args.save, save_root)
        args.frames_dir = unique_dir(under_save_root(args.frames_dir, save_root))
        args.other_packets_dir = unique_dir(under_save_root(args.other_packets_dir, save_root))
        args.record_video = unique_file(under_save_root(args.record_video, save_root))
        args.record_echogram = unique_file(
            under_save_root(args.record_echogram, save_root).with_suffix(".png")
        )
        print(f"Saving outputs under: {save_root}")
        print(f"Raw video: {args.record_video}")
        print(f"Echogram PNG: {args.record_echogram}")
        print(f"Frames folder: {args.frames_dir}")
        if args.log_other_packets:
            print(f"Other-packet logs: {args.other_packets_dir}")
    else:
        args.record_video = append_timestamp_to_path(args.record_video, run_timestamp)
        args.record_echogram = append_timestamp_to_path(
            args.record_echogram.with_suffix(".png"), run_timestamp
        )
        args.frames_dir = append_timestamp_to_path(args.frames_dir, run_timestamp)
        args.other_packets_dir = append_timestamp_to_path(args.other_packets_dir, run_timestamp)

    save_recorded_frames = not args.no_save_frames
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)
    frame_rate = RecentFrameRate()
    display_frame_rate = RecentFrameRate()
    latest_display_frame = LatestFrame()
    polar_frames_saved = 0
    viewer_started_at = time.time()
    last_frame_time: Optional[float] = None
    last_echogram_display: Optional[np.ndarray] = None
    latest_temperature_c = None
    printed_temperature_detected = False
    autosave_seconds = max(1.0, args.autosave_minutes * 60.0)
    recording = RecordingState(
        args.record_video,
        args.video_fps,
        label="raw footage",
        auto_segment_seconds=autosave_seconds if args.autosave_raw else None,
        video_enabled=not args.no_save_video,
        png_enabled=save_recorded_frames,
    )
    echogram_recording = EchogramImageRecordingState(
        args.record_echogram,
        label="echogram image",
        auto_segment_seconds=autosave_seconds if args.autosave_echogram else None,
    )
    view = ViewState(warp_enabled=args.warp_xy)
    echogram = None if args.no_echogram else EchogramState(
        width=args.echogram_width,
        height=args.echogram_height,
        background_alpha=float(np.clip(args.echogram_motion_alpha, 0.0, 1.0)),
        threshold=max(0, args.echogram_motion_threshold),
        gain=max(0.0, args.echogram_motion_gain),
        brightness_power=max(0.0, args.echogram_brightness_power),
    )
    motion = MotionState(
        enabled=args.motion,
        alpha=float(np.clip(args.motion_alpha, 0.0, 1.0)),
        threshold=max(0, args.motion_threshold),
        gain=max(0.0, args.motion_gain),
    )
    intensity_min = 0
    intensity_max = 255
    sonar_gain = 21
    echogram_gain_percent = int(round(echogram.gain * 100)) if echogram else 100
    echogram_range_cropped = False
    live_range_cropped = False
    distance_grid_enabled = True
    display_enabled = True
    force_render = False
    last_captured_frame = None
    control = None
    if args.emulator_control_port is not None:
        control = EmulatorControlClient(
            args.emulator_control_port,
            initial_range=args.initial_range,
        )
    elif args.gls10_ip:
        control = SonarControlClient(
            args.gls10_ip,
            dry_run=args.control_dry_run,
            initial_range=args.initial_range,
            connect_timeout=args.control_timeout,
        )
        control.connect()
    web_viewer = None
    if args.web:
        web_viewer = BrowserViewerServer(args.web_host, args.web_port)
        web_viewer.state.update_status(
            streaming=False,
            stream_stalled=False,
            display_enabled=display_enabled,
            display_fps=0.0,
            recording=False,
            recording_requested=False,
            echogram_recording=False,
            record_video_enabled=recording.video_enabled,
            record_png_enabled=recording.png_enabled,
            autosave_raw=args.autosave_raw,
            autosave_echogram=args.autosave_echogram,
            autosave_minutes=args.autosave_minutes,
            raw_autosaved_clips=0,
            echogram_autosaved_clips=0,
            recording_path=str(args.record_video.resolve()),
            echogram_recording_path=str(args.record_echogram.resolve()),
            snapshot_path=str(args.frames_dir.resolve()),
            paths_confirmed=args.paths_confirmed,
            range=getattr(control, "actual_range", None),
            range_requested=getattr(control, "requested_range", None),
            range_pending=getattr(control, "range_change_pending", False),
            intensity_min=intensity_min,
            intensity_max=intensity_max,
            gain=sonar_gain,
            echogram_gain=echogram_gain_percent,
            echogram_range_cropped=echogram_range_cropped,
            live_range_cropped=live_range_cropped,
            color_scheme=args.colorscheme,
            motion_enabled=motion.enabled,
            tilt_available=False,
            tilt_degrees=None,
            tilt_reversed=False,
            roll_available=False,
            roll_degrees=None,
            roll_reversed=False,
            distance_grid_enabled=distance_grid_enabled,
            no_signal_state="waiting",
            sonar_enabled=getattr(control, "current_sonar_enabled", None),
            sonar_pending=getattr(control, "pending_sonar_enabled", None),
            sonar_requested=getattr(control, "last_requested_sonar_enabled", None),
            sonar_transition=getattr(control, "sonar_transition_state", "unknown"),
        )
        web_viewer.start()
        host, port = web_viewer.address
        print(f"Browser viewer: http://{host}:{port}")

    print("Listening for Sonar packets...")
    if not args.web_only:
        print("Press q in the OpenCV window to quit.")
    print("Click WARP VIEW/RAW VIEW in the OpenCV window, or press w, to switch display modes.")
    print("Click MOTION ON/OFF in the OpenCV window, or press m, to toggle background subtraction.")
    print("Click -/+/gain value in the OpenCV window, or press [, ], and g, to adjust motion gain.")
    print("Click START REC in the main OpenCV window to start/stop raw recording.")
    print("Click SAVE FRAME to save the currently displayed frame, whether recording or not.")
    if args.autosave_raw:
        print(f"Autosaving raw footage every {args.autosave_minutes:g} minute(s) to {args.record_video}")
    if args.autosave_echogram:
        print(f"Autosaving echogram PNGs every {args.autosave_minutes:g} minute(s) to {args.record_echogram}")
    if echogram is not None:
        print("Showing the scrolling echogram. Its capture button saves one PNG column per displayed sonar frame.")
    if args.log_other_packets:
        print("Logging non-image UDP packets for reverse engineering.")
    if control is not None:
        print("GLS 10 controls: sonar toggle button or s, z=zoom in, x=zoom out, r=set range.")

    last_decoded_frame = None
    web_no_signal_state = None
    latest_tilt_raw_deg = None
    latest_roll_raw_deg = None
    # Confirmed from known 0°, ±30°, and ±90° positions: device's raw 90°
    # attitude corresponds to a physically level transducer.
    tilt_zero_deg = 90.0
    # Confirmed against known physical positions: the GLS10 sensor angle falls
    # as the transducer tilts upward, so negate the raw angular difference.
    tilt_direction = -1.0
    tilt_reversed = False
    roll_zero_deg = 0.0
    roll_direction = 1.0
    roll_reversed = False

    storage_guard = DiskSpaceGuard((args.record_video.parent, args.record_echogram.parent, args.frames_dir))
    storage_failure = None

    def report_recording_error(message):
        print(message, file=sys.stderr)
        if web_viewer is not None:
            web_viewer.state.update_status(recording_error=message, notice=message)

    def check_recording_storage():
        nonlocal storage_failure
        with recording.lock:
            if not (recording.active or recording.requested or recording.auto_segment_seconds is not None
                    or echogram_recording.active or echogram_recording.requested
                    or echogram_recording.auto_segment_seconds is not None):
                return
            try:
                storage_guard.check()
            except OSError as exc:
                storage_failure = str(exc)
                recording.requested = False
                recording.auto_segment_seconds = None
                args.autosave_raw = False
                recording.finish()
                report_recording_error(storage_failure)
            if web_viewer is not None:
                web_viewer.state.update_status(disk_free_bytes=storage_guard.free_bytes)

    def handle_web_actions() -> None:
        nonlocal intensity_min, intensity_max, sonar_gain, echogram_gain_percent
        nonlocal echogram_range_cropped, live_range_cropped
        nonlocal distance_grid_enabled, display_enabled, display_frame_rate, force_render
        nonlocal last_echogram_display, storage_guard, storage_failure
        nonlocal tilt_zero_deg, tilt_direction, tilt_reversed
        nonlocal roll_zero_deg, roll_direction, roll_reversed
        if web_viewer is None:
            return
        while True:
            try:
                action = web_viewer.state.actions.get_nowait()
            except queue.Empty:
                return
            action_type = action["type"]
            if action_type in {"toggle_recording", "toggle_echogram_recording", "set_autosave", "save_frame", "save_echogram_frame"} and not args.paths_confirmed:
                web_viewer.state.update_status(notice="Choose and apply available output folders before recording.")
                continue
            if action_type == "set_theme":
                if action.get("value") not in ("dark", "light"):
                    continue
                web_viewer.state.update_status(theme=action["value"])
            elif action_type == "acknowledge_recording_error":
                web_viewer.state.update_status(recording_error=None)
            elif action_type == "toggle_display":
                display_enabled = not display_enabled
                display_frame_rate = RecentFrameRate()
                force_render = display_enabled
                if display_enabled:
                    motion.reset_background()
                web_viewer.state.update_status(
                    display_enabled=display_enabled, display_fps=0.0,
                    notice=("Display resumed" if display_enabled
                            else "Display hidden; polar recording continues; echogram capture paused"),
                )
            elif action_type == "toggle_view":
                view.toggle()
                motion.reset_background()
            elif action_type == "zero_tilt":
                if latest_tilt_raw_deg is None:
                    web_viewer.state.update_status(
                        notice="Tilt telemetry is not available yet"
                    )
                else:
                    tilt_zero_deg = latest_tilt_raw_deg
                    web_viewer.state.update_status(
                        tilt_degrees=0.0,
                        notice="Transducer tilt zeroed",
                    )
            elif action_type == "reverse_tilt":
                tilt_direction *= -1.0
                tilt_reversed = not tilt_reversed
                tilt = None
                if latest_tilt_raw_deg is not None and tilt_zero_deg is not None:
                    tilt = tilt_direction * relative_angle_degrees(
                        latest_tilt_raw_deg, tilt_zero_deg
                    )
                web_viewer.state.update_status(
                    tilt_degrees=tilt,
                    tilt_reversed=tilt_reversed,
                    notice="Transducer tilt direction reversed",
                )
            elif action_type == "reset_tilt":
                tilt_zero_deg = 90.0
                tilt_direction = -1.0
                tilt_reversed = False
                tilt = None if latest_tilt_raw_deg is None else (
                    tilt_direction * relative_angle_degrees(
                        latest_tilt_raw_deg, tilt_zero_deg
                    )
                )
                web_viewer.state.update_status(
                    tilt_degrees=tilt,
                    tilt_reversed=False,
                    notice="Transducer tilt reset",
                )
            elif action_type == "zero_roll":
                if latest_roll_raw_deg is None:
                    web_viewer.state.update_status(
                        notice="Roll telemetry is not available yet"
                    )
                else:
                    roll_zero_deg = latest_roll_raw_deg
                    web_viewer.state.update_status(
                        roll_degrees=0.0,
                        notice="Transducer roll zeroed",
                    )
            elif action_type == "reverse_roll":
                roll_direction *= -1.0
                roll_reversed = not roll_reversed
                roll = None if latest_roll_raw_deg is None else (
                    roll_direction * relative_angle_degrees(
                        latest_roll_raw_deg, roll_zero_deg
                    )
                )
                web_viewer.state.update_status(
                    roll_degrees=roll,
                    roll_reversed=roll_reversed,
                    notice="Transducer roll direction reversed",
                )
            elif action_type == "reset_roll":
                roll_zero_deg = 0.0
                roll_direction = 1.0
                roll_reversed = False
                web_viewer.state.update_status(
                    roll_degrees=latest_roll_raw_deg,
                    roll_reversed=False,
                    notice="Transducer roll reset",
                )
            elif action_type == "set_color_scheme":
                color_scheme = action.get("value")
                if color_scheme not in COLOR_SCHEMES:
                    web_viewer.state.update_status(
                        notice="Unknown warped-view color scheme"
                    )
                    continue
                args.colorscheme = color_scheme
                web_viewer.state.update_status(
                    color_scheme=color_scheme,
                    notice=f"Color scheme: {color_scheme.replace('-', ' ')}",
                )
            elif action_type == "toggle_motion":
                motion.toggle()
            elif action_type == "toggle_distance_grid":
                distance_grid_enabled = not distance_grid_enabled
                web_viewer.state.update_status(
                    distance_grid_enabled=distance_grid_enabled,
                    notice=(
                        "Distance grid shown"
                        if distance_grid_enabled
                        else "Distance grid hidden"
                    ),
                )
            elif action_type == "set_intensity_window":
                values = action.get("value") or {}
                try:
                    minimum = values["minimum"]
                    maximum = values["maximum"]
                    if (
                        isinstance(minimum, bool)
                        or isinstance(maximum, bool)
                        or not isinstance(minimum, int)
                        or not isinstance(maximum, int)
                        or not 0 <= minimum <= maximum <= 255
                    ):
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    web_viewer.state.update_status(
                        notice="Intensity range must be between 0 and 255"
                    )
                    continue
                intensity_min = minimum
                intensity_max = maximum
                web_viewer.state.update_status(
                    intensity_min=minimum,
                    intensity_max=maximum,
                )
            elif action_type == "set_recording_formats":
                values = action.get("value")
                try:
                    if not isinstance(values, dict):
                        raise ValueError("Recording formats are required")
                    recording.set_formats(values.get("video"), values.get("png"))
                except ValueError as exc:
                    web_viewer.state.update_status(notice=str(exc))
                    continue
                web_viewer.state.update_status(
                    record_video_enabled=recording.video_enabled,
                    record_png_enabled=recording.png_enabled,
                    notice="Sonar recording formats applied",
                )
            elif action_type == "toggle_recording":
                if not recording.active and not recording.requested:
                    try:
                        storage_guard.check(force=True)
                        storage_failure = None
                    except OSError as exc:
                        report_recording_error(str(exc))
                        continue
                if recording.active:
                    error = recording.finish()
                    if error:
                        report_recording_error(error)
                else:
                    recording.toggle_requested()
                web_viewer.state.update_status(
                    recording=recording.active,
                    recording_requested=recording.requested,
                    notice="Sonar recording toggled",
                )
            elif action_type == "toggle_echogram_recording":
                if not echogram_recording.active and not echogram_recording.requested:
                    try:
                        storage_guard.check(force=True)
                        storage_failure = None
                    except OSError as exc:
                        report_recording_error(str(exc))
                        continue
                echogram_error = echogram_recording.toggle_requested()
                if echogram_error:
                    report_recording_error(echogram_error)
                web_viewer.state.update_status(
                    notice=(
                        echogram_error or "Echogram PNG capture toggled"
                    )
                )
            elif action_type == "toggle_echogram_range_crop":
                echogram_range_cropped = not echogram_range_cropped
                if echogram is not None and display_enabled:
                    full_display = echogram.render()
                    last_echogram_display = (
                        echogram.crop_at_range_boundary(full_display)
                        if echogram_range_cropped else full_display
                    )
                    web_viewer.state.publish_echogram(last_echogram_display)
                web_viewer.state.update_status(
                    echogram_range_cropped=echogram_range_cropped,
                    notice=(
                        "Echogram cropped below range boundary"
                        if echogram_range_cropped else "Echogram full range shown"
                    ),
                )
            elif action_type == "toggle_live_range_crop":
                live_range_cropped = not live_range_cropped
                web_viewer.state.update_status(
                    live_range_cropped=live_range_cropped,
                    notice=("Live view cropped at selected range"
                            if live_range_cropped else "Live view full range shown"),
                )
            elif action_type == "save_frame":
                if last_decoded_frame is None:
                    web_viewer.state.update_status(
                        notice="No sonar frame is available to save"
                    )
                else:
                    frame_id, current_display, _fps = last_decoded_frame
                    timestamp = time.strftime("%Y-%m-%d_%H%M%S")
                    output = unique_file(
                        args.frames_dir
                        / f"snapshot_frame_{frame_id:06d}_{timestamp}.png"
                    )
                    try:
                        storage_guard.check(force=True)
                        write_snapshot(output, current_display)
                    except (OSError, RuntimeError, cv2.error) as exc:
                        web_viewer.state.update_status(
                            notice=f"Snapshot failed: {exc}"
                        )
                        report_recording_error(f"Snapshot failed: {exc}")
                    else:
                        web_viewer.state.update_status(
                            notice=f"Saved frame: {output}"
                        )
                        print(f"Saved frame: {output}")
            elif action_type == "save_echogram_frame":
                if last_echogram_display is None:
                    web_viewer.state.update_status(
                        notice="No echogram frame is available to save"
                    )
                else:
                    timestamp = time.strftime("%Y-%m-%d_%H%M%S")
                    output = unique_file(
                        args.frames_dir / f"snapshot_echogram_{timestamp}.png"
                    )
                    try:
                        storage_guard.check(force=True)
                        write_snapshot(output, last_echogram_display)
                    except (OSError, RuntimeError, cv2.error) as exc:
                        web_viewer.state.update_status(
                            notice=f"Echogram snapshot failed: {exc}"
                        )
                        report_recording_error(f"Echogram snapshot failed: {exc}")
                    else:
                        web_viewer.state.update_status(
                            notice=f"Saved echogram frame: {output}"
                        )
                        print(f"Saved echogram frame: {output}")
            elif action_type == "set_output_paths":
                if (recording.active or recording.requested
                        or recording.auto_segment_seconds is not None
                        or echogram_recording.active):
                    web_viewer.state.update_status(
                        notice="Stop both recordings before changing paths"
                    )
                    continue
                values = action.get("value") or {}
                try:
                    if not isinstance(values, dict):
                        raise ValueError("output paths must be an object")
                    supplied = (
                        values["recording"], values["echogram"],
                        values["snapshots"],
                    )
                    if not all(isinstance(value, str) for value in supplied):
                        raise ValueError("output paths must be text")
                    raw_value, echogram_value, snapshot_value = (
                        value.strip() for value in supplied
                    )
                    if not all((raw_value, echogram_value, snapshot_value)):
                        raise ValueError
                    raw_path = Path(raw_value).expanduser().resolve()
                    echogram_path = Path(echogram_value).expanduser().resolve()
                    snapshot_path = Path(snapshot_value).expanduser().resolve()
                    if echogram_path.suffix.lower() != ".png":
                        raise ValueError(
                            "echogram image path must end in .png"
                        )
                    echogram_path = echogram_path.with_suffix(".png")
                    if save_root is not None and hasattr(os, "geteuid") and os.geteuid() == 0:
                        allowed_root = save_root.expanduser().resolve()
                        for candidate in (raw_path, echogram_path, snapshot_path):
                            if candidate != allowed_root and allowed_root not in candidate.parents:
                                raise ValueError(
                                    f"output paths must stay inside {allowed_root}"
                                )
                    preflight_output_paths(
                        raw_path,
                        echogram_path,
                        snapshot_path,
                    )
                    new_storage_guard = DiskSpaceGuard((raw_path.parent, echogram_path.parent, snapshot_path))
                    new_storage_guard.check(force=True)
                except (KeyError, TypeError, ValueError, OSError) as exc:
                    detail = str(exc) or "all three output paths are required"
                    message = f"Output path validation failed: {detail}"
                    web_viewer.state.update_status(
                        notice=message
                    )
                    print(message)
                    continue
                storage_guard = new_storage_guard
                storage_failure = None
                with recording.lock:
                    args.record_video = raw_path
                    args.record_echogram = echogram_path
                    args.frames_dir = snapshot_path
                    recording.base_path = raw_path
                    recording.clip_index = 0
                echogram_recording.base_path = echogram_path
                echogram_recording.clip_index = 0
                args.paths_confirmed = True
                if args.settings_file is not None:
                    try:
                        preferences.update({"output_paths": {"recording": str(raw_path), "echogram": str(echogram_path), "snapshots": str(snapshot_path)}})
                    except OSError as exc:
                        report_recording_error(f"Output paths applied, but could not save preferences: {exc}")
                web_viewer.state.update_status(
                    recording_path=str(raw_path.resolve()),
                    echogram_recording_path=str(echogram_path.resolve()),
                    snapshot_path=str(snapshot_path.resolve()),
                    paths_confirmed=True,
                    notice="Output paths ready",
                )
                print("Output paths ready")
            elif action_type == "open_output_folder":
                paths = {"recording": args.record_video.parent, "echogram": args.record_echogram.parent, "snapshots": args.frames_dir}
                folder = paths.get(action.get("value"))
                try:
                    if folder is None or not folder.is_dir():
                        raise OSError("This output folder does not exist yet. Save a recording first.")
                    command = ["open", str(folder)] if sys.platform == "darwin" else ["explorer" if os.name == "nt" else "xdg-open", str(folder)]
                    subprocess.run(command, check=True, timeout=10, capture_output=True)
                except (OSError, subprocess.SubprocessError) as exc:
                    web_viewer.state.update_status(notice=f"Could not open output folder: {exc}")
            elif action_type == "set_autosave":
                values = action.get("value") or {}
                try:
                    raw_enabled = values["raw"]
                    echogram_enabled = values["echogram"]
                    minutes = float(values["minutes"])
                    if (
                        not isinstance(raw_enabled, bool)
                        or not isinstance(echogram_enabled, bool)
                        or not 0 < minutes <= 1440
                    ):
                        raise ValueError
                except (KeyError, TypeError, ValueError):
                    web_viewer.state.update_status(
                        notice="Autosave duration must be between 0 and 1440 minutes"
                    )
                    continue
                if raw_enabled or echogram_enabled:
                    try:
                        storage_guard.check(force=True)
                        storage_failure = None
                    except OSError as exc:
                        report_recording_error(str(exc))
                        continue
                segment_seconds = minutes * 60.0
                with recording.lock:
                    if raw_enabled:
                        recording.requested = False
                    if echogram_enabled:
                        echogram_recording.requested = False
                    recording.auto_segment_seconds = (
                        segment_seconds if raw_enabled else None
                    )
                    echogram_recording.auto_segment_seconds = (
                        segment_seconds if echogram_enabled else None
                    )
                args.autosave_raw = raw_enabled
                args.autosave_echogram = echogram_enabled
                args.autosave_minutes = minutes
                web_viewer.state.update_status(
                    autosave_raw=raw_enabled,
                    autosave_echogram=echogram_enabled,
                    autosave_minutes=minutes,
                    notice=(
                        f"Autosave updated: {minutes:g}-minute segments"
                        if raw_enabled or echogram_enabled
                        else "Autosave disabled"
                    ),
                )
            elif action_type == "stop_autosave":
                with recording.lock:
                    raw_was_autosaving = recording.auto_segment_seconds is not None
                    recording.auto_segment_seconds = None
                    if raw_was_autosaving:
                        recording.requested = False
                        error = recording.finish()
                        if error:
                            report_recording_error(error)
                echogram_was_autosaving = (
                    echogram_recording.auto_segment_seconds is not None
                )
                echogram_recording.auto_segment_seconds = None
                if echogram_was_autosaving:
                    echogram_recording.requested = False
                    error = echogram_recording.stop()
                    if error:
                        report_recording_error(error)
                args.autosave_raw = False
                args.autosave_echogram = False
                web_viewer.state.update_status(
                    record_video_enabled=recording.video_enabled,
                    record_png_enabled=recording.png_enabled,
                    recording=recording.active,
                    recording_requested=recording.requested,
                    echogram_recording=echogram_recording.active,
                    recording_started_at=recording.segment_started_at,
                    echogram_recording_started_at=(
                        echogram_recording.segment_started_at
                    ),
                    autosave_raw=False,
                    autosave_echogram=False,
                    notice="Autosave recording stopped",
                )
            elif action_type == "stop_emulator":
                web_viewer.state.update_status(notice="Stopping emulator…")
                raise KeyboardInterrupt
            elif action_type == "set_gain":
                value = action.get("value")
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                    web_viewer.state.update_status(
                        notice="Sonar gain must be between 0 and 100%"
                    )
                    continue
                sonar_gain = value
                web_viewer.state.update_status(
                    gain=sonar_gain,
                    notice=f"Sonar display gain set to {sonar_gain}%",
                )
            elif action_type == "set_echogram_gain":
                value = action.get("value")
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 300:
                    web_viewer.state.update_status(
                        notice="Echogram gain must be between 0 and 300%"
                    )
                    continue
                echogram_gain_percent = value
                if echogram is not None:
                    echogram.gain = value / 100.0
                if echogram is not None and display_enabled:
                    full_display = echogram.render()
                    last_echogram_display = (
                        echogram.crop_at_range_boundary(full_display)
                        if echogram_range_cropped else full_display
                    )
                    web_viewer.state.publish_echogram(last_echogram_display)
                web_viewer.state.update_status(
                    echogram_gain=echogram_gain_percent,
                    notice=f"Echogram display gain set to {value}%",
                )
            elif control is not None:
                if action_type == "zoom_in":
                    try:
                        changed = control.zoom_in()
                    except ValueError:
                        changed = False
                    web_viewer.state.update_status(
                        range_requested=getattr(
                            control, "requested_range", None
                        ),
                        range_pending=getattr(
                            control, "range_change_pending", False
                        ),
                        notice=(
                            f"Range requested: {control.current_range} ft"
                            if changed
                            else "Range change failed or is already at minimum"
                        )
                    )
                elif action_type == "zoom_out":
                    try:
                        changed = control.zoom_out()
                    except ValueError:
                        changed = False
                    web_viewer.state.update_status(
                        range_requested=getattr(
                            control, "requested_range", None
                        ),
                        range_pending=getattr(
                            control, "range_change_pending", False
                        ),
                        notice=(
                            f"Range requested: {control.current_range} ft"
                            if changed
                            else "Range change failed or is already at maximum"
                        )
                    )
                elif action_type == "toggle_sonar":
                    if control.toggle_sonar():
                        requested = getattr(
                            control, "pending_sonar_enabled", None
                        )
                        web_viewer.state.update_status(
                            sonar_enabled=getattr(
                                control, "current_sonar_enabled", None
                            ),
                            sonar_pending=requested,
                            sonar_requested=getattr(
                                control, "last_requested_sonar_enabled", None
                            ),
                            sonar_transition=getattr(
                                control, "sonar_transition_state", "pending"
                            ),
                            notice=(
                                "Enable requested; waiting for image stream"
                                if requested
                                else "Disable requested; waiting for stream to stop"
                            ),
                        )
                    else:
                        web_viewer.state.update_status(
                            notice="Sonar command did not reach the emulator"
                        )
                elif action_type == "set_range":
                    try:
                        changed = control.set_range(action.get("value"))
                    except ValueError as exc:
                        print(f"Range unchanged: {exc}")
                        changed = False
                    web_viewer.state.update_status(
                        range_requested=getattr(
                            control, "requested_range", None
                        ),
                        range_pending=getattr(
                            control, "range_change_pending", False
                        ),
                        notice=(
                            f"Range requested: {control.current_range} ft"
                            if changed
                            else "Range command did not reach the emulator"
                        )
                    )

            if args.settings_file is not None and action_type in {"set_theme", "set_gain", "set_echogram_gain", "set_intensity_window", "set_color_scheme", "set_recording_formats"}:
                try:
                    saved = preferences.read().get("viewer", {})
                    if not isinstance(saved, dict):
                        saved = {}
                    saved[action_type] = {"type": action_type, "value": action.get("value")}
                    preferences.update({"viewer": saved})
                except OSError as exc:
                    web_viewer.state.update_status(notice=f"Could not save viewer settings: {exc}")

    def on_mouse(event, x, y, _flags, _param) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return

        if (
            control is not None
            and last_decoded_frame is None
            and getattr(control, "supports_sonar", True)
        ):
            x1, y1, x2, y2 = SONAR_RECOVERY_BUTTON
            if x1 <= x <= x2 and y1 <= y <= y2:
                control.set_sonar_enabled(True)
                return

        x1, y1, x2, y2 = VIEW_BUTTON
        if x1 <= x <= x2 and y1 <= y <= y2:
            view.toggle()
            motion.reset_background()
            return

        x1, y1, x2, y2 = MOTION_BUTTON
        if x1 <= x <= x2 and y1 <= y <= y2:
            motion.toggle()
            return

        x1, y1, x2, y2 = GAIN_DOWN_BUTTON
        if x1 <= x <= x2 and y1 <= y <= y2:
            motion.adjust_gain(-MOTION_GAIN_STEP)
            return

        x1, y1, x2, y2 = GAIN_VALUE_RECT
        if x1 <= x <= x2 and y1 <= y <= y2:
            prompt_motion_gain(motion)
            return

        x1, y1, x2, y2 = GAIN_UP_BUTTON
        if x1 <= x <= x2 and y1 <= y <= y2:
            motion.adjust_gain(MOTION_GAIN_STEP)
            return

        x1, y1, x2, y2 = SAVE_FRAME_BUTTON
        if x1 <= x <= x2 and y1 <= y <= y2:
            if last_decoded_frame is None:
                print("No Sonar frame is available to save.")
                return
            frame_id, current_display, _fps = last_decoded_frame
            timestamp = time.strftime("%Y-%m-%d_%H%M%S")
            output = unique_file(
                args.frames_dir
                / f"snapshot_frame_{frame_id:06d}_{timestamp}.png"
            )
            try:
                write_snapshot(output, current_display)
            except (OSError, RuntimeError, cv2.error) as exc:
                print(f"Snapshot failed: {exc}", file=sys.stderr)
            else:
                print(f"Saved frame: {output}")
            return

        if control is not None:
            actions = [
                (ZOOM_IN_BUTTON, control.zoom_in),
                (ZOOM_OUT_BUTTON, control.zoom_out),
            ]
            if getattr(control, "supports_sonar", True):
                actions[:0] = [
                    (SONAR_TOGGLE_BUTTON, control.toggle_sonar),
                ]
            for rect, action in actions:
                x1, y1, x2, y2 = rect
                if x1 <= x <= x2 and y1 <= y <= y2:
                    action()
                    return

        if recording is not None:
            x1, y1, x2, y2 = RECORD_BUTTON
            if x1 <= x <= x2 and y1 <= y <= y2:
                if recording.active or recording.requested:
                    error = recording.finish()
                    if error:
                        report_recording_error(error)
                else:
                    recording.toggle_requested()

    def on_echogram_mouse(event, x, y, _flags, _param) -> None:
        if event != cv2.EVENT_LBUTTONDOWN:
            return

        x1, y1, x2, y2 = RECORD_BUTTON
        if x1 <= x <= x2 and y1 <= y <= y2:
            echogram_recording.toggle_requested()
            return

    if not args.web_only:
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(WINDOW_NAME, on_mouse)
        cv2.moveWindow(WINDOW_NAME, 20, 40)
    if echogram is not None and not args.web_only:
        cv2.namedWindow(ECHOGRAM_WINDOW_NAME, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(ECHOGRAM_WINDOW_NAME, on_echogram_mouse)
        cv2.resizeWindow(
            ECHOGRAM_WINDOW_NAME,
            echogram.width,
            echogram.height + CONTROL_BAR_HEIGHT,
        )
        cv2.moveWindow(ECHOGRAM_WINDOW_NAME, 560, 40)

    bpf = "udp"
    if args.udp_port is not None:
        bpf = f"udp port {args.udp_port}"

    def show_no_signal_warning_if_needed() -> None:
        nonlocal web_no_signal_state
        handle_web_actions()
        now = time.time()
        previous_frame_time = (
            last_frame_time
            if last_frame_time is not None
            else viewer_started_at
        )
        seconds_since_frame = now - previous_frame_time
        sonar_outcome = (
            control.observe_sonar_silence()
            if control is not None
            and hasattr(control, "observe_sonar_silence")
            else None
        )
        if sonar_outcome is not None:
            notice = {
                "disabled_confirmed": "Sonar disabled; image stream stopped",
                "enable_failed": (
                    "Warning: sonar failed to enable after repeated attempts"
                ),
                "disable_unconfirmed": (
                    "Warning: sonar failed to disable after repeated attempts"
                ),
            }.get(sonar_outcome)
            if notice is not None:
                print(notice)
                if web_viewer is not None:
                    web_viewer.state.update_status(
                        sonar_enabled=getattr(
                            control, "current_sonar_enabled", None
                        ),
                        sonar_pending=getattr(
                            control, "pending_sonar_enabled", None
                        ),
                        sonar_requested=getattr(
                            control, "last_requested_sonar_enabled", None
                        ),
                        sonar_transition=getattr(
                            control, "sonar_transition_state", "unknown"
                        ),
                        notice=notice,
                    )
        sonar_disabled = (
            getattr(control, "current_sonar_enabled", None) is False
        )
        if seconds_since_frame < args.no_signal_seconds and not (
            sonar_disabled and last_decoded_frame is not None
        ):
            return
        no_signal_state = (
            "waiting"
            if last_decoded_frame is None
            else ("disabled" if sonar_disabled else "stalled")
        )
        if web_viewer is not None and web_no_signal_state != no_signal_state:
            web_viewer.state.update_status(
                streaming=False,
                stream_stalled=no_signal_state == "stalled",
                no_signal_state=no_signal_state,
            )
            web_no_signal_state = no_signal_state

        if not display_enabled:
            return
        if last_decoded_frame is None:
            display = render_no_signal_display(
                seconds_since_frame, waiting_for_first_frame=True
            )
            if (
                control is not None
                and getattr(control, "supports_sonar", True)
            ):
                draw_sonar_recovery_button(display)
        else:
            frame_id, frozen_display, fps = last_decoded_frame
            display = frozen_display.copy()
            # Browser status banners are DOM elements; keep its retained frame
            # free of baked-in warning graphics. Native OpenCV windows retain
            # their drawn overlay.
            web_display = display.copy()
            draw_frozen_frame_status(
                display,
                seconds_since_frame,
                sonar_disabled,
            )
            display = compose_main_window(
                display, frame_id, fps, recording, view, motion, control
            )
        if web_viewer is not None:
            # The initial no-signal screen belongs to the browser UI, rather
            # than being encoded as a giant synthetic video frame. Retained
            # frames still publish so the browser can show the last image.
            if last_decoded_frame is not None:
                web_viewer.state.publish(
                    web_display,
                    streaming=False,
                    stream_stalled=no_signal_state == "stalled",
                    no_signal_state=no_signal_state,
                    sonar_enabled=getattr(control, "current_sonar_enabled", None),
                    sonar_pending=getattr(control, "pending_sonar_enabled", None),
                    sonar_requested=getattr(
                        control, "last_requested_sonar_enabled", None
                    ),
                    sonar_transition=getattr(
                        control, "sonar_transition_state", "unknown"
                    ),
                )
            if sonar_disabled and last_echogram_display is not None:
                web_viewer.state.publish_echogram(last_echogram_display)
        if (
            sonar_disabled
            and last_echogram_display is not None
            and not args.web_only
        ):
            disabled_echogram = last_echogram_display.copy()
            draw_frozen_frame_status(disabled_echogram, seconds_since_frame, True)
            echogram_bar = make_control_bar(disabled_echogram.shape[1])
            draw_record_button(
                echogram_bar,
                recording_enabled=True,
                recording=echogram_recording.active,
                elapsed_seconds=echogram_recording.elapsed_seconds(),
            )
            cv2.imshow(
                ECHOGRAM_WINDOW_NAME,
                stack_control_bar(echogram_bar, disabled_echogram),
            )
        if not args.web_only:
            cv2.imshow(WINDOW_NAME, display)
            key = cv2.waitKey(1) & 0xFF
            handle_keyboard(key, view, motion, control)

    def handle_packet(pkt) -> None:
        nonlocal last_frame_time, polar_frames_saved
        nonlocal latest_temperature_c, printed_temperature_detected
        nonlocal web_no_signal_state
        nonlocal latest_tilt_raw_deg, tilt_zero_deg
        nonlocal latest_roll_raw_deg

        payload = udp_payload(pkt)
        if payload is None:
            return

        temperature_c = extract_temperature_c(payload)
        if temperature_c is not None:
            latest_temperature_c = temperature_c
            if not printed_temperature_detected:
                temperature_f = temperature_c * 9.0 / 5.0 + 32.0
                print(f"Temperature detected: {temperature_c:.2f} C / {temperature_f:.2f} F")
                printed_temperature_detected = True

        tilt_raw_deg = extract_tilt_degrees(payload)
        if tilt_raw_deg is not None:
            latest_tilt_raw_deg = tilt_raw_deg
            if web_viewer is not None:
                web_viewer.state.update_status(
                    tilt_available=True,
                    tilt_degrees=tilt_direction * relative_angle_degrees(
                        tilt_raw_deg, tilt_zero_deg
                    ),
                    tilt_reversed=tilt_reversed,
                )
        roll_deg = extract_roll_degrees(payload)
        if roll_deg is not None:
            latest_roll_raw_deg = roll_deg
            if web_viewer is not None:
                web_viewer.state.update_status(
                    roll_available=True,
                    roll_degrees=roll_direction * relative_angle_degrees(
                        roll_deg, roll_zero_deg
                    ),
                    roll_reversed=roll_reversed,
                )

        if packet_logger is not None and not is_image_chunk(payload, stream_filter):
            packet_logger.log(pkt, payload)
            return

        result = decode_packet_frame(payload, assembler)
        if result is None:
            return

        frame_id, img, prejpg = result
        captured_at = float(getattr(pkt, "time", time.time()))
        last_frame_time = time.time()
        raw_view_img, raw_record_frame = prepare_raw_view_and_record_frame(img)
        # Controls may change the writer only between complete raw saves.
        # Rendering never acquires this lock.
        with recording.lock:
            check_recording_storage()
            recording_error = record_frame(recording, raw_record_frame)
            if recording_error is not None:
                args.autosave_raw = False
                report_recording_error(recording_error)
            if recording.png_enabled and recording.active:
                try:
                    save_decoded_frame(
                        args.frames_dir, frame_id, raw_view_img, prejpg,
                        img.shape, capture_time=captured_at,
                        temperature_c=latest_temperature_c,
                    )
                    polar_frames_saved += 1
                except (OSError, RuntimeError, cv2.error) as exc:
                    recording.png_enabled = False
                    if not recording.video_enabled:
                        recording.requested = False
                        recording.auto_segment_seconds = None
                        error = recording.finish()
                        if error:
                            report_recording_error(error)
                    message = f"PNG and metadata recording disabled: {exc}"
                    report_recording_error(message)
        if args.save:
            out = args.save / f"frame_{frame_id:06d}.jpg"
            if not cv2.imwrite(str(out), raw_view_img):
                raise RuntimeError(f"Could not write polar frame: {out}")
        capture_fps = frame_rate.tick(time.monotonic())
        if web_viewer is not None:
            web_viewer.state.update_status(
                fps=capture_fps,
                capture_fps=capture_fps,
                polar_frames_saved=polar_frames_saved,
                record_video_enabled=recording.video_enabled,
                record_png_enabled=recording.png_enabled,
                recording=recording.active,
                recording_requested=recording.requested,
                recording_started_at=recording.segment_started_at,
                raw_autosaved_clips=recording.autosaved_clips,
                autosave_raw=recording.auto_segment_seconds is not None,
                streaming=True, stream_stalled=False, no_signal_state=None,
            )
            web_no_signal_state = None
        # Only display handoffs can be skipped. Raw data was already saved
        # using the original intensities and the packet's capture timestamp.
        img.flags.writeable = False
        latest_display_frame.publish(CapturedFrame(
            frame_id, img, prejpg, captured_at, capture_fps,
        ))

    def observe_captured_frame(frame: CapturedFrame) -> bool:
        nonlocal web_no_signal_state
        frame_id, img, prejpg = frame.frame_id, frame.image, frame.prejpg
        sonar_outcome = (
            control.observe_sonar_frame()
            if control is not None and hasattr(control, "observe_sonar_frame")
            else None
        )
        if sonar_outcome is not None:
            notice = {
                "stream_detected": "Sonar image stream detected",
                "enabled_confirmed": "Sonar enabled; image stream confirmed",
                "disable_failed": (
                    "Warning: sonar failed to disable after repeated attempts; "
                    "the GLS10 continued sending images"
                ),
                "unexpected_stream": (
                    "Sonar stream resumed unexpectedly; showing live images"
                ),
            }.get(sonar_outcome)
            if notice is not None:
                print(notice)
                if web_viewer is not None:
                    web_viewer.state.update_status(
                        sonar_enabled=getattr(
                            control, "current_sonar_enabled", None
                        ),
                        sonar_pending=getattr(
                            control, "pending_sonar_enabled", None
                        ),
                        sonar_requested=getattr(
                            control, "last_requested_sonar_enabled", None
                        ),
                        sonar_transition=getattr(
                            control, "sonar_transition_state", "unknown"
                        ),
                        notice=notice,
                    )
        # A decoded frame after a confirmed stop means the stream is live again;
        # observe_sonar_frame updates the state before this guard is evaluated.
        if getattr(control, "current_sonar_enabled", None) is False:
            return False
        # Clear the browser's retained-frame warning as soon as a frame has
        # been decoded.  Echogram work and display rendering follow below and
        # can be comparatively slow; waiting for them left a stale "stopped"
        # message on screen even while new image packets were arriving.
        if web_viewer is not None:
            web_viewer.state.update_status(
                streaming=True,
                stream_stalled=False,
                no_signal_state=None,
            )
            web_no_signal_state = None

        if control is not None and hasattr(control, "observe_reported_range"):
            reported_range_ft = estimate_selected_range_feet(
                read_float32_from_prejpg(prejpg, RANGE_SCALE_OFFSET)
            )
            requested_range = control.requested_range
            range_outcome = control.observe_reported_range(reported_range_ft)
            if web_viewer is not None and range_outcome == "confirmed":
                web_viewer.state.update_status(
                    range_change_failed=False,
                    range=control.actual_range,
                    range_requested=control.actual_range,
                    range_pending=False,
                    notice=(
                        f"GLS10 confirmed range: "
                        f"{control.current_range} ft"
                    )
                )
            elif web_viewer is not None and range_outcome == "retrying":
                web_viewer.state.update_status(
                    range_requested=control.current_range,
                    range_pending=True,
                    notice=f"Range {control.current_range} ft pending; retrying",
                )
            elif web_viewer is not None and range_outcome == "failed":
                web_viewer.state.update_status(
                    range_change_failed=True,
                    range=control.actual_range,
                    range_requested=control.actual_range,
                    range_pending=False,
                    notice=(
                        f"Warning: range failed to apply; requested "
                        f"{requested_range} ft, GLS10 still reports "
                        f"{reported_range_ft:.1f} ft"
                    )
                )

        return True

    def render_captured_frame(frame: CapturedFrame) -> None:
        nonlocal last_decoded_frame, last_echogram_display, web_no_signal_state
        frame_id, img, prejpg = frame.frame_id, frame.image, frame.prejpg
        display_img = apply_sonar_gain(
            apply_intensity_window(img, intensity_min, intensity_max),
            sonar_gain,
        )

        if echogram is not None:
            echogram_display, echogram_recording_error = update_echogram_window(
                img, img, prejpg, echogram, echogram_recording,
                args, control,
                show_window=not args.web_only,
                crop_at_range=echogram_range_cropped,
            )
            if echogram_display is not None:
                last_echogram_display = echogram_display.copy()
            if echogram_recording_error is not None:
                args.autosave_echogram = False
                report_recording_error(echogram_recording_error)
                if web_viewer is not None:
                    web_viewer.state.update_status(
                        echogram_recording=False,
                        autosave_echogram=False,
                        notice=echogram_recording_error,
                    )
            if web_viewer is not None and echogram_display is not None:
                web_viewer.state.publish_echogram(echogram_display)

        raw_view_img = rotate_raw_view(display_img)
        fps = frame.capture_fps

        display = render_main_display(
            display_img, img, prejpg, raw_view_img, view, motion, args,
            frame_id, control, distance_grid_enabled, sonar_gain,
            crop_at_range=live_range_cropped,
        )
        if display is None:
            return
        last_decoded_frame = (frame_id, display.copy(), fps)

        if web_viewer is not None:
            web_viewer.state.publish(
                display,
                streaming=True,
                stream_stalled=False,
                no_signal_state=None,
                frame_id=frame_id,
                display_fps=display_frame_rate.tick(time.monotonic()),
                display_frames_skipped=latest_display_frame.skipped,
                range=getattr(control, "actual_range", None),
                range_requested=getattr(control, "requested_range", None),
                range_pending=getattr(control, "range_change_pending", False),
                sonar_enabled=getattr(
                    control, "current_sonar_enabled", None
                ),
                sonar_pending=getattr(
                    control, "pending_sonar_enabled", None
                ),
                sonar_requested=getattr(
                    control, "last_requested_sonar_enabled", None
                ),
                sonar_transition=getattr(
                    control, "sonar_transition_state", "unknown"
                ),
                gain=sonar_gain,
                echogram_gain=echogram_gain_percent,
                histogram=intensity_histogram(img),
                intensity_min=intensity_min,
                intensity_max=intensity_max,
                record_video_enabled=recording.video_enabled,
                record_png_enabled=recording.png_enabled,
                recording=recording.active,
                recording_requested=recording.requested,
                echogram_recording=echogram_recording.active,
                recording_started_at=recording.segment_started_at,
                echogram_recording_started_at=echogram_recording.segment_started_at,
                autosave_raw=recording.auto_segment_seconds is not None,
                autosave_echogram=(
                    echogram_recording.auto_segment_seconds is not None
                ),
                autosave_minutes=args.autosave_minutes,
                raw_autosaved_clips=recording.autosaved_clips,
                echogram_autosaved_clips=echogram_recording.autosaved_clips,
                recording_path=str(args.record_video.resolve()),
                echogram_recording_path=str(args.record_echogram.resolve()),
                snapshot_path=str(args.frames_dir.resolve()),
                warp_enabled=view.warp_enabled,
                motion_enabled=motion.enabled,
                color_scheme=args.colorscheme,
            )
            web_no_signal_state = None
        if not args.web_only:
            window_display = compose_main_window(
                display, frame_id, fps, recording, view, motion, control
            )
            cv2.imshow(WINDOW_NAME, window_display)

        if args.save:
            if view.warp_enabled:
                warped_out = args.save / f"frame_{frame_id:06d}_xy.png"
                cv2.imwrite(str(warped_out), display)

        if not args.web_only:
            key = cv2.waitKey(1) & 0xFF
            handle_keyboard(key, view, motion, control)

    packet_logger = OtherPacketLogger(args.other_packets_dir) if args.log_other_packets else None
    stop_requested = threading.Event()
    previous_sigterm = None
    if threading.current_thread() is threading.main_thread():
        previous_sigterm = signal.signal(signal.SIGTERM, lambda *_: stop_requested.set())
    try:
        with ExitStack() as capture_resources:
            if packet_logger is not None:
                capture_resources.enter_context(packet_logger)
            capture_loop = CaptureLoop(handle_packet, iface=args.iface, bpf=bpf, drop_privileges=True)
            capture_resources.callback(capture_loop.close)
            # Readiness means the persistent capture socket has actually opened.
            capture_loop.start()
            from preferences import Preferences
            preferences = Preferences(args.settings_file)
            saved_viewer = preferences.read().get("viewer", {})
            if isinstance(saved_viewer, dict) and web_viewer is not None:
                for key, value in saved_viewer.items():
                    if key in {"set_theme", "set_gain", "set_echogram_gain", "set_intensity_window", "set_color_scheme", "set_recording_formats"} and isinstance(value, dict) and value.get("type") == key:
                        web_viewer.state.actions.put(value)
            saved_paths = preferences.read().get("output_paths")
            if isinstance(saved_paths, dict) and web_viewer is not None:
                args.paths_confirmed = False
                if all(isinstance(saved_paths.get(key), str) for key in ("recording", "echogram", "snapshots")):
                    saved_paths = dict(saved_paths)
                    saved_paths["recording"] = str(unique_file(Path(saved_paths["recording"]).parent / args.record_video.name))
                    saved_paths["echogram"] = str(unique_file(Path(saved_paths["echogram"]).parent / args.record_echogram.name))
                    web_viewer.state.update_status(recording_path=saved_paths["recording"], echogram_recording_path=saved_paths["echogram"], snapshot_path=saved_paths["snapshots"])
                web_viewer.state.update_status(paths_confirmed=False)
                web_viewer.state.actions.put({"type": "set_output_paths", "value": saved_paths})
            if args.ready_file is not None:
                args.ready_file.touch()
            while not stop_requested.is_set():
                capture_loop.check()
                handle_web_actions()
                check_recording_storage()
                if storage_failure is not None:
                    echogram_recording.requested = False
                    echogram_recording.auto_segment_seconds = None
                    args.autosave_echogram = False
                    error = echogram_recording.stop()
                    if error:
                        report_recording_error(error)
                    if web_viewer is not None:
                        web_viewer.state.update_status(recording=False, recording_requested=False,
                            echogram_recording=False, autosave_raw=False, autosave_echogram=False)
                frame = latest_display_frame.take()
                frame_ready = False
                if frame is not None:
                    last_captured_frame = frame
                    frame_ready = observe_captured_frame(frame)
                if display_enabled and last_captured_frame is not None and (frame_ready or force_render):
                    force_render = False
                    render_captured_frame(last_captured_frame)
                show_no_signal_warning_if_needed()
    except KeyboardInterrupt:
        print("\nStopped.")
    except (OSError, CaptureError) as exc:
        print(f"Packet capture failed on interface {args.iface}: {exc}",
              file=sys.stderr)
        if platform.system() == "Windows":
            print(
                "Npcap is required for packet capture on Windows. "
                "Install Npcap manually from https://npcap.com/ with "
                "\"Install Npcap in WinPcap API-compatible Mode\" enabled, "
                "then run PowerShell as Administrator.",
                file=sys.stderr,
            )
        return 2
    finally:
        # Finalize every output even if another cleanup operation fails. Keep
        # the termination handler installed until file finalization is done.
        with ExitStack() as cleanup:
            if previous_sigterm is not None:
                cleanup.callback(signal.signal, signal.SIGTERM, previous_sigterm)
            cleanup.callback(cv2.destroyAllWindows)
            if web_viewer is not None:
                cleanup.callback(web_viewer.close)
            if control is not None:
                cleanup.callback(control.close)
            if echogram is not None:
                cleanup.callback(lambda: report_recording_error(error) if (error := echogram_recording.stop()) else None)
            if recording is not None:
                cleanup.callback(lambda: report_recording_error(error) if (error := recording.finish()) else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
