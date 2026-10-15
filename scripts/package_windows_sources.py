"""Package the verified Windows Conda source recipes and native notices."""
import json
import sys
import zipfile

from native_runtime import PROFILE, ROOT, checked_file, digest, load_profile
from prepare_license_sources import fetch_source


def main():
    profile = load_profile()
    if sys.platform != "win32" or profile.get("kind") != "conda":
        raise ValueError("Use the prepared Windows Conda runtime")
    build = ROOT / "build"
    notices = build / "THIRD_PARTY_LICENSES"
    inventory = json.loads((notices / "native-inventory.json").read_text())
    if inventory["unresolved_notices"]:
        raise ValueError("Resolve native notices before preparing release assets")
    evidence_path = build / "conda-source-notices.json"
    evidence = json.loads(evidence_path.read_text())
    required = {b["owner"]["name"] for b in inventory["binaries"]
                if b.get("owner", {}).get("kind") == "conda-package" and
                "GPL" in b["owner"].get("license", "")}
    retained = set()
    for receipt, entry in evidence.items():
        record = build / "native-conda/conda-meta" / receipt
        if digest(record) != entry["record_sha256"]:
            raise ValueError(f"Conda source receipt changed: {receipt}")
        retained.add(json.loads(record.read_text())["name"])
        for item in entry["sources"] + entry["notices"]:
            checked_file(build, item)
    if required - retained:
        raise ValueError(f"Missing required corresponding sources: {sorted(required - retained)}")
    python_sources = build / "python-license-sources"
    python_sources.mkdir(exist_ok=True)
    manifest = json.loads((ROOT / "third_party/source-manifest.json").read_text())
    zeroconf, = [item for item in manifest["sources"] if item["name"] == "zeroconf"]
    fetch_source(zeroconf, python_sources)
    output = ROOT / "dist"
    with zipfile.ZipFile(output / "OpenScope-0.1.0-win-x64-third-party-sources.zip", "w", zipfile.ZIP_STORED) as archive:
        for directory in (build / "conda-sources", python_sources, ROOT / "scripts", ROOT / "third_party", ROOT / "docs"):
            for file in sorted(directory.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, file.relative_to(ROOT).as_posix())
        for file in (PROFILE, evidence_path, build / "conda-explicit.txt"):
            archive.write(file, file.relative_to(ROOT).as_posix())
        for entry in profile["package_records"]:
            file = checked_file(build, entry)
            archive.write(file, file.relative_to(ROOT).as_posix())
    resources = output / "win-unpacked/resources"
    with zipfile.ZipFile(output / "OpenScope-0.1.0-win-x64-notices.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for directory in (resources / "licenses", resources / "backend/THIRD_PARTY_LICENSES"):
            for file in directory.rglob("*"):
                if file.is_file():
                    archive.write(file, file.relative_to(resources).as_posix())
    print("Prepared Windows source and notice archives")


if __name__ == "__main__":
    main()
