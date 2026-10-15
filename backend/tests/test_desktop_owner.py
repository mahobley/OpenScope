import ctypes
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import browser_controller as controller


@pytest.mark.parametrize("status,alive", [(258, True), (0, False)])
def test_windows_owner_uses_nonblocking_wait_and_closes_handle(monkeypatch, status, alive):
    # A pointer-sized handle catches accidental truncation of 64-bit handles.
    handle = 0x123456789
    api = SimpleNamespace(OpenProcess=Mock(return_value=handle),
                          WaitForSingleObject=Mock(return_value=status),
                          CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    monkeypatch.setattr(controller.sys, "platform", "win32")
    monkeypatch.setattr(controller.os, "kill", Mock(side_effect=AssertionError("must not signal owner")))
    assert controller.desktop_owner_alive(None, 1234) is alive
    api.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
    api.WaitForSingleObject.assert_called_once_with(handle, 0)
    api.CloseHandle.assert_called_once_with(handle)
    assert api.OpenProcess.restype is ctypes.c_void_p


@pytest.mark.parametrize("error,alive", [(87, False), (5, True)])
def test_windows_owner_missing_or_inaccessible(monkeypatch, error, alive):
    api = SimpleNamespace(OpenProcess=Mock(return_value=None),
                          WaitForSingleObject=Mock(), CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    assert controller._windows_process_alive(1234) is alive
    api.WaitForSingleObject.assert_not_called()
    api.CloseHandle.assert_not_called()


def test_windows_owner_wait_failure_closes_handle(monkeypatch):
    api = SimpleNamespace(OpenProcess=Mock(return_value=123),
                          WaitForSingleObject=Mock(return_value=0xFFFFFFFF),
                          CloseHandle=Mock())
    monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=api), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 6, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda error: OSError(error, "invalid handle"), raising=False)
    with pytest.raises(OSError):
        controller._windows_process_alive(1234)
    api.CloseHandle.assert_called_once_with(123)


def test_missing_lifetime_file_skips_process_check(tmp_path, monkeypatch):
    check = Mock(side_effect=AssertionError("must not query owner"))
    monkeypatch.setattr(controller, "_windows_process_alive", check)
    assert not controller.desktop_owner_alive(tmp_path / "missing", 1234)


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows process APIs")
def test_native_windows_owner_survives_check_and_detects_exit():
    assert controller.desktop_owner_alive(None, os.getpid())
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert controller.desktop_owner_alive(None, process.pid)
        assert process.poll() is None
        process.terminate()
        process.wait(timeout=10)
        assert not controller.desktop_owner_alive(None, process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
