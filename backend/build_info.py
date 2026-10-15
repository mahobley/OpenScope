"""Identify packaged builds and source checkouts without relying on Git."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def build_info():
    root = Path(__file__).resolve().parent
    manifest = root / "build-manifest.json"
    if manifest.is_file():
        return json.loads(manifest.read_text())
    version = json.loads((root.parent / "desktop" / "package.json").read_text())["version"]
    digest = hashlib.sha256()
    files = list(root.rglob("*.py")) + list((root / "controller_web").rglob("*")) + list((root / "live_viewer" / "web").rglob("*")) + list((root.parent / "desktop" / "src").rglob("*"))
    for path in sorted(files):
        if path.is_file() and not any(part in {"tests", "dist", "build", "__pycache__"} for part in path.relative_to(root.parent).parts):
            digest.update(str(path.relative_to(root.parent)).encode())
            digest.update(path.read_bytes())
    return {"version": version, "id": digest.hexdigest()[:12]}


if __name__ == "__main__":
    manifest = Path(__file__).resolve().parent.parent / "build" / "build-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(build_info()) + "\n")
