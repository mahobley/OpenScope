"""Prepare an isolated Windows runtime with shared, inspectable LGPL FFmpeg."""
import argparse
import json
import os
import platform
import subprocess
import sys

from native_runtime import PROFILE, ROOT, digest, load_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conda", default="conda")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("Run this recipe on x64 Windows")
    if PROFILE.is_file():
        load_profile()
        print("Verified prepared native runtime:", PROFILE)
        return
    build = ROOT / "build"
    prefix = build / "native-conda"
    cache = build / "conda-pkgs"
    cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CONDA_PKGS_DIRS=str(cache))
    action = "update" if (prefix / "conda-meta").is_dir() else "create"
    subprocess.run([args.conda, "env", action, "--prefix", str(prefix), "--file",
                    str(ROOT / "third_party/windows-runtime.yml"), "--yes"], env=env, check=True)

    def record(path):
        return {"path": path.relative_to(build).as_posix(), "sha256": digest(path)}

    profile = {"schema_version": 1, "platform": sys.platform,
               "architecture": platform.machine().lower(), "kind": "conda",
               "environment": "native-conda", "interpreter": record(prefix / "python.exe"),
               "wheels": [], "native_inputs": [],
               "package_records": [record(p) for p in sorted((prefix / "conda-meta").glob("*.json"))],
               "recipes": [{"path": name, "sha256": digest(ROOT / name)} for name in (
                   "scripts/prepare_windows_runtime.py", "third_party/windows-runtime.yml")]}
    PROFILE.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    load_profile()
    print("Prepared", PROFILE)


if __name__ == "__main__":
    main()
