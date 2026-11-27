import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from outbound_proxy import docker_access as docker


class HostCommands:
    """Model host inventories. Record firewall programs and network provisioning."""
    def __init__(self):
        self.calls = []
        self.names = ["minipc-outbound-proxy"]
        self.network = {"Driver": "bridge", "Internal": True,
                        "Options": {"com.docker.network.bridge.name": "br-minipc-proxy"},
                        "IPAM": {"Config": [{"Subnet": "192.168.70.0/28", "Gateway": "192.168.70.1"}]}}
        self.routes = [{"dst": "192.168.70.0/28", "dev": "br-minipc-proxy"}]
        self.rules = []
        self.firewall_failed = False

    def __call__(self, *args, data=None, timeout=None):
        self.calls.append(args)
        if args[1:3] == ("network", "ls"):
            return "\n".join(self.names)
        if args[1:3] == ("network", "inspect"):
            return json.dumps([self.network])
        if "link" in args:
            return json.dumps([{"operstate": "UP"}])
        if "route" in args:
            return json.dumps(self.routes)
        if "addr" in args:
            return json.dumps([{"ifname": "br-minipc-proxy", "addr_info":
                                [{"local": "192.168.70.1", "prefixlen": 28}]}])
        if args[1:] == ("list", "tables"):
            return ""
        if data:
            if self.firewall_failed:
                raise RuntimeError("nft failed")
            self.rules.append(data)
        return ""


def test_off_without_previous_rules_needs_no_tools(tmp_path):
    host = HostCommands()
    docker.Access(tmp_path, host).withdraw()
    assert host.calls == []


def test_conflicts_refused_before_firewall(tmp_path):
    host = HostCommands()
    host.network["Internal"] = False
    with pytest.raises(RuntimeError):
        docker.Access(tmp_path, host).prepare(18888)
    assert not host.rules
    host.network["Internal"] = True
    host.routes.append({"dst": "192.168.70.0/24", "dev": "lan0"})
    with pytest.raises(RuntimeError):
        docker.Access(tmp_path, host).prepare(18888)
    assert not host.rules


def test_port_replacement_and_removal_preserve_network(tmp_path):
    host = HostCommands()
    policy = docker.Access(tmp_path, host)
    policy.prepare(18888)
    policy.prepare(19000)
    assert 'iifname "br-minipc-proxy"' in host.rules[-1]
    assert "192.168.70.0/28" in host.rules[-1]
    assert "19000" in host.rules[-1] and "18888" not in host.rules[-1]
    assert "drop" in host.rules[-1]
    policy.withdraw()
    assert host.rules[-1].endswith("delete table inet outbound_proxy\n")
    assert not any("rm" in args for args in host.calls)


def test_failed_firewall_can_be_withdrawn_on_rollback(tmp_path):
    host = HostCommands()
    host.firewall_failed = True
    policy = docker.Access(tmp_path, host)
    with pytest.raises(RuntimeError):
        policy.prepare(18888)
    host.firewall_failed = False
    policy.withdraw()
    assert "delete table" in host.rules[-1]


def test_inventory_reads_members_without_provisioning(tmp_path):
    host = HostCommands()
    host.network["Containers"] = {
        "id2": {"Name": "worker", "IPv4Address": "192.168.70.3/28", "EndpointID": "private"},
        "id1": {"Name": "api", "IPv4Address": "192.168.70.2/28"},
    }
    inventory = docker.Access(tmp_path, host).status()
    assert inventory == {"name": docker.NETWORK, "status": "present", "interface": docker.BRIDGE,
                         "link": "UP", "subnet": docker.SUBNET, "gateway": docker.GATEWAY,
                         "members": [{"name": "api", "ip": "192.168.70.2"},
                                     {"name": "worker", "ip": "192.168.70.3"}]}
    assert all(command[1:3] in (("network", "ls"), ("network", "inspect"), ("-j", "link"))
               for command in host.calls)
    assert not list(tmp_path.iterdir())


def test_inventory_distinguishes_missing_empty_and_unavailable(tmp_path):
    host = HostCommands()
    host.network["Containers"] = {}
    access = docker.Access(tmp_path, host)
    assert access.status()["members"] == []
    host.names = []
    assert access.status() == {"name": docker.NETWORK, "status": "missing"}

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args, 2)

    assert docker.Access(tmp_path, timeout).status() == {"name": docker.NETWORK, "status": "unavailable"}
