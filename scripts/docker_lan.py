"""Restore the deployment-owned ingress table on a Linux boot."""

import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

INGRESS = "172.29.240.2"
TABLE = "outbound_proxy_deploy"


class Error(RuntimeError):
    pass


def run(*args, data=None, env=None):
    result = subprocess.run(args, input=data, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, check=False)
    if result.returncode:
        raise Error(result.stderr.strip() or f"{args[0]} exited {result.returncode}")
    return result.stdout.strip()


def lan_interface(address, subnet, run):
    host = ipaddress.IPv4Address(address)
    sources = ipaddress.IPv4Network(subnet)
    excluded = ["127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "172.29.240.0/28", "172.29.241.0/28"]
    if host.is_unspecified or any(sources.overlaps(ipaddress.ip_network(net)) or
                                  host in ipaddress.ip_network(net) for net in excluded):
        raise Error("Select an attached LAN address and subnet.")
    links = json.loads(run("ip", "-j", "-4", "address", "show", "up"))
    for link in links:
        for item in link["addr_info"]:
            if item["local"] != str(host):
                continue
            local = ipaddress.ip_interface(f"{host}/{item['prefixlen']}").network
            if sources.subnet_of(local) and re.fullmatch(r"[\w.:-]{1,15}", link["ifname"]):
                return link["ifname"]
    raise Error("LAN address and source subnet must belong to the same active host interface.")


def firewall(config, owner):
    program = f'table ip {TABLE} {{\n comment "{owner}"\n'
    program += (" chain ingress_guard {\n  type filter hook prerouting priority raw; policy accept;\n"
                f"  ip daddr {INGRESS} tcp dport {{ 18888, 18890 }} drop\n")
    address, subnet, interface, port = (config[k] for k in ("lan", "subnet", "interface", "port"))
    program += (f'  ip daddr {address} tcp dport {port} iifname != "{interface}" drop\n'
                f"  ip daddr {address} tcp dport {port} ip saddr != {subnet} drop\n }}\n"
                " chain publish {\n  type nat hook prerouting priority -101; policy accept;\n"
                f'  iifname "{interface}" ip saddr {subnet} ip daddr {address} tcp dport {port} '
                f"dnat to {INGRESS}:18888\n }}\n}}\n")
    return program


def apply_policy(config, owner, run):
    tables = json.loads(run("nft", "-j", "list", "tables"))["nftables"]
    exists = any(t.get("table", {}).get("family") == "ip" and
                 t["table"].get("name") == TABLE for t in tables)
    changes = ""
    if exists:
        entries = json.loads(run("nft", "-j", "list", "table", "ip", TABLE))["nftables"]
        if not any(t.get("table", {}).get("comment") == owner for t in entries):
            raise Error("The nftables table belongs to another installation.")
        changes = f"delete table ip {TABLE}\n"
    if config and config["lan"]:
        changes += firewall(config, owner)
    if changes:
        run("nft", "-c", "-f", "-", data=changes)
        run("nft", "-f", "-", data=changes)
        if config and config["lan"]:
            live = run("nft", "list", "table", "ip", TABLE)
            if owner not in live or f"dnat to {INGRESS}:18888" not in live:
                raise Error("The installed LAN policy could not be verified.")


def restore_on_boot(path):
    import fcntl

    with (path.parent / "lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = json.loads(path.read_text())
        config = None if "pending" in record else record["active"]
        if config and config["lan"]:
            # Withdraw stale rules before validating a possibly changed host interface.
            apply_policy(None, record["owner"], run)
            if lan_interface(config["lan"], config["subnet"], run) != config["interface"]:
                raise Error("The configured LAN address moved to a different interface.")
        apply_policy(config, record["owner"], run)


if __name__ == "__main__":
    try:
        restore_on_boot(Path(sys.argv[1]))
    except (OSError, ValueError, RuntimeError) as exc:
        sys.exit(f"LAN policy restore failed: {exc}")
