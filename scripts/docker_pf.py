"""Configure one private macOS PF translation anchor on the operator's Desktop host."""

import ipaddress
import re
import stat
from pathlib import Path
import uuid

from docker_lan import Error

PARENT = "outbound-proxy"
FRAGMENTS = Path("/etc/pf.anchors")
MAIN_RULES = Path("/etc/pf.conf")


def lan_interface(address, subnet, run):
    host = ipaddress.IPv4Address(address)
    sources = ipaddress.IPv4Network(subnet)
    for reserved in ("0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4",
                     "172.29.240.0/28", "172.29.241.0/28"):
        network = ipaddress.ip_network(reserved)
        if host in network or sources.overlaps(network):
            raise Error("Select an attached LAN IPv4 address and source subnet.")
    output = run("/sbin/ifconfig", "-a")
    for block in re.split(r"\n(?=[^\s])", output):
        header = re.match(r"([A-Za-z0-9_.-]+): flags=\w+<([^>]+)>", block)
        if not header or "UP" not in header[2].split(",") or "status: active" not in block:
            continue
        for ip, mask in re.findall(r"\binet ([\d.]+) netmask (0x[0-9a-fA-F]+)", block):
            if ip == str(host):
                netmask = ipaddress.IPv4Address(int(mask, 16))
                if sources.subnet_of(ipaddress.IPv4Network(f"{host}/{netmask}", strict=False)):
                    return header[1]
    raise Error("Select an IPv4 LAN address and source subnet on the same active macOS interface.")


def pf(run, *args, data=None):
    return run("/usr/bin/sudo", "--", "/sbin/pfctl", *args, data=data)


def anchor(owner):
    return f"{PARENT}/{uuid.UUID(owner)}"


def check_hook(run, interface):
    if not re.search(r"^Status:\s+Enabled\b", pf(run, "-s", "info"), re.MULTILINE):
        raise Error("Enable PF through your host firewall policy before requesting LAN forwarding.")
    interfaces = pf(run, "-i", interface, "-v", "-s", "Interfaces")
    if (not re.search(rf"^\s*{re.escape(interface)}(?:\s|$)", interfaces, re.MULTILINE)
            or re.search(r"\bskip\b", interfaces, re.I)):
        raise Error("PF must inspect packets on the selected LAN interface.")
    rules = pf(run, "-s", "nat").splitlines()
    if not any(re.fullmatch(r'rdr-anchor "outbound-proxy/\*"(?: all)?', rule.strip()) for rule in rules):
        raise Error('The active PF translation rules must include rdr-anchor "outbound-proxy/*". '
                    "Have the host firewall administrator install that hook before enabling LAN forwarding.")


def rule(config):
    host = ipaddress.IPv4Address(config["lan"])
    sources = ipaddress.IPv4Network(config["subnet"])
    port, interface = config["port"], config["interface"]
    if (type(port) is not int or not 1024 <= port <= 65535 or port in (18889, 18890)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface)):
        raise Error("Invalid port or interface for the private PF translation.")
    return (f"rdr on {interface} inet proto tcp from {sources} to {host} "
            f"port {port} -> 127.0.0.1 port {port}")


def canonical(text):
    return " ".join(text.replace("port = ", "port ").replace("/32 ", " ").split())


def read_live(name, run):
    loaded = pf(run, "-v", "-s", "Anchors").splitlines()
    if name not in {line.strip() for line in loaded}:
        return ""
    return pf(run, "-a", name, "-s", "nat")


def persist(config, owner, known, run):
    # The administrator opts in by provisioning the root-owned fragment and its load hook.
    name = anchor(owner)
    path = FRAGMENTS / f"outbound-proxy-{uuid.UUID(owner)}"
    if not path.exists() and not path.is_symlink():
        return
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise Error("The macOS PF boot fragment must be a root-owned, protected regular file.")
    marker = "# " + name
    lines = path.read_text().splitlines()
    allowed = {"", *(canonical(rule(entry)) for entry in (*known, config) if entry and entry["lan"])}
    if not lines or lines[0] != marker or canonical(" ".join(lines[1:])) not in allowed:
        raise Error("The macOS PF boot fragment is owned by another configuration.")
    desired = rule(config) if config and config["lan"] else ""
    if desired:
        load = f'load anchor "{name}" from "{path}"'
        if load not in {line.strip() for line in MAIN_RULES.read_text().splitlines()}:
            raise Error("Add the private fragment's load anchor declaration to the managed macOS PF boot policy.")
    content = marker + "\n" + (desired + "\n" if desired else "")
    if path.read_text() == content:
        return
    # The staging file is inside the root-controlled PF directory. Replace the fragment by rename.
    stage = str(path) + "." + uuid.uuid4().hex
    try:
        run("/usr/bin/sudo", "--", "/usr/bin/tee", stage, data=content)
        run("/usr/bin/sudo", "--", "/bin/chmod", "0644", stage)
        run("/usr/bin/sudo", "--", "/bin/mv", "-f", stage, str(path))
    finally:
        if Path(stage).exists():
            run("/usr/bin/sudo", "--", "/bin/rm", "-f", stage)


def apply(config, owner, known, run):
    candidates = [entry for entry in (config, *known) if entry and entry.get("lan")]
    if not candidates:
        return
    name = anchor(owner)
    expected = {canonical(rule(entry)) for entry in candidates}
    if config and config["lan"]:
        check_hook(run, config["interface"])
    live = read_live(name, run)
    if live and canonical(live) not in expected:
        raise Error("The private PF anchor contains an unrecognized translation rule.")
    desired = rule(config) if config and config["lan"] else ""
    if canonical(live) == canonical(desired):
        return
    # Load only the translation rules in the deployment-specific child anchor.
    data = desired + "\n"
    pf(run, "-a", name, "-N", "-n", "-f", "-", data=data)
    pf(run, "-a", name, "-N", "-f", "-", data=data)
    if canonical(read_live(name, run)) != canonical(desired):
        if desired:
            pf(run, "-a", name, "-N", "-f", "-", data="\n")
        raise Error("The installed PF translation rule could not be verified.")
