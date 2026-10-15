"""Repack the pinned Mac Pillow wheel with patched, ABI-compatible PNG/TIFF libs.

Preserves Pillow's other codecs and font support. Verifies referenced symbols,
updates wheel RECORD with wheel/delocate, and retains the replacement notices.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import zipfile

from prepare_license_sources import fetch_source

ROOT = Path(__file__).resolve().parents[1]


def symbols(library, exported):
    flags = "-gU" if exported else "-u"
    output = subprocess.check_output(["nm", flags, str(library)], text=True)
    return {line.split()[-1] for line in output.splitlines() if line.split() and line.split()[-1].startswith("_")}


def repair(root):
    root = root.resolve()
    tools, prefix = root / "tools/bin", root / "prefix"
    entry = json.loads((ROOT / "third_party/macos-native-sources.json").read_text())["pillow_wheel"]
    fetch_source(entry, root / "sources")
    original = root / "sources" / entry["filename"]
    if hashlib.file_digest(original.open("rb"), "sha256").hexdigest() != entry["sha256"]:
        raise ValueError("Upstream Pillow wheel hash mismatch")
    unpacked = root / "pillow-repair"
    if unpacked.exists():
        shutil.rmtree(unpacked)
    unpacked.mkdir()
    with zipfile.ZipFile(original) as archive:
        archive.extractall(unpacked)
    binaries = [p for p in (unpacked / "PIL").rglob("*") if p.suffix in {".so", ".dylib"}]
    undefined = set().union(*(symbols(p, False) for p in binaries))
    checks = []
    for target_name, replacement_name in [("libpng16.16.dylib", "libpng16.dylib"),
                                           ("libtiff.6.dylib", "libtiff.dylib")]:
        target = unpacked / "PIL/.dylibs" / target_name
        replacement = (prefix / "lib" / replacement_name).resolve()
        referenced = symbols(target, True) & undefined
        missing = referenced - symbols(replacement, True)
        checks.append({"library": target_name, "referenced_symbols": sorted(referenced),
                       "missing_symbols": sorted(missing),
                       "replacement_sha256": hashlib.file_digest(replacement.open("rb"), "sha256").hexdigest()})
        if missing:
            raise RuntimeError(f"Incompatible replacement {target_name}: {sorted(missing)}")
        shutil.copy2(replacement, target)
        subprocess.run(["install_name_tool", "-id", "@loader_path/" + target_name, str(target)], check=True)
        # delocate resolves this private prefix then removes external rpaths.
        subprocess.run(["install_name_tool", "-add_rpath", str(prefix / "lib"), str(target)], check=True)
        subprocess.run(["codesign", "--force", "--sign", "-", str(target)], check=True, capture_output=True)
    notice = ["OpenScope macOS native replacements: libpng 1.6.59, libtiff 4.7.2,",
              "and their rebuilt zlib 1.3.2 / libjpeg-turbo 3.2.0 dependencies.",
              "Original Pillow components retain their upstream terms.\n"]
    for directory, files in [("libpng-1.6.59", ["LICENSE", "LICENSE.md"]),
                             ("tiff-4.7.2", ["LICENSE.md"]),
                             ("zlib-1.3.2", ["LICENSE"]),
                             ("libjpeg-turbo-3.2.0", ["LICENSE.md", "README.ijg", "LICENSE-3RD-PARTY.md"])]:
        found = [root / "work" / directory / name for name in files if (root / "work" / directory / name).is_file()]
        if not found:
            raise RuntimeError(f"Missing source notice for {directory}")
        for file in found:
            notice += ["\n--- " + directory + "/" + file.name + " ---\n", file.read_text()]
    info, = unpacked.glob("*.dist-info")
    license_dir = info / "licenses"
    license_dir.mkdir(exist_ok=True)
    (license_dir / "OpenScope-native-replacements.txt").write_text("\n".join(notice))
    (root / "pillow-abi-check.json").write_text(json.dumps(checks, indent=2) + "\n")
    raw = root / "wheels-raw-pillow"
    raw.mkdir(exist_ok=True)
    subprocess.run([str(tools / "python"), "-m", "wheel", "pack", str(unpacked), "-d", str(raw)], check=True)
    wheel, = raw.glob("pillow-*.whl")
    env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET="13.0", DYLD_LIBRARY_PATH=str(prefix / "lib"))
    subprocess.run([str(tools / "delocate-wheel"), "--require-archs=arm64", "--sanitize-rpaths",
                    "-L", ".openscope-dylibs", "-w", str(root / "wheels"), str(wheel)], env=env, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=ROOT / "build/native-security")
    repair(parser.parse_args().work_root)
