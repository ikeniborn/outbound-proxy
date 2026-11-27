"""Remove owned standalone proxy jobs and programs while keeping operator data."""

import errno
import hashlib
import os
from pathlib import Path
import platform
import plistlib
import re
import stat
import sys

from . import native
from .docker_access import Access
from .store import OperationError, Store, private, read_json, sync_dir, write_json

SERVICE = Path(__file__).resolve().parents[2] / "scripts/native"
ROLES = ("panel", "tick", "recover", "boot", "tinyproxy", "stunnel")
SYSTEMD_UNITS = ("panel.service", "tick.timer", "recover.service", "boot.service", "tick.service",
                 "tinyproxy.service", "stunnel.socket", "stunnel@.service")
RELEASE = re.compile(r"tmp[a-z0-9_]{8}")
MANIFEST = "installed-files"
PROC = Path("/proc")
# This release layout predates installation receipts.
PRIOR_RELEASE = (
    "outbound-proxy", "minipc_proxy_apply.py", "native/job.plist", "native/operator.service.in",
    "native/pair.service.in", "native/tick.timer.in", "native/transport.socket",
    *(f"outbound_proxy/{module}.py" for module in ("__init__", "activation", "cli", "docker_access",
      "installer", "migrate", "native", "operations", "store", "ui")),
    "outbound_proxy/web/index.html", "outbound_proxy/web/app.js", "outbound_proxy/web/style.css",
)


def manifest(root):
    try:
        return read_json(root / MANIFEST)
    except FileNotFoundError:
        return {"definitions": {}, "releases": {}}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def record_install(root, host, files, release):
    """Record the new release before publishing its native definitions."""
    inventory = manifest(root)
    if release.parent != (host.package / "releases").resolve() or not RELEASE.fullmatch(release.name):
        raise OperationError("Unexpected installed release path")
    inventory["releases"][release.name] = [
        str(path.relative_to(release)) for path in release.rglob("*") if path.is_file()
    ]
    for path, data in files.items():
        hashes = inventory["definitions"].setdefault(str(path), [])
        if digest(data) not in hashes:
            hashes.append(digest(data))
    write_json(root / MANIFEST, inventory)


def owns_definition(root, path, data):
    return digest(data) in manifest(root)["definitions"].get(str(path), [])


def matches_job(data, role, arguments, **options):
    expected = plistlib.loads((SERVICE / "job.plist").read_bytes())
    if "inetdCompatibility" in options:
        expected.pop("StandardOutPath")
    expected.update(Label=native.LABEL + role, ProgramArguments=arguments, **options)
    return plistlib.loads(data) == expected


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise OperationError("Unexpected ownership or type of an installed file")
    return path.read_bytes()


def safe_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise OperationError("Unexpected installed directory")


def unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        return
    sync_dir(path.parent)


def empty_directory(path):
    try:
        path.rmdir()
        sync_dir(path.parent)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise


def remove_known(directory, names):
    """Unlink only recorded paths. Leave operator additions in place."""
    if not directory.exists():
        return
    safe_directory(directory)
    parents = set()
    for name in names:
        tail = Path(name)
        if tail.is_absolute() or not tail.parts or ".." in tail.parts:
            raise OperationError("Invalid installed file record")
        path = directory
        for part in tail.parts[:-1]:
            path = path / part
            if path.exists():
                safe_directory(path)
            parents.add(path)
        path = directory / tail
        unlink(path)
        cache = path.parent / "__pycache__"
        if path.suffix == ".py" and cache.exists():
            safe_directory(cache)
            for compiled in cache.glob(path.stem + ".*.pyc"):
                if re.fullmatch(re.escape(path.stem) + r"\.(?:cpython|pypy)-[\w-]+(?:\.opt-[12])?\.pyc",
                                compiled.name):
                    unlink(compiled)
            parents.add(cache)
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        empty_directory(parent)
    empty_directory(directory)


class Removal:
    def __init__(self, store, host, registrations=None, command_file=None):
        self.store = store
        self.host = host
        self.linux = host.system == "Linux"
        self.registration = registrations or (native.SYSTEMD if self.linux else native.DAEMONS)
        self.command_file = command_file or Path("/usr/local/bin/outbound-proxy" if self.linux
                                                 else "/etc/paths.d/outbound-proxy")
        self.manager = native.Manager(host, host.runtime)
        self.access = Access(store.root, native.system_command)
        self.inventory = None

    def definitions(self):
        if self.linux:
            return [self.registration / ("outbound-proxy-" + unit) for unit in SYSTEMD_UNITS]
        return [(self.registration if role in ("panel", "recover", "boot") else self.manager.jobs)
                / (native.LABEL + role + ".plist") for role in ROLES]

    def scheduled_file(self, path, data):
        if self.linux and path.name == "outbound-proxy-tick.timer":
            pattern = re.escape((SERVICE / "tick.timer.in").read_bytes())
            before, between, after = pattern.split(b"@INTERVAL@")
            return bool(re.fullmatch(before + rb"([1-9][0-9]*)" + between + rb"\1" + after, data))
        if not self.linux and path.name == native.LABEL + "tick.plist":
            job = plistlib.loads(data)
            interval = job.get("StartInterval")
            return (type(interval) is int and interval > 0 and
                    matches_job(data, "tick", [str(self.host.package / "outbound-proxy"), "tick"],
                                StartInterval=interval))
        return False

    def inventory_prior_install(self):
        """Recognize releases from the installer that predates ownership receipts."""
        releases = self.host.package / "releases"
        if not releases.exists():
            return
        safe_directory(releases)
        for release in releases.iterdir():
            if release.name in self.inventory["releases"] or not RELEASE.fullmatch(release.name):
                continue
            safe_directory(release)
            if all((release / name).is_file() for name in ("outbound-proxy", "minipc_proxy_apply.py",
                                                            "outbound_proxy/installer.py", "native/job.plist")):
                self.inventory["releases"][release.name] = list(PRIOR_RELEASE)

    def prior_definition(self, path, data):
        if not self.inventory["releases"]:
            return False
        launcher = str(self.host.package / "outbound-proxy")
        config = self.host.runtime / "active"
        if not self.linux:
            job = plistlib.loads(data)
            role = path.name.removeprefix(native.LABEL).removesuffix(".plist")
            args = job.get("ProgramArguments", [])
            if role in ("tinyproxy", "stunnel"):
                if not args or not Path(args[0]).is_absolute() or Path(args[0]).name != role:
                    return False
                if role == "tinyproxy":
                    return matches_job(data, role, [args[0], "-d", "-c", str(config / "tinyproxy.conf")],
                                       UserName="_outboundproxy", GroupName="_outboundproxy",
                                       RunAtLoad=True, KeepAlive=True)
                listener = {"SockNodeName": "127.0.0.1", "SockServiceName": "18889",
                            "SockType": "stream", "SockFamily": "IPv4"}
                return matches_job(data, role, [args[0], str(config / "stunnel.conf")],
                                   UserName="_outboundtls", GroupName="_outboundtls",
                                   inetdCompatibility={"Wait": False}, Sockets={"Listener": listener})
            if role in ("boot", "recover"):
                options = {"RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 15}
                if role == "boot":
                    options["Disabled"] = True
                return matches_job(data, role, [launcher, role], **options)
            return False
        unit = path.name.removeprefix("outbound-proxy-")
        if unit == "stunnel.socket":
            return data == (SERVICE / "transport.socket").read_bytes()
        action = unit.removesuffix(".service")
        if action in ("boot", "tick", "recover"):
            body = (SERVICE / "operator.service.in").read_text()
            retry = "" if action == "tick" else "Restart=on-failure\nRestartSec=15s"
            return data == body.replace("@ACTION@", action).replace("@LAUNCHER@", launcher).replace("@RETRY@", retry).encode()
        if action in ("tinyproxy", "stunnel@"):
            role = action.rstrip("@")
            # Staged legacy installs lack receipts. Check the complete generated unit.
            user = "outbound-proxy" if role == "tinyproxy" else "outbound-tls"
            command = re.search(rb"(?m)^ExecStart=(/[^\s]+) ", data)
            if not command:
                return False
            executable = os.fsdecode(command.group(1))
            if Path(executable).name != role:
                return False
            relation = ("BindsTo=outbound-proxy-stunnel.socket\nAfter=outbound-proxy-stunnel.socket"
                        if role == "tinyproxy" else "PartOf=outbound-proxy-stunnel.socket")
            io = ("StandardOutput=null\nRestart=on-failure\nRestartSec=2" if role == "tinyproxy"
                  else "StandardInput=socket\nStandardOutput=socket")
            args = [executable, "-d", "-c"] if role == "tinyproxy" else [executable]
            args.append(str(config / (role + ".conf")))
            template = (SERVICE / "pair.service.in").read_text()
            for key, value in (("ROLE", role), ("USER", user), ("GROUP", user), ("COMMAND", " ".join(args)),
                               ("RELATION", relation), ("IO", io)):
                template = template.replace("@" + key + "@", value)
            return data == template.encode()
        return False

    def validate_paths(self):
        current = self.host.package / "current"
        for path in (self.host.package, self.host.runtime.parent, self.host.runtime, self.manager.jobs):
            if path.exists() or path.is_symlink():
                safe_directory(path)
        if current.exists() or current.is_symlink():
            if (not current.is_symlink() or current.readlink().parent != Path("releases")
                    or current.readlink().name not in self.inventory["releases"]):
                raise OperationError("Unexpected installed release pointer")
        links = [(self.host.package / "outbound-proxy", Path("current/outbound-proxy"))]
        if self.linux:
            links.append((self.command_file, self.host.package / "outbound-proxy"))
        elif self.command_file.exists() or self.command_file.is_symlink():
            if regular(self.command_file) != (str(self.host.package) + "\n").encode():
                raise OperationError("Foreign command search path")
        for path, target in links:
            if (path.exists() or path.is_symlink()) and (not path.is_symlink() or path.readlink() != target):
                raise OperationError("Foreign command or package alias")
        for path in self.definitions():
            if not (path.exists() or path.is_symlink()):
                continue
            data = regular(path)
            recorded = self.inventory["definitions"].get(str(path))
            if digest(data) not in (recorded or []):
                scheduled = self.inventory["releases"] and self.scheduled_file(path, data)
                if not scheduled and (recorded is not None or not self.prior_definition(path, data)):
                    raise OperationError("A native definition belongs to another installation")
            self.inventory["definitions"][str(path)] = [digest(data)]

    def systemd_job(self, unit, expected):
        result = self.manager.ctl("show", "-p", "LoadState", "-p", "FragmentPath", "-p", "DropInPaths",
                                  "-p", "ActiveState", unit, check=False)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if fields.get("LoadState") == "not-found" and fields.get("ActiveState") == "inactive":
            return False
        if (result.returncode or fields.get("FragmentPath") != str(expected) or fields.get("DropInPaths")
                or str(expected) not in self.inventory["definitions"]):
            raise OperationError("Cannot verify ownership of a systemd job")
        return True

    def loaded(self):
        jobs = []
        if self.linux:
            for path in self.definitions():
                if "@" not in path.name and self.systemd_job(path.name, path):
                    jobs.append(path.name)
            template = self.registration / "outbound-proxy-stunnel@.service"
            output = self.manager.ctl("list-units", "--all", "--no-legend", "--plain", "--no-pager",
                                      "outbound-proxy-stunnel@*.service").stdout
            for line in output.splitlines():
                unit = line.split()[0]
                if not re.fullmatch(r"outbound-proxy-stunnel@[^\s/]+\.service", unit):
                    raise OperationError("Unexpected transport instance")
                if self.systemd_job(unit, template):
                    jobs.append(unit)
        else:
            for path in self.definitions():
                label = "system/" + path.stem
                result = self.manager.ctl("print", label, check=False)
                if result.returncode == 113:
                    continue
                match = re.search(r"(?m)^\s*path = (.+)$", result.stdout)
                if (result.returncode or not match or Path(match[1]).resolve() != path.resolve()
                        or str(path) not in self.inventory["definitions"]):
                    raise OperationError("Cannot verify ownership of a launchd job")
                jobs.append(label)
        return jobs

    def quiesce(self, jobs):
        if not self.linux:
            labels = []
            for role in ("panel", "recover", "boot"):
                label = native.LABEL + role
                if str(self.registration / (label + ".plist")) in self.inventory["definitions"]:
                    labels.append(label)
                    self.manager.ctl("disable", "system/" + label)
            if labels:
                overrides = self.manager.ctl("print-disabled", "system").stdout
                if not all(re.search(r'"' + re.escape(label) + r'"\s*=>\s*(?:disabled|true)\b', overrides)
                           for label in labels):
                    raise OperationError("Cannot verify launchd startup was disabled")
        for job in jobs:
            if self.linux:
                if "@" not in job and (self.registration / job).exists():
                    self.manager.ctl("disable", job)
                self.manager.ctl("stop", job)
            else:
                self.manager.ctl("bootout", job)

    def check_listeners(self, port):
        # Enumerate all addresses without connecting to an unrelated or legacy listener.
        ports = {18889, 18890, port}
        if self.linux:
            inodes = set()
            proc = PROC
            for name in ("tcp", "tcp6"):
                table = proc / "net" / name
                if name == "tcp6" and not table.exists():
                    continue
                for line in table.read_text().splitlines()[1:]:
                    row = line.split()
                    if row[3] == "0A" and int(row[1].rsplit(":", 1)[1], 16) in ports:
                        inodes.add("socket:[" + row[9] + "]")
            if not inodes:
                return
            for process in proc.iterdir():
                if not process.name.isdecimal():
                    continue
                try:
                    fds = (process / "fd").iterdir()
                    for descriptor in fds:
                        try:
                            target = os.readlink(descriptor)
                        except FileNotFoundError:
                            continue
                        if target in inodes:
                            command = (process / "cmdline").read_bytes().replace(b"\0", b" ")
                            cgroup = (process / "cgroup").read_bytes()
                            owned_command = any(os.fsencode(p) in command for p in (self.host.runtime, self.host.package))
                            owned_unit = re.search(rb"/outbound-proxy-[^/]+\.service(?:/|$)", cgroup, re.M)
                            if owned_command or owned_unit:
                                raise OperationError("Standalone proxy listener is still active")
                            break
                except (FileNotFoundError, ProcessLookupError):
                    continue
        else:
            for number in ports:
                result = native.execute([native.privileged("lsof"), "-nP", "-iTCP:" + str(number),
                                         "-sTCP:LISTEN", "-Fp"], check=False)
                if result.returncode == 1 and not result.stderr.strip() and not result.stdout.strip():
                    continue
                if result.returncode or result.stderr.strip():
                    raise OperationError("Cannot inspect remaining listeners")
                for pid in re.findall(r"(?m)^p(\d+)$", result.stdout):
                    command = native.system_command("ps", "-p", pid, "-o", "command=")
                    if any(str(prefix) in command for prefix in (self.host.runtime, self.host.package)):
                        raise OperationError("Standalone proxy listener is still active")

    def remove_program(self):
        for name, files in self.inventory["releases"].items():
            if not RELEASE.fullmatch(name):
                raise OperationError("Invalid release in installation receipt")
            remove_known(self.host.package / "releases" / name, files)
        for path in (self.command_file, self.host.package / "current", self.host.package / "outbound-proxy"):
            unlink(path)
        empty_directory(self.host.package / "releases")
        empty_directory(self.host.package)
        if self.host.runtime.exists():
            for directory in self.host.runtime.iterdir():
                if re.fullmatch(r"(?:generation-|tmp)[a-z0-9_]{8}", directory.name):
                    remove_known(directory, ["tinyproxy.conf", "stunnel.conf"])
            active = self.host.runtime / "active"
            if active.is_symlink() and re.fullmatch(r"generation-[a-z0-9_]{8}", str(active.readlink())):
                unlink(active)
            empty_directory(self.host.runtime)
        empty_directory(self.manager.jobs)
        empty_directory(self.host.runtime.parent)

    def run(self):
        root = self.store.root
        with self.store.lifecycle_lock():
            with self.store.locked(maintenance=True):
                backup = root / "legacy-backup"
                if backup.exists() or backup.is_symlink():
                    private(backup.lstat(), directory=True)
                    try:
                        phase = read_json(backup / "handover.json").get("phase")
                    except FileNotFoundError:
                        phase = None
                    if phase not in ("accepted", "restored"):
                        raise OperationError("Resolve handover with outbound-proxy migrate inspect or rollback")
                self.inventory = manifest(root)
                self.inventory_prior_install()
                self.validate_paths()
                jobs = self.loaded()
                state = self.store.read(self.store.current(), allow_panel_port=True)
                reset_port = state["port"] == 18890
                write_json(root / MANIFEST, self.inventory)
                write_json(root / "removal", "removing")
            # Management workers have drained. Keep the state lock free while
            # waiting for native stop jobs whose CLI may still be waiting for it.
            self.quiesce(jobs)
            self.access.withdraw()
            for path in self.definitions():
                if path.exists() or path.is_symlink():
                    expected = self.inventory["definitions"].get(str(path), [])
                    if digest(regular(path)) not in expected:
                        raise OperationError("Native definition changed during removal")
                    unlink(path)
            if self.linux:
                wants = self.registration / "multi-user.target.wants"
                if wants.exists():
                    safe_directory(wants)
                    for role in ("panel", "recover", "boot"):
                        path = self.registration / f"outbound-proxy-{role}.service"
                        link = wants / path.name
                        if (str(path) in self.inventory["definitions"] and link.is_symlink()
                                and link.resolve() == path.resolve()):
                            unlink(link)
                self.manager.ctl("daemon-reload")
            if self.loaded():
                raise OperationError("Standalone jobs remain registered")
            self.check_listeners(state["port"])
            with self.store.locked(maintenance=True):
                if reset_port:
                    state["port"] = 18888
                if state["enabled"] or reset_port:
                    state["enabled"] = False
                    self.store.commit(self.store.stage(state))
                if self.store.pending() is not None:
                    self.store.clear_pending()
                self.store.prune(self.store.current())
                pairing = root / "panel-pairing"
                if pairing.exists() or pairing.is_symlink():
                    private(pairing.lstat(), directory=True)
                    remove_known(pairing, ["records.json", *(p.name for p in pairing.glob(".write-*"))])
                self.remove_program()
                write_json(root / "removal", "removed")
                (root / MANIFEST).unlink(missing_ok=True)
                sync_dir(root)
            return reset_port


def main():
    if os.geteuid() or len(sys.argv) != 1:
        raise SystemExit("Run the removal script as root without arguments")
    os.umask(0o077)
    try:
        system = platform.system()
        release = Path("/etc/os-release").read_text() if system == "Linux" else ""
        native.platform_name(system, release)
        # Removing a broken installation must work without its forwarder binaries.
        host = native.Host(system, "", "", "", Path("/"), "", "")
        removal = Removal(Store(Path("/etc/outbound-proxy")), host)
        for path in (removal.store.root, host.package, host.runtime, removal.registration, removal.command_file.parent):
            while not path.exists():
                path = path.parent
            native.trusted(path)
        reset_port = removal.run()
    except OperationError as error:
        raise SystemExit(str(error)) from None
    except Exception:
        raise SystemExit("Standalone removal incomplete. Check native services and retry this script") from None
    if reset_port:
        print("Reset the saved forwarding port from 18890 (reserved for the panel) to 18888.")
    print("Removed standalone proxy services. Saved upstreams and MiniPC backups are retained.")
