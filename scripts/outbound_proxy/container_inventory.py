"""Optional fixed Docker network read through a separate service identity."""

import http.client
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading

def inventory_network(env):
    name = env.get("OP_INVENTORY_NETWORK", "outbound-proxy-clients")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
        raise ValueError("Invalid inventory network name")
    return name


NETWORK = inventory_network(os.environ)
DAEMON = "/var/run/docker.sock"
PANEL_UID = 10001
HELPER_UID = 10002
UNAVAILABLE = {"name": NETWORK, "status": "unavailable"}


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(2)
        self.sock.connect(DAEMON)


def inspect_network():
    connection = DockerConnection("localhost")
    try:
        connection.request("GET", "/networks/" + NETWORK)
        response = connection.getresponse()
        if response.status == 404:
            return {"name": NETWORK, "status": "missing"}
        if response.status != 200:
            return UNAVAILABLE
        limit = 1024 * 1024
        data = response.read(limit + 1)
        if len(data) > limit:
            return UNAVAILABLE
        net = json.loads(data)
        if net["Name"] != NETWORK or net["Driver"] != "bridge" or net["Internal"] is not True:
            return {"name": NETWORK, "status": "conflict"}
        config, = net["IPAM"]["Config"]
        members = sorted(({"name": item["Name"], "ip": item["IPv4Address"].partition("/")[0]}
                          for item in (net["Containers"] or {}).values()), key=lambda m: m["name"])
        return {"name": NETWORK, "status": "present", "interface": NETWORK, "link": "Unknown",
                "subnet": str(ipaddress.IPv4Network(config["Subnet"])),
                "gateway": str(ipaddress.IPv4Address(config["Gateway"])), "members": members}
    finally:
        connection.close()


def serve(fd):
    with socket.socket(fileno=fd) as channel:
        while request := channel.recv(32):
            result = UNAVAILABLE
            if request == b"inventory":
                try:
                    result = inspect_network()
                except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, AttributeError):
                    pass
            data = json.dumps(result).encode()
            channel.send(data if len(data) < 65536 else json.dumps(UNAVAILABLE).encode())


def drop_to_panel():
    os.setgroups([])
    os.setgid(PANEL_UID)
    os.setuid(PANEL_UID)


class Inventory:
    def __init__(self):
        self.channel = None
        self.proc = None
        self.lock = threading.Lock()

    def start(self):
        info = os.stat(DAEMON)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid == PANEL_UID or
                info.st_gid == PANEL_UID or info.st_mode & stat.S_IWOTH):
            raise RuntimeError("Panel identity could access the Docker socket")
        if os.geteuid() != 0:
            raise RuntimeError("Inventory bootstrap requires a separate privileged identity")
        self.channel, remote = socket.socketpair(type=socket.SOCK_SEQPACKET)
        with remote:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "outbound_proxy.container_inventory", str(remote.fileno())],
                pass_fds=(remote.fileno(),), user=HELPER_UID, group=info.st_gid, extra_groups=[],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.channel.settimeout(3)
        drop_to_panel()
        if os.access(DAEMON, os.W_OK):
            raise RuntimeError("Panel identity could access the Docker socket")

    def status(self):
        with self.lock:
            if self.channel is None:
                return UNAVAILABLE
            try:
                self.channel.send(b"inventory")
                return json.loads(self.channel.recv(65536))
            except (ValueError, OSError):
                self.channel.close()
                self.channel = None
                return UNAVAILABLE

    def close(self):
        if self.channel:
            self.channel.close()
        if self.proc:
            self.proc.wait(timeout=5)


if __name__ == "__main__":
    serve(int(sys.argv[1]))
