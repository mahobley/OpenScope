"""Bounded controller logs, including output relayed from child processes."""

import logging
from logging.handlers import RotatingFileHandler
import threading
import os
from pathlib import Path
from live_viewer.permissions import make_user_editable


class QuietRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        # Avoid recursively logging a full-disk error through redirected stderr.
        try:
            os.write(2, b"OpenScope could not write diagnostic log\n")
        except OSError:
            pass


class DiagnosticStream:
    encoding = "utf-8"

    def __init__(self, path):
        self.handler = QuietRotatingHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self._pending = ""
        self._lock = threading.RLock()
        self._inode = None

    def _emit(self, line):
        self.handler.emit(logging.LogRecord("controller", logging.INFO, "", 0, line, (), None))
        if self.handler.stream is None:
            return
        inode = os.fstat(self.handler.stream.fileno()).st_ino
        if inode != self._inode:
            make_user_editable(Path(self.handler.baseFilename))
            os.chmod(self.handler.baseFilename, 0o600)
            self._inode = inode

    def write(self, value):
        with self._lock:
            self._pending += value
            # Bound even output that never supplies a newline.
            while "\n" in self._pending or len(self._pending) >= 8192:
                newline = self._pending.find("\n")
                if 0 <= newline < 8192:
                    line, self._pending = self._pending.split("\n", 1)
                else:
                    line, self._pending = self._pending[:8192], self._pending[8192:]
                self._emit(line)
        return len(value)

    def flush(self):
        with self._lock:
            if self._pending:
                line, self._pending = self._pending, ""
                self._emit(line)
            self.handler.flush()

    def isatty(self):
        return False
