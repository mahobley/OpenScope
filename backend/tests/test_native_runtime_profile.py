import importlib.util
import json
import platform
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("native_runtime", Path(__file__).resolve().parents[2] / "scripts/native_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def test_native_input_hash_and_path_are_enforced(tmp_path):
    binary = tmp_path / "library"
    binary.write_bytes(b"reviewed")
    entry = {"path": binary.name, "sha256": runtime.digest(binary)}
    assert runtime.checked_file(tmp_path, entry) == binary
    binary.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        runtime.checked_file(tmp_path, entry)
    with pytest.raises(ValueError, match="escaped"):
        runtime.checked_file(tmp_path, {"path": "../outside", "sha256": "unused"})


def test_profile_rejects_other_platform_before_using_artifacts(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"platform": "not-" + sys.platform, "architecture": platform.machine().lower()}))
    with pytest.raises(ValueError, match="different OS"):
        runtime.load_profile(profile)
