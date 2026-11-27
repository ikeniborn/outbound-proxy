"""Install and recover the single-container deployment from a local Docker context."""

import argparse
from contextlib import contextmanager
from functools import lru_cache
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import socket
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import uuid

import docker_lan as linux_lan
from docker_lan import Error, run

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "ghcr.io/ikeniborn/outbound-proxy"
DIGEST = r"sha256:[a-f0-9]{64}"
INGRESS = "172.29.240.2"
CLIENT_IP = "172.29.241.2"
CLIENT_NET = "outbound-proxy-clients"
GATEWAYS = "172.29.240.1/32,127.0.0.1/32"
UNIT = Path("/etc/systemd/system/outbound-proxy-lan.service")
OWNER = "io.github.ikeniborn.outbound-proxy.owner"


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name == "posix":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def local_engine():
    context = run("docker", "context", "show")
    endpoint = json.loads(run("docker", "context", "inspect", context))[0]["Endpoints"]["docker"]["Host"]
    if os.getenv("DOCKER_HOST") and not os.getenv("DOCKER_CONTEXT"):
        endpoint = os.environ["DOCKER_HOST"]
    local_pipe = endpoint.startswith("npipe:////./pipe/") and os.name == "nt"
    if not (endpoint.startswith("unix:///") or local_pipe):
        raise Error("Select a local Docker socket or Windows named pipe.")
    info = json.loads(run("docker", "info", "--format", "{{json .}}"))
    if info["OSType"] != "linux" or int(info["ServerVersion"].split(".")[0]) < 28:
        raise Error("Linux containers and Docker Engine 28 or newer are required.")
    if any("rootless" in entry for entry in info.get("SecurityOptions", [])):
        raise Error("Rootless packet translation has not been qualified.")
    return platform.system() == "Linux" and "desktop" not in info["OperatingSystem"].lower()


def resolve_image(image):
    if re.fullmatch(DIGEST, image):
        actual = run("docker", "image", "inspect", "--format", "{{.Id}}", image)
        if image != actual:
            raise Error("The selected local image ID is unavailable.")
        return image
    if not re.fullmatch(re.escape(REPOSITORY) + r"(?::[\w][\w.-]{0,127}|@" + DIGEST + r")", image):
        raise Error(f"Select a tag or an index digest from {REPOSITORY}.")
    run("docker", "pull", image)
    entries = json.loads(run("docker", "image", "inspect", image))[0]["RepoDigests"]
    matches = [item for item in entries if re.fullmatch(re.escape(REPOSITORY) + "@" + DIGEST, item)]
    if "@" in image and image in matches:
        return image
    if "@" not in image and len(matches) == 1:
        return matches[0]
    raise Error("Cannot identify a unique registry digest. Supply the immutable image reference.")


def check_routes(clients):
    intended = {"outbound-proxy-ingress": ipaddress.ip_network("172.29.240.0/28")}
    if clients:
        intended[CLIENT_NET] = ipaddress.ip_network("172.29.241.0/28")
    existing = set(run("docker", "network", "ls", "--format", "{{.Name}}").splitlines())
    routes = json.loads(run("ip", "-j", "-4", "route", "show", "table", "all"))
    for name, subnet in intended.items():
        own_device = None
        if name in existing:
            net, = json.loads(run("docker", "network", "inspect", name))
            labels = net.get("Labels") or {}
            owned = (labels.get("com.docker.compose.project") == "outbound-proxy" and
                     labels.get("com.docker.compose.network") == "ingress") if name != CLIENT_NET else (
                         labels.get(OWNER) == "clients-v1")
            if not owned:
                raise Error(f"The existing {name} network is owned by another installation.")
            own_device = (net.get("Options") or {}).get("com.docker.network.bridge.name",
                                                        "br-" + net["Id"][:12])
        for route in routes:
            destination = route.get("dst", "default")
            if destination == "default" or (own_device and route.get("dev") == own_device):
                continue
            if ipaddress.ip_network(destination, strict=False).overlaps(subnet):
                raise Error(f"{name} overlaps the host route to {destination}.")


def lan_interface(address, subnet):
    return linux_lan.lan_interface(address, subnet, run)


def install_policy(config, owner, linux):
    if linux:
        linux_lan.apply_policy(config, owner, run)
    elif config and config["lan"]:
        raise Error("LAN publication requires a local Linux Engine and its nftables ingress path.")


def owned_text(path, marker):
    if path.is_symlink() or (path.exists() and path.read_text().splitlines()[:1] != [marker]):
        raise Error(f"Refusing to replace an independently owned file: {path}")
    return path.exists()


def save_text(path, content, mode):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def boot_policy(config, owner, state, linux):
    if not linux:
        return
    helper = state.parent / "restore-lan.py"
    marker = "# outbound-proxy deployment " + owner
    enabled = bool(config and config["lan"])
    if not (enabled or UNIT.exists() or helper.exists()):
        return
    owned_text(helper, marker)
    installed = owned_text(UNIT, marker)
    if enabled:
        if not installed:
            load = run("systemctl", "show", "-p", "LoadState", "--value", UNIT.name)
            if load != "not-found":
                raise Error("An independent systemd LAN policy unit already exists.")
        unit = f"""{marker}
[Unit]
Description=Restore outbound proxy LAN ingress
Wants=network-online.target
After=network-online.target nftables.service docker.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {helper} {state}
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN
ProtectSystem=strict
ReadWritePaths={state.parent}
ProtectHome=yes
[Install]
WantedBy=multi-user.target
"""
        save_text(helper, marker + "\n" + Path(linux_lan.__file__).read_text(), 0o600)
        save_text(UNIT, unit, 0o644)
        run("systemctl", "daemon-reload")
        run("systemctl", "enable", UNIT.name)
        run("systemctl", "is-enabled", "--quiet", UNIT.name)
    else:
        if installed:
            run("systemctl", "disable", UNIT.name)
            UNIT.unlink()
            run("systemctl", "daemon-reload")
        helper.unlink(missing_ok=True)


def verify_resources(owner):
    volumes = run("docker", "volume", "ls", "--format", "{{.Name}}").splitlines()
    if "outbound-proxy-catalog" in volumes:
        vol, = json.loads(run("docker", "volume", "inspect", "outbound-proxy-catalog"))
        if (vol.get("Labels") or {}).get(OWNER) != owner:
            raise Error("The existing catalog volume is owned by another installation.")
    networks = run("docker", "network", "ls", "--format", "{{.Name}}").splitlines()
    if "outbound-proxy-ingress" in networks:
        net, = json.loads(run("docker", "network", "inspect", "outbound-proxy-ingress"))
        if (net.get("Labels") or {}).get(OWNER) != owner:
            raise Error("The existing host ingress network is owned by another installation.")


def prepare_clients():
    names = run("docker", "network", "ls", "--format", "{{.Name}}").splitlines()
    if CLIENT_NET not in names:
        run("docker", "network", "create", "--driver", "bridge", "--internal", "--subnet",
            "172.29.241.0/28", "--ip-range", "172.29.241.8/29", "--gateway", "172.29.241.1", "--label", OWNER + "=clients-v1", CLIENT_NET)
    net, = json.loads(run("docker", "network", "inspect", CLIENT_NET))
    if (net["Driver"] != "bridge" or not net["Internal"] or net.get("EnableIPv6") or
            (net.get("Labels") or {}).get(OWNER) != "clients-v1" or
            len(net["IPAM"]["Config"]) != 1 or net["IPAM"]["Config"][0]["Subnet"] != "172.29.241.0/28" or
            net["IPAM"]["Config"][0]["Gateway"] != "172.29.241.1"):
        raise Error("Existing Docker client network conflicts with this installation.")


@lru_cache(maxsize=1)
def compose_cli():
    try:
        run("docker", "compose", "version", "--short")
        return ["docker", "compose"]
    except Error:
        run("docker-compose", "version", "--short")
        return ["docker-compose"]


def compose(config, owner, *args):
    service = {"labels": {OWNER: owner}, "environment": {}}
    override = {"services": {"proxy": service}, "networks": {"ingress": {"labels": {OWNER: owner}}},
                "volumes": {"catalog": {"labels": {OWNER: owner}}}}
    if config["clients"]:
        service["networks"] = {"clients": {"ipv4_address": CLIENT_IP}}
        service["environment"].update(OP_CLIENT_IP=CLIENT_IP, OP_CLIENT_SUBNET="172.29.241.0/28")
        override["networks"]["clients"] = {"external": True, "name": CLIENT_NET}
    if config["inventory"]:
        service.update(user="0:0", cap_add=["SETUID", "SETGID"],
                       volumes=[{"type": "bind", "source": "/var/run/docker.sock",
                                 "target": "/var/run/docker.sock", "read_only": True,
                                 "bind": {"create_host_path": False}}])
        service["environment"]["OP_INVENTORY"] = "1"
    env = {**os.environ, "OP_IMAGE": config["image"], "OP_HOST_PORT": str(config["port"]),
           "OP_FORWARD_SOURCES": GATEWAYS + ("," + config["subnet"] if config["lan"] else ""),
           "OP_LAN_IP": config["lan"] or "", "COMPOSE_DISABLE_ENV_FILE": "1"}
    return run(*compose_cli(), "--project-name", "outbound-proxy", "-f", str(ROOT / "compose.yaml"),
               "-f", "-", *args, data=json.dumps(override), env=env)


DENIAL_CHECK = """import http.client, socket, sys
for target in sys.argv[2:]:
    conn = http.client.HTTPConnection(target, int(sys.argv[1]), timeout=2)
    try:
        conn.request("GET", "/", headers={"Host": "127.0.0.1:18890"})
        if conn.getresponse().status != 403:
            sys.exit("Management is reachable from the client network via " + target)
    except socket.gaierror:
        sys.exit("Cannot resolve isolation probe target " + target)
    except (ConnectionError, TimeoutError, http.client.RemoteDisconnected):
        pass
    except OSError as exc:
        import errno
        if exc.errno not in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            raise
    finally:
        conn.close()
"""


def probe_denial(config, network, port, targets):
    run("docker", "run", "--rm", "--pull=never", "--network", network, "--user", "10001:10001",
        "--cap-drop", "ALL", "--read-only", "--add-host", "engine-gateway:host-gateway",
        "--entrypoint", "python3", config["image"], "-c", DENIAL_CHECK, str(port), *targets)


def preflight_loopback(config, linux):
    # Check host-gateway translation before starting an image with access to the catalog.
    canary = """import http.server, signal
class Canary(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'loopback probe')
signal.alarm(120)
http.server.HTTPServer(('0.0.0.0', 18890), Canary).serve_forever()
"""
    cid = run("docker", "run", "--rm", "-d", "--pull=never", "--cap-drop", "ALL", "--read-only",
              "--user", "10001:10001", "-p", "127.0.0.1::18890", "--entrypoint", "python3",
              config["image"], "-c", canary)
    try:
        mapping = run("docker", "port", cid, "18890/tcp")
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", mapping)
        if not match:
            raise Error("Docker did not publish the isolation probe on IPv4 loopback.")
        port = int(match[1])
        await_panel(port, b"loopback probe")
        targets = ["engine-gateway"] if linux else ["engine-gateway", "host.docker.internal"]
        for network in ["bridge"] + ([CLIENT_NET] if config["clients"] else []):
            probe_denial(config, network, port, targets)
    finally:
        run("docker", "rm", "-f", cid)


def await_panel(port, expected):
    opener = build_opener(ProxyHandler({}))
    for attempt in range(30):
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
                if response.status == 200 and expected in response.read(65536).lower():
                    return
        except (OSError, URLError):
            pass
        if attempt != 29:
            time.sleep(0.4)
    raise Error("The published loopback endpoint failed its local HTTP check.")


def verify(config, owner, linux):
    cid = compose(config, owner, "ps", "-q", "proxy")
    if not cid or len(cid.splitlines()) != 1:
        raise Error("Expected one running proxy container.")
    image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", config["image"])
    if run("docker", "inspect", "--format", "{{.Image}}", cid) != image_id:
        raise Error("The running container does not use the selected immutable image.")
    members = json.loads(run("docker", "network", "inspect", "outbound-proxy-ingress"))[0]["Containers"]
    if set(members) != {cid}:
        raise Error("The host ingress network must contain only the proxy service.")
    await_panel(18890, b"outbound proxy")
    targets = [INGRESS, "engine-gateway"]
    if not linux:
        targets.append("host.docker.internal")
    if config["lan"]:
        targets.append(config["lan"])
    for network in ["bridge"] + ([CLIENT_NET] if config["clients"] else []):
        probe_denial(config, network, 18890, targets + ([CLIENT_IP] if network == CLIENT_NET else []))


def available_port(address, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # A recently stopped publisher can leave accepted connections in TIME_WAIT.
        if os.name == "posix":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((address, port))
            listener.listen(1)
        except OSError as exc:
            raise Error(f"Cannot bind {address}:{port}: {exc}") from exc


def configure(args, current, linux):
    config = dict(current or {"port": 18888, "clients": False, "inventory": False,
                              "lan": None, "subnet": None, "interface": None})
    if args.port is not None:
        if not 1024 <= args.port <= 65535 or args.port in (18889, 18890):
            raise Error("Select port 1024 through 65535, excluding 18889 and 18890.")
        config["port"] = args.port
    if args.clients is not None:
        config["clients"] = args.clients
    if args.inventory is not None:
        config["inventory"] = args.inventory
    if config["inventory"] and not config["clients"]:
        raise Error("Enable Docker clients to inspect their network.")
    if args.local_only and args.subnet:
        raise Error("Select --local-only or configure a LAN subnet.")
    if args.local_only:
        config.update(lan=None, subnet=None, interface=None)
    elif args.lan or args.subnet:
        if not args.lan or not args.subnet:
            raise Error("Supply both --lan and --subnet, or select --local-only.")
        config.update(lan=args.lan, subnet=args.subnet)
    if config["lan"]:
        if not linux:
            raise Error("LAN publication is unavailable on Docker Desktop.")
        config["interface"] = lan_interface(config["lan"], config["subnet"])
        if Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() != "1":
            raise Error("IPv4 forwarding is disabled on this Docker host.")
        available_port(config["lan"], config["port"])
    if not current:
        available_port("127.0.0.1", 18890)
    if not current or current["port"] != config["port"]:
        available_port("127.0.0.1", config["port"])
    if args.image:
        config["image"] = resolve_image(args.image)
    if not config.get("image"):
        raise Error("Installation requires --image with a reviewed tag or digest.")
    return config


def restore(previous, candidate, owner, linux):
    install_policy(None, owner, linux)
    if previous:
        if previous["clients"]:
            prepare_clients()
        compose(previous, owner, "up", "-d", "--pull", "never")
        try:
            verify(previous, owner, linux)
            install_policy(previous, owner, linux)
        except (OSError, RuntimeError, KeyboardInterrupt):
            compose(previous, owner, "down")
            raise
    elif candidate:
        compose(candidate, owner, "down")


def deploy(args, path):
    linux = local_engine()
    record = (json.loads(path.read_text()) if path.exists() else
              {"owner": str(uuid.uuid4()), "active": None, "previous": None})
    owner, active = record["owner"], record["active"]
    containers = run("docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=outbound-proxy")
    for cid in containers.splitlines():
        labels = json.loads(run("docker", "inspect", "--format", "{{json .Config.Labels}}", cid))
        if labels.get(OWNER) != owner:
            raise Error("This Compose project is owned by another deployment state directory.")
    if args.action == "status":
        print(json.dumps({"settings": active, "recovery_required": "pending" in record,
                          "container": compose(active, owner, "ps") if active else None}, indent=2))
        return
    verify_resources(owner)
    if "pending" in record:
        restore(active, record["pending"], owner, linux)
        boot_policy(active, owner, path, linux)
        del record["pending"]
        atomic_json(path, record)
    if args.action == "remove":
        if active:
            atomic_json(path, dict(record, pending=None))
            install_policy(None, owner, linux)
            boot_policy(None, owner, path, linux)
            compose(active, owner, "down")
            record.update(active=None, previous=None)
            atomic_json(path, record)
        print("Removed. The catalog volume is retained.")
        return
    if args.action == "rollback":
        candidate = record["previous"]
        if not candidate:
            raise Error("No previous deployment settings are recorded.")
    else:
        if args.action == "reconfigure" and not active:
            raise Error("Install the service before reconfiguration.")
        candidate = configure(args, active, linux)
    if linux:
        check_routes(candidate["clients"])
    if candidate["clients"]:
        prepare_clients()
    compose(candidate, owner, "config", "--quiet")
    preflight_loopback(candidate, linux)
    atomic_json(path, dict(record, pending=candidate))
    try:
        install_policy(None, owner, linux)
        compose(candidate, owner, "up", "-d", "--pull", "never")
        verify(candidate, owner, linux)
        boot_policy(candidate, owner, path, linux)
        install_policy(candidate, owner, linux)
        committed = dict(record, active=candidate, previous=active if candidate != active else record["previous"])
        atomic_json(path, committed)
    except (OSError, RuntimeError, KeyboardInterrupt):
        restore(active, candidate, owner, linux)
        boot_policy(active, owner, path, linux)
        atomic_json(path, record)
        raise
    print(f"{candidate['image']}\nPanel http://127.0.0.1:18890/\nProxy http://127.0.0.1:{candidate['port']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "reconfigure"):
        cmd = actions.add_parser(action)
        cmd.add_argument("--image")
        cmd.add_argument("--port", type=int)
        cmd.add_argument("--subnet")
        lan = cmd.add_mutually_exclusive_group()
        lan.add_argument("--lan")
        lan.add_argument("--local-only", action="store_true")
        cmd.add_argument("--clients", action=argparse.BooleanOptionalAction, default=None)
        cmd.add_argument("--inventory", action=argparse.BooleanOptionalAction, default=None)
    for action in ("remove", "rollback", "status"):
        actions.add_parser(action)
    args = parser.parse_args()
    if platform.system() == "Linux":
        directory = Path("/var/lib/outbound-proxy-deployment")
    elif os.name == "nt":
        directory = Path(os.environ["LOCALAPPDATA"]) / "outbound-proxy"
    else:
        directory = Path.home() / "Library" / "Application Support" / "outbound-proxy"
    with locked(directory):
        deploy(args, directory / "deployment.json")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyboardInterrupt) as exc:
        sys.exit(f"Deployment failed: {exc}")
