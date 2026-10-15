from __future__ import annotations

import math
from functools import lru_cache
from typing import Optional

import cv2
import numpy as np

from live_viewer.warp_polar_to_xy import (
    COLOR_SCHEMES,
    apply_color_scheme,
    warp_polar_image_to_xy_array,
)
from live_viewer.outputs import (
    RANGE_SCALE_OFFSET,
    estimate_selected_range_feet,
    feet_to_meters,
    read_float32_from_prejpg,
)

FEET_PER_METER = 3.280839895
GRID_TARGET_INTERVALS = 10
# The observed sonar footprint reaches at most this many range bins in Y,
# even when off-axis returns extend farther in radial distance.
CARTESIAN_VERTICAL_RANGE_BINS = 351


def effective_grid_spacing_ft(selected_range_ft: float, minimum_spacing_ft: float) -> float:
    """Choose a readable grid interval for the selected range.

    ``minimum_spacing_ft`` remains the interval for short ranges, while longer
    ranges use a larger multiple of it so the display does not become filled
    with closely spaced rings or horizontal lines.
    """
    spacing = float(minimum_spacing_ft)
    if spacing <= 0:
        raise ValueError("grid spacing must be greater than 0")
    range_ft = float(selected_range_ft)
    if not np.isfinite(range_ft) or range_ft <= 0:
        raise ValueError("selected range must be greater than 0")
    target_spacing = range_ft / GRID_TARGET_INTERVALS
    return spacing * max(1, math.ceil(target_spacing / spacing))


def read_theta_table_from_prejpg(prejpg: bytes, n_theta: int, offset: int = 68) -> np.ndarray:
    """Read n_theta little-endian float32 theta values from the pre-JPEG bytes."""
    need = offset + n_theta * 4
    if len(prejpg) < need:
        raise ValueError(f"pre-JPEG block is too short for {n_theta} theta floats at offset {offset}")

    theta = np.frombuffer(prejpg[offset:need], dtype="<f4").astype(np.float64)
    diffs = np.diff(theta)
    if not (np.all(diffs > 0) or np.all(diffs < 0)):
        raise ValueError("theta table is not monotonic")
    if np.nanmax(np.abs(theta)) > math.pi * 1.25:
        raise ValueError("theta table does not look like radians")
    return theta


def polar_image_to_xy(
    img: np.ndarray,
    prejpg: bytes,
    geometry_img: Optional[np.ndarray] = None,
    out_width: Optional[int] = None,
    out_height: Optional[int] = None,
    max_range: Optional[float] = None,
    range_offset_bins: float = 0.0,
    theta_offset_deg: float = 0.0,
    flip_theta: bool = False,
    flip_range: bool = False,
    forward_is_up: bool = True,
    output_scale: float = 1.0,
    interpolation_mode: str = "linear",
    include_all_range_rows: bool = False,
) -> np.ndarray:
    """Warp all available ranges in one grayscale X/Y raster."""
    n_theta = img.shape[0]
    theta = read_theta_table_from_prejpg(prejpg, n_theta=n_theta)
    img = rotate_raw_view(img)
    geometry = (
        img
        if geometry_img is None
        else rotate_raw_view(geometry_img)
    )
    if max_range is None:
        range_scale = read_float32_from_prejpg(prejpg, RANGE_SCALE_OFFSET)
        max_range = feet_to_meters(estimate_selected_range_feet(range_scale))
        if max_range is None:
            raise ValueError("frame metadata does not include a selected range")
    valid_range_span = center_column_valid_span_for_warp(
        geometry, flip_theta, flip_range
    )
    first_row, last_row, selected_rows = valid_range_span
    extent_geometry = geometry
    if flip_theta:
        extent_geometry = extent_geometry[:, ::-1]
    if flip_range:
        extent_geometry = extent_geometry[::-1, :]
    signal_rows = np.flatnonzero(np.max(extent_geometry, axis=1) > 8)
    outer_first_row = (
        0
        if include_all_range_rows
        else (
            min(first_row, int(signal_rows[0]))
            if signal_rows.size else first_row
        )
    )
    expanded_rows = last_row - outer_first_row + 1
    expanded_max_range = (
        float(max_range) * max(expanded_rows - 1, 1)
        / max(selected_rows - 1, 1)
    )
    return warp_polar_image_to_xy_array(
        img=img,
        theta=theta,
        out_width=out_width,
        out_height=out_height,
        max_range=expanded_max_range,
        range_offset_bins=range_offset_bins,
        theta_offset_deg=theta_offset_deg,
        flip_theta=flip_theta,
        flip_range=flip_range,
        forward_is_up=forward_is_up,
        output_scale=output_scale,
        interpolation_mode=interpolation_mode,
        valid_range_span=(outer_first_row, last_row, expanded_rows),
    )


def blank_cartesian_headroom_rows(
    image: np.ndarray,
    signal_threshold: int = 3,
) -> int:
    """Return the contiguous blank-row count at the top of a Cartesian warp."""
    if image.ndim != 2:
        raise ValueError("Cartesian headroom detection expects a grayscale image")
    # An isolated return can otherwise preserve a large empty band.  Start
    # where a meaningful portion of the row contains sonar data; fall back to
    # any return for sparse scenes.
    required_pixels = max(1, int(math.ceil(image.shape[1] * 0.03)))
    rows_with_signal = np.flatnonzero(
        np.count_nonzero(image > signal_threshold, axis=1) >= required_pixels
    )
    if rows_with_signal.size == 0:
        rows_with_signal = np.flatnonzero(
            np.max(image, axis=1) > signal_threshold
        )
    # Do not turn an entirely empty frame into a zero-height image.
    return int(rows_with_signal[0]) if rows_with_signal.size else 0


def cartesian_vertical_crop(
    geometry_img: np.ndarray,
    output_height: int,
    flip_theta: bool = False,
    flip_range: bool = False,
    forward_is_up: bool = True,
    range_offset_bins: float = 0.0,
    interpolation_mode: str = "linear",
) -> slice:
    """Map the fixed polar Y extent into the expanded Cartesian raster.

    Keep the full width: returns beyond the centre-line range can still
    contribute horizontally. Round outward so the boundary row is retained.
    """
    geometry = rotate_raw_view(geometry_img)
    if flip_theta:
        geometry = geometry[:, ::-1]
    if flip_range:
        geometry = geometry[::-1, :]
    first_row, last_row, _ = center_column_valid_span_for_warp(
        geometry, False, False
    )
    signal_rows = np.flatnonzero(np.max(geometry, axis=1) > 8)
    outer_first_row = min(first_row, int(signal_rows[0])) if signal_rows.size else first_row
    expanded_bins = max(last_row - outer_first_row, 1)
    # Sampling neighbouring range/angle bins can spread a boundary return
    # slightly beyond its geometric position. Retain a small kernel margin.
    margin_bins = {"nearest": 1, "linear": 2, "area": 2,
                   "cubic": 3, "lanczos4": 5}[interpolation_mode]
    vertical_bins = max(
        0.0, CARTESIAN_VERTICAL_RANGE_BINS + range_offset_bins + margin_bins
    )
    fraction = min(1.0, vertical_bins / expanded_bins)
    trim = int(math.floor((1.0 - fraction) * max(output_height - 1, 0)))
    return slice(trim, None) if forward_is_up else slice(None, output_height - trim)


def crop_raw_view_to_selected_range(display: np.ndarray, geometry_img: np.ndarray) -> np.ndarray:
    """Remove raw/polar rows outside the center-beam selected range."""
    from live_viewer.warp_polar_to_xy import center_column_valid_span

    first_row, last_row, _ = center_column_valid_span(rotate_raw_view(geometry_img))
    return display[first_row:last_row + 1, :]


def crop_cartesian_to_selected_range(display: np.ndarray, geometry_img: np.ndarray,
                                     flip_theta: bool = False, flip_range: bool = False) -> np.ndarray:
    """Trim X/Y columns that only show data beyond the selected range."""
    from live_viewer.warp_polar_to_xy import center_column_valid_span

    geometry = rotate_raw_view(geometry_img)
    if flip_theta:
        geometry = geometry[:, ::-1]
    if flip_range:
        geometry = geometry[::-1, :]
    first_row, last_row, selected_rows = center_column_valid_span(geometry)
    signal_rows = np.flatnonzero(np.max(geometry, axis=1) > 8)
    outer_first_row = min(first_row, int(signal_rows[0])) if signal_rows.size else first_row
    expanded_rows = last_row - outer_first_row + 1
    if expanded_rows <= 1 or selected_rows >= expanded_rows:
        return display
    scale = (selected_rows - 1) / (expanded_rows - 1)
    left = int(round((1.0 - scale) * display.shape[1] / 2.0))
    return display[:, left:display.shape[1] - left]


def echogram_full_coverage_boundary(
    image: np.ndarray,
    flip_theta: bool = False,
    flip_range: bool = False,
) -> float:
    """Return the normalized row where center-beam coverage starts.

    Rows before this boundary are retained in the all-range echogram, but do
    not have the center-beam return used to establish the selected range.
    """
    geometry = rotate_raw_view(image)
    first_row, last_row, _ = center_column_valid_span_for_warp(
        geometry, flip_theta, flip_range
    )
    return float(np.clip(first_row / max(last_row, 1), 0.0, 1.0))


def center_column_valid_span_for_warp(
    image: np.ndarray,
    flip_theta: bool,
    flip_range: bool,
) -> tuple[int, int, int]:
    """Measure warp geometry from an unfiltered frame with matching flips."""
    from live_viewer.warp_polar_to_xy import center_column_valid_span

    geometry = image
    if flip_theta:
        geometry = geometry[:, ::-1]
    if flip_range:
        geometry = geometry[::-1, :]
    return center_column_valid_span(geometry)


def add_polar_grid_and_outer_data(
    display: np.ndarray,
    img: np.ndarray,
    prejpg: bytes,
    max_range: Optional[float] = None,
    theta_offset_deg: float = 0.0,
    flip_theta: bool = False,
    flip_range: bool = False,
    forward_is_up: bool = True,
    grid_spacing_ft: float = 2.0,
    signal_threshold: int = 8,
    geometry_img: Optional[np.ndarray] = None,
    color_scheme: str = "orange",
    show_grid: bool = True,
) -> np.ndarray:
    """Overlay a distance grid on a unified all-range Cartesian image."""
    from live_viewer.warp_polar_to_xy import center_column_valid_span

    polar = rotate_raw_view(img)
    theta = read_theta_table_from_prejpg(prejpg, n_theta=img.shape[0])
    if flip_theta:
        theta = theta[::-1]
        polar = polar[:, ::-1]
    if flip_range:
        polar = polar[::-1, :]
    theta = theta + math.radians(theta_offset_deg)

    geometry = (
        polar
        if geometry_img is None
        else rotate_raw_view(geometry_img)
    )
    if flip_theta:
        geometry = geometry[:, ::-1]
    if flip_range:
        geometry = geometry[::-1, :]
    first_row, last_row, valid_rows = center_column_valid_span(
        geometry, signal_threshold
    )
    if max_range is None:
        range_scale = read_float32_from_prejpg(prejpg, RANGE_SCALE_OFFSET)
        max_range = feet_to_meters(estimate_selected_range_feet(range_scale))
    if max_range is None or not np.isfinite(max_range) or max_range <= 0:
        raise ValueError("frame metadata does not include a valid selected range")

    feet_per_bin = max_range * FEET_PER_METER / max(valid_rows - 1, 1)
    selected_range_ft = max_range * FEET_PER_METER
    theta_min, theta_max = float(np.min(theta)), float(np.max(theta))

    signal_rows = np.flatnonzero(np.max(geometry, axis=1) > signal_threshold)
    outer_first_row = (
        min(first_row, int(signal_rows[0]))
        if signal_rows.size else first_row
    )
    plot_y_max = (last_row - outer_first_row) * feet_per_bin
    plot_y_max = max(selected_range_ft, plot_y_max)
    plot_x_min = plot_y_max * math.sin(theta_min)
    plot_x_max = plot_y_max * math.sin(theta_max)
    spacing = effective_grid_spacing_ft(selected_range_ft, grid_spacing_ft)
    canvas = display.copy()
    plot_h, plot_w = canvas.shape[:2]
    pixels_per_ft_x = (plot_w - 1) / max(plot_x_max - plot_x_min, 1e-6)
    pixels_per_ft_y = (plot_h - 1) / max(plot_y_max, 1e-6)

    def point(x_ft: float, y_ft: float) -> tuple[int, int]:
        """Return an integer pixel coordinate for labels and bounds."""
        px = int(round((x_ft - plot_x_min) * pixels_per_ft_x))
        py_from_bottom = int(round(y_ft * pixels_per_ft_y))
        py = plot_h - 1 - py_from_bottom if forward_is_up else py_from_bottom
        return px, py

    # Keep the grid geometry at sub-pixel precision. Rounding every sampled
    # point before drawing made shallow arcs visibly stair-step in Cartesian
    # mode, especially when a browser scaled the JPEG further.
    fixed_shift = 4
    fixed_scale = 1 << fixed_shift

    def point_fixed(x_ft: float, y_ft: float) -> tuple[int, int]:
        px = (x_ft - plot_x_min) * pixels_per_ft_x
        py_from_bottom = y_ft * pixels_per_ft_y
        py = plot_h - 1 - py_from_bottom if forward_is_up else py_from_bottom
        return int(round(px * fixed_scale)), int(round(py * fixed_scale))
    if not show_grid:
        return canvas

    overlay = canvas.copy()
    grid_color = (255, 255, 255)
    range_ticks = np.arange(spacing, plot_y_max + spacing * 0.5, spacing)
    pixels_per_ft = min(pixels_per_ft_x, pixels_per_ft_y)
    label_stride = max(1, int(math.ceil(30.0 / max(spacing * pixels_per_ft, 1.0))))
    # The denser sampling and fixed-point coordinates give OpenCV's antialiased
    # rasterizer a smooth path instead of a chain of rounded line segments.
    arc_angles = np.linspace(theta_min, theta_max, 721)
    arc_sines = np.sin(arc_angles)
    arc_cosines = np.cos(arc_angles)
    for index, tick in enumerate(range_ticks):
        arc_x = (tick * arc_sines - plot_x_min) * pixels_per_ft_x
        arc_y = tick * arc_cosines * pixels_per_ft_y
        if forward_is_up:
            arc_y = plot_h - 1 - arc_y
        arc = np.rint(
            np.column_stack((arc_x, arc_y)) * fixed_scale
        ).astype(np.int32)
        cv2.polylines(
            overlay, [arc], False, grid_color, 1, cv2.LINE_AA,
            shift=fixed_shift,
        )
        if index % label_stride == 0:
            label_x, label_y = point(0.0, float(tick))
            cv2.putText(canvas, f"{tick:g} ft", (label_x + 5, max(12, label_y - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, grid_color, 1)

    # Bearing spokes make the grid read as a sonar fan. Keep them far enough
    # apart to avoid obscuring narrow or low-resolution views.
    min_spoke_step = math.radians(10.0)
    spoke_step = max(min_spoke_step, 24.0 / max(plot_y_max * pixels_per_ft, 1.0))
    first_spoke = math.ceil(theta_min / spoke_step) * spoke_step
    for angle in np.arange(first_spoke, theta_max + spoke_step * 0.5, spoke_step):
        cv2.line(
            overlay,
            point_fixed(0.0, 0.0),
            point_fixed(
                plot_y_max * math.sin(float(angle)),
                plot_y_max * math.cos(float(angle)),
            ),
            grid_color,
            1,
            cv2.LINE_AA,
            shift=fixed_shift,
        )
    cv2.addWeighted(overlay, 0.25, canvas, 0.75, 0, canvas)

    return canvas


# Compatibility for callers importing the old Cartesian-grid name.
add_cartesian_grid_and_outer_data = add_polar_grid_and_outer_data


def add_raw_range_scale(
    display: np.ndarray,
    raw_view_img: np.ndarray,
    prejpg: bytes,
    max_range: Optional[float] = None,
    spacing_ft: float = 2.0,
    signal_threshold: int = 8,
    geometry_img: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Overlay horizontal distance ticks on the unwarped polar view."""
    from live_viewer.warp_polar_to_xy import center_column_valid_span

    if max_range is None:
        range_scale = read_float32_from_prejpg(prejpg, RANGE_SCALE_OFFSET)
        max_range = feet_to_meters(estimate_selected_range_feet(range_scale))
    if max_range is None or not np.isfinite(max_range) or max_range <= 0:
        raise ValueError("frame metadata does not include a valid selected range")
    selected_range_ft = max_range * FEET_PER_METER
    spacing_ft = effective_grid_spacing_ft(selected_range_ft, spacing_ft)

    try:
        geometry = (
            raw_view_img
            if geometry_img is None
            else rotate_raw_view(geometry_img)
        )
        first_row, last_row, valid_rows = center_column_valid_span(
            geometry, signal_threshold
        )
    except ValueError:
        # Very short ranges and empty water can leave the center column below
        # the signal threshold. Distance still maps across the raw range axis.
        first_row = 0
        last_row = max(0, raw_view_img.shape[0] - 1)
        valid_rows = max(1, raw_view_img.shape[0])
    overlay = display.copy()
    width = display.shape[1]
    label_x = max(4, width - 48)
    ticks = np.arange(
        0.0, selected_range_ft + spacing_ft * 0.5, spacing_ft
    )
    pixels_per_tick = spacing_ft * max(valid_rows - 1, 1) / selected_range_ft
    label_stride = max(1, int(math.ceil(28.0 / max(pixels_per_tick, 1.0))))

    for index, tick in enumerate(ticks):
        y = int(round(
            last_row - tick * max(valid_rows - 1, 1) / selected_range_ft
        ))
        y = int(np.clip(y, first_row, last_row))
        cv2.line(overlay, (0, y), (width - 1, y), (255, 255, 255), 1)
        if index % label_stride == 0 or index == len(ticks) - 1:
            cv2.rectangle(
                display,
                (label_x - 3, max(0, y - 13)),
                (width - 1, min(display.shape[0] - 1, y + 4)),
                (20, 20, 20),
                thickness=-1,
            )
            cv2.putText(
                display,
                f"{tick:g} ft",
                (label_x, max(11, y)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.38,
                (255, 255, 255),
                1,
            )
    cv2.addWeighted(overlay, 0.22, display, 0.78, 0, display)
    return display


def colorize_for_cv2(img: np.ndarray, color_scheme: str) -> np.ndarray:
    """Convert an 8-bit grayscale image into an OpenCV-displayable BGR image."""
    return _bgr_color_table(color_scheme)[img]


@lru_cache(maxsize=len(COLOR_SCHEMES))
def _bgr_color_table(color_scheme: str) -> np.ndarray:
    """Build the existing ramp once for every possible 8-bit intensity."""
    ramp = np.arange(256, dtype=np.uint8)[None, :]
    arr = np.asarray(apply_color_scheme(ramp, color_scheme))
    if arr.ndim == 2:
        table = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)[0]
    else:
        table = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)[0]
    table.flags.writeable = False
    return table


def rotate_raw_view(img: np.ndarray) -> np.ndarray:
    """Rotate the decoded raw sonar frame into the preferred viewing orientation."""
    return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)


def prepare_raw_view_and_record_frame(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return the rotated raw display frame and the BGR frame used for raw recording."""
    raw_view_img = rotate_raw_view(img)
    raw_record_frame = cv2.cvtColor(raw_view_img, cv2.COLOR_GRAY2BGR)
    return raw_view_img, raw_record_frame
