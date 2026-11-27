"""Private generation storage. Call state methods under the host lock."""

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from minipc_proxy_apply import parse_upstream


class OperationError(RuntimeError):
    """A message safe to show to the operator."""


PANEL_PORT = 18890
GENERATION = re.compile(r"generation-[0-9a-f]{32}")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")


def defaults():
    return {"upstreams": [], "selected": None, "port": 18888, "enabled": False,
            "docker": False, "failover": False, "interval": 60}


def parse_uri(uri):
    if not isinstance(uri, str) or "\n" in uri:
        raise OperationError("Invalid upstream HTTPS URI")
    try:
        return parse_upstream(uri)
    except ValueError:
        raise OperationError("Invalid upstream HTTPS URI") from None


def validate_port(port, *, allow_panel_port=False):
    if port == PANEL_PORT and not allow_panel_port:
        raise OperationError(f"Forwarding port {PANEL_PORT} is reserved for the browser panel")
    if type(port) is not int or not 1024 <= port <= 65535 or port == 18889:
        raise OperationError("Invalid or reserved forwarding port")


def validate(state, *, allow_panel_port=False):
    if not isinstance(state, dict) or set(state) != set(defaults()):
        raise OperationError("Invalid proxy settings")
    if any(type(state[key]) is not bool for key in ("enabled", "docker", "failover")):
        raise OperationError("Invalid proxy preferences")
    if type(state["interval"]) is not int or state["interval"] <= 0:
        raise OperationError("Invalid failover interval")
    validate_port(state["port"], allow_panel_port=allow_panel_port)
    if not isinstance(state["upstreams"], list):
        raise OperationError("Invalid upstream catalog")
    names, endpoints = set(), set()
    for item in state["upstreams"]:
        if not isinstance(item, dict) or set(item) != {"name", "uri"}:
            raise OperationError("Invalid upstream entry")
        name = item["name"]
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise OperationError("Invalid upstream name")
        endpoint = parse_uri(item["uri"])
        if name in names or endpoint in endpoints:
            raise OperationError("Duplicate upstream name or endpoint")
        names.add(name)
        endpoints.add(endpoint)
    selected = state["selected"]
    if selected is not None and (not isinstance(selected, str) or selected not in names):
        raise OperationError("Selected upstream must be in the catalog")
    if state["enabled"] and selected is None:
        raise OperationError("Select an upstream before enabling forwarding")


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private(info, directory=False):
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o077 or (not directory and info.st_nlink != 1)):
        raise OperationError("Proxy state requires private ownership and permissions")


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        private(os.fstat(descriptor))
        return json.load(stream)


def write_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".write-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(data, stream, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
            sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root)

    @contextlib.contextmanager
    def locked(self, maintenance=False):
        with self._lock("operation.lock"):
            if not maintenance and self.removal_state() is not None:
                raise OperationError("Standalone proxy removal prevents new operations")
            yield

    def lifecycle_lock(self):
        return self._lock("installation.lock")

    def removal_state(self):
        try:
            value = read_json(self.root / "removal")
        except FileNotFoundError:
            return None
        if value not in ("removing", "removed"):
            raise OperationError("Invalid standalone removal record")
        return value

    def allow_install(self):
        if self.removal_state() == "removing":
            raise OperationError("Finish the interrupted standalone removal before installing")

    def finish_install(self):
        if self.removal_state() == "removed":
            (self.root / "removal").unlink()
            sync_dir(self.root)

    @contextlib.contextmanager
    def _lock(self, filename):
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            sync_dir(self.root.parent)
        private(self.root.lstat(), directory=True)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(self.root / filename, flags, 0o600)
        with os.fdopen(descriptor, "rb+") as lock:
            private(os.fstat(lock.fileno()))
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def current(self):
        try:
            identifier = read_json(self.root / "current")
        except FileNotFoundError:
            # A missing commit pointer with an existing transaction needs inspection.
            if self.pending():
                raise OperationError("Missing committed generation during recovery")
            identifier = self.stage(defaults())
            self.commit(identifier)
        self.path(identifier)
        return identifier

    def path(self, identifier):
        if not isinstance(identifier, str) or not GENERATION.fullmatch(identifier):
            raise OperationError("Invalid proxy generation")
        return self.root / identifier

    def read(self, identifier, *, allow_panel_port=False):
        directory = self.path(identifier)
        private(directory.lstat(), directory=True)
        state = read_json(directory / "state.json")
        validate(state, allow_panel_port=allow_panel_port)
        return state

    def stage(self, state):
        validate(state)
        identifier = f"generation-{uuid.uuid4().hex}"
        directory = self.path(identifier)
        directory.mkdir(mode=0o700)
        write_json(directory / "state.json", state)
        sync_dir(self.root)
        return identifier

    def commit(self, identifier):
        write_json(self.root / "current", identifier)

    def pending(self):
        try:
            return read_json(self.root / "pending")
        except FileNotFoundError:
            return None

    def write_pending(self, previous, candidate, effect):
        write_json(self.root / "pending", {"previous": previous, "candidate": candidate, "effect": effect})

    def clear_pending(self):
        (self.root / "pending").unlink()
        sync_dir(self.root)

    def prune(self, committed):
        for path in self.root.iterdir():
            if path.name != committed and GENERATION.fullmatch(path.name):
                private(path.lstat(), directory=True)
                shutil.rmtree(path)
        sync_dir(self.root)

    def observation(self):
        try:
            observation = read_json(self.root / "last-check")
        except FileNotFoundError:
            return None
        if (not isinstance(observation, dict) or set(observation) != {"name", "time", "outcome"}
                or not isinstance(observation["name"], str) or not NAME.fullmatch(observation["name"])
                or not isinstance(observation["time"], str)
                or not re.fullmatch(r"[0-9T.:+Z-]+", observation["time"])
                or observation["outcome"] not in ("passed", "failed")):
            raise OperationError("Invalid proxy observation")
        return observation

    def record_check(self, name, timestamp, passed):
        write_json(self.root / "last-check", {
            "name": name, "time": timestamp, "outcome": "passed" if passed else "failed",
        })
