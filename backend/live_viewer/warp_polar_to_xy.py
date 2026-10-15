#!/usr/bin/env python3
"""
Warp Sonar-ish extracted JPEG frames from (theta, range) space to an X/Y image.

Assumption, based on the captures/scripts in this conversation:
  - Extracted JPEG is grayscale, shape roughly 511 rows x 512 cols.
  - JPEG rows are beams/angles (theta).
  - JPEG columns are range bins/distances.
  - The corresponding .prejpg.bin contains:
      68-byte metadata header
      511 little-endian float32 theta values at offset 68
      padding until the JPEG starts

This script works on:
  1) A single JPG plus its .prejpg.bin,
  2) A directory of frame_XXXXXX.jpg files with matching frame_XXXXXX.prejpg.bin files, or
  3) A saved live-viewer frame_XXXXXX_raw_rotated.png plus frame_XXXXXX_theta.csv.

Example:
  python warp_polar_to_xy.py sonar_out/frame_000123.jpg --pre sonar_out/frame_000123.prejpg.bin --out frame_000123_xy.png

Batch:
  python warp_polar_to_xy.py sonar_out --out xy_out --make-contact-sheet

Color schemes:
  orange      black -> red/orange -> yellow -> white, default
  light-gray  black -> silver -> white
  dark-gray   black -> charcoal -> gray
  ocean-blue  black -> navy -> cyan -> white
  rainbow     black -> violet -> blue -> green -> yellow -> red -> white
  gray        legacy linear grayscale alias
"""

from __future__ import annotations

import argparse
import csv
import math
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw


ColorStop = Tuple[float, Tuple[int, int, int]]


COLOR_SCHEMES: dict[str, Tuple[ColorStop, ...]] = {
    "orange": (
        (0.00, (0, 0, 0)),
        (0.12, (28, 3, 4)),
        (0.28, (94, 13, 10)),
        (0.48, (190, 45, 16)),
        (0.68, (245, 113, 18)),
        (0.84, (255, 205, 24)),
        (1.00, (255, 255, 255)),
    ),
    "gray": (
        (0.00, (0, 0, 0)),
        (1.00, (255, 255, 255)),
    ),
    "light-gray": (
        (0.00, (0, 0, 0)),
        (0.35, (58, 62, 66)),
        (0.70, (174, 180, 184)),
        (1.00, (255, 255, 255)),
    ),
    "dark-gray": (
        (0.00, (0, 0, 0)),
        (0.45, (28, 31, 34)),
        (0.78, (82, 88, 94)),
        (1.00, (166, 174, 180)),
    ),
    "ocean-blue": (
        (0.00, (0, 0, 0)),
        (0.18, (0, 7, 22)),
        (0.38, (0, 28, 72)),
        (0.60, (0, 76, 132)),
        (0.80, (12, 150, 190)),
        (1.00, (210, 242, 248)),
    ),
    "rainbow": (
        (0.00, (0, 0, 0)),
        (0.12, (38, 0, 92)),
        (0.28, (20, 50, 210)),
        (0.44, (0, 190, 235)),
        (0.60, (30, 210, 80)),
        (0.76, (245, 225, 20)),
        (0.90, (245, 55, 12)),
        (1.00, (255, 255, 255)),
    ),
}


def read_theta_table(prejpg_path: Path, n_theta: int, offset: int = 68) -> np.ndarray:
    """Read n_theta little-endian float32 theta values from the pre-JPEG block."""
    data = prejpg_path.read_bytes()
    need = offset + n_theta * 4
    if len(data) < need:
        raise ValueError(f"{prejpg_path} is too short for {n_theta} theta floats at offset {offset}")
    theta = np.frombuffer(data[offset:need], dtype="<f4").astype(np.float64)

    # Basic sanity check. The table should be monotonic and roughly in radians.
    diffs = np.diff(theta)
    if not (np.all(diffs > 0) or np.all(diffs < 0)):
        raise ValueError(f"theta table in {prejpg_path} is not monotonic")
    if np.nanmax(np.abs(theta)) > math.pi * 1.25:
        raise ValueError(f"theta table in {prejpg_path} does not look like radians")
    return theta


def read_theta_csv(theta_csv_path: Path, n_theta: Optional[int] = None) -> np.ndarray:
    """Read theta degrees from a saved live-viewer theta CSV and return radians."""
    values = []
    with theta_csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None or "theta_degrees" not in reader.fieldnames:
            raise ValueError(f"{theta_csv_path} must contain a theta_degrees column")
        for row in reader:
            values.append(float(row["theta_degrees"]))

    if n_theta is not None and len(values) != n_theta:
        raise ValueError(f"{theta_csv_path} has {len(values)} theta rows, but image has {n_theta} rows")

    theta = np.radians(np.asarray(values, dtype=np.float64))
    diffs = np.diff(theta)
    if not (np.all(diffs > 0) or np.all(diffs < 0)):
        raise ValueError(f"theta table in {theta_csv_path} is not monotonic")
    return theta


def polar_image_as_range_theta(img: np.ndarray, theta_count: int) -> np.ndarray:
    """Return polar image as range rows x theta columns."""
    if img.ndim != 2:
        raise ValueError(f"polar image must be 2-D grayscale, got shape {img.shape}")
    if img.shape[1] == theta_count:
        return img
    if img.shape[0] == theta_count and img.shape[1] != theta_count:
        return img.T
    raise ValueError(f"theta count ({theta_count}) does not match polar image shape {img.shape}")


def center_column_valid_span(polar_image: np.ndarray, signal_threshold: int = 8) -> Tuple[int, int, int]:
    """Infer selected-range rows from signal in the center theta column."""
    center_column = polar_image[:, polar_image.shape[1] // 2]
    signal_rows = np.flatnonzero(center_column > signal_threshold)
    if signal_rows.size == 0:
        # Sparse scenes can have no center-beam return, especially at short
        # ranges. Use returns from any beam, then the full range axis when the
        # entire frame is empty, so display geometry remains available.
        signal_rows = np.flatnonzero(
            np.max(polar_image, axis=1) > signal_threshold
        )
    if signal_rows.size == 0:
        return 0, polar_image.shape[0] - 1, polar_image.shape[0]
    first_valid_row = int(signal_rows[0])
    last_valid_row = int(signal_rows[-1])
    return first_valid_row, last_valid_row, last_valid_row - first_valid_row + 1


def resolve_interpolation_mode(mode: str) -> int:
    """Translate a user-facing interpolation name to its OpenCV constant."""
    import cv2

    interpolation_modes = {
        "nearest": cv2.INTER_NEAREST,
        "linear": cv2.INTER_LINEAR,
        "cubic": cv2.INTER_CUBIC,
        "area": cv2.INTER_AREA,
        "lanczos4": cv2.INTER_LANCZOS4,
    }
    normalized_mode = str(mode).lower()
    if normalized_mode not in interpolation_modes:
        options = ", ".join(sorted(interpolation_modes))
        raise ValueError(f"unknown warp interpolation mode {mode!r}; choose one of: {options}")
    return interpolation_modes[normalized_mode]


def bilinear_sample(img: np.ndarray, row_f: np.ndarray, col_f: np.ndarray, fill: int = 0) -> np.ndarray:
    """Sample a 2-D image at floating row/column coordinates using bilinear interpolation."""
    h, w = img.shape

    valid = (row_f >= 0) & (row_f <= h - 1) & (col_f >= 0) & (col_f <= w - 1)

    r0 = np.floor(np.clip(row_f, 0, h - 1)).astype(np.int32)
    c0 = np.floor(np.clip(col_f, 0, w - 1)).astype(np.int32)
    r1 = np.clip(r0 + 1, 0, h - 1)
    c1 = np.clip(c0 + 1, 0, w - 1)

    wr = np.clip(row_f - r0, 0.0, 1.0)
    wc = np.clip(col_f - c0, 0.0, 1.0)

    a = img[r0, c0].astype(np.float32)
    b = img[r0, c1].astype(np.float32)
    c = img[r1, c0].astype(np.float32)
    d = img[r1, c1].astype(np.float32)

    out = (1 - wr) * ((1 - wc) * a + wc * b) + wr * ((1 - wc) * c + wc * d)
    out = np.where(valid, out, fill)
    return np.clip(out, 0, 255).astype(np.uint8)


def apply_color_scheme(img: np.ndarray, color_scheme: str) -> Image.Image:
    """Map an 8-bit luminance image through a color ramp."""
    try:
        stops = COLOR_SCHEMES[color_scheme]
    except KeyError as exc:
        names = ", ".join(sorted(COLOR_SCHEMES))
        raise ValueError(f"unknown color scheme {color_scheme!r}; choose one of: {names}") from exc

    if color_scheme == "gray":
        return Image.fromarray(img, mode="L")

    x = img.astype(np.float32) / 255.0
    rgb = np.zeros((*img.shape, 3), dtype=np.float32)

    for (left_pos, left_color), (right_pos, right_color) in zip(stops, stops[1:]):
        mask = (x >= left_pos) & (x <= right_pos)
        if not np.any(mask):
            continue
        span = max(right_pos - left_pos, 1e-6)
        t = ((x[mask] - left_pos) / span)[:, None]
        left = np.array(left_color, dtype=np.float32)
        right = np.array(right_color, dtype=np.float32)
        rgb[mask] = left + (right - left) * t

    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), mode="RGB")


def warp_polar_image_to_xy_array(
    img: np.ndarray,
    theta: np.ndarray,
    out_width: Optional[int] = None,
    out_height: Optional[int] = None,
    max_range: Optional[float] = None,
    range_offset_bins: float = 0.0,
    theta_offset_deg: float = 0.0,
    flip_theta: bool = False,
    flip_range: bool = False,
    forward_is_up: bool = True,
    signal_threshold: int = 8,
    output_scale: float = 1.0,
    interpolation_mode: str = "linear",
    valid_range_span: Optional[Tuple[int, int, int]] = None,
) -> np.ndarray:
    """Warp one polar range/theta image to an X/Y raster.

    Coordinate convention:
      - Source image is range rows x theta columns.
      - Valid selected-range rows are inferred from the center theta column.
      - theta is radians.
      - x = r * sin(theta)
      - y = r * cos(theta)

    Output image convention when forward_is_up=True:
      - +y / forward is toward the top of the image.
      - x positive is to the right.
    """
    theta = np.asarray(theta, dtype=np.float64)
    img = polar_image_as_range_theta(img, theta_count=theta.size)
    range_count, theta_count = img.shape

    if flip_theta:
        theta = theta[::-1]
        img = img[:, ::-1]
    if flip_range:
        img = img[::-1, :]

    theta = theta + math.radians(theta_offset_deg)
    if valid_range_span is None:
        valid_range_span = center_column_valid_span(
            img,
            signal_threshold=signal_threshold,
        )
    src_col, src_row = _cartesian_remap_maps(
        tuple(theta), valid_range_span, out_width, out_height, max_range,
        range_offset_bins, forward_is_up, output_scale,
    )

    # This mirrors the offline processing pipeline and lets the interpolation
    # strategy be selected without changing the physical mapping.
    import cv2

    return cv2.remap(
        img,
        src_col,
        src_row,
        interpolation=resolve_interpolation_mode(interpolation_mode),
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


@lru_cache(maxsize=4)
def _cartesian_remap_maps(
    theta_values: tuple[float, ...],
    valid_range_span: Tuple[int, int, int],
    out_width: Optional[int],
    out_height: Optional[int],
    max_range: Optional[float],
    range_offset_bins: float,
    forward_is_up: bool,
    output_scale: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Reuse coordinate maps until angles, range geometry or output settings change."""
    theta = np.asarray(theta_values, dtype=np.float64)
    theta_count = theta.size
    first_valid_range_row, last_valid_range_row, valid_range_rows = valid_range_span
    if max_range is None:
        meters_per_range_bin = 1.0
        r_max = float(max(valid_range_rows - 1, 1))
    else:
        meters_per_range_bin = float(max_range) / max(valid_range_rows - 1, 1)
        r_max = float(max_range)

    theta_min = float(np.min(theta))
    theta_max = float(np.max(theta))
    x_min = r_max * math.sin(theta_min)
    x_max = r_max * math.sin(theta_max)

    output_scale = float(output_scale)
    if output_scale <= 0:
        raise ValueError("warp output scale must be greater than 0")
    if out_height is None:
        out_height = int(math.ceil(valid_range_rows * output_scale))
    if out_width is None:
        out_width = int(math.ceil(abs(x_max - x_min) / meters_per_range_bin * output_scale)) + 1

    x = np.linspace(x_min, x_max, out_width, dtype=np.float32)
    if forward_is_up:
        y = np.linspace(r_max, 0.0, out_height, dtype=np.float32)
    else:
        y = np.linspace(0.0, r_max, out_height, dtype=np.float32)
    X, Y = np.meshgrid(x, y)

    R = np.sqrt(X * X + Y * Y)
    range_bin_map = R / meters_per_range_bin
    source_range_map = last_valid_range_row - range_bin_map + range_offset_bins
    TH = np.arctan2(X, Y)

    theta_indices = np.arange(theta_count, dtype=np.float64)
    if theta[0] < theta[-1]:
        src_col = np.interp(TH, theta, theta_indices, left=np.nan, right=np.nan)
    else:
        src_col = np.interp(TH, theta[::-1], theta_indices[::-1], left=np.nan, right=np.nan)

    valid_sector = (
        np.isfinite(src_col)
        & (range_bin_map >= 0)
        & (R <= r_max)
        & (source_range_map >= first_valid_range_row)
        & (source_range_map <= last_valid_range_row)
        & (TH >= theta_min)
        & (TH <= theta_max)
    )
    src_row = np.where(valid_sector, source_range_map, -1.0)
    src_col = np.where(valid_sector, src_col, -1.0)

    src_col = src_col.astype(np.float32)
    src_row = src_row.astype(np.float32)
    src_col.flags.writeable = False
    src_row.flags.writeable = False
    return src_col, src_row


def polar_image_to_xy(
    img: np.ndarray,
    theta: np.ndarray,
    out_path: Path,
    out_width: Optional[int] = None,
    out_height: Optional[int] = None,
    max_range: Optional[float] = None,
    range_offset_bins: float = 0.0,
    theta_offset_deg: float = 0.0,
    flip_theta: bool = False,
    flip_range: bool = False,
    forward_is_up: bool = True,
    color_scheme: str = "orange",
) -> Path:
    """Warp one polar image to an X/Y raster and save it."""
    warped = warp_polar_image_to_xy_array(
        img=img,
        theta=theta,
        out_width=out_width,
        out_height=out_height,
        max_range=max_range,
        range_offset_bins=range_offset_bins,
        theta_offset_deg=theta_offset_deg,
        flip_theta=flip_theta,
        flip_range=flip_range,
        forward_is_up=forward_is_up,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    apply_color_scheme(warped, color_scheme).save(out_path)
    return out_path


def polar_jpeg_to_xy(
    jpg_path: Path,
    prejpg_path: Path,
    out_path: Path,
    out_width: int = 900,
    out_height: int = 900,
    max_range: Optional[float] = None,
    range_offset_bins: float = 0.0,
    theta_offset_deg: float = 0.0,
    flip_theta: bool = False,
    flip_range: bool = False,
    forward_is_up: bool = True,
    color_scheme: str = "orange",
) -> Path:
    """Warp one extracted JPEG using theta from its .prejpg.bin metadata."""
    img = np.asarray(Image.open(jpg_path).convert("L"))
    theta = read_theta_table(prejpg_path, n_theta=img.shape[0])
    img = np.rot90(img, k=1)
    return polar_image_to_xy(
        img=img,
        theta=theta,
        out_path=out_path,
        out_width=out_width,
        out_height=out_height,
        max_range=max_range,
        range_offset_bins=range_offset_bins,
        theta_offset_deg=theta_offset_deg,
        flip_theta=flip_theta,
        flip_range=flip_range,
        forward_is_up=forward_is_up,
        color_scheme=color_scheme,
    )


def saved_live_frame_to_xy(
    image_path: Path,
    theta_csv_path: Path,
    out_path: Path,
    input_rotated_ccw: bool = False,
    out_width: int = 900,
    out_height: int = 900,
    max_range: Optional[float] = None,
    range_offset_bins: float = 0.0,
    theta_offset_deg: float = 0.0,
    flip_theta: bool = False,
    flip_range: bool = False,
    forward_is_up: bool = True,
    color_scheme: str = "orange",
) -> Path:
    """Warp a saved live-viewer raw PNG using theta from frame_XXXXXX_theta.csv."""
    img = np.asarray(Image.open(image_path).convert("L"))
    theta = read_theta_csv(theta_csv_path)
    return polar_image_to_xy(
        img=img,
        theta=theta,
        out_path=out_path,
        out_width=out_width,
        out_height=out_height,
        max_range=max_range,
        range_offset_bins=range_offset_bins,
        theta_offset_deg=theta_offset_deg,
        flip_theta=flip_theta,
        flip_range=flip_range,
        forward_is_up=forward_is_up,
        color_scheme=color_scheme,
    )


def default_pre_for_jpg(jpg: Path) -> Path:
    # frame_000123.jpg -> frame_000123.prejpg.bin
    return jpg.with_suffix(".prejpg.bin")


def iter_jobs(input_path: Path, pre: Optional[Path], out: Path) -> Iterable[Tuple[Path, Path, Path]]:
    if input_path.is_dir():
        out.mkdir(parents=True, exist_ok=True)
        for jpg in sorted(input_path.glob("*.jpg")):
            prejpg = default_pre_for_jpg(jpg)
            if prejpg.exists():
                yield jpg, prejpg, out / f"{jpg.stem}_xy.png"
    else:
        if pre is None:
            pre = default_pre_for_jpg(input_path)
        if out.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            out.mkdir(parents=True, exist_ok=True)
            out_file = out / f"{input_path.stem}_xy.png"
        else:
            out_file = out
        yield input_path, pre, out_file


def default_theta_csv_for_image(image_path: Path) -> Path:
    if image_path.name.endswith("_raw_rotated.png"):
        return image_path.with_name(image_path.name.replace("_raw_rotated.png", "_theta.csv"))
    return image_path.with_name(f"{image_path.stem}_theta.csv")


def iter_theta_csv_jobs(input_path: Path, theta_csv: Optional[Path], out: Path) -> Iterable[Tuple[Path, Path, Path]]:
    if input_path.is_dir():
        out.mkdir(parents=True, exist_ok=True)
        for image_path in sorted(input_path.glob("*_raw_rotated.png")):
            theta_path = default_theta_csv_for_image(image_path)
            if theta_path.exists():
                yield image_path, theta_path, out / f"{image_path.stem}_xy.png"
    else:
        if theta_csv is None:
            theta_csv = default_theta_csv_for_image(input_path)
        if out.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            out.mkdir(parents=True, exist_ok=True)
            out_file = out / f"{input_path.stem}_xy.png"
        else:
            out_file = out
        yield input_path, theta_csv, out_file


def make_contact_sheet(paths: list[Path], out_path: Path, thumb_w: int = 240, cols: int = 4) -> None:
    if not paths:
        return
    thumbs = []
    for p in paths:
        im = Image.open(p).convert("RGB")
        ratio = thumb_w / im.width
        im = im.resize((thumb_w, max(1, int(im.height * ratio))), Image.Resampling.LANCZOS)
        thumbs.append((p, im))
    rows = (len(thumbs) + cols - 1) // cols
    cell_h = max(im.height for _, im in thumbs) + 22
    sheet = Image.new("RGB", (cols * thumb_w, rows * cell_h), (0, 0, 0))
    draw = ImageDraw.Draw(sheet)
    for i, (p, im) in enumerate(thumbs):
        x = (i % cols) * thumb_w
        y = (i // cols) * cell_h
        sheet.paste(im, (x, y))
        draw.text((x + 3, y + im.height + 3), p.stem, fill=(220, 220, 220))
    sheet.save(out_path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Warp extracted sonar theta/range JPEGs into X/Y fan images.")
    ap.add_argument("input", type=Path, help="frame.jpg, frame_XXXXXX_raw_rotated.png, or a matching frame directory")
    ap.add_argument("--pre", type=Path, default=None, help="matching .prejpg.bin for single-image mode")
    ap.add_argument("--theta-csv", type=Path, default=None, help="matching frame_XXXXXX_theta.csv for saved live-viewer PNGs")
    ap.add_argument("--input-rotated-ccw", action="store_true", help="accepted for compatibility; rotated raw PNGs are handled directly")
    ap.add_argument("--out", type=Path, default=Path("xy_out"), help="output PNG path, or output directory for batch mode")
    ap.add_argument("--width", type=int, default=900, help="output image width")
    ap.add_argument("--height", type=int, default=900, help="output image height")
    ap.add_argument("--max-range", type=float, default=None, help="real-world max range; otherwise range units are source bins")
    ap.add_argument("--range-offset-bins", type=float, default=0.0, help="add this source-row offset to the range mapping")
    ap.add_argument("--theta-offset-deg", type=float, default=0.0, help="rotate theta table by this many degrees")
    ap.add_argument("--flip-theta", action="store_true", help="reverse theta columns if left/right is backwards")
    ap.add_argument("--flip-range", action="store_true", help="reverse range rows if near/far is backwards")
    ap.add_argument("--forward-down", action="store_true", help="put forward/far range at bottom instead of top")
    ap.add_argument(
        "--colorscheme",
        "--colourscheme",
        "--color-scheme",
        "--colour-scheme",
        default="orange",
        choices=sorted(COLOR_SCHEMES),
        help="output color ramp; default: orange",
    )
    ap.add_argument("--make-contact-sheet", action="store_true", help="batch mode: write xy_contact_sheet.jpg")
    args = ap.parse_args()

    written: list[Path] = []
    if args.theta_csv is not None or args.input_rotated_ccw:
        for image_path, theta_path, out_file in iter_theta_csv_jobs(args.input, args.theta_csv, args.out):
            print(f"warping {image_path.name} using {theta_path.name} -> {out_file}")
            written.append(
                saved_live_frame_to_xy(
                    image_path=image_path,
                    theta_csv_path=theta_path,
                    out_path=out_file,
                    input_rotated_ccw=args.input_rotated_ccw,
                    out_width=args.width,
                    out_height=args.height,
                    max_range=args.max_range,
                    range_offset_bins=args.range_offset_bins,
                    theta_offset_deg=args.theta_offset_deg,
                    flip_theta=args.flip_theta,
                    flip_range=args.flip_range,
                    forward_is_up=not args.forward_down,
                    color_scheme=args.colorscheme,
                )
            )
    else:
        for jpg, prejpg, out_file in iter_jobs(args.input, args.pre, args.out):
            print(f"warping {jpg.name} using {prejpg.name} -> {out_file}")
            written.append(
                polar_jpeg_to_xy(
                    jpg_path=jpg,
                    prejpg_path=prejpg,
                    out_path=out_file,
                    out_width=args.width,
                    out_height=args.height,
                    max_range=args.max_range,
                    range_offset_bins=args.range_offset_bins,
                    theta_offset_deg=args.theta_offset_deg,
                    flip_theta=args.flip_theta,
                    flip_range=args.flip_range,
                    forward_is_up=not args.forward_down,
                    color_scheme=args.colorscheme,
                )
            )

    print(f"wrote {len(written)} warped image(s)")
    if args.make_contact_sheet and written:
        sheet = args.out / "xy_contact_sheet.jpg" if args.out.suffix == "" else args.out.with_name("xy_contact_sheet.jpg")
        make_contact_sheet(written[:24], sheet)
        print(f"contact sheet: {sheet}")


if __name__ == "__main__":
    main()
