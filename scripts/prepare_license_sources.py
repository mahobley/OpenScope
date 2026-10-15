"""Download the reviewed source-manifest inputs and record their hashes.

This prepares inputs for a release source review; it does not declare that an
upstream tarball corresponds to a patched wheel or completes every native source.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def fetch_source(entry, output):
    filename = entry["filename"]
    if Path(filename).name != filename or filename in {"", ".", ".."}:
        raise ValueError("Source filename must be a plain filename")
    if urlparse(entry["url"]).scheme != "https":
        raise ValueError("Source downloads require HTTPS")
    destination = output / filename
    if destination.is_file() and entry.get("sha256"):
        with destination.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest == entry["sha256"]:
            return dict(entry, sha256=digest, correspondence_review="pending")
    temporary = destination.with_name(filename + ".part")
    try:
        with urlopen(entry["url"], timeout=60) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        with temporary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if entry.get("sha256") and digest != entry["sha256"]:
            raise ValueError(f"SHA-256 mismatch for {filename}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(entry, sha256=digest, correspondence_review="pending")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "third_party/source-manifest.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    report = {"schema_version": 1, "status": "source inputs downloaded; release correspondence review required", "sources": []}
    for entry in manifest["sources"]:
        source = fetch_source(entry, args.output)
        report["sources"].append(source)
        # Preserve progress if a later upstream server is unavailable.
        (args.output / "source-downloads.json").write_text(json.dumps(report, indent=2) + "\n")
        print(f"Prepared {source['filename']} ({source['sha256']})")
    shutil.copy2(ROOT / "docs/LICENSING.md", args.output / "LICENSING.md")


if __name__ == "__main__":
    main()
