"""Recover app-owned changes on the macOS dedicated sonar adapter."""

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import tempfile
import platform

HEAD_IP = "172.16.6.0"
GLS_IP = "172.16.3.0"
LOG = logging.getLogger("network-recovery")


def create_network_recovery(path, system=None):
    system = system or platform.system()
    if system == "Linux":
        from linux_network_recovery import LinuxNetworkRecovery
        return LinuxNetworkRecovery(path)
    if system == "Darwin":
        return NetworkRecovery(path)
    raise RuntimeError("Network recovery currently supports macOS and Linux")


class NetworkRecovery:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = None

    def _run(self, command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or "Network command failed")
        return result.stdout

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".network-recovery-", delete=False) as output:
                temporary = Path(output.name)
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), 0o600)
                else:
                    os.chmod(temporary, 0o600)
                json.dump(self.data, output)
                output.flush()
                os.fsync(output.fileno())
            # Close before replacement so the journal is portable to Windows.
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def begin(self, interface, head_ip=HEAD_IP):
        with self.lock:
            if self.path.exists():
                self.restore()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", interface):
                raise ValueError("Invalid network interface")
            if head_ip not in {"172.16.6.0", "172.16.6.1"}:
                raise ValueError("Invalid head-unit address")
            snapshot = self._run(["ifconfig", interface])
            original = dict(re.findall(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+|[0-9.]+)", snapshot))
            self.data = {"interface": interface, "head_ip": head_ip, "original": original,
                         "head_added": False, "gls_removed": False,
                         "neighbor": None, "previous_neighbor": None, "route_added": False}
            self._save()

    def mark_address(self, action):
        with self.lock:
            self.data[action] = True
            self._save()

    def prepare_neighbor(self, interface, mac):
        """Preserve static neighbors; dynamic cache entries can be relearned."""
        with self.lock:
            if self.data is None or self.data["interface"] != interface:
                raise RuntimeError("Network recovery was not initialized")
            result = subprocess.run(["arp", "-n", "-i", interface, GLS_IP],
                                    capture_output=True, text=True, timeout=5)
            match = re.search(r"\bat ([0-9a-fA-F:]+)\b", result.stdout)
            if match and "permanent" in result.stdout and self.data["neighbor"] is None:
                self.data["previous_neighbor"] = match.group(1)
            self.data["neighbor"] = mac
            self._save()

    def prepare_route(self, interface):
        with self.lock:
            result = subprocess.run(["route", "-n", "get", "-net", "172.16.0.0/16", "-ifscope", interface],
                                    capture_output=True, text=True, timeout=5)
            self.data["route_added"] = not (
                result.returncode == 0
                and re.search(r"destination:\s*172\.16\.0\.0\b", result.stdout)
                and re.search(r"mask:\s*255\.255\.0\.0\b", result.stdout)
                and re.search(r"interface:\s*" + re.escape(interface) + r"\s", result.stdout)
            )
            self._save()

    def cancel_route(self):
        with self.lock:
            self.data["route_added"] = False
            self._save()

    def restore(self):
        with self.lock:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text())
            if (not isinstance(data, dict) or data.get("platform", "Darwin") != "Darwin"
                    or not isinstance(data.get("original"), dict)
                    or not isinstance(data.get("interface"), str)):
                raise ValueError("Invalid network recovery journal")
            for flag in ("head_added", "gls_removed", "route_added"):
                if type(data.get(flag)) is not bool:
                    raise ValueError("Invalid network recovery journal")
            for key in ("neighbor", "previous_neighbor"):
                value = data.get(key)
                if value is not None and (not isinstance(value, str) or not re.fullmatch(
                        r"(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}", value)):
                    raise ValueError("Invalid saved neighbor")
            interface = data["interface"]
            if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", interface):
                raise ValueError("Invalid recovery interface")
            current = self._run(["ifconfig", interface])
            current_addresses = dict(re.findall(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+|[0-9.]+)", current))
            addresses = set(current_addresses)
            head_ip = data.get("head_ip")
            if head_ip is None:
                candidates = {"172.16.6.0", "172.16.6.1"} & (addresses - set(data["original"]))
                if data["head_added"] and len(candidates) > 1:
                    raise ValueError("Legacy recovery journal has ambiguous head-unit addresses")
                head_ip = next(iter(candidates), HEAD_IP)
            if head_ip not in {"172.16.6.0", "172.16.6.1"}:
                raise ValueError("Invalid saved head-unit address")
            # Only fixed sonar addresses and this adapter may be changed by recovery.
            if data.get("neighbor"):
                result = subprocess.run(["arp", "-n", "-i", interface, GLS_IP],
                                        capture_output=True, text=True, timeout=5)
                match = re.search(r"\bat ([0-9a-fA-F:]+)\b", result.stdout)
                normalize = lambda value: ":".join(f"{int(part, 16):02x}" for part in value.split(":"))
                if match and normalize(match.group(1)) == normalize(data["neighbor"]):
                    previous = data.get("previous_neighbor")
                    if previous:
                        if not re.fullmatch(r"(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}", previous):
                            raise ValueError("Invalid saved neighbor")
                        self._run(["arp", "-s", GLS_IP, previous, "ifscope", interface])
                    else:
                        self._run(["arp", "-d", GLS_IP, "ifscope", interface])
            if data.get("route_added"):
                # Refuse to remove a route that now belongs to another adapter.
                result = subprocess.run(["route", "-n", "get", "-net", "172.16.0.0/16", "-ifscope", interface],
                                        capture_output=True, text=True, timeout=5)
                if (result.returncode == 0
                        and re.search(r"destination:\s*172\.16\.0\.0\b", result.stdout)
                        and re.search(r"mask:\s*255\.255\.0\.0\b", result.stdout)
                        and re.search(r"interface:\s*" + re.escape(interface) + r"\s", result.stdout)):
                    self._run(["route", "-n", "delete", "-net", "172.16.0.0/16", "-ifscope", interface])
            if (data.get("head_added") and head_ip in addresses and head_ip not in data["original"]
                    and current_addresses[head_ip].lower() in {"0xffff0000", "255.255.0.0"}):
                self._run(["ifconfig", interface, "-alias", head_ip])
            if data.get("gls_removed") and GLS_IP not in addresses and GLS_IP in data["original"]:
                mask = data["original"][GLS_IP]
                if not re.fullmatch(r"0x[0-9a-fA-F]{8}|(?:\d{1,3}\.){3}\d{1,3}", mask):
                    raise ValueError("Invalid saved subnet mask")
                self._run(["ifconfig", interface, "inet", GLS_IP, "netmask", mask, "alias"])
            self.path.unlink()
            self.data = None
            LOG.info("Restored OpenScope network changes on %s", interface)
