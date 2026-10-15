"""Rebuild LGPL FFmpeg in the isolated Conda runtime and record exact inputs.

Run on Linux after creating build/native-conda using the reviewed environment
specification. System packages and other Conda environments are never modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

from native_runtime import ROOT, digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run this recipe on Linux")
    build = ROOT / "build"
    prefix = build / "native-conda"
    source_root = build / "native-security"
    entry, = [e for e in json.loads((ROOT / "third_party/macos-native-sources.json").read_text())["sources"]
              if e["directory"] == "ffmpeg-8.1.3"]
    archive = source_root / "sources" / entry["filename"]
    if digest(archive) != entry["sha256"]:
        raise ValueError("FFmpeg source hash mismatch")
    work = source_root / "work"
    work.mkdir(exist_ok=True)
    source = work / entry["directory"]
    if not source.exists():
        with tarfile.open(archive) as stream:
            stream.extractall(work, filter="data")
    env = dict(os.environ, PATH=str(prefix / "bin") + os.pathsep + os.environ["PATH"],
               PKG_CONFIG_PATH=str(prefix / "lib/pkgconfig"),
               PKG_CONFIG_LIBDIR=str(prefix / "lib/pkgconfig"),
               LD_LIBRARY_PATH=str(prefix / "lib"))
    commands = [[str(source / "configure"), f"--prefix={prefix}", "--enable-shared", "--disable-static",
                 "--disable-autodetect", "--disable-gpl", "--disable-nonfree", "--disable-network",
                 "--disable-programs", "--disable-doc", "--disable-debug", "--enable-pthreads",
                 "--enable-libopenh264", "--enable-zlib", f"--extra-cflags=-I{prefix}/include",
                 f"--extra-ldflags=-L{prefix}/lib -Wl,-rpath,{prefix}/lib"],
                ["make", "-j" + str(args.jobs)], ["make", "install"]]
    logs = source_root / "logs"
    logs.mkdir(exist_ok=True)
    for label, command in zip(("configure", "build", "install"), commands):
        print("FFmpeg", label, flush=True)
        with (logs / ("linux-ffmpeg-" + label + ".log")).open("w") as stream:
            subprocess.run(command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT, check=True)
    (source_root / "commands-linux-ffmpeg.json").write_text(json.dumps(commands, indent=2) + "\n")
    notices = source_root / "notices"
    notices.mkdir(exist_ok=True)
    notice = notices / "ffmpeg-8.1.3-COPYING.LGPLv2.1"
    shutil.copy2(source / "COPYING.LGPLv2.1", notice)
    def record(path):
        return {"path": str(path.relative_to(build)), "sha256": digest(path)}
    profile = {"schema_version": 1, "platform": "linux", "architecture": "x86_64", "kind": "conda",
               "environment": "native-conda", "interpreter": record(prefix / "bin/python"),
               "wheels": [], "native_inputs": [],
               "package_records": [record(p) for p in sorted((prefix / "conda-meta").glob("*.json"))],
               "recipes": [{"path": name, "sha256": digest(ROOT / name)} for name in (
                   "scripts/finish_linux_runtime.py", "third_party/linux-runtime.yml",
                   "third_party/macos-native-sources.json")]}
    for pattern in ("libav*.so.*", "libsw*.so.*"):
        for path in (prefix / "lib").glob(pattern):
            # Only the current link targets, not residual libraries from 8.1.2.
            linker_name = path.name.split(".so.")[0] + ".so"
            if path.is_symlink() or path.resolve() != (prefix / "lib" / linker_name).resolve():
                continue
            profile["native_inputs"].append(dict(record(path),
                owner={"name": "FFmpeg", "version": "8.1.3", "kind": "source-build"},
                notices=[record(notice)]))
    (build / "native-runtime.json").write_text(json.dumps(profile, indent=2) + "\n")
    print("Prepared", build / "native-runtime.json")


if __name__ == "__main__":
    main()
