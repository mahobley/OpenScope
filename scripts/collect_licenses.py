"""Collect installed Python package license files for release review."""
from importlib.metadata import distributions
from functools import lru_cache
from pathlib import Path
import argparse
import ast
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
FALLBACKS = {("pyserial", "3.5"): ROOT / "third_party/notices/pyserial-3.5-LICENSE.txt"}


def package_notices(dist, output):
    name = dist.metadata.get("Name", "unknown")
    folder = output / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}-{dist.version}"
    folder.mkdir(exist_ok=True)
    (folder / "METADATA.txt").write_text(dist.read_text("METADATA") or dist.read_text("PKG-INFO") or name, encoding="utf-8")
    copied = []
    for item in dist.files or ():
        if any(part.lower().startswith(("license", "licence", "copying", "notice", "copyright", "authors")) for part in item.parts):
            source = Path(dist.locate_file(item))
            if source.is_file():
                parts = [part for part in item.parts if part not in (".", "..", "/")]
                destination = folder.joinpath(*parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied.append(str(destination.relative_to(output)))
    if not copied:
        # Conda's Python metadata can omit RECORD/notices. Resolve the actual
        # distribution directory to its package receipt and retained cache.
        conda = _conda_owners()
        metadata = getattr(dist, "_path", None)
        match = conda.get(str((Path(metadata) / "METADATA").resolve())) if metadata else None
        if match:
            for index, notice in enumerate(match[1]):
                destination = folder / f"conda-{index}-{notice.name}"
                shutil.copy2(notice, destination)
                copied.append(str(destination.relative_to(output)))
    if not copied:
        fallback = FALLBACKS.get((name.lower(), dist.version))
        if fallback is None:
            raise RuntimeError(f"No license notice collected for {name} {dist.version}; supply a version-matched upstream notice")
        destination = folder / fallback.name
        shutil.copy2(fallback, destination)
        copied.append(str(destination.relative_to(output)))
    label = dist.metadata.get("License-Expression") or dist.metadata.get("License", "See bundled notices")
    if len(label) > 200:
        label = "See compound license text in METADATA.txt and collected notices"
    return {"name": name, "version": dist.version, "license": label,
            "license_files": copied}


def _debian_owner(source):
    for candidate in dict.fromkeys((str(source), str(source.resolve()))):
        result = subprocess.run(["dpkg-query", "-S", candidate], capture_output=True, text=True)
        if result.returncode == 0:
            package = result.stdout.splitlines()[0].split(": ", 1)[0]
            info = subprocess.run(["dpkg-query", "-W", "-f=${Version}\t${source:Package}\t${source:Version}", package],
                                  capture_output=True, text=True, check=True).stdout.split("\t")
            notice = Path("/usr/share/doc") / package.split(":")[0] / "copyright"
            return {"name": package, "version": info[0], "source_package": info[1], "source_version": info[2]}, [notice] if notice.is_file() else []
    return None, []


@lru_cache(maxsize=1)
def _conda_owners():
    owners = {}
    prefix = Path(sys.base_prefix)
    supplement = ROOT / "build/conda-source-notices.json"
    evidence = json.loads(supplement.read_text()) if supplement.is_file() else {}
    for record_path in (prefix / "conda-meta").glob("*.json"):
        record = json.loads(record_path.read_text())
        owner = {key: record.get(key) for key in ("name", "version", "build", "license", "url", "sha256")}
        cache = Path(record.get("extracted_package_dir", prefix / "pkgs" / record_path.stem)) / "info/licenses"
        notices = [p for p in cache.rglob("*") if p.is_file()]
        if not notices and record_path.name in evidence:
            from native_runtime import checked_file, digest
            entry = evidence[record_path.name]
            if digest(record_path) != entry["record_sha256"]:
                raise ValueError(f"Conda source notice receipt changed: {record_path}")
            notices = [checked_file(supplement.parent, item) for item in entry["notices"]]
        for name in record.get("files", []):
            owners[str((prefix / name).resolve())] = (owner, notices)
    return owners


def _homebrew_owner(source):
    """Use the actual input's installed keg, not whichever formula is current."""
    for keg in source.resolve().parents:
        receipt = keg / "INSTALL_RECEIPT.json"
        recipes = list((keg / ".brew").glob("*.rb"))
        if not receipt.is_file() or len(recipes) != 1:
            continue
        recipe = recipes[0]
        notices = sorted(p for p in keg.iterdir() if p.is_file() and
                         re.match(r"^(licen[cs]e|copying|copyright|notice)([._-]|$)", p.name, re.I))
        owner = {"name": recipe.stem, "version": keg.name, "kind": "homebrew-package",
                 "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                 "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
                 "provenance_review": "Installed keg ownership; source and patch correspondence remains separate"}
        # Preserve the matching recipe and receipt alongside the actual notices.
        return owner, notices, [recipe, receipt] if notices else []
    return None, [], []


def collect_native_notices(output, toc_path, installed):
    """Map the exact frozen native inputs to wheel, OS, or Conda notices."""
    entries = ast.literal_eval(toc_path.read_text())[0]
    wheels = {}
    for dist in installed:
        for item in dist.files or ():
            wheels[str(Path(dist.locate_file(item)).resolve())] = {
                "name": dist.metadata["Name"], "version": dist.version, "kind": "python-distribution"}
    conda = _conda_owners()
    records, unresolved = [], []
    for name, original, kind in entries:
        if kind not in {"BINARY", "EXTENSION"}:
            continue
        source = Path(original)
        owner = wheels.get(str(source.resolve()))
        notices = []
        provenance = []
        if owner is None:
            from native_runtime import source_owner
            owner, notices, provenance = source_owner(source)
        if owner is None and str(source.resolve()) in conda:
            owner, notices = conda[str(source.resolve())]
            owner = dict(owner, kind="conda-package")
        if owner is None and sys.platform == "linux" and shutil.which("dpkg-query"):
            owner, notices = _debian_owner(source)
            if owner:
                owner["kind"] = "debian-package"
        if owner is None and sys.platform == "darwin":
            owner, notices, provenance = _homebrew_owner(source)
        if owner and notices:
            folder = output / "native" / re.sub(r"[^A-Za-z0-9_.-]", "_", f"{owner['name']}-{owner['version']}")
            folder.mkdir(parents=True, exist_ok=True)
            copied = []
            for index, notice in enumerate(notices + provenance):
                destination = folder / f"{index}-{notice.name}"
                shutil.copy2(notice, destination)
                copied.append(str(destination.relative_to(output)))
        else:
            copied = []
        covered = owner is not None and (owner["kind"] == "python-distribution" or bool(copied))
        record = {"path": name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                  "owner": owner, "license_files": copied, "notice_coverage": "collected" if covered else "needs-review"}
        if owner and owner["kind"] == "python-distribution":
            record["notice_review"] = "Package notices collected; vendored native terms and sources require separate review"
        records.append(record)
        if not covered:
            unresolved.append(name)
    report = {"platform": sys.platform, "binaries": records, "unresolved_notices": unresolved,
              "source_review": "Corresponding sources and build recipes require the release review described in LICENSING.md"}
    (output / "native-inventory.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return unresolved


def collect_runtime_build(output, installed):
    import cv2
    report = {"python": sys.version, "platform": sys.platform, "opencv_build": cv2.getBuildInformation(), "ffmpeg": []}
    candidates = []
    for dist in installed:
        if re.sub(r"[-_.]+", "-", dist.metadata["Name"].lower()) != "opencv-python":
            continue
        for item in dist.files or ():
            name = Path(str(item)).name
            if not name.startswith(("libavutil", "avutil")) or not (".so" in name or name.endswith((".dylib", ".dll"))):
                continue
            candidates.append(Path(dist.locate_file(item)))
    from native_runtime import PROFILE, load_profile
    if PROFILE.is_file():
        profile = load_profile()
        report["packaging_profile"] = profile.get("kind", "source-build")
        report["profile_sha256"] = hashlib.sha256(PROFILE.read_bytes()).hexdigest()
        if profile.get("kind") == "conda":
            if sys.platform == "win32":
                candidates.extend((Path(sys.base_prefix) / "Library/bin").glob("avutil-*.dll"))
            else:
                candidate = Path(sys.base_prefix) / "lib/libavutil.so"
                if candidate.is_file():
                    candidates.append(candidate.resolve())
    dll_directory = os.add_dll_directory(str(Path(sys.base_prefix) / "Library/bin")) if sys.platform == "win32" and (Path(sys.base_prefix) / "Library/bin").is_dir() else None
    for candidate in dict.fromkeys(candidates):
            try:
                lib = ctypes.CDLL(str(candidate))
                details = {"file": candidate.name}
                for symbol, key in (("av_version_info", "version"), ("avutil_configuration", "configuration"), ("avutil_license", "license")):
                    function = getattr(lib, symbol)
                    function.restype = ctypes.c_char_p
                    details[key] = function().decode("utf-8", errors="replace")
                report["ffmpeg"].append(details)
            except (OSError, AttributeError) as exc:
                report["ffmpeg"].append({"file": candidate.name, "inspection_error": str(exc)})
    if dll_directory:
        dll_directory.close()
    (output / "runtime-build.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def collect_capture_notices(output):
    """Retain distribution-specific copyright notices for bundled libpcap."""
    if sys.platform != "linux":
        return []  # macOS system libpcap and separately installed Npcap aren't bundled.
    override = os.environ.get("SONAR_LIBPCAP_NOTICES")
    candidates = [Path(override)] if override else sorted(Path("/usr/share/doc").glob("libpcap*/copyright"))
    if not candidates or any(not path.is_file() for path in candidates):
        raise RuntimeError("Bundling Linux libpcap requires its copyright/license notices; set SONAR_LIBPCAP_NOTICES to the full notice file")
    folder = output / "libpcap-native"
    folder.mkdir(exist_ok=True)
    copied = []
    for index, source in enumerate(candidates):
        destination = folder / f"{index}-{source.parent.name}-copyright.txt"
        shutil.copy2(source, destination)
        copied.append(str(destination.relative_to(output)))
    return copied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--native-toc", type=Path, help="PyInstaller COLLECT-00.toc for this exact build")
    parser.add_argument("--strict-native", action="store_true", help="fail if native input notices cannot be located")
    args = parser.parse_args()
    if args.strict_native and args.native_toc is None:
        parser.error("--strict-native requires --native-toc")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    installed = sorted(distributions(), key=lambda d: d.metadata.get("Name", "").lower())
    inventory = [package_notices(dist, output) for dist in installed]
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, output / "Python-LICENSE.txt")
    native_notices = collect_capture_notices(output)
    (output / "inventory.json").write_text(json.dumps({"python": sys.version, "packages": inventory, "libpcap_notices": native_notices}, indent=2) + "\n", encoding="utf-8")
    shutil.copytree(ROOT / "third_party", output / "reference", dirs_exist_ok=True)
    shutil.copy2(ROOT / "docs/LICENSING.md", output / "LICENSING.md")
    collect_runtime_build(output, installed)
    if args.native_toc:
        unresolved = collect_native_notices(output, args.native_toc, installed)
        if unresolved and args.strict_native:
            raise RuntimeError(f"Native notice review required: {', '.join(unresolved)}")
    print(f"License inventory: {output} (review before publication)")


if __name__ == "__main__":
    main()
