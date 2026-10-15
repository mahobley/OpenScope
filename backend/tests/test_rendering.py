"""Check rendering optimizations preserve pixels and changing geometry."""

import cv2
import numpy as np
import pytest

from live_viewer.view import cartesian_vertical_crop, colorize_for_cv2
from live_viewer.warp_polar_to_xy import (
    COLOR_SCHEMES,
    _cartesian_remap_maps,
    apply_color_scheme,
    warp_polar_image_to_xy_array,
)


@pytest.mark.parametrize("scheme", COLOR_SCHEMES)
def test_color_lookup_preserves_every_intensity(scheme):
    image = np.tile(np.arange(256, dtype=np.uint8), (3, 1))
    original = np.asarray(apply_color_scheme(image, scheme))
    conversion = cv2.COLOR_GRAY2BGR if original.ndim == 2 else cv2.COLOR_RGB2BGR
    np.testing.assert_array_equal(colorize_for_cv2(image, scheme),
                                  cv2.cvtColor(original, conversion))


def test_cached_warp_uses_new_pixels_and_invalidates_changed_geometry(monkeypatch):
    _cartesian_remap_maps.cache_clear()
    theta = np.linspace(-0.7, 0.7, 31)
    image = np.random.default_rng(42).integers(0, 256, (48, 31), dtype=np.uint8)
    settings = [
        {},
        {"flip_theta": True},
        {"flip_range": True},
        {"theta_offset_deg": 5},
        {"range_offset_bins": 2},
        {"forward_is_up": False},
        {"output_scale": 0.5},
        {"out_width": 80, "out_height": 60},
        {"max_range": 12},
        {"valid_range_span": (3, 47, 45)},
    ]
    for options in settings:
        kwargs = {"valid_range_span": (0, 47, 48), **options}
        first = warp_polar_image_to_xy_array(image, theta, **kwargs)
        changed = warp_polar_image_to_xy_array(255 - image, theta, **kwargs)
        assert not np.array_equal(first, changed)
        hits = _cartesian_remap_maps.cache_info().hits
        assert hits > 0
        with monkeypatch.context() as patch:
            patch.setattr("live_viewer.warp_polar_to_xy._cartesian_remap_maps",
                          _cartesian_remap_maps.__wrapped__)
            expected = warp_polar_image_to_xy_array(255 - image, theta, **kwargs)
        np.testing.assert_array_equal(changed, expected)


@pytest.mark.parametrize("height", [256, 512, 1024])
@pytest.mark.parametrize("forward_is_up", [True, False])
def test_fixed_vertical_crop_preserves_off_axis_returns(height, forward_is_up):
    # Selected centre-beam extent is 351 bins. Off-axis returns reach 450
    # bins radially but never exceed 351 vertically.
    theta = np.linspace(-1.2, 1.2, 511)
    radius = np.arange(511, -1, -1)[:, None]
    polar = np.where((radius * np.cos(theta) <= 351) & (radius <= 450),
                     100, 0).astype(np.uint8)
    source = np.rot90(polar, -1).copy()
    warped = warp_polar_image_to_xy_array(
        polar, theta, out_height=height, forward_is_up=forward_is_up,
        valid_range_span=(61, 511, 451), interpolation_mode="nearest",
    )
    crop = cartesian_vertical_crop(source, height, forward_is_up=forward_is_up)
    kept = np.zeros_like(warped)
    kept[crop] = warped[crop]
    np.testing.assert_array_equal(kept, warped)
    assert kept[crop].shape[0] < height
    assert kept[crop].shape[1] == warped.shape[1]


def test_fixed_vertical_crop_is_stable_with_sparse_or_empty_returns():
    full = np.full((3, 512), 100, dtype=np.uint8)
    empty = np.zeros_like(full)
    assert cartesian_vertical_crop(full, 512) == slice(158, None)
    assert cartesian_vertical_crop(empty, 512) == slice(158, None)


def test_fixed_vertical_crop_honours_range_offset():
    source = np.full((3, 512), 100, dtype=np.uint8)
    assert cartesian_vertical_crop(source, 512, range_offset_bins=10) == slice(148, None)
