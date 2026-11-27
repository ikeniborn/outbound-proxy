"""Run the host-local browser panel and optional forwarding in one container."""

from contextlib import ExitStack
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import time

from . import pairing
from .container_inventory import DAEMON, Inventory
from .container_runtime import ContainerOperations, ContainerRuntime, Network
from .store import OperationError, private
from .ui import ManagementServer, valid_action

STATE = Path("/var/lib/outbound-proxy")
CONTROL = Path("/tmp/outbound-proxy/control.sock")


def interface_for(ip):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            try:
                info = fcntl.ioctl(sock, 0x8915, struct.pack("256s", name.encode()))
            except OSError:
                continue
            if socket.inet_ntoa(info[20:24]) == ip:
                return name
    raise RuntimeError("Management address is absent from the container interfaces")


class LocalPanel(ManagementServer):
    local_session = True
    auth_paths = ("/api/session",)

    def __init__(self, ops, network):
        self.network = network
        super().__init__(ops, network.management)

    def server_bind(self):
        name = interface_for(self.network.management)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, name.encode() + b"\0")
        super().server_bind()

    def verify_request(self, request, client_address):
        peer = ipaddress.IPv4Address(client_address[0])
        return any(peer in net for net in self.network.management_sources)

    def authenticate(self, path, data):
        if path != "/api/session" or data:
            raise pairing.Rejected()
        with self.auth_lock:
            self.prune_expired()
            if len(self.sessions) >= 256:
                del self.sessions[next(iter(self.sessions))]
            return self.new_session()


class Command(socketserver.StreamRequestHandler):
    timeout = 15

    def handle(self):
        try:
            data = json.loads(self.rfile.readline(131072))
            read_or_job = (isinstance(data, dict) and data.keys() == {"action"}
                           and data["action"] in ("status", "list", "tick", "recover"))
            if not isinstance(data, dict) or not (read_or_job or valid_action(data)):
                raise OperationError("Invalid proxy command")
            result = self.server.ops.run(**data)
        except OperationError as error:
            result = {"error": str(error)}
        except Exception:
            result = {"error": "Proxy operation failed"}
        self.wfile.write(json.dumps(result).encode() + b"\n")


class ControlServer(socketserver.ThreadingUnixStreamServer):
    def handle_error(self, request, client_address):
        pass


def start_server(stack, server):
    stack.enter_context(server)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1})
    worker.start()
    stack.callback(worker.join)
    stack.callback(server.shutdown)


def supervise(ops, stop):
    try:
        ops.run("boot")
    except OperationError:
        print("Forwarding startup failed. Inspect the local panel.", file=sys.stderr)
    last_restart = last_tick = time.monotonic()
    while not stop.wait(0.5):
        now = time.monotonic()
        if now - last_restart >= 5:
            last_restart = now
            with ops.exclusive():
                health = ops.native.health()
                if health["boot_enabled"] and not health["running"]:
                    try:
                        ops.run("boot")
                    except OperationError:
                        print("Could not restart forwarding.", file=sys.stderr)
        interval = ops.native.interval
        if interval is None:
            last_tick = time.monotonic()
        elif time.monotonic() - last_tick >= interval:
            last_tick = time.monotonic()
            try:
                ops.run("tick")
            except OperationError:
                print("Automatic failover check failed.", file=sys.stderr)


def main():
    os.umask(0o077)
    network = Network.configured(os.environ)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with ExitStack() as stack:
        inventory = Inventory()
        stack.callback(inventory.close)
        if os.environ.get("OP_INVENTORY") == "1":
            inventory.start()
        if os.geteuid() == 0 or os.access(DAEMON, os.W_OK):
            raise RuntimeError("Container service requires an unprivileged identity without daemon access")
        CONTROL.parent.mkdir(mode=0o700, exist_ok=True)
        private(CONTROL.parent.lstat(), directory=True)
        runtime_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=CONTROL.parent)))
        runtime = ContainerRuntime(network, runtime_dir)
        stack.callback(runtime.close)
        ops = ContainerOperations(STATE, runtime, inventory)
        CONTROL.unlink(missing_ok=True)
        stack.callback(CONTROL.unlink, missing_ok=True)
        command = ControlServer(str(CONTROL), Command)
        command.ops = ops
        start_server(stack, command)
        start_server(stack, LocalPanel(ops, network))
        supervise(ops, stop)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit("Container startup failed. Check the deployment settings and permissions.")
