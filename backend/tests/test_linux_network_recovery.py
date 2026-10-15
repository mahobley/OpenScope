import copy
import json
from types import SimpleNamespace

import pytest

import head_unit_emulator as emulator
from linux_network_recovery import LinuxNetworkRecovery
from network_recovery import GLS_IP, create_network_recovery

HEAD_IP = "172.16.6.1"


class LinuxNetwork:
    def __init__(self):
        self.addresses = {"eno1": {"10.1.2.3": 24}, "eno2": {"192.168.50.2": 24}}
        self.ipv6 = {}
        self.neighbors = {}
        self.commands = []
        self.route_device = "eno2"
        self.fail_delete = False

    def run(self, command, **kwargs):
        self.commands.append(command)
        output = ""
        if (command[:4] == ["ip", "-j", "address", "show"]
                or command[:5] == ["ip", "-j", "-4", "address", "show"]):
            ipv4_only = "-4" in command
            selected = [command[-1]] if "dev" in command else list(self.addresses)
            if any(device not in self.addresses for device in selected):
                return SimpleNamespace(returncode=1, stdout="", stderr="Device unavailable")
            output = json.dumps([{"ifname": device, "addr_info": [
                {"family": "inet", "local": address, "prefixlen": prefix}
                for address, prefix in self.addresses[device].items()
            ] + ([] if ipv4_only else [
                {"family": "inet6", "local": address, "prefixlen": prefix}
                for address, prefix in self.ipv6.get(device, {}).items()
            ])} for device in selected if not ipv4_only or self.addresses[device]])
        elif command[:5] == ["ip", "-j", "-4", "neigh", "show"]:
            neighbor = self.neighbors.get(command[-1])
            output = json.dumps([neighbor] if neighbor else [])
        elif command[:5] == ["ip", "-j", "-4", "route", "get"]:
            output = json.dumps([{"dev": self.route_device}])
        elif command[:2] == ["ip", "address"]:
            device = command[-1]
            address, prefix = command[3].split("/")
            if command[2] == "add":
                self.addresses[device][address] = int(prefix)
            else:
                if self.fail_delete:
                    return SimpleNamespace(returncode=1, stdout="", stderr="Delete failed")
                self.addresses[device].pop(address, None)
        elif command[:3] == ["ip", "neigh", "replace"]:
            self.neighbors[command[-1]] = {"dst": command[3], "lladdr": command[5],
                                           "state": [command[7].upper()]}
        elif command[:3] == ["ip", "neigh", "delete"]:
            self.neighbors.pop(command[-1], None)
        else:
            raise AssertionError(f"Unexpected network command: {command}")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")


@pytest.fixture
def network(monkeypatch):
    network = LinuxNetwork()
    monkeypatch.setattr("network_recovery.subprocess.run", network.run)
    return network


def add_head(recovery, network, interface):
    recovery.mark_address("head_added")
    network.run(["ip", "address", "add", f"{HEAD_IP}/16", "dev", interface])


@pytest.mark.parametrize("restart", [False, True])
def test_linux_cleanup_preserves_other_addresses_and_neighbors(tmp_path, network, restart):
    original = copy.deepcopy(network.addresses)
    previous = {"dst": GLS_IP, "lladdr": "aa:bb:cc:dd:ee:ff", "state": ["PERMANENT"]}
    network.neighbors["eno2"] = previous
    journal = tmp_path / "network.json"
    recovery = create_network_recovery(journal, "Linux")
    recovery.begin("eno2", HEAD_IP)
    add_head(recovery, network, "eno2")
    recovery.prepare_neighbor("eno2", "04:25:e8:68:ef:58")
    network.run(["ip", "neigh", "replace", GLS_IP, "lladdr", "04:25:e8:68:ef:58",
                 "nud", "permanent", "dev", "eno2"])
    if restart:
        recovery = LinuxNetworkRecovery(journal)
    recovery.restore()
    assert network.addresses == original
    assert network.neighbors["eno2"] == previous
    assert not journal.exists()
    recovery.restore()


def test_switching_adapters_recovers_old_address_before_snapshot(tmp_path, network):
    journal = tmp_path / "network.json"
    first = LinuxNetworkRecovery(journal)
    first.begin("eno1", HEAD_IP)
    add_head(first, network, "eno1")
    second = LinuxNetworkRecovery(journal)
    second.begin("eno2", HEAD_IP)
    assert HEAD_IP not in network.addresses["eno1"]
    add_head(second, network, "eno2")
    second.restore()
    assert all(HEAD_IP not in addresses for addresses in network.addresses.values())


def test_preexisting_head_address_is_preserved(tmp_path, network):
    network.addresses["eno2"][HEAD_IP] = 16
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    recovery.restore()
    assert network.addresses["eno2"][HEAD_IP] == 16


@pytest.mark.parametrize("ipv6", [False, True])
def test_unconfigured_adapter_can_start_and_restore_to_no_ipv4(tmp_path, network, ipv6):
    network.addresses["eno2"] = {}
    if ipv6:
        network.ipv6["eno2"] = {"fe80::1": 64}
    original_ipv6 = copy.deepcopy(network.ipv6)
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    assert recovery.data["original"] == {}
    add_head(recovery, network, "eno2")
    recovery.restore()
    assert network.addresses["eno2"] == {}
    assert network.ipv6 == original_ipv6
    assert not recovery.path.exists()


def test_unowned_duplicate_address_is_rejected_without_network_changes(tmp_path, network):
    network.addresses["eno1"][HEAD_IP] = 16
    original = copy.deepcopy(network.addresses)
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    with pytest.raises(RuntimeError, match="also assigned to eno1"):
        recovery.begin("eno2", HEAD_IP)
    assert network.addresses == original
    assert not recovery.path.exists()


def test_route_conflict_is_rejected_and_owned_address_can_be_restored(tmp_path, network):
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    add_head(recovery, network, "eno2")
    network.route_device = "eno1"
    with pytest.raises(RuntimeError, match="routes GLS10 traffic through eno1"):
        recovery.validate_route()
    recovery.restore()
    assert HEAD_IP not in network.addresses["eno2"]


def test_failed_cleanup_keeps_journal_for_retry(tmp_path, network):
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    add_head(recovery, network, "eno2")
    network.fail_delete = True
    with pytest.raises(RuntimeError, match="Delete failed"):
        recovery.restore()
    assert recovery.path.exists()
    network.fail_delete = False
    LinuxNetworkRecovery(recovery.path).restore()
    assert HEAD_IP not in network.addresses["eno2"]
    assert not recovery.path.exists()


def test_disconnected_adapter_keeps_journal(tmp_path, network):
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    add_head(recovery, network, "eno2")
    saved = network.addresses.pop("eno2")
    with pytest.raises(RuntimeError, match="Device unavailable"):
        recovery.restore()
    assert recovery.path.exists()
    network.addresses["eno2"] = saved
    recovery.restore()
    assert HEAD_IP not in saved


def test_cleanup_preserves_later_user_changes(tmp_path, network):
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    add_head(recovery, network, "eno2")
    recovery.prepare_neighbor("eno2", "04:25:e8:68:ef:58")
    changed_neighbor = {"dst": GLS_IP, "lladdr": "aa:bb:cc:dd:ee:ff", "state": ["PERMANENT"]}
    network.neighbors["eno2"] = changed_neighbor
    network.addresses["eno2"][HEAD_IP] = 24
    recovery.restore()
    assert network.addresses["eno2"][HEAD_IP] == 24
    assert network.neighbors["eno2"] == changed_neighbor


def test_removed_conflicting_address_restores_original_prefix(tmp_path, network):
    network.addresses["eno2"][GLS_IP] = 24
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    recovery.mark_address("gls_removed")
    network.addresses["eno2"].pop(GLS_IP)
    recovery.restore()
    assert network.addresses["eno2"][GLS_IP] == 24


@pytest.mark.parametrize("field,value", [("interface", "eno2;unsafe"), ("head_ip", "10.1.2.3"),
                                          ("head_added", "yes"), ("platform", "Darwin")])
def test_invalid_journals_cannot_modify_network(tmp_path, network, field, value):
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    data = json.loads(recovery.path.read_text())
    data[field] = value
    recovery.path.write_text(json.dumps(data))
    network.commands.clear()
    with pytest.raises(ValueError, match="Invalid Linux"):
        recovery.restore()
    assert not network.commands


def test_engine_startup_failure_restores_linux_network(tmp_path, network, monkeypatch):
    for name in ("ACTIVE_PROTOCOL_EXPERIMENT", "MAXIMAL_PORTABLE_PROFILE", "PORTABLE_V2_PROFILE",
                 "CONSOLIDATED_TEST_PROFILE", "HEAD_IP", "GLS_MAC", "NETWORK_RECOVERY"):
        monkeypatch.setattr(emulator, name, getattr(emulator, name))
    monkeypatch.setattr(emulator.sys, "argv", ["engine", "--iface", "eno2", "--auto-configure-interface",
                                               "--network-state", str(tmp_path / "network.json")])
    monkeypatch.setattr(emulator.signal, "signal", lambda *_: None)
    monkeypatch.setattr(emulator.platform, "system", lambda: "Linux")
    monkeypatch.setattr(emulator, "interface_details", lambda _: SimpleNamespace(name="eno2"))
    monkeypatch.setattr(emulator, "interface_addresses", lambda _: set(network.addresses["eno2"]))
    monkeypatch.setattr(emulator, "get_if_hwaddr", lambda _: (_ for _ in ()).throw(OSError("Test startup failure")))
    assert emulator.main() == 2
    assert HEAD_IP not in network.addresses["eno2"]
    assert not (tmp_path / "network.json").exists()


def test_recovery_does_not_disturb_another_live_session(tmp_path, network, monkeypatch):
    recovery = LinuxNetworkRecovery(tmp_path / "network.json")
    recovery.begin("eno2", HEAD_IP)
    add_head(recovery, network, "eno2")
    data = json.loads(recovery.path.read_text())
    data["owner"] = {"pid": 999999, "start": "1234"}
    recovery.path.write_text(json.dumps(data))
    monkeypatch.setattr("linux_network_recovery.process_start", lambda _: "1234")
    network.commands.clear()
    with pytest.raises(RuntimeError, match="other OpenScope session"):
        recovery.restore()
    assert not network.commands
    assert HEAD_IP in network.addresses["eno2"]
