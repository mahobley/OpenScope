"""Retain source notices and bind the local Mac runtime to its recipes and hashes."""
from pathlib import Path
import argparse
import json
import shutil
import subprocess
import zipfile

from native_runtime import ROOT, digest


def finalize(root):
    root = root.resolve()
    notices = root / "notices"
    notices.mkdir(exist_ok=True)
    groups = {
        "Python-3.12.15": ["LICENSE", "Modules/expat/COPYING", "Modules/_decimal/libmpdec/mpdecimal.h"],
        "openssl-3.6.5": ["LICENSE.txt"],
        "zlib-1.3.2": ["LICENSE"],
        "ffmpeg-8.1.3": ["COPYING.LGPLv2.1", "LICENSE.md"],
        "libpng-1.6.59": ["LICENSE"],
        "libjpeg-turbo-3.2.0": ["LICENSE.md", "README.ijg"],
        "tiff-4.7.2": ["LICENSE.md"],
    }
    retained = {}
    for group, files in groups.items():
        retained[group] = []
        for name in files:
            source = root / "work" / group / name
            if not source.is_file():
                raise ValueError(f"Missing source notice: {source}")
            destination = notices / (group + "-" + name.replace("/", "_"))
            shutil.copy2(source, destination)
            retained[group].append(destination)
    # Repack RECORD after adding complete notices for our newly linked libraries.
    wheel, = (root / "wheels").glob("opencv_python-*.whl")
    unpacked = root / "opencv-notice-wheel"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    unpacked.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(unpacked)
    info, = unpacked.glob("*.dist-info")
    target = info / "licenses"
    target.mkdir(exist_ok=True)
    for group in groups:
        if group.startswith(("Python", "openssl")):
            continue
        for notice in retained[group]:
            shutil.copy2(notice, target / notice.name)
    shutil.copy2(ROOT / "third_party/macos-native-sources.json", target / "native-sources.json")
    subprocess.run([str(root / "tools/bin/python"), "-m", "wheel", "pack", str(unpacked),
                    "-d", str(root / "wheels")], check=True)
    def record(path, base=root):
        return {"path": str(path.relative_to(base)), "sha256": digest(path)}
    profile = {"schema_version": 1, "platform": "darwin", "architecture": "arm64",
               "interpreter": record(root / "python/bin/python3.12"),
               "wheels": [record(p) for p in sorted((root / "wheels").glob("*.whl"))],
               "recipes": [record(ROOT / name, ROOT) for name in (
                   "scripts/build_macos_native.py", "scripts/repair_macos_pillow.py",
                   "scripts/finalize_macos_native.py", "third_party/macos-native-sources.json",
                   "third_party/patches/opencv-5.0-minimal-typing.patch")],
               "native_inputs": []}
    inputs = list((root / "python").rglob("*.so")) + list((root / "python/lib").glob("*.dylib"))
    inputs += list((root / "prefix/lib").glob("*.dylib"))
    for binary in inputs:
        if binary.is_symlink():
            continue
        if binary.is_relative_to(root / "python"):
            group, name, version = "Python-3.12.15", "Python", "3.12.15"
        elif binary.name.startswith(("libssl", "libcrypto")):
            group, name, version = "openssl-3.6.5", "OpenSSL", "3.6.5"
        elif binary.name.startswith("libz."):
            group, name, version = "zlib-1.3.2", "zlib", "1.3.2"
        else:
            continue  # Media libraries are owned by the repaired wheels.
        profile["native_inputs"].append(dict(record(binary),
            owner={"name": name, "version": version, "kind": "source-build"},
            notices=[record(p) for p in retained[group]]))
    (root / "native-runtime.json").write_text(json.dumps(profile, indent=2) + "\n")
    shutil.rmtree(unpacked)
    print("Finalized", root / "native-runtime.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=ROOT / "build/native-security")
    finalize(parser.parse_args().work_root)
