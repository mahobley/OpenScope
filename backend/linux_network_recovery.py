"""Journal and recover OpenScope's changes to a Linux sonar adapter."""

import json
import logging
import os
from pathlib import Path
import re

from network_recovery import NetworkRecovery, GLS_IP

LOG = logging.getLogger("network-recovery")
MAC_PATTERN = r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"


def process_start(pid):
    """Use Linux's process start time to avoid confusing a reused PID with its owner."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[19]
    except (OSError, IndexError):
        return None


class LinuxNetworkRecovery(NetworkRecovery):
    def _addresses(self, interface=None):
        # An unconfigured dedicated adapter may have no IPv4 address yet.
        # `ip -4 address show` omits it entirely, even though it exists.
        # Enumerate all adapters; snapshot/restore code filters IPv4 below.
        command = ["ip", "-j", "address", "show"]
        if interface is not None:
            command.extend(("dev", interface))
        return json.loads(self._run(command))

    def _neighbor(self, interface):
        rows = json.loads(self._run(["ip", "-j", "-4", "neigh", "show", GLS_IP, "dev", interface]))
        return next((row for row in rows if row.get("dst") == GLS_IP), None)

    def begin(self, interface, head_ip="172.16.6.0"):
        with self.lock:
            if self.path.exists():
                self.restore()
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", interface):
                raise ValueError("Invalid network interface")
            if head_ip not in {"172.16.6.0", "172.16.6.1"}:
                raise ValueError("Invalid head-unit address")
            rows = self._addresses()
            for row in rows:
                if row["ifname"] != interface and any(
                        address.get("local") == head_ip for address in row.get("addr_info", [])):
                    raise RuntimeError(
                        f"Emulated head-unit address {head_ip} is also assigned to {row['ifname']}. "
                        "Remove that conflicting address before starting a session; "
                        "OpenScope will preserve addresses it did not record adding. "
                        "Select the GLS10 adapter under Advanced → Connection and logging."
                    )
            selected = next((row for row in rows if row["ifname"] == interface), None)
            if selected is None:
                raise RuntimeError(f"Sonar adapter {interface} is unavailable")
            original = {address["local"]: address["prefixlen"]
                        for address in selected.get("addr_info", []) if address.get("family") == "inet"}
            self.data = {"platform": "Linux", "interface": interface, "head_ip": head_ip,
                         "original": original, "head_added": False, "gls_removed": False,
                         "neighbor": None, "previous_neighbor": None,
                         "owner": {"pid": os.getpid(), "start": process_start(os.getpid())}}
            self._save()

    def validate_route(self):
        """Reject ambiguous routing before any sonar negotiation is started."""
        routes = json.loads(self._run(["ip", "-j", "-4", "route", "get", GLS_IP,
                                       "from", self.data["head_ip"]]))
        actual = routes[0].get("dev") if routes else None
        selected = self.data["interface"]
        if actual != selected:
            raise RuntimeError(
                f"Linux routes GLS10 traffic through {actual or 'no adapter'}, "
                f"but the selected sonar adapter is {selected}. Resolve the conflicting "
                "sonar address or route, then select the GLS10 adapter under "
                "Advanced → Connection and logging."
            )

    def prepare_neighbor(self, interface, mac):
        with self.lock:
            if self.data is None or self.data["interface"] != interface:
                raise RuntimeError("Network recovery was not initialized")
            if not re.fullmatch(MAC_PATTERN, mac):
                raise ValueError("Invalid sonar neighbor")
            if self.data["neighbor"] is None:
                previous = self._neighbor(interface)
                if previous and previous.get("lladdr") and any(
                        state in previous.get("state", []) for state in ("PERMANENT", "NOARP")):
                    self.data["previous_neighbor"] = {
                        "lladdr": previous["lladdr"],
                        "state": "permanent" if "PERMANENT" in previous["state"] else "noarp",
                    }
            self.data["neighbor"] = mac.lower()
            self._save()

    def restore(self):
        with self.lock:
            if not self.path.exists():
                return
            data = json.loads(self.path.read_text())
            if (not isinstance(data, dict) or data.get("platform") != "Linux"
                    or not isinstance(data.get("interface"), str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", data["interface"])
                    or data.get("head_ip") not in {"172.16.6.0", "172.16.6.1"}
                    or not isinstance(data.get("original"), dict)
                    or any(type(value) is not int or not 0 <= value <= 32
                           for value in data["original"].values())
                    or any(type(data.get(flag)) is not bool for flag in ("head_added", "gls_removed"))):
                raise ValueError("Invalid Linux network recovery journal")
            owner = data.get("owner")
            if owner is not None:
                if (not isinstance(owner, dict) or type(owner.get("pid")) is not int or owner["pid"] <= 1
                        or (owner.get("start") is not None and
                            (not isinstance(owner["start"], str) or not owner["start"].isdigit()))):
                    raise ValueError("Invalid recovery owner")
                if (owner["pid"] != os.getpid() and owner.get("start") is not None
                        and process_start(owner["pid"]) == owner["start"]):
                    raise RuntimeError("Stop the other OpenScope session before restoring its network settings")
            neighbor, previous = data.get("neighbor"), data.get("previous_neighbor")
            if neighbor is not None and (not isinstance(neighbor, str) or not re.fullmatch(MAC_PATTERN, neighbor)):
                raise ValueError("Invalid saved neighbor")
            if previous is not None and (
                    not isinstance(previous, dict) or not isinstance(previous.get("lladdr"), str)
                    or not re.fullmatch(MAC_PATTERN, previous["lladdr"])
                    or previous.get("state") not in {"permanent", "noarp"}):
                raise ValueError("Invalid saved neighbor")
            interface, head_ip = data["interface"], data["head_ip"]
            rows = self._addresses(interface)
            if not rows:
                raise RuntimeError(f"Reconnect sonar adapter {interface} to restore network settings")
            addresses = {address["local"]: address["prefixlen"]
                         for address in rows[0].get("addr_info", []) if address.get("family") == "inet"}
            if neighbor is not None:
                current = self._neighbor(interface)
                if (current and str(current.get("lladdr", "")).lower() == neighbor.lower()
                        and "PERMANENT" in current.get("state", [])):
                    if previous:
                        self._run(["ip", "neigh", "replace", GLS_IP, "lladdr", previous["lladdr"],
                                   "nud", previous["state"], "dev", interface])
                    else:
                        self._run(["ip", "neigh", "delete", GLS_IP, "dev", interface])
            if data["head_added"] and head_ip not in data["original"] and addresses.get(head_ip) == 16:
                self._run(["ip", "address", "delete", f"{head_ip}/16", "dev", interface])
            if data["gls_removed"] and GLS_IP not in addresses and GLS_IP in data["original"]:
                self._run(["ip", "address", "add", f"{GLS_IP}/{data['original'][GLS_IP]}", "dev", interface])
            self.path.unlink()
            self.data = None
            LOG.info("Restored OpenScope network changes on %s", interface)
