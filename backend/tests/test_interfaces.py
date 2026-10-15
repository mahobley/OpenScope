import importlib
import http.client
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import browser_controller
import head_unit_emulator as emulator
from browser_controller import (
    ControllerState,
    INTERFACE_LOG_RE,
    STREAM_RATE_LOG_RE,
    SessionConfig,
    WEB_ROOT as CONTROLLER_WEB_ROOT,
    action_feedback_from_log,
    build_emulator_command,
    interface_link_state,
    _loopback_bind_address,
    request_is_same_origin as controller_same_origin,
)
import cv2
import numpy as np
from live_viewer.outputs import (
    intensity_histogram,
    preflight_output_paths,
)
from live_viewer.control import EmulatorControlClient, SonarControlClient
from live_viewer.echogram import EchogramState
from live_viewer.packet_logger import (
    extract_roll_degrees,
    extract_tilt_degrees,
    relative_angle_degrees,
)
from live_viewer import permissions
from live_viewer import web as viewer_web
from live_viewer.rate import RecentFrameRate
from live_viewer.recording import (
    EchogramImageRecordingState,
    RecordingState,
)
from live_viewer.state import (
    MotionState,
    ViewState,
    apply_intensity_window,
    apply_sonar_gain,
)
from live_viewer.live_viewer import (
    draw_frozen_frame_status,
    handle_keyboard,
    render_main_display,
    update_echogram_window,
    write_snapshot,
)
from live_viewer.web import (
    BrowserViewerState,
    WEB_ROOT,
    choose_output_directory,
    request_is_same_origin as viewer_same_origin,
    validate_loopback_bind,
)
from live_viewer.view import (
    center_column_valid_span_for_warp,
    echogram_full_coverage_boundary,
    effective_grid_spacing_ft,
    polar_image_to_xy as live_polar_image_to_xy,
)
from live_viewer.warp_polar_to_xy import (
    COLOR_SCHEMES,
    apply_color_scheme,
)


def test_packet_dependencies_are_importable():
    dpkt = importlib.import_module("dpkt")
    adapter = importlib.import_module("packet_io")
    assert dpkt.__file__
    for name in ("CaptureSocket", "AsyncCapture", "list_interfaces", "decode_packet"):
        assert hasattr(adapter, name)


def make_iface(
    name,
    *,
    description="Ethernet adapter",
    network_name=None,
    addresses=(),
    flags="UP+BROADCAST",
    mac="00:11:22:33:44:55",
):
    return SimpleNamespace(
        name=name,
        description=description,
        network_name=network_name or name,
        ips={4: list(addresses)},
        flags=flags,
        mac=mac,
    )


def test_recent_frame_rate_drops_old_session_history():
    rate = RecentFrameRate(window_seconds=5.0)

    # A slow startup must not depress the displayed rate for the whole session.
    for timestamp in (0.0, 1.0, 2.0):
        rate.tick(timestamp)
    for index in range(1, 51):
        fps = rate.tick(10.0 + index / 10.0)

    assert abs(fps - 10.0) < 1e-9


def test_recent_frame_rate_requires_two_distinct_timestamps():
    rate = RecentFrameRate()

    assert rate.tick(1.0) == 0.0


def test_extracts_experimental_gls10_tilt_and_wraps_relative_angle():
    payload = bytearray(40)
    payload[8:12] = bytes.fromhex("1c000800")
    import struct
    struct.pack_into("<f", payload, 16, np.deg2rad(89.5))
    struct.pack_into("<f", payload, 20, np.deg2rad(-3.25))

    assert abs(extract_tilt_degrees(bytes(payload)) - 89.5) < 0.001
    assert abs(extract_roll_degrees(bytes(payload)) + 3.25) < 0.001
    assert extract_tilt_degrees(bytes(payload[:-1])) is None
    assert relative_angle_degrees(-175.0, 175.0) == 10.0
    assert -relative_angle_degrees(90.0, 90.0) == 0.0
    assert -relative_angle_degrees(60.0, 90.0) == 30.0
    assert -relative_angle_degrees(120.0, 90.0) == -30.0


def test_recording_failure_disables_only_the_failed_output(tmp_path):
    recording = RecordingState(
        tmp_path / "sonar.mp4",
        fps=20,
        label="raw footage",
        auto_segment_seconds=60,
        requested=True,
    )

    def fail_sync(_frame_size):
        raise RuntimeError("codec unavailable")

    recording.sync = fail_sync
    error = recording.record(np.zeros((4, 6, 3), dtype=np.uint8))

    assert error == "raw footage recording failed: codec unavailable"
    assert recording.requested is False
    assert recording.auto_segment_seconds is None
    assert recording.writer is None
    assert recording.frame_size is None
    assert recording.segment_started_at is None


def test_echogram_capture_saves_one_png_column_per_sonar_frame(tmp_path):
    output = tmp_path / "echogram.png"
    recording = EchogramImageRecordingState(output)
    recording.toggle_requested()

    expected_columns = []
    for blue, green, red in ((10, 20, 30), (40, 50, 60), (70, 80, 90)):
        frame = np.zeros((2, 4, 3), dtype=np.uint8)
        frame[:, -1, :] = (blue, green, red)
        expected_columns.append(frame[:, -1, :])
        assert recording.record(frame) is None

    recording.toggle_requested()
    saved = cv2.imread(str(output), cv2.IMREAD_COLOR)

    assert recording.writer is None
    assert saved is not None
    assert saved.shape == (2, 3, 3)
    assert np.array_equal(saved, np.stack(expected_columns, axis=1))


def test_snapshot_write_failure_is_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "live_viewer.live_viewer.cv2.imwrite",
        lambda *_args: False,
    )

    try:
        write_snapshot(
            tmp_path / "snapshots" / "frame.png",
            np.zeros((4, 6, 3), dtype=np.uint8),
        )
    except RuntimeError as exc:
        assert "OpenCV could not write" in str(exc)
    else:
        raise AssertionError("failed snapshot write was reported as successful")


def test_output_path_preflight_probes_without_creating_directories(tmp_path):
    recording = tmp_path / "video" / "sonar.mp4"
    echogram = tmp_path / "echogram" / "history.png"
    snapshots = tmp_path / "snapshots"

    preflight_output_paths(recording, echogram, snapshots)

    assert not recording.parent.exists()
    assert not echogram.parent.exists()
    assert not snapshots.exists()
    assert not recording.exists()
    assert not echogram.exists()
    assert list(tmp_path.iterdir()) == []


def test_output_path_preflight_rejects_existing_outputs(tmp_path):
    recording = tmp_path / "sonar.mp4"
    recording.touch()

    try:
        preflight_output_paths(
            recording,
            tmp_path / "echogram.png",
            tmp_path / "snapshots",
        )
    except ValueError as exc:
        assert "existing file" in str(exc)
    else:
        raise AssertionError("preflight accepted an existing video output")


def test_live_histogram_summarizes_raw_intensities():
    raw = np.array([[0, 0, 64, 255]], dtype=np.uint8)

    histogram = intensity_histogram(raw)

    assert len(histogram["counts"]) == 256
    assert histogram["counts"][0] == 0
    assert histogram["counts"][64] == 1
    assert histogram["counts"][255] == 1
    assert histogram["pixels"] == 4
    assert histogram["nonzero_pixels"] == 2
    assert histogram["zero_percent"] == 50.0
    assert histogram["mean"] == 159.5
    assert histogram["median"] == 159.5


def test_live_histogram_handles_an_all_zero_frame():
    histogram = intensity_histogram(np.zeros((2, 3), dtype=np.uint8))

    assert histogram["counts"] == [0] * 256
    assert histogram["nonzero_pixels"] == 0
    assert histogram["zero_percent"] == 100.0
    assert histogram["mean"] is None
    assert histogram["median"] is None


def test_echogram_gain_rescales_the_complete_history_without_reingestion():
    echogram = EchogramState(width=3, height=2, gain=1.0, brightness_power=1.0)
    echogram.image[:] = np.array([[32, 64, 96], [16, 48, 80]])
    echogram.color_image[:] = 255

    original_history = echogram.image.copy()
    normal = echogram.render()
    echogram.gain = 2.0
    brighter = echogram.render()

    assert np.array_equal(echogram.image, original_history)
    assert np.all(brighter >= normal)
    assert np.any(brighter > normal)


def test_echogram_marks_the_start_of_center_beam_coverage():
    echogram = EchogramState(
        width=4, height=5, brightness_power=1.0,
        full_coverage_boundary=0.5,
    )
    echogram.image[:] = 255
    echogram.color_image[:] = (20, 20, 20)

    rendered = echogram.render()

    # Boundary 0.5 maps to row 2; the marker is placed on row 1, immediately
    # before the first center-beam row.
    assert np.all(rendered[1] == (180, 255, 180))


def test_echogram_crop_excludes_the_marker_and_outer_rows():
    echogram = EchogramState(width=2, height=5, full_coverage_boundary=0.5)
    rendered = np.zeros((5, 2, 3), dtype=np.uint8)

    cropped = echogram.crop_at_range_boundary(rendered)

    assert cropped.shape == (2, 2, 3)


def test_echogram_recalculates_warmup_columns_against_mean_background():
    echogram = EchogramState(
        width=4, height=1, brightness_power=1.0, background_warmup_frames=3,
    )

    echogram.add_frame(np.array([[10]], dtype=np.uint8))
    echogram.add_frame(np.array([[20]], dtype=np.uint8))
    echogram.add_frame(np.array([[30]], dtype=np.uint8))

    # The three startup columns are recalculated from their mean (20), rather
    # than preserving the large 20-point delta from the initial 10 baseline.
    assert np.array_equal(echogram.image, np.array([[0, 0, 0, 10]], dtype=np.uint8))
    assert echogram._warmup_frames is None
    assert np.array_equal(echogram.background, np.array([[20]], dtype=np.float32))


def test_echogram_uses_raw_range_rows_not_cartesian_warp(tmp_path, monkeypatch):
    echogram = EchogramState(
        width=2, height=4, brightness_power=1.0, background_warmup_frames=1,
    )
    recording = EchogramImageRecordingState(tmp_path / "echogram.png")
    args = SimpleNamespace(flip_theta=False, flip_range=False)
    source = np.array(
        [[10, 20, 30, 40], [10, 20, 30, 40], [10, 20, 30, 40]],
        dtype=np.uint8,
    )

    monkeypatch.setattr(
        "live_viewer.live_viewer.polar_image_to_xy",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("not used")),
    )
    update_echogram_window(
        source, source, b"", echogram, recording, args, show_window=False
    )
    brighter = source + 10
    display, error = update_echogram_window(
        brighter, brighter, b"", echogram, recording, args, show_window=False
    )

    assert error is None
    assert display is not None
    assert np.all(echogram.image[:, -1] == 10)


def test_switching_view_modes_resets_motion_coordinate_history():
    view = ViewState(warp_enabled=True)
    motion = MotionState(
        enabled=True,
        alpha=0.04,
        threshold=14,
        gain=4.0,
        background=np.ones((4, 5), dtype=np.float32),
    )

    handle_keyboard(ord("w"), view, motion)

    assert view.warp_enabled is False
    assert motion.background is None


def test_motion_view_keeps_a_dim_grayscale_background_context():
    motion = MotionState(
        enabled=True, alpha=0.04, threshold=14, gain=4.0
    )
    source = np.full((3, 4), 100, dtype=np.uint8)

    rendered = motion.apply_signed_color(source)

    assert rendered.shape == (3, 4, 3)
    assert np.all(rendered > 0)
    assert np.array_equal(rendered[:, :, 0], rendered[:, :, 1])
    assert np.array_equal(rendered[:, :, 1], rendered[:, :, 2])
    assert motion.background is not None


def test_grid_spacing_scales_up_for_long_ranges():
    assert effective_grid_spacing_ft(15.0, 2.0) == 2.0
    assert effective_grid_spacing_ft(80.0, 2.0) == 8.0
    assert effective_grid_spacing_ft(200.0, 2.0) == 20.0


def test_grid_spacing_rejects_non_positive_values():
    with pytest.raises(ValueError, match="grid spacing"):
        effective_grid_spacing_ft(80.0, 0.0)


def test_polar_motion_renders_consecutive_frames_without_freezing():
    args = SimpleNamespace(
        max_range=3.0,
        grid_spacing_ft=2.0,
        colorscheme="orange",
    )
    view = ViewState(warp_enabled=False)
    motion = MotionState(
        enabled=True, alpha=0.04, threshold=14, gain=4.0
    )
    first = np.zeros((5, 8), dtype=np.uint8)
    first[:, 2:6] = 60
    second = first.copy()
    second[2, 4] = 150

    first_display = render_main_display(
        first, first, b"", np.rot90(first), view, motion, args, 1,
        distance_grid_enabled=False,
    )
    second_display = render_main_display(
        second, second, b"", np.rot90(second), view, motion, args, 2,
        distance_grid_enabled=False,
    )

    assert first_display is not None
    assert second_display is not None
    assert not np.array_equal(first_display, second_display)


def test_sonar_gain_scales_polar_motion_output_without_changing_its_input():
    args = SimpleNamespace(
        max_range=3.0,
        grid_spacing_ft=2.0,
        colorscheme="orange",
    )
    background = np.full((5, 8), 80, dtype=np.uint8)
    changed = background.copy()
    changed[2, 4] = 100
    view = ViewState(warp_enabled=False)

    def render_at(gain):
        motion = MotionState(
            enabled=True, alpha=0.04, threshold=0, gain=1.0
        )
        render_main_display(
            background,
            background,
            b"",
            np.rot90(background),
            view,
            motion,
            args,
            1,
            distance_grid_enabled=False,
            sonar_gain_percent=gain,
        )
        return render_main_display(
            changed,
            changed,
            b"",
            np.rot90(changed),
            view,
            motion,
            args,
            1,
            distance_grid_enabled=False,
            sonar_gain_percent=gain,
        )

    normal = render_at(21)
    boosted = render_at(42)

    assert normal is not None and boosted is not None
    assert np.all(boosted >= normal)
    assert np.any(boosted > normal)
    unchanged = np.ones(normal.shape[:2], dtype=bool)
    unchanged[3, 2] = False
    assert np.array_equal(boosted[unchanged], normal[unchanged])


def test_intensity_window_stretches_values_between_black_and_white_points():
    image = np.array([[0, 20, 80, 160, 255]], dtype=np.uint8)

    filtered = apply_intensity_window(image, 20, 160)

    assert np.array_equal(
        filtered,
        np.array([[0, 0, 109, 255, 255]], dtype=np.uint8),
    )


def test_intensity_window_would_change_geometry_if_filtered_frame_were_used():
    raw_geometry = np.zeros((5, 3), dtype=np.uint8)
    raw_geometry[1:4, 1] = (10, 80, 200)
    filtered = apply_intensity_window(raw_geometry, 50, 100)

    raw_span = center_column_valid_span_for_warp(
        raw_geometry, flip_theta=False, flip_range=False
    )
    filtered_span = center_column_valid_span_for_warp(
        filtered, flip_theta=False, flip_range=False
    )

    assert raw_span == (1, 3, 3)
    assert filtered_span == (2, 3, 2)


def test_requested_warp_color_schemes_preserve_black_background():
    image = np.array([[0, 128, 255]], dtype=np.uint8)

    for name in ("orange", "light-gray", "dark-gray", "ocean-blue", "rainbow"):
        rendered = np.asarray(apply_color_scheme(image, name))

        assert name in COLOR_SCHEMES
        assert rendered.shape == (1, 3, 3)
        assert tuple(rendered[0, 0]) == (0, 0, 0)


def test_new_warp_color_schemes_have_distinct_midtones():
    image = np.array([[128]], dtype=np.uint8)
    colors = {
        tuple(np.asarray(apply_color_scheme(image, name))[0, 0])
        for name in ("orange", "light-gray", "dark-gray", "ocean-blue", "rainbow")
    }

    assert len(colors) == 5


def test_cartesian_warp_includes_selected_and_outer_ranges_in_one_remap(monkeypatch):
    rotated_geometry = np.zeros((6, 3), dtype=np.uint8)
    rotated_geometry[2:, 1] = 40
    rotated_geometry[0, 0] = 80
    source = np.rot90(rotated_geometry, -1)
    prejpg = bytearray(68 + source.shape[0] * 4)
    import struct
    for index, theta in enumerate(np.linspace(-0.5, 0.5, source.shape[0])):
        struct.pack_into("<f", prejpg, 68 + index * 4, theta)
    received = {}

    def capture(**kwargs):
        received.update(kwargs)
        return np.zeros((6, 6), dtype=np.uint8)

    monkeypatch.setattr(
        "live_viewer.view.warp_polar_image_to_xy_array", capture
    )

    live_polar_image_to_xy(
        source, bytes(prejpg), geometry_img=source, max_range=3.0
    )

    assert received["valid_range_span"] == (0, 5, 6)
    assert received["max_range"] == 5.0


def test_echogram_warp_keeps_rows_before_the_first_center_beam_return(monkeypatch):
    rotated_geometry = np.zeros((6, 3), dtype=np.uint8)
    rotated_geometry[2:, 1] = 40
    source = np.rot90(rotated_geometry, -1)
    prejpg = bytearray(68 + source.shape[0] * 4)
    import struct
    for index, theta in enumerate(np.linspace(-0.5, 0.5, source.shape[0])):
        struct.pack_into("<f", prejpg, 68 + index * 4, theta)
    received = {}

    def capture(**kwargs):
        received.update(kwargs)
        return np.zeros((6, 6), dtype=np.uint8)

    monkeypatch.setattr(
        "live_viewer.view.warp_polar_image_to_xy_array", capture
    )

    live_polar_image_to_xy(
        source, bytes(prejpg), geometry_img=source, max_range=3.0,
        include_all_range_rows=True,
    )

    assert received["valid_range_span"] == (0, 5, 6)
    assert echogram_full_coverage_boundary(source) == 2 / 5


def test_configured_windows_interface_uses_npcap_capture_name():
    capture_name = r"\Device\NPF_{12345678-1234-1234-1234-123456789ABC}"
    ethernet = make_iface(
        "Ethernet",
        description="Realtek USB GbE Family Controller",
        network_name=capture_name,
        addresses=(emulator.HEAD_IP,),
    )

    assert emulator._select_ethernet_interfaces(
        [ethernet], "Windows") == [capture_name]


def test_detection_filters_irrelevant_adapters_but_keeps_usb_ethernet():
    interfaces = [
        make_iface("Loopback", description="Npcap Loopback Adapter"),
        make_iface("Wi-Fi", description="Intel Wireless Adapter"),
        make_iface("Bluetooth Network", description="Bluetooth PAN"),
        make_iface("VPN", description="WireGuard Tunnel"),
        make_iface("vEthernet", description="Hyper-V Virtual Ethernet"),
        make_iface("Ethernet 3", description="ASIX USB 3.0 Gigabit Ethernet"),
        make_iface("Disconnected", flags="BROADCAST"),
    ]

    assert emulator._select_ethernet_interfaces(
        interfaces, "Windows") == ["Ethernet 3"]


def test_configured_interface_is_preferred_over_other_wired_adapters():
    configured = make_iface("eth1", addresses=(emulator.HEAD_IP,))
    other = make_iface("eth0")

    assert emulator._select_ethernet_interfaces(
        [other, configured], "Linux") == ["eth1"]


@pytest.mark.parametrize("system", ["Linux", "Windows", "Darwin"])
def test_picker_keeps_other_adapters_and_autodetection_is_independent_of_order(monkeypatch, system):
    monkeypatch.setattr(emulator, "_darwin_wired_link_active", lambda _: True)
    configured = make_iface("eth1", addresses=(emulator.HEAD_IP,))
    other = make_iface("eth0")
    for interfaces in ([other, configured], [configured, other]):
        assert emulator._select_ethernet_interfaces(interfaces, system) == ["eth1"]
        assert emulator._select_ethernet_interfaces(interfaces, system, prefer_configured=False) == [
            iface.network_name for iface in interfaces
        ]


def test_autodetection_leaves_multiple_unconfigured_adapters_for_the_user():
    assert emulator._select_ethernet_interfaces(
        [make_iface("eth0"), make_iface("eth1")], "Linux"
    ) == ["eth0", "eth1"]


def test_interface_addresses_come_from_resolved_capture_interface(monkeypatch):
    iface = make_iface("Ethernet", addresses=(emulator.HEAD_IP, "10.0.0.2"))
    monkeypatch.setattr(emulator, "resolve_iface", lambda _name: iface)

    assert emulator.interface_addresses("Ethernet") == {
        emulator.HEAD_IP,
        "10.0.0.2",
    }


def test_unknown_interface_returns_no_addresses(monkeypatch):
    def missing(_name):
        raise ValueError("not found")

    monkeypatch.setattr(emulator, "resolve_iface", missing)

    assert emulator.interface_details("missing") is None
    assert emulator.interface_addresses("missing") == set()


def test_macos_interface_preflight_repairs_known_device_addresses(monkeypatch):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        emulator, "interface_details",
        lambda _iface: SimpleNamespace(name="en9"),
    )
    monkeypatch.setattr(emulator.subprocess, "run", run)

    addresses = emulator.configure_interface_addresses(
        "en9", {emulator.GLS_IP}, system="Darwin"
    )

    assert addresses == {emulator.HEAD_IP}
    assert commands == [
        ["ifconfig", "en9", "-alias", emulator.GLS_IP],
        [
            "ifconfig", "en9", "inet", emulator.HEAD_IP,
            "netmask", "255.255.0.0", "alias",
        ],
    ]


@pytest.mark.parametrize("device", ["Ethernet 2", "Sonar's Ethernet", 'Sonar "USB" $adapter; 測試'])
def test_windows_ip_repair_quotes_adapter_alias(monkeypatch, device):
    import base64

    scripts = []
    def run(command, **_kwargs):
        assert command[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
        scripts.append(base64.b64decode(command[4]).decode("utf-16le"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(emulator, "interface_details", lambda _: SimpleNamespace(name=device))
    monkeypatch.setattr(emulator.subprocess, "run", run)
    assert emulator.configure_interface_addresses(
        r"\Device\NPF_{GUID}", {emulator.GLS_IP}, system="Windows"
    ) == {emulator.HEAD_IP}
    alias = "'" + device.replace("'", "''") + "'"
    assert scripts == [
        f"Remove-NetIPAddress -InterfaceAlias {alias} -IPAddress '{emulator.GLS_IP}' "
        "-Confirm:$false -ErrorAction Stop",
        f"New-NetIPAddress -InterfaceAlias {alias} -IPAddress '{emulator.HEAD_IP}' "
        "-PrefixLength 16 -ErrorAction Stop",
    ]


def test_windows_ip_repair_failure_does_not_claim_success(monkeypatch):
    monkeypatch.setattr(emulator, "interface_details", lambda _: SimpleNamespace(name="Ethernet 2"))
    monkeypatch.setattr(emulator.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout="", stderr="Access denied"))
    assert emulator.configure_interface_addresses("Ethernet 2", set(), system="Windows") is None


def test_quoted_powershell_viewer_flags_are_split():
    assert emulator.normalize_viewer_args(
        ["--no-save-prompt --no-save-frames"]
    ) == ["--no-save-prompt", "--no-save-frames"]


def test_viewer_argument_with_a_path_is_not_split():
    value = ["--save-root C:\\Sonar Files"]
    assert emulator.normalize_viewer_args(value) == value


def test_emulator_config_is_immutable_and_preserves_settings():
    config = emulator.EmulatorConfig(
        interface="en7",
        debug=True,
        viewer_enabled=False,
        viewer_args=("--no-save-prompt", "--no-save-frames"),
        startup_range="min",
    )

    assert config.interface == "en7"
    assert config.viewer_args == ("--no-save-prompt", "--no-save-frames")
    assert config.startup_range == "min"


def test_consolidated_profile_contains_only_qualified_compatible_changes(
    monkeypatch,
):
    monkeypatch.setattr(emulator, "ACTIVE_PROTOCOL_EXPERIMENT", None)
    monkeypatch.setattr(emulator, "MAXIMAL_PORTABLE_PROFILE", False)
    monkeypatch.setattr(emulator, "PORTABLE_V2_PROFILE", False)
    monkeypatch.setattr(emulator, "CONSOLIDATED_TEST_PROFILE", True)

    expected_changes = {
        "time-01-50900-delay-zero",
        "time-06-transition-delay-zero",
        "ctrl-02-no-50227-heartbeat",
        "sink-01-ephemeral-source",
        "sink-05-no-type-01",
        "sink-05-no-type-0b",
        "token-01-remove-50900",
        "token-01-remove-50224",
        "token-01-remove-50227",
        "pres-04-periodic-only",
        "pres-04-periodic-30s",
    }
    assert all(emulator.experiment_is(name) for name in expected_changes)
    assert not emulator.experiment_is("ctrl-01-no-50224-repeats")
    assert not emulator.experiment_is("pres-04-no-udp-50030")
    assert not emulator.experiment_is("sink-init-none")
    assert not emulator.experiment_is("token-02-remove-50900")
    assert not emulator.experiment_is("token-02-remove-50224")
    assert not emulator.experiment_is("token-02-remove-50227")
    assert not emulator.experiment_is("token-02-port-50900")
    assert not emulator.experiment_is("token-02-port-50224")


def test_identity_minimization_records_remove_only_the_selected_field():
    candidates = {
        "id-07b-remove-software": b"Software Version 41.05",
        "id-07c-remove-ilitek": b"ilitek_ts:",
        "id-07d-remove-map": b"BUS001L",
        "id-07e-remove-versmap": b"VERSMAP None",
    }
    for experiment, removed in candidates.items():
        record_124, record_179, record_12 = (
            emulator.discovery_records_for_identity_minimization(experiment)
        )
        assert removed not in record_179
        assert int.from_bytes(record_179[4:8], "little") == len(record_179) - 8
        assert record_124 == emulator.HEAD_DISCOVERY_124
        assert record_12 == emulator.HEAD_DISCOVERY_12

    record_124, _, _ = emulator.discovery_records_for_identity_minimization(
        "id-07f-neutral-system-code"
    )
    assert b"System Code" not in record_124
    assert b"Client Code" in record_124


def test_zero_length_model_removes_all_echomap_model_text():
    records = emulator.discovery_records_for_model("", allow_length_change=True)

    assert all(b"ECHOMAP" not in record for record in records)
    assert int.from_bytes(records[0][4:8], "little") == len(records[0]) - 8
    assert int.from_bytes(records[1][4:8], "little") == len(records[1]) - 8


def test_consolidated_identity_records_apply_minimized_identity_values():
    record_124, record_179, record_12 = (
        emulator.identity_records_for_consolidated_profile()
    )

    for removed in (
        b"ECHOMAP",
        b"Software Version 41.05",
        b"ilitek_ts:",
        b"BUS001L",
        b"VERSMAP None",
    ):
        assert removed not in record_124 + record_179
    assert b"System Code" not in record_124
    assert bytes.fromhex("505bb70b") not in record_124 + record_179
    assert int.from_bytes(record_124[4:8], "little") == len(record_124) - 8
    assert int.from_bytes(record_179[4:8], "little") == len(record_179) - 8
    assert record_12[:8] == emulator.HEAD_DISCOVERY_12[:8]
    assert record_12[8:] == bytes(4)


def test_emulator_status_is_an_observational_snapshot():
    runtime = emulator.EmulatorRuntime()
    runtime.gls_seen.set()
    runtime.tcp_50224_ready.set()

    status = emulator.get_emulator_status(runtime)

    assert status == emulator.EmulatorStatus(
        stopping=False,
        gls_detected=True,
        control_50224_ready=True,
        control_50227_ready=False,
        stream_active=False,
    )
    assert status.control_ready is False

    runtime.tcp_50227_ready.set()
    assert status.control_ready is False
    assert emulator.get_emulator_status(runtime).control_ready is True


def test_emulator_runtimes_have_isolated_events_and_queues():
    first = emulator.EmulatorRuntime()
    second = emulator.EmulatorRuntime()

    first.stop.set()
    first.range_commands.put((15, b"packet", threading.Event()))

    assert first.stop.is_set()
    assert not second.stop.is_set()
    assert first.range_commands.qsize() == 1
    assert second.range_commands.empty()


def test_sink_listener_uses_supplied_runtime():
    runtime = emulator.EmulatorRuntime()
    other = emulator.EmulatorRuntime()

    listener = emulator.SinkListener(runtime)
    listener.runtime.gls_seen.set()

    assert runtime.gls_seen.is_set()
    assert not other.gls_seen.is_set()


def test_viewer_control_loop_uses_supplied_runtime():
    class FakeSocket:
        def __init__(self, runtime):
            self.runtime = runtime
            self.replies = []
            self.closed = False
            self.calls = 0

        def settimeout(self, _timeout):
            pass

        def recvfrom(self, _size):
            self.calls += 1
            if self.calls == 1:
                return b"sonar:disable", ("127.0.0.1", 12345)
            self.runtime.stop.set()
            raise socket.timeout

        def sendto(self, message, address):
            self.replies.append((message, address))

        def close(self):
            self.closed = True

    runtime = emulator.EmulatorRuntime()
    other = emulator.EmulatorRuntime()
    control_socket = FakeSocket(runtime)

    emulator.viewer_control_loop(runtime, control_socket)

    assert runtime.sonar_requests.get_nowait() is False
    assert other.sonar_requests.empty()
    assert control_socket.replies == [
        (b"OK", ("127.0.0.1", 12345)),
    ]
    assert control_socket.closed


def test_viewer_range_request_is_retained_while_control_reconnects():
    class FakeSocket:
        def __init__(self, runtime):
            self.runtime = runtime
            self.replies = []
            self.calls = 0

        def settimeout(self, _timeout):
            pass

        def recvfrom(self, _size):
            self.calls += 1
            if self.calls == 1:
                return b"range:40", ("127.0.0.1", 12345)
            self.runtime.stop.set()
            raise socket.timeout

        def sendto(self, message, address):
            self.replies.append((message, address))

        def close(self):
            pass

    runtime = emulator.EmulatorRuntime()
    control_socket = FakeSocket(runtime)

    emulator.viewer_control_loop(runtime, control_socket)

    value, packet, _sent = runtime.range_commands.get_nowait()
    assert value == 40
    assert packet == emulator.build_range_packet(40)
    assert control_socket.replies == [
        (b"QUEUED", ("127.0.0.1", 12345)),
    ]


def test_sonar_coordinator_uses_supplied_runtime_and_preserves_order():
    runtime = emulator.EmulatorRuntime()
    other = emulator.EmulatorRuntime()
    runtime.tcp_50224_ready.set()
    runtime.tcp_50227_ready.set()
    runtime.sonar_requests.put(True)
    thread = threading.Thread(
        target=emulator.sonar_command_coordinator,
        args=(runtime,),
    )
    thread.start()

    enabled, state_sent = runtime.sonar_state_commands.get(timeout=1)
    assert enabled is True
    assert runtime.sonar_commands.empty()
    state_sent.set()

    enabled, packet, command_sent = runtime.sonar_commands.get(timeout=1)
    assert enabled is True
    assert packet == emulator.ENABLE_SONAR_PACKET
    assert other.sonar_state_commands.empty()
    assert other.sonar_commands.empty()

    command_sent.set()
    runtime.stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_browser_viewer_publishes_frames_and_owns_action_queue():
    state = BrowserViewerState()
    state.actions.put({"type": "zoom_in"})
    state.publish(
        np.zeros((8, 12, 3), dtype=np.uint8),
        streaming=True,
        frame_id=42,
    )
    state.publish_echogram(np.zeros((6, 10, 3), dtype=np.uint8))
    state.update_status(recording=True, notice="Saved frame")

    jpeg, status = state.snapshot()
    echogram_jpeg = state.echogram_snapshot()

    assert jpeg is not None and jpeg.startswith(b"\xff\xd8")
    assert echogram_jpeg is not None and echogram_jpeg.startswith(b"\xff\xd8")
    assert status["streaming"] is True
    assert status["frame_id"] == 42
    assert status["frame_sequence"] == 1
    assert status["echogram_sequence"] == 1
    assert status["recording"] is True
    assert status["notice"] == "Saved frame"
    assert state.actions.get_nowait() == {"type": "zoom_in"}
    page = (WEB_ROOT / "index.html").read_bytes()
    script = (WEB_ROOT / "app.js").read_bytes()
    assert b"<h1>OpenScope</h1>" in page
    assert b'intensity-histogram' in page
    assert b"autosave-minutes" in page
    assert b"stop-autosave" in page
    assert b">Start autosave</button>" in page
    assert b"Echogram PNG base file" in page
    assert b"Echogram video" not in page
    assert b"sonar-fullscreen" not in page
    assert b"echogram-fullscreen" not in page
    assert b"echogram-toggle" in page
    assert b"view-toggle" in page
    assert b"distance-grid" in page
    assert b'id="color-scheme-control" class="color-scheme-control"' in page
    assert b'id="save-sonar-frame"' in page
    assert b'id="save-echogram-frame"' in page
    assert b'id="empty-enable-callout"' in page
    assert b'id="empty-enable"' in page
    assert b'Press Enable sonar to start the stream' in page
    assert page.count(b'class="disabled-banner sonar-disabled-banner"') == 4
    assert b"zero-tilt" in page
    assert b"reverse-tilt" in page
    assert b"reset-tilt" in page
    assert b"zero-roll" in page
    assert b"reverse-roll" in page
    assert b"reset-roll" in page
    assert page.index(b'id="sonar-state"') < page.index(b'id="frame"')
    assert b'class="control-group display-controls"' in page
    assert b'class="control-group sonar-controls"' in page
    assert b'class="control-group visualization-controls"' in page
    assert b'class="path-grid"' in page
    assert b'class="recording-options"' in page
    assert b'id="gain-down"' in page
    assert b'id="gain-select"' in page
    assert b'id="gain-up"' in page
    assert b'id="gain-reset"' in page
    assert b'id="roll"' in page
    assert b'<option value="" selected disabled>' in page
    assert b'set_autosave' in script
    assert b'save_echogram_frame' in page
    assert b'stop_autosave' in script
    assert page.count(b'data-folder-target=') == 3
    assert b'/api/select-folder' in script
    assert b'"needs-apply"' in script
    assert b'Paths locked while recording' in script
    assert b'requestFullscreen' not in script
    assert b'echogram-hidden' in script
    assert b'"View polar" : "View Cartesian"' in script
    assert page.index(b'id="display-toggle"') < page.index(b'id="color-scheme-control"')
    assert b'data-action="toggle_display"' in page
    assert b'toggle_distance_grid' in page
    assert b'status.tilt_degrees' in script
    assert b'status.roll_degrees' in script
    assert b'drawHistogram(status.histogram)' in script
    assert b'querySelectorAll(".sonar-disabled-banner")' in script
    assert b'const selectedRange = status.range_pending' in script
    assert b': status.range;' in script
    assert b'rangeRequestGraceUntil = Date.now() + 1000' in script
    assert b'!rangeRequestIsHandingOff' in script
    assert b'"Stream stalled"' in script
    assert b'const canPromptForEnable = waiting && status.sonar_enabled !== true' in script
    controller_page = (CONTROLLER_WEB_ROOT / "index.html").read_bytes()
    assert b'allow="fullscreen"' in controller_page
    assert b"allowfullscreen" in controller_page
    controller_script = (CONTROLLER_WEB_ROOT / "app.js").read_bytes()
    assert b"function updateSessionLog" in controller_script
    assert b"selectionTouches(logs)" in controller_script


def test_macos_output_directory_picker_opens_at_existing_parent(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="/tmp/chosen folder/\n", stderr="")

    monkeypatch.setattr("live_viewer.web.platform.system", lambda: "Darwin")
    monkeypatch.setattr("live_viewer.web.subprocess.run", run)

    selected = choose_output_directory(str(tmp_path / "sonar.mp4"))

    assert selected == str(Path("/tmp/chosen folder").resolve())
    assert calls[0][0][0] == "osascript"
    assert calls[0][0][-1] == str(tmp_path)
    assert calls[0][1] == {
        "capture_output": True, "text": True, "check": False
    }


def test_disabled_frame_has_bright_top_and_bottom_bands():
    image = np.zeros((200, 400, 3), dtype=np.uint8)

    draw_frozen_frame_status(image, 3.0, sonar_disabled=True)

    expected_orange = np.array((0, 105, 255), dtype=np.uint8)
    assert np.array_equal(image[5, 5], expected_orange)
    assert np.array_equal(image[-5, 5], expected_orange)
    assert np.array_equal(image[100, 5], np.zeros(3, dtype=np.uint8))


def test_cartesian_grid_toggle_keeps_outer_data_compositor(monkeypatch):
    calls = []
    rendered = np.zeros((4, 5, 3), dtype=np.uint8)
    args = SimpleNamespace(
        max_range=None, width=None, height=None, range_offset_bins=0.0,
        theta_offset_deg=0.0, flip_theta=False, flip_range=False,
        forward_down=False, warp_output_scale=1.0,
        warp_interpolation_mode="linear", colorscheme="orange",
        grid_spacing_ft=2.0,
    )
    view = SimpleNamespace(warp_enabled=True, cartesian_headroom_rows=0)
    motion = SimpleNamespace(enabled=False)
    monkeypatch.setattr(
        "live_viewer.live_viewer.polar_image_to_xy",
        lambda *_args, **_kwargs: np.zeros((3, 4), dtype=np.uint8),
    )
    monkeypatch.setattr(
        "live_viewer.live_viewer.colorize_for_cv2",
        lambda *_args, **_kwargs: np.zeros((3, 4, 3), dtype=np.uint8),
    )
    def composite(*_args, **kwargs):
        calls.append(kwargs["show_grid"])
        return rendered
    monkeypatch.setattr(
        "live_viewer.live_viewer.add_polar_grid_and_outer_data",
        composite,
    )

    result = render_main_display(
        np.zeros((2, 3), dtype=np.uint8),
        np.zeros((2, 3), dtype=np.uint8),
        b"", np.zeros((3, 2), dtype=np.uint8), view, motion, args, 1,
        distance_grid_enabled=False,
    )

    assert np.array_equal(result, rendered)
    assert calls == [False]


def test_cartesian_vertical_crop_is_stable_through_sparse_frames(monkeypatch):
    args = SimpleNamespace(
        max_range=None, width=None, height=None, range_offset_bins=0.0,
        theta_offset_deg=0.0, flip_theta=False, flip_range=False,
        forward_down=False, warp_output_scale=1.0,
        warp_interpolation_mode="linear", colorscheme="orange",
        grid_spacing_ft=2.0,
    )
    view = SimpleNamespace(warp_enabled=True)
    motion = SimpleNamespace(enabled=False)
    source = np.full((3, 512), 100, dtype=np.uint8)
    warps = [
        np.pad(np.full((2, 4), 100, dtype=np.uint8), ((8, 0), (0, 0))),
        np.pad(np.full((6, 4), 100, dtype=np.uint8), ((4, 0), (0, 0))),
    ]
    monkeypatch.setattr(
        "live_viewer.live_viewer.polar_image_to_xy",
        lambda *_args, **_kwargs: warps.pop(0),
    )
    monkeypatch.setattr(
        "live_viewer.live_viewer.colorize_for_cv2",
        lambda image, *_args: cv2.cvtColor(image, cv2.COLOR_GRAY2BGR),
    )
    monkeypatch.setattr(
        "live_viewer.live_viewer.add_polar_grid_and_outer_data",
        lambda display, *_args, **_kwargs: display,
    )
    first = render_main_display(
        source, source, b"", np.zeros((512, 3), dtype=np.uint8),
        view, motion, args, 1,
    )
    second = render_main_display(
        source, source, b"", np.zeros((512, 3), dtype=np.uint8),
        view, motion, args, 2,
    )
    assert first.shape == second.shape == (8, 4, 3)
    assert np.count_nonzero(second) > np.count_nonzero(first)


def test_cartesian_vertical_crop_tracks_changed_output_height(monkeypatch):
    args = SimpleNamespace(
        max_range=None, width=None, height=None, range_offset_bins=0.0,
        theta_offset_deg=0.0, flip_theta=False, flip_range=False,
        forward_down=False, warp_output_scale=1.0,
        warp_interpolation_mode="linear", colorscheme="orange",
        grid_spacing_ft=2.0,
    )
    control = SimpleNamespace(actual_range=15)
    view = SimpleNamespace(warp_enabled=True)
    motion = SimpleNamespace(enabled=False)
    source = np.full((3, 512), 100, dtype=np.uint8)
    heights = [10, 20]
    monkeypatch.setattr(
        "live_viewer.live_viewer.polar_image_to_xy",
        lambda *_args, **_kwargs: np.full((heights.pop(0), 4), 100, dtype=np.uint8),
    )
    monkeypatch.setattr(
        "live_viewer.live_viewer.colorize_for_cv2",
        lambda image, *_args: cv2.cvtColor(image, cv2.COLOR_GRAY2BGR),
    )
    monkeypatch.setattr(
        "live_viewer.live_viewer.add_polar_grid_and_outer_data",
        lambda display, *_args, **_kwargs: display,
    )
    first = render_main_display(
        source, source, b"", np.zeros((512, 3), dtype=np.uint8),
        view, motion, args, 1, control,
    )
    control.actual_range = 30
    second = render_main_display(
        source, source, b"", np.zeros((512, 3), dtype=np.uint8),
        view, motion, args, 2, control,
    )
    assert first.shape == (8, 4, 3)
    assert second.shape == (15, 4, 3)


def test_browser_controller_builds_configured_emulator_command(tmp_path):
    config = SessionConfig.from_json({
        "interface": "en7",
        "save_root": str(tmp_path),
        "session_name": "lake test",
        "startup_range": "15",
        "autosave_raw": True,
        "autosave_echogram": False,
        "autosave_minutes": 5,
        "save_frames": False,
        "auto_configure_interface": True,
        "debug": False,
    })

    command = build_emulator_command(config, 8766, executable="python")

    assert command[:2] == ["python", str(emulator.Path(
        emulator.__file__
    ).resolve())]
    assert command[2:5] == [
        "--web-viewer", "--startup-range", "15",
    ]
    assert command[5:7] == ["--iface", "en7"]
    assert "en7" in command
    assert "--spoof-head-mac" not in command
    assert "--auto-configure-interface" in command
    assert "--viewer-args" in command
    assert command[-2:] == ["--autosave-raw", "--no-save-frames"]
    assert "--paths-confirmed" in command
    assert "--web-port" in command
    assert "8766" in command


def test_controller_stop_signals_engine_process_group(monkeypatch):
    class Process:
        pid = 1234

        def terminate(self):
            raise AssertionError("direct termination should not be needed")

    calls = []
    monkeypatch.setattr(browser_controller.os, "name", "posix")
    monkeypatch.setattr(browser_controller.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        browser_controller.os,
        "killpg",
        lambda pgid, sig: calls.append((pgid, sig)), raising=False,
    )

    ControllerState._terminate_session(Process())

    assert calls == [(1234, browser_controller.signal.SIGTERM)]

def test_browser_controller_validates_autosave_duration():
    values = {
        "interface": "",
        "save_root": "recordings",
        "session_name": "",
        "startup_range": "none",
        "autosave_raw": False,
        "autosave_echogram": False,
        "autosave_minutes": 0,
        "save_frames": True,
        "auto_configure_interface": True,
        "debug": False,
    }

    try:
        SessionConfig.from_json(values)
    except ValueError as exc:
        assert "Autosave minutes" in str(exc)
    else:
        raise AssertionError("zero-minute autosave should be rejected")

    assert b"<h1>OpenScope</h1>" in (
        CONTROLLER_WEB_ROOT / "index.html"
    ).read_bytes()


def test_controller_defaults_to_manual_recording_and_single_page_scroll():
    page = (CONTROLLER_WEB_ROOT / "index.html").read_text(encoding="utf-8")
    controller_script = (CONTROLLER_WEB_ROOT / "app.js").read_text(encoding="utf-8")
    viewer_script = (WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'id="autosave-raw"' not in page
    assert 'id="save-root"' not in page
    assert 'id="session-name"' not in page
    assert 'autosave_raw: false' in controller_script
    assert 'autosave_echogram: false' in controller_script
    assert '<section id="setup"' not in page
    assert '<section id="session">' in page
    assert '<details id="advanced-settings">' in page
    assert 'id="launch-full-discovery"' in page
    assert "await startEmulator()" in controller_script
    assert 'await startEmulator({fullDiscovery: true})' in controller_script
    assert 'interface: fullDiscovery ? ""' in controller_script
    assert 'auto_configure_interface: fullDiscovery ||' in controller_script
    assert 'getJson("/api/defaults"' in controller_script
    assert 'id="viewer" title="Sonar viewer" scrolling="no"' in page
    assert 'id="restart-auto-discovery"' in page
    assert 'event.data?.type !== "viewer-height"' in controller_script
    assert 'type: "viewer-height"' in viewer_script
    assert 'Restart with auto-detect &amp; repair IP' in page
    assert 'startEmulator({fullDiscovery: true})' in controller_script


def test_controller_status_polling_is_single_flight():
    script = (CONTROLLER_WEB_ROOT / "app.js").read_text(encoding="utf-8")

    assert "statusRefreshPromise" in script
    assert "setTimeout(pollStatus, 500)" in script
    assert "setInterval(refreshStatus, 500)" not in script


def test_controller_parses_interface_and_stream_diagnostics():
    interface = INTERFACE_LOG_RE.search(
        "INFO Auto-detected sonar-facing Ethernet interface: en9"
    )
    stream = STREAM_RATE_LOG_RE.search(
        "INFO GLS10 50223 stream: 12 packets, 516 bytes/s, 0 image-sized"
    )

    assert interface is not None and interface.group(1) == "en9"
    assert stream is not None and stream.groups() == ("12", "516", "0")


def test_controller_formats_action_feedback_from_logs():
    assert action_feedback_from_log(
        "Range command sent to GLS10: 40 ft"
    ) == "Range requested: 40 ft"
    assert action_feedback_from_log(
        "GLS10 confirmed requested range change: 40"
    ) == "GLS10 confirmed range: 40 ft"
    assert action_feedback_from_log(
        "GLS10 has not applied requested range 40; "
        "frames still report 15.186 ft"
    ) == (
        "Range not applied: requested 40 ft, "
        "GLS10 still reports 15.2 ft"
    )
    assert action_feedback_from_log(
        "unrelated stream message"
    ) is None
    assert action_feedback_from_log(
        "Output path validation failed: Snapshot folder is not writable"
    ) == "Output path validation failed: Snapshot folder is not writable"


def test_controller_reports_macos_physical_link_state(monkeypatch):
    monkeypatch.setattr(
        "browser_controller.platform.system",
        lambda: "Darwin",
    )

    def run_active(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="en9: flags=8863<UP,RUNNING>\n\tstatus: active\n",
        )

    monkeypatch.setattr(
        "browser_controller.subprocess.run",
        run_active,
    )
    assert interface_link_state("en9") == "active"

    def run_inactive(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="en9: flags=8863<UP,RUNNING>\n\tstatus: inactive\n",
        )

    monkeypatch.setattr(
        "browser_controller.subprocess.run",
        run_inactive,
    )
    assert interface_link_state("en9") == "inactive"


def test_browser_servers_reject_cross_origin_browser_requests():
    for same_origin in (controller_same_origin, viewer_same_origin):
        assert same_origin(None, "127.0.0.1:8765")
        assert same_origin(
            "http://127.0.0.1:8765",
            "127.0.0.1:8765",
        )
        assert not same_origin(
            "https://attacker.example",
            "127.0.0.1:8765",
        )
        assert not same_origin(
            "http://127.0.0.1:9999",
            "127.0.0.1:8765",
        )


def test_browser_servers_restrict_unauthenticated_binds_to_loopback():
    for validate in (_loopback_bind_address, validate_loopback_bind):
        assert validate("127.0.0.1") == "127.0.0.1"
        try:
            validate("0.0.0.0")
        except ValueError as exc:
            assert "loopback" in str(exc)
        else:
            raise AssertionError("non-loopback bind should be rejected")


def test_controller_token_bootstraps_cookie_and_protects_api(tmp_path):
    class State:
        def status(self):
            return {"running": False}

    handler = browser_controller._handler_for(
        State(), allowed_save_root=tmp_path, auth_token="test-token"
    )
    server = browser_controller.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        assert response.status == 401
        response.read()

        connection.request("GET", "/?token=test-token")
        response = connection.getresponse()
        assert response.status == 303
        assert response.getheader("Location") == "/"
        cookie = response.getheader("Set-Cookie")
        assert cookie is not None
        assert "HttpOnly" in cookie
        response.read()

        connection.request(
            "GET",
            "/api/status",
            headers={"Cookie": cookie.split(";", 1)[0]},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert "frame-ancestors 'none'" in response.getheader(
            "Content-Security-Policy"
        )
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert json.loads(response.read()) == {"running": False}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_viewer_response_allows_only_loopback_frame_ancestors():
    handler = viewer_web._handler_for(BrowserViewerState())
    server = viewer_web.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        policy = response.getheader("Content-Security-Policy")
        assert "frame-ancestors http://127.0.0.1:*" in policy
        assert "default-src 'none'" in policy
        response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_controller_authenticated_shutdown_endpoint(tmp_path):
    stopped = threading.Event()

    class State:
        pass

    handler = browser_controller._handler_for(
        State(),
        allowed_save_root=tmp_path,
        auth_token="test-token",
        shutdown=stopped.set,
    )
    server = browser_controller.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "POST",
            "/api/shutdown",
            body="{}",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
        )
        response = connection.getresponse()
        assert response.status == 202
        response.read()
        assert stopped.wait(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_make_user_editable_uses_legacy_stat_compatible_call(tmp_path, monkeypatch):
    target = tmp_path / "recordings"
    target.mkdir()
    calls = []

    monkeypatch.setenv("SUDO_UID", "501")
    monkeypatch.setenv("SUDO_GID", "20")
    monkeypatch.setattr(permissions.os, "chown", lambda *args, **kwargs: None, raising=False)

    real_stat = permissions.os.stat

    def record_stat(*args, **kwargs):
        calls.append((args, kwargs))
        return real_stat(*args, **kwargs)

    monkeypatch.setattr(permissions.os, "stat", record_stat)

    permissions.make_user_editable(target)

    assert calls
    assert calls[0][0] == (target,)
    assert calls[0][1] == {"follow_symlinks": False}
    assert target.stat().st_mode & 0o700 == 0o700


def test_controller_configuration_rejects_type_confusion_and_path_escape(tmp_path):
    values = {
        "interface": "en7", "save_root": str(tmp_path / "recordings"),
        "session_name": None, "startup_range": "15",
        "autosave_raw": True, "autosave_echogram": False,
        "autosave_minutes": 5, "save_frames": True,
        "auto_configure_interface": False, "debug": False,
    }
    SessionConfig.from_json(values, allowed_save_root=tmp_path)
    for key, bad_value in (("interface", 7), ("startup_range", 15)):
        invalid = dict(values, **{key: bad_value})
        try:
            SessionConfig.from_json(invalid, allowed_save_root=tmp_path)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{key} accepted an invalid JSON type")
    escaped = dict(values, save_root=str(tmp_path.parent / "outside"))
    try:
        SessionConfig.from_json(escaped, allowed_save_root=tmp_path)
    except ValueError as exc:
        assert "inside" in str(exc)
    else:
        raise AssertionError("save path escaped the configured recording root")


def test_session_sonar_state_packets_match_enable_disable_capture():
    session_base = 0x8E2CA3D0

    assert emulator.build_session_sonar_state_packet(
        False, session_base
    ).hex() == (
        "0409efbe1400000010000200"
        "d0a32c8edca32c8ed1a32c8ed1a32c8e"
    )
    assert emulator.build_session_sonar_state_packet(
        True, session_base
    ).hex() == (
        "0409efbe1400000010000200"
        "d0a32c8edca32c8ed0a32c8ed1a32c8e"
    )


def test_session_perspective_packet_matches_transition_capture():
    session_base = 0x6D990606

    assert emulator.build_session_perspective_packet(session_base) == bytes.fromhex(
        "04 09 ef be 14 00 00 00 10 00 02 00 "
        "06 06 99 6d 35 06 99 6d 0a 06 9b 6d 0a 06 99 6d"
    )


def test_sonar_gain_preserves_21_percent_baseline_and_changes_intensity():
    image = np.array([[0, 21, 100, 255]], dtype=np.uint8)

    assert np.array_equal(apply_sonar_gain(image, 21), image)
    assert np.array_equal(
        apply_sonar_gain(image, 0),
        np.zeros_like(image),
    )
    assert np.array_equal(
        apply_sonar_gain(image, 100),
        np.array([[0, 100, 255, 255]], dtype=np.uint8),
    )


def test_sonar_gain_rejects_out_of_range_values():
    for value in (-1, 101, True, 1.5):
        try:
            apply_sonar_gain(np.zeros((1, 1), dtype=np.uint8), value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid gain {value!r}")


def test_all_range_packet_words_are_session_relocated():
    session_base = 0x0050D15E

    packet_15 = emulator.build_session_range_packet(15, session_base)
    assert packet_15.hex() == (
        "0409efbe1400000010000200"
        "60d1500093d15000311fe34062d15000"
    )

    assert int.from_bytes(packet_15[12:16], "little") == session_base + 2
    assert int.from_bytes(packet_15[16:20], "little") == session_base + 0x35
    assert int.from_bytes(packet_15[20:24], "little") == (
        session_base + 0x40924DD3
    ) & 0xFFFFFFFF
    assert int.from_bytes(packet_15[24:28], "little") == session_base + 4


def test_range_selector_low_24_bit_carry_does_not_change_bank():
    # This real session previously turned 15 ft into about 60 ft and 50 ft
    # into about 200 ft because normal addition carried into the bank byte.
    session_base = 0x5B8F7291

    assert emulator.build_session_range_packet(
        15, session_base
    )[20:24].hex() == "64c0219b"
    assert emulator.build_session_range_packet(
        50, session_base
    )[20:24].hex() == "9b49029c"


def test_range_selector_without_low_24_bit_carry_is_unchanged():
    session_base = 0x861B2617

    assert emulator.build_session_range_packet(
        15, session_base
    )[20:24].hex() == "ea73adc6"


def test_numeric_selector_bank_relocates_independently_of_low_24_bits():
    # This session exposed the missing rule: 20 and 80 stayed one bank too
    # low when their selector was relocated with ordinary 32-bit arithmetic.
    session_base = 0x031857C6

    assert emulator.build_session_range_packet(
        20, session_base
    )[20:24].hex() == "356adb43"
    assert emulator.build_session_range_packet(
        80, session_base
    )[20:24].hex() == "356adb44"


def test_min_retains_working_legacy_relocation():
    session_base = 0x031857C6

    assert emulator.build_session_range_packet(
        "min", session_base
    )[20:24].hex() == "356adb41"


def test_emulator_control_client_tracks_requested_and_confirmed_sonar_state(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, command, address):
            self.sent.append((command, address))

        def recvfrom(self, _size):
            return b"OK", ("127.0.0.1", 12345)

    control = EmulatorControlClient(15, initial_range=15)
    real_socket = control._socket
    real_socket.close()
    control._socket = FakeSocket()

    now = [1.0]
    monkeypatch.setattr("live_viewer.control.time.monotonic", lambda: now[0])
    assert control.current_sonar_enabled is None
    assert control.observe_sonar_frame() == "stream_detected"

    now[0] = 1.1
    assert control.set_sonar_enabled(False)
    assert control.current_sonar_enabled is True
    assert control.pending_sonar_enabled is False
    assert control.sonar_transition_state == "pending"
    assert control._socket.sent == [
        (b"sonar:disable", ("127.0.0.1", 15))
    ]

    assert control.zoom_in()
    assert control.zoom_out()
    assert control.current_range == 15
    assert control.actual_range is None
    assert control.pending_range_count == 1
    assert control._socket.sent == [
        (b"sonar:disable", ("127.0.0.1", 15))
    ]

    assert not control.toggle_sonar()
    now[0] = 3.2
    assert control.observe_sonar_silence() == "disabled_confirmed"
    assert control.current_sonar_enabled is False
    assert control.sonar_transition_state == "confirmed"

    control._socket.sent.clear()
    assert control.toggle_sonar()
    assert control.current_sonar_enabled is False
    assert control.pending_sonar_enabled is True
    assert control.pending_range_count == 1
    assert control._socket.sent == [
        (b"sonar:enable", ("127.0.0.1", 15)),
    ]

    now[0] = 3.3
    assert control.observe_sonar_frame() == "enabled_confirmed"
    assert control.pending_sonar_enabled is None
    assert control.pending_range_count == 0
    assert control._socket.sent == [
        (b"sonar:enable", ("127.0.0.1", 15)),
        (b"range:15", ("127.0.0.1", 15)),
    ]

    control._socket.sent.clear()
    assert control.set_gain(73)
    assert control.current_gain == 73
    assert control._socket.sent == []


def test_direct_control_range_remains_available_to_browser_status():
    control = SonarControlClient("127.0.0.1", dry_run=True, initial_range=15)
    try:
        assert control.zoom_out()
        assert control.current_range == 20
        assert control.requested_range == 20
        assert control.actual_range == 20
        assert control.range_change_pending is False
    finally:
        control.close()


def test_disable_is_confirmed_by_stream_silence_or_rejected_by_continued_frames(
    monkeypatch,
):
    control = EmulatorControlClient(15, initial_range=15)
    now = [10.0]
    monkeypatch.setattr("live_viewer.control.time.monotonic", lambda: now[0])
    try:
        control.observe_sonar_frame()
        control._accept_sonar_request(False)
        now[0] = 12.1
        assert control.observe_sonar_silence() == "disabled_confirmed"
        assert control.current_sonar_enabled is False

        control._accept_sonar_request(True)
        now[0] = 12.2
        assert control.observe_sonar_frame() == "enabled_confirmed"
        assert control.current_sonar_enabled is True

        control._accept_sonar_request(False)
        now[0] = 18.0
        assert control.observe_sonar_frame() == "disable_failed"
        assert control.current_sonar_enabled is True
        assert control.pending_sonar_enabled is None
        assert control.sonar_transition_state == "failed"
    finally:
        control.close()


def test_first_sonar_enable_waits_longer_before_reporting_failure(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, command, address):
            self.sent.append((command, address))

        def recvfrom(self, _size):
            return b"OK", ("127.0.0.1", 12345)

        def close(self):
            pass

    now = [1.0]
    monkeypatch.setattr("live_viewer.control.time.monotonic", lambda: now[0])
    control = EmulatorControlClient(15, initial_range=15)
    control._socket.close()
    control._socket = FakeSocket()
    control.current_sonar_enabled = False

    assert control.set_sonar_enabled(True)
    for second in (2.0, 3.0, 4.0, 5.0):
        now[0] = second
        assert control.observe_sonar_silence() is None
    assert len(control._socket.sent) == control.SONAR_MAX_ATTEMPTS
    assert control.pending_sonar_enabled is True

    now[0] = 7.1
    assert control.observe_sonar_silence() is None
    assert control.current_sonar_enabled is False
    assert control.pending_sonar_enabled is True
    assert control.sonar_transition_state == "pending"

    now[0] = 16.1
    assert control.observe_sonar_silence() == "enable_failed"
    assert control.current_sonar_enabled is False
    assert control.pending_sonar_enabled is None
    assert control.sonar_transition_state == "failed"

    # A recovering/power-cycled unit may start streaming after the bounded
    # retry window. Incoming images are authoritative evidence of enable.
    now[0] = 8.0
    assert control.observe_sonar_frame() == "enabled_confirmed"
    assert control.current_sonar_enabled is True
    assert control.sonar_transition_state == "confirmed"


def test_subsequent_sonar_enable_uses_normal_failure_timeout(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, command, address):
            self.sent.append((command, address))

        def recvfrom(self, _size):
            return b"OK", ("127.0.0.1", 12345)

        def close(self):
            pass

    now = [1.0]
    monkeypatch.setattr("live_viewer.control.time.monotonic", lambda: now[0])
    control = EmulatorControlClient(15, initial_range=15)
    control._socket.close()
    control._socket = FakeSocket()
    control.current_sonar_enabled = False

    assert control.set_sonar_enabled(True)
    now[0] = 2.0
    assert control.observe_sonar_frame() == "enabled_confirmed"

    control.current_sonar_enabled = False
    now[0] = 10.0
    assert control.set_sonar_enabled(True)
    now[0] = 16.0
    assert control.observe_sonar_silence() == "enable_failed"


def test_reported_range_confirms_nearest_supported_setting():
    control = EmulatorControlClient(15, initial_range=200)
    try:
        control._pending_since = 1.0
        control._reported_at_request = 175

        outcome = control.observe_reported_range(198.683)

        assert outcome == "confirmed"
        assert control.reported_range == 200
        assert control._pending_since is None
    finally:
        control.close()


def test_initial_range_does_not_override_frame_reported_range():
    control = EmulatorControlClient(15, initial_range=15)
    try:
        assert control.actual_range is None
        assert control.observe_reported_range(198.683) is None
        assert control.actual_range == 200
        assert control.current_range == 200
    finally:
        control.close()


def test_unconfirmed_range_does_not_rescale_warp_geometry(monkeypatch):
    control = EmulatorControlClient(15, initial_range=15)
    try:
        control._actual_range = 15
        control.current_range = 40
        control._apply_started_at = 1.0
        monkeypatch.setattr(
            "live_viewer.control.time.monotonic",
            lambda: 10.0,
        )

        assert control.actual_range == 15
        assert control.applying_range is False

        control._pending_since = 2.0
        control._reported_at_request = 15
        outcome = control.observe_reported_range(39.8)
        assert outcome == "confirmed"
        assert control.actual_range == 40
    finally:
        control.close()


def test_unconfirmed_range_retries_then_reports_failure(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []

        def sendto(self, command, address):
            self.sent.append((command, address))

        def recvfrom(self, _size):
            return b"OK", ("127.0.0.1", 12345)

        def close(self):
            pass

    control = EmulatorControlClient(15, initial_range=15)
    try:
        control._socket.close()
        control._socket = FakeSocket()
        control.current_range = 40
        control._pending_since = 1.0
        control._last_range_attempt_at = 1.0
        control._range_attempts = 1
        control._reported_at_request = 15
        now = [2.0]
        monkeypatch.setattr("live_viewer.control.time.monotonic", lambda: now[0])

        for second in (2.0, 3.0, 4.0, 5.0):
            now[0] = second
            assert control.observe_reported_range(15.186) == "retrying"
        assert len(control._socket.sent) == 4
        now[0] = 6.0
        assert control.observe_reported_range(15.186) == "failed"
        assert control.observe_reported_range(15.186) is None
        assert control.actual_range == 15
        assert control.requested_range == 15
        assert control.range_change_pending is False
    finally:
        control.close()



def test_browser_controller_uses_packaged_engine_when_frozen(tmp_path, monkeypatch):
    config = SessionConfig.from_json({
        "interface": "",
        "save_root": str(tmp_path),
        "session_name": "",
        "startup_range": "15",
        "autosave_raw": False,
        "autosave_echogram": False,
        "autosave_minutes": 5,
        "save_frames": True,
        "auto_configure_interface": False,
        "debug": False,
    })
    monkeypatch.setattr(browser_controller, "FROZEN", True)
    monkeypatch.setattr(
        browser_controller,
        "bundle_executable",
        lambda name: [f"/bundle/{name}"],
    )

    command = build_emulator_command(config, 8766, executable="python")

    assert command[0] == "/bundle/SonarHeadUnitEngine"
    assert str(emulator.Path(emulator.__file__).resolve()) not in command


@pytest.mark.parametrize("frozen", [False, True])
def test_engine_reaches_integrated_viewer_launch(monkeypatch, frozen):
    """Exercise startup through viewer launch without changing a real adapter."""
    for name in ("ACTIVE_PROTOCOL_EXPERIMENT", "MAXIMAL_PORTABLE_PROFILE",
                 "PORTABLE_V2_PROFILE", "CONSOLIDATED_TEST_PROFILE", "HEAD_IP", "GLS_MAC"):
        monkeypatch.setattr(emulator, name, getattr(emulator, name))
    monkeypatch.setattr(emulator, "NETWORK_RECOVERY", None)
    monkeypatch.setattr(emulator, "create_network_recovery", lambda _: None)
    monkeypatch.setattr(emulator, "FROZEN", frozen)
    monkeypatch.setattr(emulator.sys, "argv", ["engine", "--iface", "test-ethernet", "--web-viewer"])
    monkeypatch.setattr(emulator.signal, "signal", lambda *_: None)
    monkeypatch.setattr(emulator.platform, "system", lambda: "Linux")
    monkeypatch.setattr(emulator, "interface_details", lambda _: make_iface("test-ethernet"))
    monkeypatch.setattr(emulator, "interface_addresses", lambda _: ["172.16.6.1"])
    monkeypatch.setattr(emulator, "get_if_hwaddr", lambda _: "00:11:22:33:44:55")
    monkeypatch.setattr(emulator, "bundle_executable", lambda name: [f"/bundle/{name}"])
    monkeypatch.setattr(emulator.socket, "socket", lambda *_: SimpleNamespace(
        bind=lambda _: None, getsockname=lambda: ("127.0.0.1", 12345),
    ))
    commands = []

    class ViewerLaunchReached(Exception):
        pass

    def launch_viewer(command):
        commands.append(command)
        raise ViewerLaunchReached

    monkeypatch.setattr(emulator.subprocess, "Popen", launch_viewer)
    with pytest.raises(ViewerLaunchReached):
        emulator.run_engine(emulator.EmulatorRuntime())

    command = commands[0]
    ready_file = Path(command[command.index("--ready-file") + 1])
    import tempfile
    assert ready_file.parent == (Path("/tmp") if emulator.os.name == "posix" else Path(tempfile.gettempdir()))
    assert command[command.index("--iface") + 1] == "test-ethernet"
    assert "--web-only" in command
    if frozen:
        assert command[0] == "/bundle/SonarHeadUnitViewer"
    else:
        assert str(emulator.VIEWER_SCRIPT) in command
