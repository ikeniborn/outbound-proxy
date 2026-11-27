"""Docker listeners and private Tinyproxy/stunnel child lifetimes."""

from contextlib import ExitStack
from dataclasses import dataclass
import grp
import ipaddress
import os
from pathlib import Path
import pwd
import shutil
import socket
import tempfile
import threading

import minipc_proxy_apply as legacy
from . import activation
from .native import Host, LOOPBACK, Native, child, stop_child
from .operations import Operations, entry
from .store import OperationError, validate_port

FORWARD_PORT = 18888


def address(value):
    ip = ipaddress.IPv4Address(value)
    if ip.is_unspecified or ip.is_multicast:
        raise ValueError("Expected a concrete IPv4 address")
    return str(ip)


def sources(value):
    return tuple(ipaddress.IPv4Network(item) for item in value.split(","))


@dataclass(frozen=True)
class Network:
    management: str
    management_sources: tuple
    forward: str
    forward_sources: tuple
    client: str
    client_subnet: str
    host_port: int
    lan: str
    image: str

    @classmethod
    def configured(cls, env):
        port = int(env.get("OP_HOST_PORT", FORWARD_PORT))
        validate_port(port)
        client = env.get("OP_CLIENT_IP") or ""
        subnet = env.get("OP_CLIENT_SUBNET") or ""
        if bool(client) != bool(subnet):
            raise ValueError("Provide the client address and subnet together")
        management = address(env["OP_MANAGEMENT_IP"])
        forward = address(env["OP_FORWARD_IP"])
        if client and (address(client) in {forward, management}
                       or ipaddress.IPv4Address(client) not in ipaddress.IPv4Network(subnet)):
            raise ValueError("Invalid client network")
        management_sources = sources(env["OP_MANAGEMENT_SOURCES"])
        if any(net.prefixlen != 32 for net in management_sources):
            raise ValueError("Management sources must be single IPv4 addresses")
        lan = env.get("OP_LAN_IP") or ""
        return cls(management, management_sources, forward, sources(env["OP_FORWARD_SOURCES"]),
                   client, subnet, port, address(lan) if lan else "", env.get("OP_IMAGE", ""))


class Transport:
    """Retain the private upstream socket for the entire life of the frontends."""

    def __init__(self, directory, adapter, uri):
        self.listener = socket.socket()
        try:
            self.listener.bind((LOOPBACK, 0))
            self.listener.listen(100)
            self.listener.settimeout(0.2)
            self.port = self.listener.getsockname()[1]
            _, tls = legacy.render_pair(legacy.parse_upstream(uri), (), (), 0, self.port,
                                        ca=adapter.host.ca, inetd=True)
            config = directory / "stunnel.conf"
            config.write_text(tls)
            self.command = [adapter.host.stunnel, str(config)]
            self.identity = pwd.getpwnam(adapter.host.tls_user)
            self.stopping = threading.Event()
            self.thread = threading.Thread(target=self.serve, name="stunnel-accept")
            self.thread.start()
        except BaseException:
            self.listener.close()
            raise

    def serve(self):
        children = []
        try:
            while not self.stopping.is_set():
                children = [proc for proc in children if proc.poll() is None]
                try:
                    stream, _ = self.listener.accept()
                except TimeoutError:
                    continue
                with stream:
                    if len(children) < 100:
                        children.append(child(self.command, self.identity, stream))
        finally:
            for proc in children:
                proc.terminate()
            for proc in children:
                stop_child(proc)

    def close(self):
        self.stopping.set()
        self.thread.join()
        self.listener.close()


class ContainerRuntime(Native):
    """Adapt native candidate checks and activation to foreground processes."""

    def __init__(self, network, runtime, *, host=None):
        account = pwd.getpwuid(os.geteuid()).pw_name
        self.host = host or Host("Linux", shutil.which("tinyproxy"), shutil.which("stunnel"),
                                 shutil.which("curl"), Path("/etc/ssl/certs/ca-certificates.crt"),
                                 account, account)
        self.target = "https://example.com/"
        self.network = network
        self.runtime = runtime
        self.docker_supported = bool(network.client)
        self.stack = ExitStack()
        self.fronts = []
        self.transport = None
        self.interval = None
        self.boot_enabled = False
        self.restoring = False

    def close(self):
        self.stack.close()
        self.fronts.clear()
        self.transport = None

    def apply(self, state):
        self.close()
        self.schedule(False, state["interval"])
        self.boot_enabled = state["enabled"]
        if not state["enabled"]:
            return
        uri = entry(state, state["selected"])["uri"]
        groups = [([LOOPBACK, self.network.forward], [LOOPBACK, *self.network.forward_sources])]
        if state["docker"] and self.network.client:
            groups.append(([self.network.client], [self.network.client_subnet]))
        with ExitStack() as cleanup:
            directory = Path(cleanup.enter_context(tempfile.TemporaryDirectory(dir=self.runtime)))
            transport = Transport(directory, self, uri)
            cleanup.callback(transport.close)
            identity = pwd.getpwnam(self.host.proxy_user)
            self.fronts = []
            for index, (listeners, allowed) in enumerate(groups):
                data, _ = legacy.render_pair(legacy.parse_upstream(uri), listeners, allowed,
                                             FORWARD_PORT, transport.port,
                                             user=identity.pw_name, group=grp.getgrgid(identity.pw_gid).gr_name)
                config = directory / f"tinyproxy-{index}.conf"
                config.write_text(data)
                proc = child([self.host.tinyproxy, "-d", "-c", str(config)], identity)
                cleanup.callback(stop_child, proc)
                self.fronts.append(proc)
            legacy.wait_for_listener(FORWARD_PORT)
            # A verified selection survives an upstream outage across a restart.
            # Restore its listener so the opt-in health check can choose a replacement.
            if not self.restoring:
                self.probe(FORWARD_PORT)
            if any(proc.poll() is not None for proc in self.fronts):
                raise RuntimeError("Forwarding listener failed")
            self.transport = transport
            self.stack = cleanup.pop_all()
        self.schedule(state["failover"], state["interval"])

    def schedule(self, enabled, interval):
        self.interval = interval if enabled else None

    def health(self):
        running = bool(self.transport and self.transport.thread.is_alive() and
                       self.fronts and all(proc.poll() is None for proc in self.fronts))
        return {"running": running, "boot_enabled": self.boot_enabled}


class ContainerOperations(Operations):
    def __init__(self, root, runtime, inventory):
        super().__init__(root, runtime)
        self.inventory = inventory

    def deployment_settings(self, state):
        return {**state, "port": FORWARD_PORT,
                "docker": state["docker"] and self.native.docker_supported}

    def run(self, action, **fields):
        if action == "port":
            raise OperationError("Change the published port with the deployment script")
        with self.exclusive():
            if action == "disable" and self.store.pending():
                # A broken committed route can make forward recovery impossible.
                # Commit the explicit stop and withdraw any partially started children.
                state = self.deployment_settings(self.get_state())
                state["enabled"] = False
                view = self.import_state(state)
            elif action == "boot":
                self.native.restoring = True
                try:
                    view = super().run(action, **fields)
                    state = self.get_state()
                    normalized = self.deployment_settings(state)
                    if normalized != state:
                        # The restored process already listens on the deployment interfaces.
                        activation.publish(self.store, self.native, self.store.current(), normalized, None)
                        view = self._view(normalized)
                finally:
                    self.native.restoring = False
            else:
                view = super().run(action, **fields)
        network = self.native.network
        view["docker"] = view["docker"] and self.native.docker_supported
        view["deployment"] = {"image": network.image, "endpoints": [
            f"{host}:{network.host_port}" for host in [LOOPBACK, network.lan] if host]}
        view["bridge"] = self.inventory.status()
        return view
