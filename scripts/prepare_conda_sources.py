"""Retain exact Conda native source recipes/archives and recover missing notices.

Run with a Python containing ruamel.yaml (Conda's own Python provides it).
Reads the completed native inventory; never updates the Conda environment.
"""
import hashlib
import json
import re
from pathlib import Path
import shutil
import tarfile
from urllib.parse import urlparse
from urllib.error import URLError
from urllib.request import urlopen
from ruamel.yaml import YAML

from native_runtime import ROOT, digest


def main():
    build = ROOT / "build"
    prefix = build / "native-conda"
    output = build / "conda-sources"
    output.mkdir(exist_ok=True)
    inventory = json.loads((build / "THIRD_PARTY_LICENSES/native-inventory.json").read_text())
    native_names = {b["owner"]["name"] for b in inventory["binaries"] if b.get("owner") and
                    b["owner"]["kind"] == "conda-package"}
    names = {b["owner"]["name"] for b in inventory["binaries"] if b.get("owner") and
             b["owner"]["kind"] == "conda-package" and
             ("GPL" in b["owner"].get("license", "") or b["notice_coverage"] != "collected")}
    evidence_file = build / "conda-source-notices.json"
    evidence = json.loads(evidence_file.read_text()) if evidence_file.is_file() else {}
    for receipt in sorted((prefix / "conda-meta").glob("*.json")):
        record = json.loads(receipt.read_text())
        cache = Path(record["extracted_package_dir"])
        if record["name"] not in native_names or (record["name"] not in names and (cache / "info/licenses").is_dir()):
            continue
        previous = evidence.get(receipt.name)
        if previous and previous["record_sha256"] == digest(receipt):
            retained = previous["sources"] + previous["notices"]
            if all((build / item["path"]).is_file() and digest(build / item["path"]) == item["sha256"] for item in retained):
                print("Verified retained sources/notices", record["name"], flush=True)
                continue
        recipe = cache / "info/recipe"
        rendered = recipe / "rendered_recipe.yaml"
        if not rendered.is_file():
            rendered = recipe / "meta.yaml"
        data = YAML(typ="safe").load(rendered.read_text(encoding="utf-8"))
        data = data.get("recipe", data)
        sources = data.get("source", [])
        if not sources and (recipe / "recipe.yaml").is_file():
            parent = YAML(typ="safe").load((recipe / "recipe.yaml").read_text(encoding="utf-8"))
            context = data.get("context", {})
            serialized = json.dumps(parent.get("source", []))
            serialized = re.sub(r"\$\{\{\s*(\w+)\s*\}\}",
                                lambda match: str(context.get(match[1], match[0])), serialized)
            sources = json.loads(serialized)
        if isinstance(sources, dict):
            sources = [sources]
        folder = output / receipt.stem
        folder.mkdir(exist_ok=True)
        shutil.copytree(recipe, folder / "recipe", dirs_exist_ok=True)
        shutil.copy2(receipt, folder / "conda-record.json")
        notices, archives = [], []
        for source in sources:
            urls = source.get("url", [])
            if isinstance(urls, str):
                urls = [urls]
            if not urls:
                raise ValueError(f"Source recipe requires non-archive review: {receipt.name}")
            url = urls[0].replace("http://", "https://", 1)
            if "${" in url:
                raise ValueError(f"Unresolved source template: {url}")
            archive = output / Path(urlparse(url).path).name
            algorithm = "sha256" if source.get("sha256") else "md5"
            expected = source.get(algorithm)
            if not expected:
                raise ValueError(f"Source has no recorded checksum: {url}")
            if not archive.is_file():
                temporary = archive.with_suffix(archive.suffix + ".part")
                candidates = [item.replace("http://", "https://", 1) for item in urls]
                # Both are official FreeType release locations. Some Savannah
                # redirect mirrors have incomplete certificate chains on Windows.
                if urlparse(url).hostname == "download.savannah.gnu.org" and "/freetype/" in url:
                    version = re.fullmatch(r"freetype-([0-9.]+)\.tar\.(?:gz|xz|bz2)", archive.name)
                    if version:
                        candidates.append(f"https://downloads.sourceforge.net/project/freetype/freetype2/{version[1]}/{archive.name}")
                if urlparse(url).hostname == "ftp.fau.de" and urlparse(url).path.startswith("/qtproject/official_releases/"):
                    qt_path = urlparse(url).path.removeprefix("/qtproject/official_releases/")
                    candidates.extend(("https://download.qt.io/official_releases/" + qt_path,
                                       "https://download.qt.io/archive/" + qt_path))
                for index, candidate in enumerate(candidates):
                    if not candidate.startswith("https://") or "${" in candidate:
                        raise ValueError(f"Invalid source URL: {candidate}")
                    print("Downloading", candidate, flush=True)
                    try:
                        with urlopen(candidate, timeout=60) as stream, temporary.open("wb") as target:
                            shutil.copyfileobj(stream, target)
                        with temporary.open("rb") as stream:
                            if hashlib.file_digest(stream, algorithm).hexdigest() != expected:
                                raise ValueError(f"Source checksum mismatch: {candidate}")
                        temporary.replace(archive)
                        url = candidate
                        break
                    except (URLError, TimeoutError, OSError):
                        if index == len(candidates) - 1:
                            raise
                    finally:
                        temporary.unlink(missing_ok=True)
            with archive.open("rb") as stream:
                actual = hashlib.file_digest(stream, algorithm).hexdigest()
            if actual != expected:
                raise ValueError(f"Source checksum mismatch: {archive}")
            archives.append({"path": str(archive.relative_to(build)), "sha256": digest(archive), "url": url,
                             "recipe_checksum_algorithm": algorithm})
            if not tarfile.is_tarfile(archive):
                continue  # e.g. the retained Qt Windows-only Mesa build input
            with tarfile.open(archive) as stream:
                for member in stream.getmembers():
                    name = Path(member.name).name
                    sqlite = record["name"] == "libsqlite" and name == "sqlite3.c"
                    if not member.isfile() or (member.size > 2_000_000 and not sqlite):
                        continue
                    if not sqlite and not (name.upper().startswith(("COPYING", "LICENSE", "LICENCE")) or name == "FTL.TXT"
                                           or "LICENSES" in Path(member.name).parts):
                        continue
                    content = stream.extractfile(member).read()
                    if sqlite:
                        content = content.split(b"*/", 1)[0] + b"*/\n"
                    target = folder / ("notice-" + member.name.replace("/", "_"))
                    target.write_bytes(content)
                    notices.append({"path": str(target.relative_to(build)), "sha256": digest(target)})
        if not archives or not notices:
            raise ValueError(f"No complete source/notice evidence for {record['name']}")
        evidence[receipt.name] = {"record_sha256": digest(receipt), "sources": archives, "notices": notices}
        (build / "conda-source-notices.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print("Retained", record["name"], record["version"], flush=True)


if __name__ == "__main__":
    main()
