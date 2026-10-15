"""Package the retained Mac native sources, notices, recipes and replacement inputs."""
import json
from pathlib import Path
import zipfile
from native_runtime import PROFILE, ROOT, digest, load_profile
from prepare_license_sources import fetch_source


def main():
    load_profile()
    root = PROFILE.parent
    inputs = root / "sources"
    manifest = json.loads((ROOT / "third_party/macos-native-sources.json").read_text())
    for entry in manifest["sources"] + [manifest["pillow_wheel"]]:
        if digest(inputs / entry["filename"]) != entry["sha256"]:
            raise ValueError(f"Source input changed: {entry['filename']}")
    source_manifest = json.loads((ROOT / "third_party/source-manifest.json").read_text())
    for entry in source_manifest["sources"]:
        if entry["name"] in {"zeroconf", "pillow"}:
            fetch_source(entry, inputs)
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    target = output / "OpenScope-0.1.0-mac-arm64-third-party-sources.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        for directory in (inputs, root / "notices"):
            for file in sorted(directory.rglob("*")):
                if file.is_file():
                    archive.write(file, "native/" + str(file.relative_to(root)))
        for file in [PROFILE, *root.glob("commands*.json")]:
            archive.write(file, "native/" + file.name)
        for directory in (ROOT / "scripts", ROOT / "third_party"):
            for file in sorted(directory.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, str(file.relative_to(ROOT)))
        for name in ("docs/BUILD.md", "docs/LICENSING.md", "docs/RELEASE_STATUS.md", "backend/requirements.txt"):
            archive.write(ROOT / name, name)
    notices = ROOT / "build/THIRD_PARTY_LICENSES"
    with zipfile.ZipFile(output / "OpenScope-0.1.0-mac-arm64-notices.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(notices.rglob("*")):
            if file.is_file():
                archive.write(file, str(file.relative_to(notices)))
        application_notices = ROOT / "dist/mac-arm64/OpenScope.app/Contents/Resources/licenses"
        for file in application_notices.rglob("*"):
            if file.is_file():
                archive.write(file, "application/" + str(file.relative_to(application_notices)))
    print("Prepared", target)


if __name__ == "__main__":
    main()
