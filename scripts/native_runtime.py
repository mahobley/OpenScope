"""Validate locally built native artifacts before packaging consumes them."""
import hashlib
from functools import lru_cache
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / ("build/native-security/native-runtime.json" if sys.platform == "darwin" else "build/native-runtime.json")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_file(root, entry):
    path = (root / entry["path"]).resolve()
    if not path.is_relative_to(root.resolve()) or digest(path) != entry["sha256"]:
        raise ValueError(f"Native runtime input changed or escaped its build directory: {path}")
    return path


def load_profile(path=PROFILE):
    profile = json.loads(path.read_text())
    if profile["platform"] != sys.platform or profile["architecture"] != platform.machine().lower():
        raise ValueError("Native runtime profile is for a different OS/architecture")
    for entry in profile["recipes"]:
        checked_file(ROOT, entry)
    for entry in profile["wheels"]:
        checked_file(path.parent, entry)
    for entry in profile.get("package_records", []):
        checked_file(path.parent, entry)
    checked_file(path.parent, profile["interpreter"])
    return profile


@lru_cache(maxsize=4)
def _source_index(path, modified_ns):
    # A freeze can contain hundreds of inputs. Validate the immutable profile
    # once per process/profile revision, while checking each actual input below.
    profile = load_profile(path)
    return {(path.parent / entry["path"]).resolve(): entry for entry in profile["native_inputs"]}


def source_owner(source, path=PROFILE):
    if not path.is_file() or not source.resolve().is_relative_to(path.parent.resolve()):
        return None, [], []
    source = source.resolve()
    entry = _source_index(path, path.stat().st_mtime_ns).get(source)
    if entry:
        checked_file(path.parent, entry)
        notices = [checked_file(path.parent, item) for item in entry["notices"]]
        return entry["owner"], notices, [path]
    return None, [], []
