"""Small atomic settings files, independent of the browser's changing port."""
import json
import os
import tempfile
import threading
from pathlib import Path

from live_viewer.permissions import make_user_editable


class Preferences:
    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.RLock()

    def read(self):
        if self.path is None:
            return {}
        try:
            if self.path.stat().st_size > 65536:
                return {}
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, values):
        with self.lock:
            self._update(values)

    def _update(self, values):
        if self.path is None:
            return
        merged = self.read() | values
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(merged, file)
                file.flush()
                os.fsync(file.fileno())
            make_user_editable(temporary)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
