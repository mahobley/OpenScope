import struct
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from diagnostics import DiagnosticStream
from live_viewer.outputs import DiskSpaceGuard, save_decoded_frame
from live_viewer.packets import SonarFrameAssembler, MAX_FRAME_BYTES
from network_recovery import NetworkRecovery, HEAD_IP, GLS_IP


def chunk(frame, data=b'x', total=100, offset=0, stream=0x60044):
    return struct.pack('<7I', 0x904, 20 + len(data), stream, frame, total, offset, len(data)) + data


def test_incomplete_frames_bound_memory_without_any_successful_frames():
    assembler = SonarFrameAssembler(max_frames=4, max_bytes=10)
    for frame in range(10000):
        assembler.add_payload(chunk(frame, b'1234'))
        assert len(assembler.parts) <= 4
        assert assembler.buffered_bytes <= 10
    assert assembler.buffered_bytes == sum(len(part) for parts in assembler.parts.values() for part in parts.values())
    assert assembler.add_payload(chunk(10000, total=MAX_FRAME_BYTES + 1)) is None
    assert all(key[1] != 10000 for key in assembler.parts)


def test_assembler_expiry_reset_wrap_and_reordered_chunks():
    clock = [0]
    assembler = SonarFrameAssembler(clock=lambda: clock[0], expiry_seconds=2)
    assembler.add_payload(chunk(1000, b'old', total=10))
    clock[0] = 3
    assembler.add_payload(b'invalid')
    assert not assembler.parts and assembler.buffered_bytes == 0
    assembler.add_payload(chunk(0xffffffff, b'old', total=10))
    jpeg = b'\xff\xd8new\xff\xd9'
    assert assembler.add_payload(chunk(1, jpeg[3:], len(jpeg), 3)) is None
    assert not any(key[1] == 0xffffffff for key in assembler.parts)
    assert assembler.add_payload(chunk(1, jpeg[:3], len(jpeg))) == (1, jpeg, b'')
    assert not assembler.parts and assembler.buffered_bytes == 0


def test_overlapping_chunks_do_not_count_as_complete():
    assembler = SonarFrameAssembler()
    assert assembler.add_payload(chunk(2, b'abcdefgh', 10)) is None
    assert assembler.add_payload(chunk(2, b'bcdefghi', 10, 1)) is None
    assert len(assembler.parts) == 1


def test_repeated_frame_ids_preserve_pixels_and_metadata(tmp_path):
    for value in (10, 20):
        save_decoded_frame(tmp_path, 1, np.full((3, 2), value, np.uint8), b'', (2, 3), value, None)
    images = sorted(tmp_path.glob('*_raw_rotated.png'))
    assert len(images) == 2
    assert sorted(int(cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)[0, 0]) for path in images) == [10, 20]
    for path in images:
        stem = path.name.removesuffix('_raw_rotated.png')
        assert (tmp_path / f'{stem}_warp_metadata.txt').exists()
    assert not list(tmp_path.glob('.*'))


def test_metadata_failure_does_not_publish_a_partial_capture(tmp_path, monkeypatch):
    def fail(*_args):
        raise OSError('volume disconnected')
    monkeypatch.setattr('live_viewer.outputs.save_warp_metadata', fail)
    with pytest.raises(OSError, match='volume disconnected'):
        save_decoded_frame(tmp_path, 1, np.zeros((3, 2), np.uint8), b'', (2, 3), 1, None)
    assert not list(tmp_path.iterdir())


def test_storage_guard_checks_space_and_removal_without_recreating_volume(tmp_path, monkeypatch):
    volume = tmp_path / 'drive'
    volume.mkdir()
    guard = DiskSpaceGuard([volume / 'frames'], reserve_bytes=100)
    monkeypatch.setattr('live_viewer.outputs.shutil.disk_usage', lambda _: SimpleNamespace(free=99))
    with pytest.raises(OSError, match='disk space'):
        guard.check(force=True)
    volume.rmdir()
    with pytest.raises(OSError, match='disconnected'):
        guard.check(force=True)
    assert not volume.exists()


def test_storage_guard_detects_volume_replacement(tmp_path, monkeypatch):
    guard = DiskSpaceGuard([tmp_path])
    original = Path.stat
    def stat(path, **kwargs):
        result = original(path, **kwargs)
        return SimpleNamespace(st_dev=result.st_dev + 1) if path == tmp_path else result
    monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(OSError, match='changed'):
        guard.check(force=True)


def test_logs_rotate_and_preserve_previous_launch(tmp_path):
    path = tmp_path / 'controller.log'
    path.write_text('previous launch\n')
    stream = DiagnosticStream(path)
    stream.handler.maxBytes = 100
    for i in range(100):
        stream.write(f'event {i:03d} ' + 'x' * 60 + '\n')
    stream.flush()
    stream.handler.close()
    assert 'event 099' in path.read_text()
    assert len(list(tmp_path.iterdir())) == 5
    assert all(path.stat().st_size <= 100 for path in tmp_path.iterdir())
    next_launch = DiagnosticStream(path)
    next_launch.write('next launch\n')
    next_launch.flush()
    next_launch.handler.close()
    assert 'event 099' in ''.join(path.read_text() for path in tmp_path.iterdir())


class FakeNetwork:
    def __init__(self):
        self.addresses = {GLS_IP: '0xffffff00', '192.168.1.5': '0xffffff00'}
        self.neighbor = '00:11:22:33:44:55'
        self.commands = []
        self.missing = False

    def run(self, command, **kwargs):
        self.commands.append(command)
        stdout = ''
        code = 0
        if command[0] == 'ifconfig':
            if self.missing:
                return SimpleNamespace(returncode=1, stdout='', stderr='no such interface')
            if len(command) == 2:
                stdout = '\n'.join(f'inet {ip} netmask {mask}' for ip, mask in self.addresses.items())
            elif command[2] == '-alias':
                self.addresses.pop(command[3], None)
            else:
                self.addresses[command[3]] = command[5]
        elif command[:2] == ['arp', '-n']:
            if self.neighbor:
                stdout = f'({GLS_IP}) at {self.neighbor} on en9 permanent'
            else:
                code = 1
        elif command[:2] == ['arp', '-s']:
            self.neighbor = command[3]
        elif command[:2] == ['arp', '-d']:
            self.neighbor = None
        return SimpleNamespace(returncode=code, stdout=stdout, stderr='')


def test_network_recovery_survives_process_loss_and_preserves_other_addresses(tmp_path, monkeypatch):
    network = FakeNetwork()
    monkeypatch.setattr('network_recovery.subprocess.run', network.run)
    journal = tmp_path / 'network.json'
    recovery = NetworkRecovery(journal)
    recovery.begin('en9')
    recovery.mark_address('gls_removed')
    network.addresses.pop(GLS_IP)
    recovery.mark_address('head_added')
    network.addresses[HEAD_IP] = '0xffff0000'
    recovery.prepare_neighbor('en9', 'aa:bb:cc:dd:ee:ff')
    network.neighbor = 'aa:bb:cc:dd:ee:ff'
    # New instance simulates an interrupted process followed by recovery.
    NetworkRecovery(journal).restore()
    assert network.addresses == {GLS_IP: '0xffffff00', '192.168.1.5': '0xffffff00'}
    assert network.neighbor == '00:11:22:33:44:55'
    assert not journal.exists()
    NetworkRecovery(journal).restore()  # idempotent


def test_recovery_keeps_journal_for_unplugged_adapter_and_does_not_remove_user_changes(tmp_path, monkeypatch):
    network = FakeNetwork()
    network.addresses[HEAD_IP] = '0xffff0000'  # preexisting address
    monkeypatch.setattr('network_recovery.subprocess.run', network.run)
    journal = tmp_path / 'network.json'
    recovery = NetworkRecovery(journal)
    recovery.begin('en9')
    recovery.prepare_neighbor('en9', 'aa:bb:cc:dd:ee:ff')
    network.neighbor = 'ab:cd:ef:01:23:45'  # changed by another tool
    network.missing = True
    with pytest.raises(RuntimeError, match='no such interface'):
        recovery.restore()
    assert journal.exists()
    network.missing = False
    recovery.restore()
    assert HEAD_IP in network.addresses
    assert network.neighbor == 'ab:cd:ef:01:23:45'


def test_failed_image_encoding_leaves_no_partial_final_image(tmp_path, monkeypatch):
    from live_viewer.outputs import write_image_atomic
    def fail(path, _image):
        Path(path).write_bytes(b'partial')
        return False
    monkeypatch.setattr(cv2, 'imwrite', fail)
    with pytest.raises(RuntimeError, match='could not write'):
        write_image_atomic(tmp_path / 'snapshot.png', np.zeros((2, 2), np.uint8))
    assert not list(tmp_path.iterdir())


def test_video_is_published_only_after_finalization(tmp_path, monkeypatch):
    from live_viewer.recording import RecordingState
    opened = []
    class Writer:
        def write(self, image):
            pass
        def release(self):
            opened[0].write_bytes(b'finalized')
    def make(path, *_):
        opened.append(path)
        path.write_bytes(b'incomplete')
        return Writer()
    monkeypatch.setattr('live_viewer.recording.make_video_writer', make)
    state = RecordingState(tmp_path / 'video.mp4', 20, 'sonar', requested=True)
    assert state.record(np.zeros((2, 2, 3), np.uint8)) is None
    assert opened[0].name == 'video.inprogress.mp4'
    assert not (tmp_path / 'video.mp4').exists()
    state.stop()
    assert (tmp_path / 'video.mp4').read_bytes() == b'finalized'
    assert not opened[0].exists()


@pytest.mark.parametrize('failure', ['disk space exhausted', 'volume disconnected'])
def test_storage_failure_stops_recording_but_capture_continues_and_error_persists(tmp_path, monkeypatch, failure):
    from live_viewer import live_viewer as viewer
    from live_viewer.web import BrowserViewerState
    frames = []
    browser = []
    writers = []
    class Guard:
        free_bytes = 1000000000
        def __init__(self, *_):
            self.checks = 0
        def check(self, force=False):
            self.checks += 1
            if self.checks > 1:
                raise OSError(failure)
    class Browser:
        address = ('127.0.0.1', 0)
        def __init__(self, *_):
            self.state = BrowserViewerState()
            self.state.actions.put({'type': 'set_gain', 'value': 30})
            browser.append(self)
        def start(self):
            pass
        def close(self):
            pass
    class Capture:
        def __init__(self, callback, **kwargs):
            self.callback = callback
            self.checks = 0
        def start(self):
            for frame_id in (1, 2, 3):
                self.callback(SimpleNamespace(frame_id=frame_id, time=frame_id))
                frames.append(frame_id)
        def check(self):
            self.checks += 1
            if self.checks > 1:
                raise KeyboardInterrupt
        def close(self):
            pass
    class Writer:
        released = False
        def write(self, _):
            pass
        def release(self):
            self.released = True
    def make_writer(*_):
        writer = Writer()
        writers.append(writer)
        return writer
    monkeypatch.setattr(viewer, 'DiskSpaceGuard', Guard)
    monkeypatch.setattr(viewer, 'BrowserViewerServer', Browser)
    monkeypatch.setattr(viewer, 'CaptureLoop', Capture)
    monkeypatch.setattr(viewer, 'udp_payload', lambda packet: packet)
    for name in ('extract_temperature_c', 'extract_tilt_degrees', 'extract_roll_degrees'):
        monkeypatch.setattr(viewer, name, lambda _: None)
    monkeypatch.setattr(viewer, 'decode_packet_frame', lambda pkt, _: (pkt.frame_id, np.zeros((2, 3), np.uint8), b''))
    monkeypatch.setattr('live_viewer.recording.make_video_writer', make_writer)
    monkeypatch.setattr(viewer.cv2, 'destroyAllWindows', lambda: None)
    monkeypatch.setattr('sys.argv', ['viewer', '--iface', 'fake', '--web-only', '--no-echogram',
        '--autosave-raw', '--save-root', str(tmp_path), '--no-save-prompt', '--paths-confirmed'])
    assert viewer.main() == 0
    status = browser[0].state.snapshot()[1]
    assert failure in status['recording_error']
    assert status['gain'] == 30  # later controls do not clear the failure
    assert not status['recording'] and not status['autosave_raw']
    assert frames == [1, 2, 3]
    assert len(list(tmp_path.rglob('*_raw_rotated.png'))) == 1
    assert writers and all(writer.released for writer in writers)


def test_controller_startup_does_not_traverse_existing_recordings(tmp_path, monkeypatch):
    import browser_controller as controller
    root = tmp_path / 'recordings'
    root.mkdir()
    archive = root / 'old-session'
    archive.mkdir()
    (archive / 'frame.png').write_bytes(b'original capture')
    def forbid_walk(*_, **__):
        raise AssertionError('startup traversed archive')
    monkeypatch.setattr('live_viewer.permissions.os.walk', forbid_walk)
    monkeypatch.setattr(controller, 'available_loopback_port', lambda _: 8766)
    class Server:
        server_port = 8765
        def __init__(self, *_):
            pass
        def serve_forever(self):
            raise KeyboardInterrupt
        def server_close(self):
            pass
    monkeypatch.setattr(controller, 'ThreadingHTTPServer', Server)
    monkeypatch.setattr('sys.argv', ['controller', '--recordings-root', str(root)])
    assert controller.main() == 0
    assert (archive / 'frame.png').read_bytes() == b'original capture'


def test_recovery_removes_only_the_owned_scoped_route(tmp_path, monkeypatch):
    network = FakeNetwork()
    route = ['']
    base_run = network.run
    def run(command, **kwargs):
        if command[:3] == ['route', '-n', 'get']:
            network.commands.append(command)
            return SimpleNamespace(returncode=0, stdout=route[0], stderr='')
        if command[:3] == ['route', '-n', 'delete']:
            network.commands.append(command)
            route[0] = ''
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        return base_run(command, **kwargs)
    monkeypatch.setattr('network_recovery.subprocess.run', run)
    recovery = NetworkRecovery(tmp_path / 'network.json')
    recovery.begin('en9')
    recovery.prepare_route('en9')
    route[0] = 'destination: 172.16.0.0\nmask: 255.255.0.0\ninterface: en9\n'
    recovery.restore()
    assert not route[0]
    assert ['route', '-n', 'delete', '-net', '172.16.0.0/16', '-ifscope', 'en9'] in network.commands
    route[0] = 'destination: 172.16.0.0\nmask: 255.255.0.0\ninterface: en9\n'
    recovery.begin('en9')
    recovery.prepare_route('en9')  # preexisting route belongs to the user
    network.commands.clear()
    recovery.restore()
    assert route[0]
    assert not any(command[:3] == ['route', '-n', 'delete'] for command in network.commands)
