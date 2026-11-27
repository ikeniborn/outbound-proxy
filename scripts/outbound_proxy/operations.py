"""Serialized, redacted operations shared by local operators and native jobs."""

import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import socket
import threading

from minipc_proxy_apply import CandidateVerificationError, select_failover

from . import activation
from .store import OperationError, Store, parse_uri, validate, validate_port


def entry(state, name):
    for item in state["upstreams"]:
        if item["name"] == name:
            return item
    raise OperationError("Unknown upstream name")


def flag(value):
    if type(value) is bool:
        return value
    if value in ("on", "off"):
        return value == "on"
    raise OperationError("Use on or off")


def check_available(port):
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    except OSError:
        raise OperationError("Requested forwarding port is unavailable") from None


@contextlib.contextmanager
def sanitized_errors():
    try:
        yield
    except OperationError:
        raise
    except Exception:
        raise OperationError("Proxy operation failed. Check state permissions and native services") from None


class Operations:
    def __init__(self, root, native):
        self.store = Store(root)
        self.native = native
        self._local = threading.local()

    @contextlib.contextmanager
    def exclusive(self):
        """Hold the host lock throughout a migration, including nested operations."""
        if getattr(self._local, "locked", False):
            yield
            return
        with self.store.locked():
            self._local.locked = True
            try:
                yield
            finally:
                self._local.locked = False

    def get_state(self):
        """Read a private migration snapshot while the caller holds the host lock."""
        if not getattr(self._local, "locked", False):
            raise OperationError("A migration snapshot requires the operation lock")
        return self.store.read(self.store.current())

    def import_state(self, state):
        with sanitized_errors(), self.exclusive():
            validate(state)
            if state["docker"] and not self.native.docker_supported:
                raise OperationError("Docker access is unavailable on this platform")
            if state["enabled"]:
                activation.recover(self.store, self.native)
                self._verify(entry(state, state["selected"]))
                activation.publish(self.store, self.native, self.store.current(), state, "forwarding")
            else:
                # Rollback must release the port even when the interrupted route cannot serve.
                previous = self.store.current()
                candidate = self.store.stage(state)
                self.store.write_pending(previous, candidate, "forwarding")
                self.store.commit(candidate)
                activation.recover(self.store, self.native)
            return self._view(state)

    def _view(self, state):
        try:
            health = self.native.health()
        except Exception:
            health = {"running": None, "boot_enabled": None}
        public = {key: value for key, value in state.items() if key != "upstreams"}
        public["upstreams"] = []
        for item in state["upstreams"]:
            host, port, _, _ = parse_uri(item["uri"])
            public["upstreams"].append({"name": item["name"], "host": host, "port": port})
        public.update(running=health["running"], boot_enabled=health["boot_enabled"],
                      docker_supported=self.native.docker_supported,
                      recovery_pending=self.store.pending() is not None,
                      last_check=self.store.observation())
        return public

    def _check(self, name, probe, argument):
        try:
            passed = probe(argument) is not False
        except Exception:
            passed = False
        self.store.record_check(name, datetime.now(timezone.utc).isoformat(), passed)
        return passed

    def _verify(self, item):
        if not self._check(item["name"], self.native.check, item["uri"]):
            raise OperationError("Proxy candidate verification failed")

    def _tick(self, state):
        if not state["enabled"] or not state["failover"]:
            return False
        def active_ok():
            return self._check(state["selected"], self.native.probe, state["port"])

        def try_alternate(item):
            return self._check(item["name"], self.native.check, item["uri"])

        try:
            alternate = select_failover(state["upstreams"], entry(state, state["selected"]),
                                        active_ok, try_alternate)
        except CandidateVerificationError as error:
            raise OperationError(str(error)) from None
        if alternate is None:
            return False
        state["selected"] = alternate["name"]
        return True

    def run(self, action, name=None, uri=None, value=None):
        with sanitized_errors(), self.exclusive():
            if action in ("list", "status"):
                return self._view(self.get_state())
            recovered_effect = activation.recover(self.store, self.native)
            previous = self.store.current()
            original = self.store.read(previous)
            state = deepcopy(original)
            effect = None
            if action == "boot" and recovered_effect != "forwarding":
                self.store.write_pending(previous, previous, "forwarding")
                activation.recover(self.store, self.native)
            elif action in ("recover", "boot"):
                pass
            elif action == "add":
                state["upstreams"].append({"name": name, "uri": uri})
            elif action == "update":
                item = entry(state, name)
                if uri not in (None, "") and uri != item["uri"]:
                    item["uri"] = uri
                    validate(state)
                    if name == state["selected"] and state["enabled"]:
                        self._verify(item)
                        effect = "forwarding"
            elif action == "remove":
                item = entry(state, name)
                if name == state["selected"]:
                    raise OperationError("Select a replacement before removing the selected upstream")
                state["upstreams"].remove(item)
            elif action in ("check", "use"):
                self._verify(entry(state, name))
                if action == "use":
                    state["selected"] = name
                    if state["enabled"]:
                        effect = "forwarding"
            elif action == "port":
                validate_port(value)
                if value != state["port"]:
                    check_available(value)
                    state["port"] = value
                    if state["enabled"]:
                        self._verify(entry(state, state["selected"]))
                        effect = "forwarding"
            elif action in ("enable", "disable"):
                if action == "enable":
                    if state["selected"] is None:
                        raise OperationError("Select an upstream before enabling forwarding")
                    self._verify(entry(state, state["selected"]))
                    if not state["enabled"]:
                        check_available(state["port"])
                state["enabled"] = action == "enable"
                effect = "forwarding"
            elif action in ("failover", "docker"):
                setting = flag(value)
                if action == "docker" and not self.native.docker_supported:
                    raise OperationError("Docker access is unavailable on this platform")
                if state["enabled"] and state[action] != setting:
                    if action == "docker":
                        self._verify(entry(state, state["selected"]))
                        effect = "forwarding"
                    else:
                        effect = "schedule"
                state[action] = setting
            elif action == "tick":
                if self._tick(state):
                    effect = "forwarding"
            else:
                raise OperationError("Unknown proxy operation")
            if effect or state != original:
                activation.publish(self.store, self.native, previous, state, effect)
            return self._view(state)
