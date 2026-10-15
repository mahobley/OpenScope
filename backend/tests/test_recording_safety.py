import os
import signal
import subprocess

import cv2
import numpy as np
import pytest

from browser_controller import ControllerState, desktop_owner_alive
from live_viewer.recording import EchogramImageRecordingState, RecordingState


@pytest.mark.parametrize("video,png", [(True, False), (False, True), (True, True)])
def test_recording_formats_control_encoder_and_png_only_lifecycle(tmp_path, monkeypatch, video, png):
    writes = []
    class Writer:
        def write(self, frame):
            writes.append(frame.copy())
        def release(self):
            writes.append("closed")
    opened = []
    def open_writer(*args):
        opened.append(args)
        return Writer()
    monkeypatch.setattr("live_viewer.recording.make_video_writer", open_writer)
    state = RecordingState(tmp_path / "sonar.mp4", 20, "sonar")
    state.set_formats(video, png)
    state.toggle_requested()
    assert state.record(np.zeros((2, 3, 3), np.uint8)) is None
    assert state.active
    assert state.elapsed_seconds() is not None
    assert bool(opened) is video
    assert state.png_enabled is png
    with pytest.raises(ValueError, match="Stop sonar"):
        state.set_formats(True, True)
    state.toggle_requested()
    assert not state.active and not state.requested
    assert ("closed" in [item for item in writes if isinstance(item, str)]) is video
    state.set_formats(True, True)
    with pytest.raises(ValueError, match="Select video"):
        state.set_formats(False, False)
    with pytest.raises(ValueError):
        state.set_formats("false", True)


@pytest.mark.parametrize("budget,columns", [(18, 8192), (1024, 3)])
def test_echogram_segments_preserve_every_column_and_bound_memory(tmp_path, budget, columns):
    state = EchogramImageRecordingState(
        tmp_path / "echogram.png", max_buffer_bytes=budget, max_columns=columns,
    )
    state.toggle_requested()
    expected = []
    for i in range(14):
        frame = np.full((2, 5, 3), i, dtype=np.uint8)
        expected.append(frame[:, -1:, :].copy())
        assert state.record(frame) is None
        assert state.buffered_bytes <= budget
        assert len(state.columns) <= columns
        assert state.active and state.requested
    assert state.toggle_requested() is None
    paths = [tmp_path / "echogram.png", *sorted(tmp_path.glob("echogram_*.png"))]
    actual = np.concatenate([cv2.imread(str(path)) for path in paths], axis=1)
    np.testing.assert_array_equal(actual, np.concatenate(expected, axis=1))
    assert state.columns is None and state.buffered_bytes == 0
    assert state.autosaved_clips > 0


def test_echogram_rollover_write_failure_disables_recording(tmp_path, monkeypatch):
    state = EchogramImageRecordingState(tmp_path / "echogram.png", max_columns=1)
    state.requested = True
    frame = np.zeros((2, 3, 3), dtype=np.uint8)
    assert state.record(frame) is None
    monkeypatch.setattr(cv2, "imwrite", lambda *_: False)
    assert "capture failed" in state.record(frame)
    assert not state.active and not state.requested
    assert state.auto_segment_seconds is None
    assert state.columns is None and state.buffered_bytes == 0
    assert state.record(frame) is None
    assert not state.active


def test_echogram_dimension_change_splits_without_losing_columns(tmp_path):
    state = EchogramImageRecordingState(tmp_path / "echogram.png", requested=True)
    assert state.record(np.full((2, 3, 3), 10, np.uint8)) is None
    assert state.record(np.full((4, 3, 3), 20, np.uint8)) is None
    assert state.stop() is None
    assert cv2.imread(str(tmp_path / "echogram.png")).shape == (2, 1, 3)
    assert cv2.imread(str(tmp_path / "echogram_002.png")).shape == (4, 1, 3)


def test_desktop_lifetime_requires_file_and_live_owner(tmp_path):
    lifetime = tmp_path / "backend.alive"
    lifetime.touch()
    assert desktop_owner_alive(lifetime, os.getpid())
    lifetime.unlink()
    assert not desktop_owner_alive(lifetime, os.getpid())
    assert desktop_owner_alive(None, None)


@pytest.mark.skipif(os.name == "nt", reason="Unix process-group cleanup")
def test_controller_force_cleanup_targets_session_group(monkeypatch):
    state = ControllerState(0)
    class HungProcess:
        pid = 12345
        waits = 0
        def poll(self):
            return None
        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("engine", timeout)
    state._process = HungProcess()
    signals = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    state.close()
    assert signals == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
    with pytest.raises(RuntimeError, match="shutting down"):
        state.start(None)


@pytest.mark.parametrize("stop", ["lifetime", pytest.param("sigterm", marks=pytest.mark.skipif(
    os.name == "nt", reason="Windows terminate() does not deliver a catchable SIGTERM"))])
def test_running_controller_exits_cleanly_without_http_shutdown(tmp_path, stop):
    import queue
    import threading
    import sys
    from pathlib import Path

    lifetime = tmp_path / "backend.alive"
    lifetime.touch()
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "browser_controller.py"),
         "--port", "0", "--viewer-port", "0", "--recordings-root", str(tmp_path),
         "--desktop-lifetime-file", str(lifetime), "--desktop-parent-pid", str(os.getpid())],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        readiness = queue.Queue()
        threading.Thread(target=lambda: readiness.put(process.stdout.readline()), daemon=True).start()
        assert readiness.get(timeout=10).startswith("SONAR_CONTROLLER_READY ")
        if stop == "lifetime":
            lifetime.unlink()
        else:
            process.terminate()
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
