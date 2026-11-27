"""Manage the dedicated internal Docker bridge and the owned nftables table."""

import ipaddress
import json
import os
import subprocess

NETWORK = "minipc-outbound-proxy"
BRIDGE = "br-minipc-proxy"
GATEWAY = "192.168.70.1"
SUBNET = "192.168.70.0/28"
TABLE = "outbound_proxy"


def sync_dir(directory):
    fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def dedicated_network(net):
    configs = net["IPAM"]["Config"] or []
    return (net["Driver"] == "bridge" and net["Internal"] is True
            and (net["Options"] or {}).get("com.docker.network.bridge.name") == BRIDGE
            and len(configs) == 1 and configs[0].get("Subnet") == SUBNET
            and configs[0].get("Gateway") == GATEWAY)


class Access:
    def __init__(self, root, run):
        self.marker = root / "owns-nft-table"
        self.run = run

    def status(self):
        """Read live membership without provisioning the network or touching forwarding."""
        try:
            names = self.run("docker", "network", "ls", "--filter", f"name={NETWORK}",
                             "--format", "{{.Name}}", timeout=2).splitlines()
            if NETWORK not in names:
                return {"name": NETWORK, "status": "missing"}
            net, = json.loads(self.run("docker", "network", "inspect", NETWORK, timeout=2))
            if not dedicated_network(net):
                return {"name": NETWORK, "status": "conflict"}
            members = sorted(({"name": item["Name"], "ip": item["IPv4Address"].partition("/")[0]}
                              for item in (net["Containers"] or {}).values()), key=lambda row: row["name"])
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, AttributeError):
            return {"name": NETWORK, "status": "unavailable"}

        try:
            link, = json.loads(self.run("ip", "-j", "link", "show", "dev", BRIDGE, timeout=2))
            link_state = link["operstate"]
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            link_state = "Unknown"
        return {"name": NETWORK, "status": "present", "interface": BRIDGE, "link": link_state,
                "subnet": SUBNET, "gateway": GATEWAY, "members": members}

    def network(self):
        names = self.run("docker", "network", "ls", "--format", "{{.Name}}").splitlines()
        exists = NETWORK in names
        subnet = ipaddress.ip_network(SUBNET)
        routes = json.loads(self.run("ip", "-j", "-4", "route", "show", "table", "all"))
        for route in routes:
            dest = route.get("dst", "default")
            if dest == "default" or (exists and route.get("dev") == BRIDGE):
                continue
            if ipaddress.ip_network(dest, strict=False).overlaps(subnet):
                raise RuntimeError("Dedicated Docker subnet conflicts with a host route")
        if not exists:
            links = json.loads(self.run("ip", "-j", "link", "show"))
            if any(link["ifname"] == BRIDGE for link in links):
                raise RuntimeError("Dedicated Docker bridge interface already exists")
            self.run("docker", "network", "create", "--driver", "bridge", "--internal",
                     "--subnet", SUBNET, "--gateway", GATEWAY,
                     "--opt", f"com.docker.network.bridge.name={BRIDGE}", NETWORK)
        net, = json.loads(self.run("docker", "network", "inspect", NETWORK))
        if not dedicated_network(net):
            raise RuntimeError("Existing Docker network conflicts with the dedicated bridge")
        addr, = json.loads(self.run("ip", "-j", "-4", "addr", "show", "dev", BRIDGE))
        if not any(item["local"] == GATEWAY and item["prefixlen"] == subnet.prefixlen
                   for item in addr["addr_info"]):
            raise RuntimeError("Dedicated Docker bridge has the wrong gateway")

    def prepare(self, port):
        self.network()
        if not self.marker.exists():
            if f"table inet {TABLE}" in self.run("nft", "list", "tables").splitlines():
                raise RuntimeError("Firewall table already exists outside this installation")
            self.marker.touch(mode=0o600, exist_ok=False)
            sync_dir(self.marker.parent)
        # nft applies this entire program as one transaction. The marker survives a crash.
        self.run("nft", "-f", "-", data=f"""table inet {TABLE}
delete table inet {TABLE}
table inet {TABLE} {{
    chain input {{
        type filter hook input priority -10; policy accept;
        ip daddr {GATEWAY} tcp dport {port} iifname "{BRIDGE}" ip saddr {SUBNET} return
        ip daddr {GATEWAY} tcp dport {port} drop
        ip daddr 127.0.0.1 tcp dport {{ {port}, 18889 }} iifname != "lo" drop
    }}
}}
""")

    def withdraw(self):
        if self.marker.exists():
            self.run("nft", "-f", "-", data=f"table inet {TABLE}\ndelete table inet {TABLE}\n")
            self.marker.unlink()
            sync_dir(self.marker.parent)
