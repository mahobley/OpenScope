from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
from pathlib import Path
import platform
import queue
import socket
import subprocess
import threading
from typing import Any
from urllib.parse import urlparse

import cv2
import numpy as np

from packaging_runtime import bundled_path

# In a PyInstaller build this module is loaded from the Python archive, while
# the browser assets live as normal files under the bundle's _internal folder.
WEB_ROOT = bundled_path("live_viewer", "web")
MAX_REQUEST_BODY = 64 * 1024
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors http://127.0.0.1:* http://localhost:*"
)


def choose_output_directory(initial_path: str = "") -> str | None:
    """Open the platform directory chooser and return an absolute path."""
    initial = Path(initial_path).expanduser() if initial_path else Path.cwd()
    if not initial.is_dir():
        initial = initial.parent
    while not initial.exists() and initial != initial.parent:
        initial = initial.parent
    if not initial.is_dir():
        initial = Path.cwd()

    if platform.system() == "Darwin":
        script = (
            "on run argv\n"
            "try\n"
            "set chosenFolder to choose folder with prompt "
            '"Choose an output folder" default location (POSIX file (item 1 of argv))\n'
            "return POSIX path of chosenFolder\n"
            "on error number -128\nreturn \"\"\nend try\nend run"
        )
        command = ["osascript", "-e", script, str(initial.resolve())]
    elif platform.system() == "Windows":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$dialog=New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$dialog.SelectedPath=$args[0];"
            "if($dialog.ShowDialog() -eq 'OK'){$dialog.SelectedPath}"
        )
        command = ["powershell", "-NoProfile", "-Command", script, str(initial.resolve())]
    else:
        command = [
            "zenity", "--file-selection", "--directory",
            f"--filename={initial.resolve()}/",
        ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or "Folder chooser failed")
    selected = result.stdout.strip()
    return str(Path(selected).resolve()) if selected else None


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def validate_loopback_bind(host: str) -> str:
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve viewer host {host!r}") from exc
    if not addresses or not all(_is_loopback(address) for address in addresses):
        raise ValueError("Unauthenticated viewer must bind only to loopback")
    return host


def request_is_same_origin(origin: str | None, host: str | None) -> bool:
    """Allow non-browser clients or a browser request from this HTTP host."""
    if not origin:
        return True
    parsed = urlparse(origin)
    return (
        parsed.scheme in {"http", "https"}
        and bool(host)
        and parsed.netloc.lower() == host.lower()
    )


@dataclass
class BrowserViewerState:
    """Thread-safe frame, status, and action bridge for the browser UI."""

    actions: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _jpeg: bytes | None = None
    _echogram_jpeg: bytes | None = None
    _sequence: int = 0
    _echogram_sequence: int = 0
    _status: dict[str, Any] = field(default_factory=dict)

    def publish(self, frame: np.ndarray, **status: Any) -> None:
        ok, encoded = cv2.imencode(".jpg", frame, (cv2.IMWRITE_JPEG_QUALITY, 88))
        if not ok:
            return
        with self._lock:
            self._jpeg = encoded.tobytes()
            self._sequence += 1
            self._status.update(status)
            self._status["frame_sequence"] = self._sequence

    def snapshot(self) -> tuple[bytes | None, dict[str, Any]]:
        with self._lock:
            return self._jpeg, dict(self._status)

    def update_status(self, **status: Any) -> None:
        with self._lock:
            self._status.update(status)

    def publish_echogram(self, frame: np.ndarray) -> None:
        ok, encoded = cv2.imencode(".jpg", frame, (cv2.IMWRITE_JPEG_QUALITY, 88))
        if not ok:
            return
        with self._lock:
            self._echogram_jpeg = encoded.tobytes()
            self._echogram_sequence += 1
            self._status["echogram_sequence"] = self._echogram_sequence

    def echogram_snapshot(self) -> bytes | None:
        with self._lock:
            return self._echogram_jpeg


def _handler_for(state: BrowserViewerState):
    class Handler(BaseHTTPRequestHandler):
        def _authorized(self) -> bool:
            if not _is_loopback(self.client_address[0]):
                return False
            try:
                parsed = urlparse(f"//{self.headers.get('Host', '')}")
                return (
                    parsed.hostname is not None
                    and parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
                    and parsed.port in {None, self.server.server_port}
                )
            except ValueError:
                return False

        def _send(self, body: bytes, content_type: str, status=HTTPStatus.OK):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), usb=()",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorized():
                self._send(b"Forbidden", "text/plain", HTTPStatus.FORBIDDEN)
                return
            path = urlparse(self.path).path
            if path == "/api/status":
                _, status = state.snapshot()
                self._send(json.dumps(status).encode(), "application/json")
                return
            if path == "/api/frame.jpg":
                frame, _ = state.snapshot()
                if frame is None:
                    self._send(b"", "image/jpeg", HTTPStatus.NO_CONTENT)
                else:
                    self._send(frame, "image/jpeg")
                return
            if path == "/api/echogram.jpg":
                frame = state.echogram_snapshot()
                if frame is None:
                    self._send(b"", "image/jpeg", HTTPStatus.NO_CONTENT)
                else:
                    self._send(frame, "image/jpeg")
                return
            asset = "index.html" if path == "/" else path.lstrip("/")
            target = WEB_ROOT / asset
            if not target.is_file() or WEB_ROOT not in target.resolve().parents:
                self._send(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
                return
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
            }.get(target.suffix, "application/octet-stream")
            self._send(target.read_bytes(), content_type)

        def do_POST(self):
            if not self._authorized():
                self._send(b'{"accepted":false}', "application/json", HTTPStatus.FORBIDDEN)
                return
            request_path = urlparse(self.path).path
            if request_path not in {"/api/action", "/api/select-folder"}:
                self._send(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)
                return
            if not request_is_same_origin(
                self.headers.get("Origin"),
                self.headers.get("Host"),
            ):
                self._send(
                    b'{"accepted":false,"error":"Cross-origin requests are not allowed"}',
                    "application/json",
                    HTTPStatus.FORBIDDEN,
                )
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= MAX_REQUEST_BODY:
                    raise ValueError
                action = json.loads(self.rfile.read(length))
                if not isinstance(action, dict):
                    raise ValueError
                if request_path == "/api/select-folder":
                    initial_path = action.get("initial_path", "")
                    if not isinstance(initial_path, str) or len(initial_path) > 4096:
                        raise ValueError
                    try:
                        selected = choose_output_directory(initial_path)
                    except (OSError, RuntimeError) as exc:
                        self._send(
                            json.dumps({"selected": False, "error": str(exc)}).encode(),
                            "application/json",
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                        )
                        return
                    self._send(
                        json.dumps({"selected": selected is not None, "path": selected}).encode(),
                        "application/json",
                    )
                    return
                if not isinstance(action.get("type"), str):
                    raise ValueError
                if action.get("type") not in {
                    "zoom_in", "zoom_out", "set_range", "toggle_sonar",
                    "set_gain", "set_echogram_gain", "set_intensity_window",
                    "set_color_scheme",
                    "toggle_distance_grid",
                    "zero_tilt", "reverse_tilt", "reset_tilt",
                    "zero_roll", "reverse_roll", "reset_roll",
                    "toggle_display", "toggle_view", "toggle_motion", "toggle_recording",
                    "toggle_echogram_recording", "save_frame",
                    "save_echogram_frame",
                    "toggle_echogram_range_crop",
                    "toggle_live_range_crop",
                    "acknowledge_recording_error", "set_output_paths", "set_recording_formats", "set_autosave", "stop_autosave",
                    "stop_emulator", "open_output_folder", "set_theme",
                }:
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                self._send(b'{"accepted":false}', "application/json",
                           HTTPStatus.BAD_REQUEST)
                return
            state.actions.put(action)
            self._send(b'{"accepted":true}', "application/json",
                       HTTPStatus.ACCEPTED)

        def log_message(self, _format, *_args):
            pass

    return Handler


class BrowserViewerServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.state = BrowserViewerState()
        self._server = ThreadingHTTPServer(
            (validate_loopback_bind(host), port), _handler_for(self.state)
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="browser-viewer",
            daemon=True,
        )

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
