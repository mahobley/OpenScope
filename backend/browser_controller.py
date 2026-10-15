#!/usr/bin/env python3
"""Persistent browser controller for sonar emulator sessions."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import errno
from http.cookies import SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import os
from pathlib import Path
import platform
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from packaging_runtime import FROZEN, bundle_executable, user_data_path
import head_unit_emulator as emulator
from live_viewer.permissions import make_user_editable


ROOT = Path(__file__).resolve().parent
EMULATOR_SCRIPT = ROOT / "head_unit_emulator.py"
WEB_ROOT = ROOT / "controller_web"
DEFAULT_RECORDINGS_ROOT = (
    user_data_path("recordings") if FROZEN else ROOT / "recordings"
)
RANGE_VALUES = ("none", "min", "7", "9", "10", "15", "20", "25", "30",
                "35", "40", "45", "50", "55", "60", "65", "70", "75",
                "80", "90", "100", "110", "120", "135", "150", "175", "200")
INTERFACE_LOG_RE = re.compile(
    r"(?:Auto-detected sonar-facing Ethernet interface|"
    r"Using sonar-facing Ethernet interface):\s*(\S+)"
)
STREAM_RATE_LOG_RE = re.compile(
    r"GLS10 50223 stream:\s*(\d+) packets,\s*(\d+) bytes/s,\s*"
    r"(\d+) image-sized"
)
MAX_REQUEST_BODY = 64 * 1024
MAX_TEXT_FIELD = 255
STREAM_SAMPLE_STALE_SECONDS = 2.5
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; "
    "frame-src http://127.0.0.1:*; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


def action_feedback_from_log(line: str) -> str | None:
    """Convert control log output into concise dashboard feedback."""
    match = re.search(r"Range command sent to GLS10:\s*(\S+) ft", line)
    if match:
        return f"Range requested: {match.group(1)} ft"
    match = re.search(
        r"GLS10 confirmed requested range change:\s*(\S+)",
        line,
    )
    if match:
        return f"GLS10 confirmed range: {match.group(1)} ft"
    match = re.search(
        r"GLS10 has not applied requested range\s+(\S+); "
        r"frames still report\s+([\d.]+) ft",
        line,
    )
    if match:
        return (
            f"Range not applied: requested {match.group(1)} ft, "
            f"GLS10 still reports {float(match.group(2)):.1f} ft"
        )
    if "Range command" in line and (
        "did not reach the emulator" in line or "rejected:" in line
    ):
        return line
    if line.startswith("Sonar transmission "):
        return line
    if line.startswith("Sonar ") and (
        "did not reach the emulator" in line or "rejected:" in line
    ):
        return line
    if line == "Output paths ready" or line.startswith(
        "Output path validation failed:"
    ):
        return line
    return None


def interface_link_state(interface: str | None) -> str:
    """Return active, inactive, or unknown without modifying the adapter."""
    if not interface:
        return "unknown"
    system = platform.system()
    if system == "Darwin":
        try:
            result = subprocess.run(
                ["ifconfig", interface],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "unknown"
        if result.returncode:
            return "unknown"
        match = re.search(r"^\s*status:\s*(\S+)", result.stdout, re.MULTILINE)
        if match:
            return "active" if match.group(1).lower() == "active" else "inactive"
    elif system == "Linux":
        try:
            state = Path(
                f"/sys/class/net/{interface}/operstate"
            ).read_text().strip().lower()
        except OSError:
            return "unknown"
        return "active" if state == "up" else "inactive"
    return "unknown"


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


@dataclass(frozen=True)
class SessionConfig:
    interface: str | None
    save_root: Path
    session_name: str | None
    startup_range: str
    autosave_raw: bool
    autosave_echogram: bool
    autosave_minutes: float
    save_frames: bool
    auto_configure_interface: bool
    debug: bool

    @classmethod
    def from_json(
        cls,
        value: Any,
        *,
        allowed_save_root: Path | None = None,
    ) -> "SessionConfig":
        if not isinstance(value, dict):
            raise ValueError("Session configuration is required")
        allowed_keys = {
            "interface", "save_root", "session_name", "startup_range",
            "autosave_raw", "autosave_echogram", "autosave_minutes",
            "save_frames", "auto_configure_interface", "debug",
        }
        unknown = set(value) - allowed_keys
        if unknown:
            raise ValueError(f"Unknown configuration field: {sorted(unknown)[0]}")
        interface = _optional_text(value.get("interface"), "interface")
        save_root_value = value.get("save_root", "")
        if not isinstance(save_root_value, str):
            raise ValueError("save_root must be text")
        save_root_text = save_root_value.strip()
        if not save_root_text:
            raise ValueError("A save folder is required")
        if "\x00" in save_root_text or len(save_root_text) > 4096:
            raise ValueError("Invalid save folder")
        save_root = Path(save_root_text).expanduser().resolve()
        if allowed_save_root is not None:
            allowed = allowed_save_root.expanduser().resolve()
            if save_root != allowed and allowed not in save_root.parents:
                raise ValueError(f"Save folder must be inside {allowed}")
        session_name = _optional_text(value.get("session_name"), "session_name")
        startup_range_value = value.get("startup_range", "15")
        if not isinstance(startup_range_value, str):
            raise ValueError("startup_range must be text")
        startup_range = startup_range_value.lower()
        if startup_range not in RANGE_VALUES:
            raise ValueError("Unsupported startup range")
        try:
            autosave_minutes = float(value.get("autosave_minutes", 10))
        except (TypeError, ValueError) as exc:
            raise ValueError("Autosave minutes must be a number") from exc
        if not 0 < autosave_minutes <= 1440:
            raise ValueError("Autosave minutes must be greater than 0 and at most 1440")
        return cls(
            interface=interface,
            save_root=save_root,
            session_name=session_name,
            startup_range=startup_range,
            autosave_raw=_required_bool(value, "autosave_raw"),
            autosave_echogram=_required_bool(value, "autosave_echogram"),
            autosave_minutes=autosave_minutes,
            save_frames=_required_bool(value, "save_frames"),
            auto_configure_interface=_required_bool(
                value, "auto_configure_interface"
            ),
            debug=_required_bool(value, "debug"),
        )


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    text = value.strip()
    if "\x00" in text or len(text) > MAX_TEXT_FIELD:
        raise ValueError(f"Invalid {name}")
    return text or None


def _required_bool(value: dict[str, Any], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ValueError(f"{key} must be true or false")
    return result


def build_emulator_command(
    config: SessionConfig,
    viewer_port: int,
    executable: str = sys.executable,
    network_state: Path | None = None,
) -> list[str]:
    command = [
        *(bundle_executable("SonarHeadUnitEngine") if FROZEN else
          [executable, str(EMULATOR_SCRIPT)]),
        "--web-viewer",
        "--startup-range",
        config.startup_range,
    ]
    if config.interface:
        command.extend(("--iface", config.interface))
    if config.auto_configure_interface:
        command.append("--auto-configure-interface")
    if config.debug:
        command.append("--debug")
    if network_state is not None:
        command.extend(("--network-state", str(network_state)))
    command.extend((
        "--viewer-args",
        "--web-port", str(viewer_port),
        "--no-save-prompt",
        "--paths-confirmed",
        "--save-root", str(config.save_root),
        "--autosave-minutes", f"{config.autosave_minutes:g}",
    ))
    if config.session_name:
        command.extend(("--session-name", config.session_name))
    if network_state is not None:
        command.extend(("--settings-file", str(network_state.parent / "viewer-settings.json")))
    if config.autosave_raw:
        command.append("--autosave-raw")
    if config.autosave_echogram:
        command.append("--autosave-echogram")
    if not config.save_frames:
        command.append("--no-save-frames")
    return command


def available_loopback_port(preferred: int) -> int:
    """Return the preferred port, or an OS-selected free loopback port."""
    for port in (preferred, 0):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return probe.getsockname()[1]
        except OSError:
            if port != 0:
                continue
            raise
        finally:
            probe.close()
    raise RuntimeError("could not find an available viewer port")


class ControllerState:
    def __init__(self, viewer_port: int, network_state: Path | None = None) -> None:
        self.viewer_port = viewer_port
        self.network_state = network_state
        self._lock = threading.Lock()
        self._closed = False
        self._process: subprocess.Popen[str] | None = None
        self._exit_code: int | None = None
        self._message = "Ready to start"
        self._viewer_ready = False
        self._logs: deque[str] = deque(maxlen=300)
        self._interface: str | None = None
        self._stream_packets: int | None = None
        self._stream_bytes_per_second: int | None = None
        self._image_packets: int | None = None
        self._stream_sample_at: float | None = None
        self._action_feedback = "No actions yet"
        self._link_state = "unknown"
        self._link_checked_at = 0.0

    def start(self, config: SessionConfig) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("OpenScope is shutting down")
            self._refresh_locked()
            if self._process is not None:
                raise RuntimeError("An OpenScope session is already running")
            # A viewer from an interrupted older session can outlive its
            # engine and retain the previous port.  Pick a free port for
            # every session rather than making that orphan prevent restart.
            self.viewer_port = available_loopback_port(self.viewer_port)
            command = build_emulator_command(config, self.viewer_port, network_state=self.network_state)
            environment = dict(os.environ)
            environment["PYTHONUNBUFFERED"] = "1"
            self._exit_code = None
            self._message = "Starting OpenScope…"
            self._viewer_ready = False
            self._logs.clear()
            self._interface = config.interface
            self._stream_packets = None
            self._stream_bytes_per_second = None
            self._image_packets = None
            self._stream_sample_at = None
            self._action_feedback = "No actions yet"
            self._link_state = "unknown"
            self._link_checked_at = 0.0
            self._process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                # The engine launches the viewer.  Its own signal handler
                # normally closes that child, but a separate session lets us
                # also stop the complete process tree if shutdown is abrupt.
                start_new_session=(os.name != "nt"),
            )
            process = self._process
        threading.Thread(
            target=self._capture_output,
            args=(process,),
            name="controller-output",
            daemon=True,
        ).start()

    def restore_network(self) -> None:
        with self._lock:
            self._refresh_locked()
            if self._process is not None:
                raise RuntimeError("Stop the session before restoring network settings")
            if platform.system() not in {"Darwin", "Linux"}:
                raise RuntimeError("Network recovery currently supports macOS and Linux")
            if self.network_state is None or not self.network_state.exists():
                self._message = "No network changes need restoring"
                return
            from network_recovery import create_network_recovery
            create_network_recovery(self.network_state).restore()
            self._message = "Network settings restored"

    def stop(self) -> None:
        with self._lock:
            self._refresh_locked()
            process = self._process
            if process is None:
                raise RuntimeError("No OpenScope session is running")
            self._message = "Stopping OpenScope…"
            self._terminate_session(process)
        threading.Thread(
            target=self._finish_stop,
            args=(process,),
            name="controller-stop",
            daemon=True,
        ).start()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_locked()
            self._refresh_link_state_locked()
            process = self._process
            stream_fresh = (
                self._stream_sample_at is not None
                and time.monotonic() - self._stream_sample_at
                <= STREAM_SAMPLE_STALE_SECONDS
            )
            packets_per_second = self._stream_packets if stream_fresh else 0
            bytes_per_second = self._stream_bytes_per_second if stream_fresh else 0
            image_packets_per_second = self._image_packets if stream_fresh else 0
            stream_state = "Waiting for packet diagnostics"
            guidance = ""
            if self._link_state == "inactive":
                stream_state = "Ethernet link inactive"
                guidance = (
                    "Reconnect the Ethernet cable or adapter and power-cycle "
                    "the GLS10."
                )
            elif not stream_fresh:
                stream_state = "No recent sonar packets"
                guidance = "Check the Ethernet connection and GLS10 power."
            elif image_packets_per_second == 0:
                stream_state = "GLS10 connected, but no sonar images"
                guidance = (
                    "If this persists, disable and re-enable sonar or "
                    "power-cycle the GLS10."
                )
            elif image_packets_per_second > 0:
                stream_state = "Sonar image packets flowing"
            return {
                "running": process is not None,
                "pid": None if process is None else process.pid,
                "exit_code": self._exit_code,
                "network_recovery_pending": bool(
                    self.network_state is not None and self.network_state.exists()
                ),
                "message": self._message,
                "error": (
                    next(
                        (line for line in reversed(self._logs)
                         if "ERROR" in line or "Error" in line),
                        None,
                    )
                    if self._exit_code not in (None, 0) else None
                ),
                "viewer_ready": self._viewer_ready,
                "viewer_url": f"http://127.0.0.1:{self.viewer_port}/",
                "logs": list(self._logs),
                "diagnostics": {
                    "interface": self._interface,
                    "link_state": self._link_state,
                    "packets_per_second": packets_per_second,
                    "bytes_per_second": bytes_per_second,
                    "image_packets_per_second": image_packets_per_second,
                    "stream_state": stream_state,
                    "guidance": guidance,
                    "action_feedback": self._action_feedback,
                },
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
            if process is not None and process.poll() is None:
                self._terminate_session(process)
        if process is not None:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._kill_session(process)
                process.wait()

    def _capture_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is not None:
            for line in process.stdout:
                with self._lock:
                    stripped = line.rstrip()
                    self._logs.append(stripped)
                    # Preserve engine diagnostics after the browser closes.
                    print(stripped, flush=True)
                    interface_match = INTERFACE_LOG_RE.search(stripped)
                    if interface_match:
                        self._interface = interface_match.group(1)
                        self._link_checked_at = 0.0
                    stream_match = STREAM_RATE_LOG_RE.search(stripped)
                    if stream_match:
                        self._stream_packets = int(stream_match.group(1))
                        self._stream_bytes_per_second = int(
                            stream_match.group(2)
                        )
                        self._image_packets = int(stream_match.group(3))
                        self._stream_sample_at = time.monotonic()
                    action_feedback = action_feedback_from_log(stripped)
                    if action_feedback is not None:
                        self._action_feedback = action_feedback
                    # The viewer is a sibling packaged executable, so its
                    # stdout is not part of this engine pipe. Its readiness is
                    # reported by the engine after the viewer has opened its
                    # browser server and capture provider.
                    if ("Browser viewer:" in line or
                            "Sonar viewer is ready; starting GLS10 negotiation" in line):
                        self._viewer_ready = True
                    if ("Head-unit emulator running" in line or
                            "Sonar viewer is ready; starting GLS10 negotiation" in line):
                        self._message = "OpenScope running"
        process.wait()
        with self._lock:
            self._refresh_locked()

    def _finish_stop(self, process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._kill_session(process)
            process.wait()
        with self._lock:
            self._refresh_locked()

    @staticmethod
    def _terminate_session(process: subprocess.Popen[str]) -> None:
        """Ask the engine and any integrated viewer child to stop together."""
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                return
            except ProcessLookupError:
                return
            except OSError:
                # Fall back to the direct child if process-group signalling is
                # unavailable (for example in a constrained test/runtime).
                pass
        process.terminate()

    @staticmethod
    def _kill_session(process: subprocess.Popen[str]) -> None:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        process.kill()

    def _refresh_locked(self) -> None:
        if self._process is None:
            return
        result = self._process.poll()
        if result is None:
            return
        self._exit_code = result
        self._process = None
        self._viewer_ready = False
        self._message = (
            "OpenScope stopped"
            if result == 0
            else f"OpenScope exited with status {result}"
        )

    def _refresh_link_state_locked(self) -> None:
        now = time.monotonic()
        if now - self._link_checked_at < 0.5:
            return
        self._link_state = interface_link_state(self._interface)
        self._link_checked_at = now


def available_interfaces() -> list[str]:
    return emulator.detect_ethernet_interfaces(prefer_configured=False)


def _is_loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _loopback_bind_address(host: str) -> str:
    """Resolve a bind name and reject aliases that reach non-loopback clients."""
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(
                host, None, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve controller host {host!r}") from exc
    if not addresses or not all(_is_loopback(address) for address in addresses):
        raise ValueError(
            "Unauthenticated controller must bind only to a loopback address"
        )
    return host


def _handler_for(
    state: ControllerState,
    *,
    allowed_save_root: Path | None = None,
    auth_token: str | None = None,
    shutdown: Callable[[], None] | None = None,
):
    from preferences import Preferences
    from build_info import build_info
    preferences = Preferences(allowed_save_root.parent / "controller-settings.json" if allowed_save_root else None)
    class Handler(BaseHTTPRequestHandler):
        def _request_is_local(self) -> bool:
            return _is_loopback(self.client_address[0])

        def _host_is_allowed(self) -> bool:
            host = self.headers.get("Host", "")
            try:
                parsed = urlparse(f"//{host}")
                hostname = parsed.hostname
                port = parsed.port
            except ValueError:
                return False
            return bool(
                hostname
                and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
                and port in {None, self.server.server_port}
            )

        def _authorize_request(self) -> bool:
            if not self._request_is_local() or not self._host_is_allowed():
                self._json(
                    {"accepted": False, "error": "Request host is not allowed"},
                    HTTPStatus.FORBIDDEN,
                )
                return False
            if auth_token is None or self._has_auth_token():
                return True
            self._json(
                {"accepted": False, "error": "Authentication is required"},
                HTTPStatus.UNAUTHORIZED,
            )
            return False

        def _has_auth_token(self) -> bool:
            authorization = self.headers.get("Authorization", "")
            candidate = (
                authorization[7:]
                if authorization.startswith("Bearer ")
                else None
            )
            if candidate is None:
                cookies = SimpleCookie()
                try:
                    cookies.load(self.headers.get("Cookie", ""))
                except Exception:
                    return False
                morsel = cookies.get("sonar_controller_token")
                candidate = None if morsel is None else morsel.value
            return bool(candidate) and secrets.compare_digest(
                candidate, auth_token
            )

        def _bootstrap_session(self) -> bool:
            """Trade a one-use URL token for an HTTP-only session cookie."""
            if auth_token is None or urlparse(self.path).path != "/":
                return False
            if not self._request_is_local() or not self._host_is_allowed():
                return False
            values = parse_qs(urlparse(self.path).query)
            candidate = values.get("token", [None])[0]
            if not candidate or not secrets.compare_digest(candidate, auth_token):
                return False
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                "sonar_controller_token=" + auth_token
                + "; HttpOnly; SameSite=Strict; Path=/",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        def _send(
            self,
            body: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), usb=()",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(
                json.dumps(value).encode(),
                "application/json",
                status,
            )

        def do_GET(self) -> None:
            if self._bootstrap_session():
                return
            if not self._authorize_request():
                return
            path = urlparse(self.path).path
            if path == "/api/status":
                self._json(state.status())
                return
            if path == "/api/interfaces":
                try:
                    interfaces = available_interfaces()
                    self._json({"interfaces": interfaces})
                except Exception as exc:
                    self._json(
                        {"interfaces": [], "error": str(exc)},
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                return
            if path == "/api/defaults":
                save_root = allowed_save_root or DEFAULT_RECORDINGS_ROOT
                self._json({"save_root": str(save_root), "preferences": preferences.read(), "build": build_info()})
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

        def do_POST(self) -> None:
            if not self._authorize_request():
                return
            path = urlparse(self.path).path
            if not request_is_same_origin(
                self.headers.get("Origin"),
                self.headers.get("Host"),
            ):
                self._json(
                    {
                        "accepted": False,
                        "error": "Cross-origin requests are not allowed",
                    },
                    HTTPStatus.FORBIDDEN,
                )
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Transfer-Encoding is not supported")
                content_type = self.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("Content-Type must be application/json")
                length_text = self.headers.get("Content-Length")
                if length_text is None:
                    raise ValueError("Content-Length is required")
                length = int(length_text)
                if length < 0 or length > MAX_REQUEST_BODY:
                    raise ValueError(
                        f"Request body must be at most {MAX_REQUEST_BODY} bytes"
                    )
                body = json.loads(self.rfile.read(length) or b"{}")
                if path == "/api/preferences":
                    if not isinstance(body, dict) or set(body) - {"theme", "interface", "startup_range", "auto_configure_interface", "debug"}:
                        raise ValueError("Unsupported preferences")
                    for key, value in body.items():
                        if key in {"debug", "auto_configure_interface"}:
                            if type(value) is not bool:
                                raise ValueError("Expected a boolean preference")
                        elif not isinstance(value, str) or len(value) > 128:
                            raise ValueError("Invalid text preference")
                    if "theme" in body and body["theme"] not in {"dark", "light"}:
                        raise ValueError("Invalid theme")
                    preferences.update(body)
                    self._json({"accepted": True})
                    return
                if path == "/api/session/start":
                    state.start(SessionConfig.from_json(
                        body, allowed_save_root=allowed_save_root
                    ))
                    self._json({"accepted": True}, HTTPStatus.ACCEPTED)
                    return
                if path == "/api/session/stop":
                    state.stop()
                    self._json({"accepted": True}, HTTPStatus.ACCEPTED)
                    return
                if path == "/api/network/restore":
                    state.restore_network()
                    self._json({"accepted": True})
                    return
                if path == "/api/shutdown" and shutdown is not None:
                    self._json({"accepted": True}, HTTPStatus.ACCEPTED)
                    threading.Thread(
                        target=shutdown,
                        name="controller-shutdown",
                        daemon=True,
                    ).start()
                    return
            except (json.JSONDecodeError, ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                self._json(
                    {"accepted": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send(b"Not found", "text/plain", HTTPStatus.NOT_FOUND)

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    return Handler


def _windows_process_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    # SYNCHRONIZE permits a nonblocking wait without terminating the owner.
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
            return False
        if error == 5:  # ERROR_ACCESS_DENIED: owner exists but is inaccessible.
            return True
        raise ctypes.WinError(error)
    try:
        status = kernel32.WaitForSingleObject(handle, 0)
        if status == 0:  # WAIT_OBJECT_0: process has exited.
            return False
        if status == 258:  # WAIT_TIMEOUT: process is still running.
            return True
        raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def desktop_owner_alive(lifetime_file: Path | None, parent_pid: int | None) -> bool:
    if lifetime_file is not None and not lifetime_file.is_file():
        return False
    if parent_pid is not None:
        if sys.platform == "win32":
            return _windows_process_alive(parent_pid)
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # The process exists, but belongs to another user.
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s Python {platform.python_version()}",
    )
    parser.add_argument("--repair-recording-permissions", action="store_true",
                        help="Repair ownership of existing recordings and exit (explicit legacy migration)")
    parser.add_argument("--log-file", type=Path, help="Write bounded rotating diagnostic logs to this file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--viewer-port", type=int, default=8766)
    parser.add_argument("--desktop-lifetime-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--desktop-parent-pid", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--auth-token",
        help="authentication token (a random token is generated when omitted)",
    )
    parser.add_argument(
        "--open-browser", action="store_true",
        help="open the controller URL after the server is ready",
    )
    parser.add_argument(
        "--recordings-root",
        type=Path,
        default=DEFAULT_RECORDINGS_ROOT,
        help="only permit output paths inside this directory",
    )
    args = parser.parse_args()
    if args.log_file is not None:
        from diagnostics import DiagnosticStream
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = DiagnosticStream(args.log_file)
        print(f"OpenScope controller launch: Python {platform.python_version()}, PID {os.getpid()}", flush=True)
    if args.desktop_parent_pid is not None and args.desktop_parent_pid <= 1:
        parser.error("Invalid desktop parent PID")
    if not desktop_owner_alive(args.desktop_lifetime_file, args.desktop_parent_pid):
        return 0
    if args.port == args.viewer_port and args.port != 0:
        parser.error("--port and --viewer-port must be different")
    try:
        bind_host = _loopback_bind_address(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    recordings_root = args.recordings_root.expanduser().resolve()
    recordings_root.mkdir(parents=True, exist_ok=True)
    make_user_editable(recordings_root)
    if args.repair_recording_permissions:
        from live_viewer.permissions import make_output_tree_user_editable
        make_output_tree_user_editable(recordings_root)
        return 0

    viewer_port = available_loopback_port(args.viewer_port)
    state = ControllerState(viewer_port, recordings_root.parent / "network-recovery.json")
    auth_token = args.auth_token or secrets.token_urlsafe(32)
    server: ThreadingHTTPServer
    handler = _handler_for(
        state,
        allowed_save_root=recordings_root,
        auth_token=auth_token,
        shutdown=lambda: server.shutdown(),
    )
    port = args.port
    while True:
        if port != 0 and port == viewer_port:
            port += 1
        try:
            server = ThreadingHTTPServer((bind_host, port), handler)
            break
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or port >= 65535:
                raise
            print(f"Port {port} is already in use; trying {port + 1}.")
            port += 1
    port = server.server_port
    stop_requested = threading.Event()
    watch_finished = threading.Event()
    serve_started = threading.Event()
    previous_signals = {
        sig: signal.signal(sig, lambda *_: stop_requested.set())
        for sig in (signal.SIGTERM, signal.SIGINT)
    }

    def watch_desktop() -> None:
        while not watch_finished.is_set():
            if stop_requested.wait(0.2) or not desktop_owner_alive(
                args.desktop_lifetime_file, args.desktop_parent_pid
            ):
                # Close the process group even if the desktop's HTTP shutdown
                # request failed or the app disappeared before readiness.
                state.close()
                serve_started.wait()
                server.shutdown()
                return

    watcher = threading.Thread(target=watch_desktop, name="desktop-lifetime", daemon=True)
    watcher.start()
    query = urlencode({"token": auth_token})
    controller_url = f"http://{bind_host}:{port}/?{query}"
    print(
        "SONAR_CONTROLLER_READY "
        + json.dumps({"url": controller_url, "port": port}),
        flush=True,
    )
    print(f"Sonar browser controller: {controller_url}")
    print(f"Initial recording folder: {recordings_root}")
    if args.open_browser:
        webbrowser.open(controller_url)
    try:
        serve_started.set()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping controller.")
    finally:
        watch_finished.set()
        server.server_close()
        state.close()
        watcher.join(timeout=16)
        for sig, previous in previous_signals.items():
            signal.signal(sig, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
