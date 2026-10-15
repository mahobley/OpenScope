"""Prepare the isolated, patched Linux runtime without modifying system packages."""
import argparse
import json
import os
import subprocess
from native_runtime import PROFILE, ROOT, load_profile
from prepare_license_sources import fetch_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--conda", default="conda")
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    if PROFILE.is_file():
        load_profile()
        print("Verified prepared native runtime:", PROFILE)
        return
    prefix = ROOT / "build/native-conda"
    cache = ROOT / "build/conda-pkgs"
    cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CONDA_PKGS_DIRS=str(cache))
    action = "update" if (prefix / "conda-meta").is_dir() else "create"
    subprocess.run([args.conda, "env", action, "--prefix", str(prefix), "--file",
                    str(ROOT / "third_party/linux-runtime.yml"), "--yes"], env=env, check=True)
    sources = ROOT / "build/native-security/sources"
    sources.mkdir(parents=True, exist_ok=True)
    entries = json.loads((ROOT / "third_party/macos-native-sources.json").read_text())["sources"]
    entry, = [item for item in entries if item["directory"] == "ffmpeg-8.1.3"]
    fetch_source(entry, sources)
    subprocess.run([str(prefix / "bin/python"), str(ROOT / "scripts/finish_linux_runtime.py"),
                    "--jobs", str(args.jobs)], check=True)
    load_profile()


if __name__ == "__main__":
    main()
