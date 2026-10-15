"""Check frozen child executable resolution across native platforms."""
from pathlib import Path
import pytest
import packaging_runtime


@pytest.mark.parametrize("platform,suffix", [("win32", ".exe"), ("darwin", ""), ("linux", "")])
def test_frozen_child_executable(monkeypatch, tmp_path, platform, suffix):
    child = tmp_path / ("SonarHeadUnitEngine" + suffix)
    child.touch()
    monkeypatch.setattr(packaging_runtime, "FROZEN", True)
    monkeypatch.setattr(packaging_runtime.sys, "platform", platform)
    monkeypatch.setattr(packaging_runtime.sys, "executable", str(tmp_path / ("SonarHeadUnitController" + suffix)))
    assert packaging_runtime.bundle_executable("SonarHeadUnitEngine") == [str(child)]
    child.unlink()
    with pytest.raises(FileNotFoundError):
        packaging_runtime.bundle_executable("SonarHeadUnitEngine")
