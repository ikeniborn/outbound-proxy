"""Native forwarding, boot registration, and isolated TLS candidate checks."""

import contextlib
import grp
import os
import platform
import plistlib
import pwd
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import minipc_proxy_apply as legacy
from .docker_access import Access, GATEWAY, SUBNET, sync_dir

TUNNEL_PORT = 18889
LOOPBACK = "127.0.0.1"
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
ENV = {"PATH": SAFE_PATH, "HOME": "/var/empty", "LANG": "C", "LC_ALL": "C"}
TEMPLATES = Path(__file__).resolve().parents[1] / "native"
LABEL = "local.outbound-proxy."
SYSTEMD = Path("/etc/systemd/system")
DAEMONS = Path("/Library/LaunchDaemons")


def platform_name(system, release):
    if system == "Darwin":
        return system
    if system == "Linux" and re.search(r'^ID=["\']?(debian|ubuntu)["\']?$', release, re.M):
        return system
    raise RuntimeError("Requires Debian or Ubuntu with systemd, or macOS with launchd")


def trusted(path):
    """Check every component, including the path leading to each symlink target."""
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError("Privileged path must be absolute")
    if path.parent != path:
        trusted(path.parent)
    info = path.lstat()
    if info.st_uid or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
        raise RuntimeError(f"Unsafe ownership or writable privileged path: {path}")
    if stat.S_ISLNK(info.st_mode):
        link = path.readlink()
        # Preserve .. during traversal so a writable preceding directory is rejected.
        trusted(link if link.is_absolute() else path.parent / link)
    return path


def executable(name, directories):
    for directory in directories:
        path = Path(directory) / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise RuntimeError(f"Missing executable: {name}")


def privileged(name):
    return str(trusted(executable(name, SAFE_PATH.split(":"))))


def execute(args, *, data=None, check=True, timeout=90):
    result = subprocess.run(args, input=data, capture_output=True, text=True,
                            timeout=timeout, env=ENV, cwd="/", check=False)
    if check and result.returncode:
        # Native diagnostics can include private configuration text.
        raise RuntimeError(f"{Path(args[0]).name} operation failed")
    return result


def system_command(name, *args, data=None, timeout=90):
    return execute([privileged(name), *args], data=data, timeout=timeout).stdout


@dataclass(frozen=True)
class Host:
    system: str
    tinyproxy: str
    stunnel: str
    curl: str
    ca: Path
    proxy_user: str
    tls_user: str

    @classmethod
    def discover(cls):
        system = platform.system()
        release = Path("/etc/os-release").read_text() if system == "Linux" else ""
        platform_name(system, release)
        dirs = SAFE_PATH.split(":")
        if system == "Linux":
            users = ("outbound-proxy", "outbound-tls")
            ca = Path("/etc/ssl/certs/ca-certificates.crt")
        else:
            dirs = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/usr/local/sbin"]
            users = ("_outboundproxy", "_outboundtls")
            ca = Path("/etc/ssl/cert.pem")
        trusted(ca)
        if not ca.is_file() or not ca.stat().st_size:
            raise RuntimeError("System CA bundle is unavailable")
        return cls(system, executable("tinyproxy", dirs), executable("stunnel", dirs),
                   privileged("curl"), ca, *users)

    @property
    def package(self):
        if self.system == "Linux":
            return Path("/usr/local/libexec/outbound-proxy")
        return Path("/Library/PrivilegedHelperTools/outbound-proxy")

    @property
    def runtime(self):
        if self.system == "Linux":
            return Path("/var/lib/outbound-proxy/config")
        return Path("/private/var/db/outbound-proxy/config")


def replace_file(path, data, mode=0o644):
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Expected regular service definition: {path}")
        if (info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == mode
                and path.read_bytes() == data):
            return False
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return True


class Manager:
    def __init__(self, host, runtime):
        self.host = host
        self.jobs = runtime.parent / "jobs"
        self.linux = host.system == "Linux"

    def ctl(self, *args, check=True):
        program = "systemctl" if self.linux else "launchctl"
        return execute([privileged(program), *args], check=check)

    def boot(self, enabled):
        if self.linux:
            self.ctl("enable" if enabled else "disable", "outbound-proxy-boot.service")
        else:
            self.ctl("enable" if enabled else "disable", "system/" + LABEL + "boot")

    def running(self, role):
        if self.linux:
            unit = "outbound-proxy-stunnel.socket" if role == "stunnel" else "outbound-proxy-tinyproxy.service"
            result = self.ctl("show", "--property=ActiveState", "--value", unit)
            state = result.stdout.strip()
            if result.returncode or not state:
                raise RuntimeError("Cannot inspect systemd service state")
            return state == "active"
        result = self.ctl("print", "system/" + LABEL + role, check=False)
        if result.returncode == 113:
            return False
        if result.returncode:
            raise RuntimeError("Cannot inspect launchd service state")
        if role == "stunnel":
            sockets = execute([privileged("lsof"), "-nP", "-a", "-p", "1",
                               f"-iTCP:{TUNNEL_PORT}", "-sTCP:LISTEN", "-Fn"], check=False)
            if sockets.returncode not in (0, 1) or sockets.stderr.strip():
                raise RuntimeError("Cannot inspect launchd transport socket")
            return f"n127.0.0.1:{TUNNEL_PORT}" in sockets.stdout.splitlines()
        return re.search(r"^\s*state = running$", result.stdout, re.M) is not None

    def boot_enabled(self):
        if self.linux:
            result = self.ctl("is-enabled", "outbound-proxy-boot.service", check=False)
            state = result.stdout.strip()
            if state not in ("enabled", "disabled", "masked") or (state == "enabled" and result.returncode):
                raise RuntimeError("Cannot inspect systemd boot registration")
            return state == "enabled"
        status = self.ctl("print-disabled", "system").stdout
        return re.search(r'"local\.outbound-proxy\.boot"\s*=>\s*(?:enabled|false)', status) is not None

    def unload(self, role):
        label = "system/" + LABEL + role
        result = self.ctl("print", label, check=False)
        if result.returncode == 0:
            self.ctl("bootout", label)
        elif result.returncode != 113:  # launchctl's "Could not find service" status
            raise RuntimeError("Cannot inspect launchd registration")

    def stop_pair(self):
        if self.linux:
            self.ctl("stop", "outbound-proxy-tinyproxy.service", "outbound-proxy-stunnel.socket")
        else:
            for role in ("tinyproxy", "stunnel"):
                self.unload(role)

    def start_pair(self):
        for role in ("stunnel", "tinyproxy"):
            if self.linux:
                unit = "outbound-proxy-stunnel.socket" if role == "stunnel" else "outbound-proxy-tinyproxy.service"
                self.ctl("start", unit)
            else:
                self.ctl("bootstrap", "system", str(self.jobs / (LABEL + role + ".plist")))
            if role == "stunnel" and not self.running("stunnel"):
                raise RuntimeError("Native manager did not acquire the private transport socket")

    def schedule(self, enabled, interval):
        if self.linux:
            unit = "outbound-proxy-tick.timer"
            if not enabled:
                self.ctl("stop", unit)
                return
            template = (TEMPLATES / "tick.timer.in").read_text()
            changed = replace_file(SYSTEMD / unit, template.replace("@INTERVAL@", str(interval)).encode())
            if changed:
                self.ctl("daemon-reload")
            self.ctl("restart" if changed else "start", unit)
        else:
            role = "tick"
            if not enabled:
                self.unload(role)
                return
            path = self.jobs / (LABEL + role + ".plist")
            body = launch_job(role, [str(self.host.package / "outbound-proxy"), "tick"],
                              StartInterval=interval)
            changed = replace_file(path, body)
            if changed:
                self.unload(role)
            if self.ctl("print", "system/" + LABEL + role, check=False).returncode:
                self.ctl("bootstrap", "system", str(path))


def launch_job(role, command, **fields):
    job = plistlib.loads((TEMPLATES / "job.plist").read_bytes())
    if "inetdCompatibility" in fields:
        del job["StandardOutPath"]  # stunnel writes the accepted socket through stdout.
    job.update(Label=LABEL + role, ProgramArguments=command, **fields)
    return plistlib.dumps(job)


class Native:
    def __init__(self, root, *, host=None, runtime=None, target="https://example.com/"):
        self.root = Path(root)
        self.host = host or Host.discover()
        self.runtime = Path(runtime) if runtime is not None else self.host.runtime
        self.target = target
        self.manager = Manager(self.host, self.runtime)
        self.access = Access(self.root, system_command)
        self.docker_supported = self.host.system == "Linux"

    def probe(self, port):
        result = execute([self.host.curl, "-q", "--silent", "--noproxy", "", "--max-time", "15",
                          "--proxy", f"http://127.0.0.1:{port}", "--cacert", str(self.host.ca),
                          "--output", "/dev/null", "--write-out", "%{http_connect} %{http_code}",
                          self.target])
        if result.stdout != "200 200":
            raise RuntimeError("Forwarding probe failed")

    def pair(self, directory, uri, port, tunnel_port, docker):
        front = pwd.getpwnam(self.host.proxy_user)
        group = grp.getgrgid(front.pw_gid).gr_name
        identities = (front, pwd.getpwnam(self.host.tls_user))
        if any(account.pw_uid == 0 or account.pw_gid == 0 for account in identities):
            raise RuntimeError("Forwarders require unprivileged service identities")
        addresses = (LOOPBACK, GATEWAY) if docker else (LOOPBACK,)
        allowed = (LOOPBACK, SUBNET) if docker else (LOOPBACK,)
        pair = legacy.render_pair(legacy.parse_upstream(uri), addresses, allowed, port, tunnel_port,
                                  user=front.pw_name, group=group, ca=self.host.ca, inetd=True)
        legacy.write_pair(directory, pair)
        for role, account in zip(("tinyproxy", "stunnel"), identities):
            path = directory / (role + ".conf")
            if os.geteuid() == 0:
                os.chown(path, 0, account.pw_gid)
            path.chmod(0o440)
        directory.chmod(0o711)
        return identities

    def commands(self, directory):
        return ([self.host.tinyproxy, "-d", "-c", str(directory / "tinyproxy.conf")],
                [self.host.stunnel, str(directory / "stunnel.conf")])

    def check(self, uri):
        with tempfile.TemporaryDirectory(dir=self.runtime) as name:
            directory = Path(name)
            # Retain the actual listening socket until Tinyproxy exits. No other process can
            # acquire the private upstream port, even if stunnel fails to load the fixture.
            with socket.socket() as tunnel, contextlib.ExitStack() as stack:
                tunnel.bind((LOOPBACK, 0))
                tunnel.listen(1)
                tunnel.settimeout(5)
                with socket.socket() as proxy:
                    proxy.bind((LOOPBACK, 0))
                    port = proxy.getsockname()[1]
                    identities = self.pair(directory, uri, port, tunnel.getsockname()[1], False)
                front, tls = self.commands(directory)
                tiny = child(front, identities[0])
                stack.callback(stop_child, tiny)
                legacy.wait_for_listener(port)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    probe = pool.submit(self.probe, port)
                    try:
                        connection, _ = tunnel.accept()
                    except TimeoutError:
                        raise RuntimeError("Candidate did not connect to the private transport") from None
                    with connection:
                        backend = child(tls, identities[1], connection)
                        stack.callback(stop_child, backend)
                        # Release the parent's duplicate so curl sees EOF if stunnel exits early.
                        connection.close()
                        probe.result()
                if tiny.poll() is not None:
                    raise RuntimeError("Candidate forwarder exited")

    def publish(self, state):
        uri = next(item["uri"] for item in state["upstreams"] if item["name"] == state["selected"])
        directory = Path(tempfile.mkdtemp(dir=self.runtime, prefix="generation-"))
        self.pair(directory, uri, state["port"], TUNNEL_PORT, state["docker"])
        legacy.select_generation(self.runtime / "active", directory.name)
        return directory

    def verify_pair(self, port):
        legacy.wait_for_listener(port)
        if not self.health()["running"]:
            raise RuntimeError("Native forwarding pair did not start")

    def apply(self, state):
        if state["docker"] and not self.docker_supported:
            raise RuntimeError("Docker access requires Linux")
        active = self.runtime / "active"
        previous = active.resolve() if active.is_symlink() else None
        current = None
        self.manager.stop_pair()
        if state["enabled"] and state["docker"]:
            self.access.prepare(state["port"])
        else:
            self.access.withdraw()
        if state["enabled"]:
            current = self.publish(state)
            self.manager.start_pair()
            self.verify_pair(state["port"])
            self.probe(state["port"])
        self.manager.schedule(state["enabled"] and state["failover"], state["interval"])
        self.manager.boot(state["enabled"])
        if current:
            for directory in self.runtime.glob("generation-*"):
                if directory not in (current, previous):
                    shutil.rmtree(directory)

    def schedule(self, enabled, interval):
        self.manager.schedule(enabled, interval)

    def health(self):
        running = all(self.manager.running(role) for role in ("tinyproxy", "stunnel"))
        return {"running": running, "boot_enabled": self.manager.boot_enabled()}


def child(command, identity, stream=None):
    credentials = {}
    if os.geteuid() == 0:
        credentials = dict(user=identity.pw_uid, group=identity.pw_gid, extra_groups=[])
    elif identity.pw_uid != os.geteuid():
        raise RuntimeError("Candidate probe requires service account access")
    io = stream if stream is not None else subprocess.DEVNULL
    return subprocess.Popen(command, stdin=io, stdout=io, stderr=subprocess.DEVNULL,
                            env=ENV, cwd="/", **credentials)


def stop_child(process):
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
