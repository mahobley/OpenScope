"""Helpers for running from source or from a PyInstaller bundle."""

from __future__ import annotations

import os
from pathlib import Path
import sys


FROZEN = bool(getattr(sys, "frozen", False))


def bundle_executable(name: str) -> list[str]:
    """Return a command prefix for a sibling packaged executable or source."""
    if FROZEN:
        suffix = ".exe" if sys.platform == "win32" else ""
        executable = Path(sys.executable).with_name(name + suffix)
        if not executable.is_file():
            raise FileNotFoundError(f"Packaged executable not found: {executable}")
        return [str(executable)]
    return [sys.executable]


def bundled_path(*parts: str) -> Path:
    """Resolve a read-only resource in both source and frozen execution."""
    return Path(__file__).resolve().parent.joinpath(*parts)


def user_data_path(*parts: str) -> Path:
    """Return a user-writable application-data path for the current platform."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "SonarHeadUnit"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        base /= "SonarHeadUnit"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        base /= "SonarHeadUnit"
    return base.joinpath(*parts)
