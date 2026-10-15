"""Assemble verified Linux native sources/recipes and notices beside the AppImage."""
import argparse
import json
from pathlib import Path
import zipfile
from native_runtime import ROOT, PROFILE, checked_file, digest, load_profile
from prepare_license_sources import fetch_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ubuntu-manifest", type=Path, required=True)
    parser.add_argument("--ubuntu-sources", type=Path, required=True)
    args = parser.parse_args()
    load_profile()
    build = ROOT / "build"
    native_manifest = json.loads((ROOT / "third_party/macos-native-sources.json").read_text())
    ffmpeg, = [item for item in native_manifest["sources"] if item["directory"] == "ffmpeg-8.1.3"]
    if digest(build / "native-security/sources" / ffmpeg["filename"]) != ffmpeg["sha256"]:
        raise ValueError("FFmpeg source archive changed")
    notices = build / "THIRD_PARTY_LICENSES"
    inventory = json.loads((notices / "native-inventory.json").read_text())
    if inventory["unresolved_notices"]:
        raise ValueError("Resolve native notices before preparing release assets")
    required = {(item["owner"]["source_package"], item["owner"]["source_version"])
                for item in inventory["binaries"] if item.get("owner", {}).get("kind") == "debian-package"}
    manifest = json.loads(args.ubuntu_manifest.read_text())
    selected = [item for item in manifest["sources"] if (item["name"], item["version"]) in required]
    if {(item["name"], item["version"]) for item in selected} != required:
        raise ValueError("Ubuntu source manifest does not cover the actual native inventory")
    for item in selected:
        if digest(args.ubuntu_sources / item["filename"]) != item["sha256"]:
            raise ValueError(f"Ubuntu source checksum mismatch: {item['filename']}")
    evidence = json.loads((build / "conda-source-notices.json").read_text())
    for receipt, entry in evidence.items():
        if digest(build / "native-conda/conda-meta" / receipt) != entry["record_sha256"]:
            raise ValueError(f"Conda source receipt changed: {receipt}")
        for item in entry["sources"] + entry["notices"]:
            checked_file(build, item)
    python_sources = build / "python-license-sources"
    python_sources.mkdir(exist_ok=True)
    manifest = json.loads((ROOT / "third_party/source-manifest.json").read_text())
    zeroconf, = [item for item in manifest["sources"] if item["name"] == "zeroconf"]
    fetch_source(zeroconf, python_sources)
    output = ROOT / "dist"
    with zipfile.ZipFile(output / "OpenScope-0.1.0-linux-x64-third-party-sources.zip", "w", zipfile.ZIP_STORED) as archive:
        for directory in (build / "conda-sources", python_sources, ROOT / "scripts", ROOT / "third_party"):
            for file in sorted(directory.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, str(file.relative_to(ROOT)))
        for file in [PROFILE, build / "conda-source-notices.json", build / "native-security/commands-linux-ffmpeg.json",
                     build / "native-security/sources/ffmpeg-8.1.3.tar.xz"]:
            archive.write(file, str(file.relative_to(ROOT)))
        if (build / "conda-explicit.txt").is_file():
            archive.write(build / "conda-explicit.txt", "build/conda-explicit.txt")
        archive.writestr("ubuntu-source-manifest.json", json.dumps({"sources": selected}, indent=2))
        for item in selected:
            archive.write(args.ubuntu_sources / item["filename"], "ubuntu/" + item["filename"])
        for file in (ROOT / "docs").glob("*.md"):
            archive.write(file, str(file.relative_to(ROOT)))
    resources = output / "linux-unpacked/resources"
    with zipfile.ZipFile(output / "OpenScope-0.1.0-linux-x64-notices.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in (resources / "licenses", resources / "backend/THIRD_PARTY_LICENSES"):
            for file in directory.rglob("*"):
                if file.is_file():
                    archive.write(file, str(file.relative_to(resources)))
    print("Prepared Linux source and notice archives")


if __name__ == "__main__":
    main()
