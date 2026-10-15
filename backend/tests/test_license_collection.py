import hashlib
import base64
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_pinned_pyserial_missing_wheel_license_gets_full_upstream_notice(tmp_path):
    collector = load_script("collect_licenses")
    dist = SimpleNamespace(metadata={"Name": "pyserial", "License": "BSD"}, version="3.5", files=[], read_text=lambda _: "Name: pyserial")
    result = collector.package_notices(dist, tmp_path)
    assert result["license_files"]
    notice = (tmp_path / result["license_files"][0]).read_text()
    assert "Copyright (c) 2001-2020 Chris Liechti" in notice
    assert "Redistribution and use in source and binary forms" in notice
    assert "THIS SOFTWARE IS PROVIDED" in notice


def test_unreviewed_package_version_cannot_silently_use_old_notice(tmp_path):
    collector = load_script("collect_licenses")
    dist = SimpleNamespace(metadata={"Name": "pyserial"}, version="99.0", files=[], read_text=lambda _: "metadata")
    with pytest.raises(RuntimeError, match="version-matched"):
        collector.package_notices(dist, tmp_path)


def test_reference_licenses_match_recorded_upstream_hashes():
    for entry in json.loads((ROOT / "third_party/license-origins.json").read_text()):
        assert hashlib.sha256((ROOT / "third_party" / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]


def test_native_inventory_keeps_unknown_input_open_for_review(tmp_path, monkeypatch):
    collector = load_script("collect_licenses")
    binary = tmp_path / "vendor.so"
    binary.write_bytes(b"native input")
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(repr(([('vendor.so', str(binary), 'BINARY')],)))
    monkeypatch.setattr(collector, "_conda_owners", lambda: {})
    monkeypatch.setattr(collector, "_debian_owner", lambda _: (None, []))
    unresolved = collector.collect_native_notices(tmp_path, toc, [])
    assert unresolved == ["vendor.so"]
    report = json.loads((tmp_path / "native-inventory.json").read_text())
    assert report["binaries"][0]["notice_coverage"] == "needs-review"


def test_native_dependency_notice_and_source_package_version_are_retained(tmp_path, monkeypatch):
    collector = load_script("collect_licenses")
    binary = tmp_path / "library.so"
    binary.write_bytes(b"native input")
    notice = tmp_path / "copyright"
    notice.write_text("Complete upstream notice")
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(repr(([('library.so', str(binary), 'BINARY')],)))
    monkeypatch.setattr(collector.sys, "platform", "linux")
    monkeypatch.setattr(collector.shutil, "which", lambda _: "/usr/bin/dpkg-query")
    monkeypatch.setattr(collector, "_conda_owners", lambda: {})
    monkeypatch.setattr(collector, "_debian_owner", lambda _: ({"name": "libexample", "version": "1.2-3", "source_package": "example", "source_version": "1.2-3"}, [notice]))
    assert collector.collect_native_notices(tmp_path, toc, []) == []
    report = json.loads((tmp_path / "native-inventory.json").read_text())
    entry, = report["binaries"]
    assert entry["owner"]["source_version"] == "1.2-3"
    assert (tmp_path / entry["license_files"][0]).read_text() == "Complete upstream notice"


def test_macos_native_notices_follow_the_input_keg_not_the_current_formula(tmp_path, monkeypatch):
    collector = load_script("collect_licenses")
    keg = tmp_path / "Cellar" / "python@3.12" / "3.12.9"
    (keg / ".brew").mkdir(parents=True)
    (keg / "lib").mkdir()
    (keg / ".brew/python@3.12.rb").write_text('class PythonAT312 < Formula\nend\n')
    (keg / "INSTALL_RECEIPT.json").write_text('{"built_as_bottle":true}')
    (keg / "LICENSE").write_text("Exact installed Python distribution notice")
    binary = keg / "lib/Python"
    binary.write_bytes(b"native input")
    toc = tmp_path / "COLLECT-00.toc"
    toc.write_text(repr(([('Python', str(binary), 'BINARY')],)))
    monkeypatch.setattr(collector.sys, "platform", "darwin")
    monkeypatch.setattr(collector, "_conda_owners", lambda: {})
    assert collector.collect_native_notices(tmp_path, toc, []) == []
    entry, = json.loads((tmp_path / "native-inventory.json").read_text())["binaries"]
    assert entry["owner"]["version"] == "3.12.9"
    assert entry["owner"]["kind"] == "homebrew-package"
    retained = [(tmp_path / file).read_text() for file in entry["license_files"]]
    assert "Exact installed Python distribution notice" in retained
    assert '{"built_as_bottle":true}' in retained


def test_homebrew_receipt_alone_does_not_clear_a_missing_notice(tmp_path):
    collector = load_script("collect_licenses")
    keg = tmp_path / "Cellar" / "vendor" / "1.2"
    (keg / ".brew").mkdir(parents=True)
    (keg / ".brew/vendor.rb").write_text("recipe")
    (keg / "INSTALL_RECEIPT.json").write_text("{}")
    binary = keg / "vendor.dylib"
    binary.write_bytes(b"native input")
    owner, notices, provenance = collector._homebrew_owner(binary)
    assert owner["name"] == "vendor"
    assert notices == provenance == []


def test_source_hash_mismatch_does_not_replace_previous_archive(tmp_path, monkeypatch):
    sources = load_script("prepare_license_sources")
    monkeypatch.setattr(sources, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"wrong bytes"))
    original = tmp_path / "source.tar.gz"
    original.write_bytes(b"previous verified source")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        sources.fetch_source({"filename": original.name, "url": "https://example.org/source.tar.gz", "sha256": "0" * 64}, tmp_path)
    assert original.read_bytes() == b"previous verified source"
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("filename,url", [("../escape.tar.gz", "https://example.org/source"), ("source.tar.gz", "http://example.org/source")])
def test_source_download_rejects_unsafe_manifest_inputs(tmp_path, filename, url):
    sources = load_script("prepare_license_sources")
    with pytest.raises(ValueError):
        sources.fetch_source({"filename": filename, "url": url}, tmp_path)


def test_wheel_review_reports_local_dependency_edits(tmp_path):
    review = load_script("review_license_sources")
    source = tmp_path / "library.py"
    source.write_bytes(b"original package code")
    digest = base64.urlsafe_b64encode(hashlib.sha256(source.read_bytes()).digest()).rstrip(b"=").decode()
    dist = SimpleNamespace(metadata={"Name": "example"}, version="1.0",
                           read_text=lambda _: f"library.py,sha256={digest},21\n",
                           locate_file=lambda _: source)
    assert review.check_wheel(dist)["failures"] == []
    source.write_bytes(b"locally patched code")
    assert review.check_wheel(dist)["failures"] == [{"path": "library.py", "error": "modified"}]


def test_source_comparison_reports_different_files(tmp_path):
    review = load_script("review_license_sources")
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        content = b"upstream code"
        member = tarfile.TarInfo("example-1.0/src/example.py")
        member.size = len(content)
        stream.addfile(member, io.BytesIO(content))
    installed = tmp_path / "example.py"
    installed.write_bytes(b"modified code")
    dist = SimpleNamespace(metadata={"Name": "example"}, version="1.0", files=[Path("example.py")], locate_file=lambda _: installed)
    assert review.compare_source_files(dist, archive)["different_files"] == ["example.py"]


def test_ubuntu_source_lookup_requires_exact_binary_package_revision(monkeypatch):
    review = load_script("review_license_sources")
    monkeypatch.setattr(review.subprocess, "run", lambda *_args, **_kwargs:
                        SimpleNamespace(stdout="Package: example\nVersion: 1.2-3ubuntu1\n"))
    package = {"kind": "debian-package", "source_package": "example", "source_version": "1.2-3ubuntu2"}
    with pytest.raises(ValueError, match="Exact Ubuntu source version is not indexed"):
        review.ubuntu_manifest([package], Path("sources.list"), Path("lists"))


def test_source_review_rejects_corrupted_archive(tmp_path):
    review = load_script("review_license_sources")
    (tmp_path / "source.tar.gz").write_bytes(b"corrupted")
    manifest = {"sources": [{"name": "example", "version": "1.0", "filename": "source.tar.gz", "sha256": "0" * 64}]}
    with pytest.raises(ValueError, match="Source hash mismatch"):
        review.verify_archives(manifest, tmp_path)
