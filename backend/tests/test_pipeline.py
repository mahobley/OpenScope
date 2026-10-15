import struct
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from live_viewer.pipeline import CaptureLoop, CapturedFrame, LatestFrame
from live_viewer.recording import RecordingState
from packet_io import CaptureError


def test_latest_display_frame_replaces_only_unconsumed_frames():
    slot = LatestFrame()
    for frame_id in range(10):
        slot.publish(CapturedFrame(frame_id, np.zeros((2, 3)), b"", 0, 20))
    assert slot.take().frame_id == 9
    assert slot.skipped == 9
    assert slot.take(timeout=0) is None


@pytest.mark.parametrize("hide_display", [False, True])
@pytest.mark.parametrize("format", ["both", "video", "png"])
@pytest.mark.parametrize("shutdown", ["keyboard", "sigterm"])
def test_capture_saves_original_polar_frames_while_rendering_is_blocked(monkeypatch, tmp_path, hide_display, format, shutdown):
    from live_viewer import live_viewer as viewer
    from live_viewer.web import BrowserViewerState

    rendering = threading.Event()
    saved = threading.Event()
    renders = []
    writers = []
    paused = threading.Event()
    browsers = []

    class FakeSniffer:
        def __init__(self, **options):
            self.options = options
            self.running = False
            self.exception = None
            self.thread = None
            self.stopped = threading.Event()

        def start(self):
            assert not list(tmp_path.glob("frames_*")), "startup created an unused frames folder"
            self.running = True
            def run():
                try:
                    self.options["started_callback"]()
                    self.options["prn"](SimpleNamespace(frame_id=1, time=1700000001.0))
                    assert (paused if hide_display else rendering).wait(3)
                    for frame_id in range(2, 11):
                        self.options["prn"](SimpleNamespace(
                            frame_id=frame_id, time=1700000000.0 + frame_id,
                        ))
                    saved.set()
                    if hide_display:
                        assert browsers[0].state.snapshot()[1]["display_enabled"] is False
                        assert not renders
                        browsers[0].state.actions.put({"type": "toggle_display"})
                    self.stopped.wait(3)
                except Exception as exc:
                    self.exception = exc
                finally:
                    self.running = False
            self.thread = threading.Thread(target=run)
            self.thread.start()

        def stop(self, join=False):
            self.stopped.set()

    class ObservedState(BrowserViewerState):
        def update_status(self, **status):
            super().update_status(**status)
            if status.get("display_enabled") is False:
                paused.set()

    class FakeBrowser:
        def __init__(self, *_args):
            self.state = ObservedState()
            browsers.append(self)
            # Change the display radically; raw PNGs must remain unchanged.
            self.state.actions.put({"type": "set_gain", "value": 0})
            if hide_display:
                self.state.actions.put({"type": "toggle_display"})
                self.state.actions.put({"type": "set_echogram_gain", "value": 50})
                self.state.actions.put({"type": "toggle_echogram_range_crop"})
            self.address = ("127.0.0.1", 0)
        def start(self):
            pass
        def close(self):
            pass

    class FakeWriter:
        def __init__(self):
            self.released = False
            writers.append(self)
        def write(self, frame):
            assert not self.released
        def release(self):
            self.released = True

    prejpg = bytearray(76)
    struct.pack_into("<f", prejpg, 32, 10.0)
    struct.pack_into("<ff", prejpg, 68, -0.5, 0.5)
    monkeypatch.setattr(viewer, "BrowserViewerServer", FakeBrowser)
    monkeypatch.setattr(viewer, "CaptureLoop", lambda callback, **kwargs:
                        CaptureLoop(callback, sniffer_factory=FakeSniffer, **kwargs))
    monkeypatch.setattr(viewer, "udp_payload", lambda pkt: pkt)
    for name in ("extract_temperature_c", "extract_tilt_degrees", "extract_roll_degrees"):
        monkeypatch.setattr(viewer, name, lambda _payload: None)
    monkeypatch.setattr(viewer, "decode_packet_frame", lambda pkt, _assembler:
                        (pkt.frame_id, np.full((2, 3), pkt.frame_id, dtype=np.uint8), bytes(prejpg)))
    monkeypatch.setattr("live_viewer.recording.make_video_writer", lambda *_args: FakeWriter())
    monkeypatch.setattr(viewer.cv2, "destroyAllWindows", lambda: None)

    def finish_render(image):
        if shutdown == "sigterm":
            import signal
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        raise KeyboardInterrupt

    def render(image, _geometry, _prejpg, _raw, *_args, **_kwargs):
        assert not np.any(image)  # zero display gain is active
        renders.append(True)
        if hide_display:
            assert saved.is_set()
            assert browsers[0].state.snapshot()[1]["display_enabled"] is True
            return finish_render(image)
        if len(renders) == 1:
            rendering.set()
            assert saved.wait(3), "raw saving waited for rendering"
            assert len(list(tmp_path.rglob("*_raw_rotated.png"))) == (0 if format == "video" else 10)
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        return finish_render(image)

    monkeypatch.setattr(viewer, "render_main_display", render)
    original_echogram_render = viewer.EchogramState.render
    def check_echogram_render(self, *args, **kwargs):
        assert browsers[0].state.snapshot()[1].get("display_enabled") is not False
        return original_echogram_render(self, *args, **kwargs)
    monkeypatch.setattr(viewer.EchogramState, "render", check_echogram_render)
    original_histogram = viewer.intensity_histogram
    def check_histogram(image):
        assert browsers[0].state.snapshot()[1].get("display_enabled") is not False
        return original_histogram(image)
    monkeypatch.setattr(viewer, "intensity_histogram", check_histogram)
    ready = tmp_path / "capture.ready"
    monkeypatch.setattr("sys.argv", [
        "viewer", "--iface", "fake", "--web-only",
        *([] if hide_display else ["--no-echogram"]),
        *(["--no-save-video"] if format == "png" else []),
        *(["--no-save-frames"] if format == "video" else []),
        "--no-save-prompt", "--paths-confirmed", "--autosave-raw",
        "--save-root", str(tmp_path), "--ready-file", str(ready),
    ])
    assert viewer.main() == 0
    assert ready.exists()
    for frame_id in ([] if format == "video" else range(1, 11)):
        path = next(tmp_path.rglob(f"frame_{frame_id:06d}_raw_rotated.png"))
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        np.testing.assert_array_equal(image, np.full((3, 2), frame_id, dtype=np.uint8))
        metadata = path.with_name(f"frame_{frame_id:06d}_warp_metadata.txt").read_text()
        assert str(1700000000.0 + frame_id) in metadata
    if format == "png":
        assert not writers
    else:
        assert writers and all(writer.released for writer in writers)
    if format == "video":
        assert not list(tmp_path.rglob("*_warp_metadata.txt"))
        assert not list(tmp_path.glob("frames_*"))


def test_capture_initialization_failure_is_reported():
    class BrokenSniffer:
        def __init__(self, **_kwargs):
            self.exception = OSError("capture device unavailable")
            self.running = False
            self.thread = None
        def start(self):
            pass
    capture = CaptureLoop(lambda _pkt: None, iface="bad", bpf="udp",
                          sniffer_factory=BrokenSniffer)
    with pytest.raises(CaptureError, match="capture device unavailable"):
        capture.start()
    capture.close()


def test_capture_callback_cannot_write_before_privileges_drop(monkeypatch):
    dropped = threading.Event()
    received = threading.Event()
    monkeypatch.setattr("live_viewer.pipeline.drop_invoking_user_privileges", dropped.set)
    class Sniffer:
        exception = None
        running = True
        def __init__(self, **options):
            self.options = options
            self.stop_requested = threading.Event()
        def start(self):
            def run():
                self.options["started_callback"]()
                self.options["prn"](object())
                self.stop_requested.wait(3)
            self.thread = threading.Thread(target=run)
            self.thread.start()
        def stop(self, join=False): self.stop_requested.set()
    def callback(_):
        assert dropped.is_set()
        received.set()
    capture = CaptureLoop(callback, iface="fake", bpf="udp", sniffer_factory=Sniffer, drop_privileges=True)
    try:
        capture.start()
        assert received.wait(3)
        capture.check()
    finally:
        capture.close()


def test_recording_stop_waits_for_an_in_progress_write(monkeypatch, tmp_path):
    writing = threading.Event()
    finish_write = threading.Event()
    stopped = threading.Event()

    class Writer:
        def write(self, _frame):
            writing.set()
            assert finish_write.wait(3)
        def release(self):
            stopped.set()
    state = RecordingState(tmp_path / "raw.mp4", 20, "raw", requested=True)
    monkeypatch.setattr("live_viewer.recording.make_video_writer", lambda *_args: Writer())
    producer = threading.Thread(target=lambda: state.record(np.zeros((2, 3, 3), dtype=np.uint8)))
    producer.start()
    assert writing.wait(3)
    stop = threading.Thread(target=state.toggle_requested)
    stop.start()
    assert not stopped.wait(0.05)
    finish_write.set()
    producer.join(3)
    stop.join(3)
    assert stopped.is_set()
    assert state.writer is None
