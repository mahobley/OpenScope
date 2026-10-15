import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from live_viewer.outputs import require_available_volume
from network_recovery import NetworkRecovery
from preferences import Preferences
from test_beta_reliability import FakeNetwork


@pytest.mark.parametrize("address", ["172.16.6.0", "172.16.6.1"])
@pytest.mark.parametrize("legacy", [False, True])
def test_network_restore_uses_actual_profile_address(tmp_path, monkeypatch, address, legacy):
    network = FakeNetwork()
    monkeypatch.setattr("network_recovery.subprocess.run", network.run)
    journal = tmp_path / "network.json"
    recovery = NetworkRecovery(journal)
    recovery.begin("en9", address)
    recovery.mark_address("head_added")
    network.addresses[address] = "0xffff0000"
    if legacy:
        data = json.loads(journal.read_text())
        del data["head_ip"]
        journal.write_text(json.dumps(data))
    NetworkRecovery(journal).restore()
    assert address not in network.addresses
    assert "192.168.1.5" in network.addresses
    assert not journal.exists()


def test_unmounted_mac_volume_is_rejected_before_any_directory_creation(monkeypatch):
    monkeypatch.setattr("live_viewer.outputs.sys.platform", "darwin")
    monkeypatch.setattr("live_viewer.outputs.os.path.ismount", lambda _: False)
    with pytest.raises(OSError, match="unavailable"):
        require_available_volume(Path("/Volumes/Unplugged Drive/session/frames"))


def test_preferences_survive_restart_and_ignore_corrupt_files(tmp_path):
    path = tmp_path / "settings.json"
    Preferences(path).update({"theme": "light"})
    Preferences(path).update({"interface": "en9"})
    assert Preferences(path).read() == {"theme": "light", "interface": "en9"}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("interrupted")
    assert Preferences(path).read() == {}


@pytest.mark.parametrize("restore,available", [(False, True), (True, True), (True, False)])
def test_viewer_external_destination_and_restart(tmp_path, monkeypatch, restore, available):
    from live_viewer import live_viewer as viewer
    from live_viewer.web import BrowserViewerState
    internal = tmp_path / "app-recordings"
    external = tmp_path / "External Drive"
    external.mkdir()
    paths = {"recording": str(external / "sonar.mp4"), "echogram": str(external / "echogram.png"), "snapshots": str(external / "frames")}
    settings = tmp_path / "viewer-settings.json"
    if restore:
        Preferences(settings).update({"output_paths": paths})
    browsers = []
    class Browser:
        address = ("127.0.0.1", 0)
        def __init__(self, *_):
            self.state = BrowserViewerState()
            if not restore:
                self.state.actions.put({"type": "set_output_paths", "value": paths})
            self.state.actions.put({"type": "set_recording_formats", "value": {"video": False, "png": True}})
            browsers.append(self)
        def start(self): pass
        def close(self): pass
    class Capture:
        def __init__(self, callback, **kwargs):
            assert kwargs["drop_privileges"]
            self.callback, self.count = callback, 0
        def start(self): pass
        def close(self): pass
        def check(self):
            self.count += 1
            if self.count == 2:
                browsers[0].state.actions.put({"type": "toggle_recording"})
            elif self.count == 3:
                self.callback(SimpleNamespace(time=1))
            elif self.count > 3:
                raise KeyboardInterrupt
    monkeypatch.setattr(viewer, "BrowserViewerServer", Browser)
    monkeypatch.setattr(viewer, "CaptureLoop", Capture)
    monkeypatch.setattr(viewer.os, "geteuid", lambda: 501, raising=False)
    monkeypatch.setattr(viewer, "udp_payload", lambda packet: packet)
    for name in ("extract_temperature_c", "extract_tilt_degrees", "extract_roll_degrees"):
        monkeypatch.setattr(viewer, name, lambda _: None)
    monkeypatch.setattr(viewer, "decode_packet_frame", lambda *_: (1, np.zeros((2, 3), np.uint8), b""))
    monkeypatch.setattr(viewer.cv2, "destroyAllWindows", lambda: None)
    if not available:
        def reject(path):
            if external in Path(path).parents or Path(path) == external:
                raise OSError("Recording drive is unavailable")
        monkeypatch.setattr("live_viewer.outputs.require_available_volume", reject)
    monkeypatch.setattr("sys.argv", ["viewer", "--iface", "fake", "--web-only", "--no-echogram", "--no-save-prompt", "--paths-confirmed", "--save-root", str(internal), "--settings-file", str(settings)])
    assert viewer.main() == 0
    status = browsers[0].state.snapshot()[1]
    assert status["paths_confirmed"] is available
    assert bool(list(external.rglob("*_raw_rotated.png"))) is available
    assert not list(internal.rglob("*_raw_rotated.png"))
    assert Preferences(settings).read()["output_paths"]["snapshots"] == paths["snapshots"]


def test_video_finish_failure_disarms_recording_and_retains_partial(tmp_path, monkeypatch):
    from live_viewer.recording import RecordingState
    state = RecordingState(tmp_path / "video.mp4", 20, "sonar", requested=True)
    class Writer:
        def write(self, _): pass
        def release(self): raise OSError("drive removed")
    def create(path, *_):
        path.write_bytes(b"unfinished")
        return Writer()
    monkeypatch.setattr("live_viewer.recording.make_video_writer", create)
    state.record(np.zeros((2, 2, 3), np.uint8))
    assert "drive removed" in state.finish()
    assert not state.active and not state.requested and state.writer is None
    assert (tmp_path / "video.inprogress.mp4").exists()
