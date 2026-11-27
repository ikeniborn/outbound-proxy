"""Install privileged operator files and register the standalone service definitions."""

import argparse
import grp
import http.client
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import minipc_proxy_apply as legacy
from . import native
from .store import PANEL_PORT, Store
from .native import DAEMONS, LABEL, SYSTEMD, Host, Native, privileged, replace_file, system_command, trusted


def directory(path, mode=0o755):
    if not path.exists():
        directory(path.parent)
        path.mkdir(mode=mode)
        path.chmod(mode)
    trusted(path)
    if not path.is_dir() or not os.access(path, os.W_OK):
        raise RuntimeError(f"Cannot install in directory: {path}")


def stage(package, repository):
    launcher = package / "outbound-proxy"
    expected = Path("current/outbound-proxy")
    check_alias(launcher, expected)
    releases = package / "releases"
    current = package / "current"
    if package.is_symlink() or releases.is_symlink():
        raise RuntimeError("Unexpected package directory alias")
    if os.path.lexists(current):
        from .uninstall import RELEASE
        target = current.readlink() if current.is_symlink() else None
        if (target is None or target.parent != Path("releases") or not RELEASE.fullmatch(target.name)
                or (package / target).is_symlink() or not (package / target / "outbound-proxy").is_file()):
            raise RuntimeError("Current release pointer belongs to another installation")
    releases.mkdir(mode=0o755, exist_ok=True)
    new = Path(tempfile.mkdtemp(dir=releases))
    scripts = repository / "scripts"
    for source in (scripts / "outbound-proxy", scripts / "minipc_proxy_apply.py"):
        shutil.copyfile(source, new / source.name)
    shutil.copytree(scripts / "outbound_proxy", new / "outbound_proxy",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(repository / "scripts/native", new / "native")
    # copytree preserves source mode bits; the installed copy has uniform administrative modes.
    for path in (new, *new.rglob("*")):
        mode = 0o755 if path.is_dir() or path == new / "outbound-proxy" else 0o644
        path.chmod(mode)
    legacy.select_generation(package / "current", str(new.relative_to(package)))
    alias(launcher, expected)


def check_legacy(root, run, staging):
    if staging:
        return
    records = run("systemctl", "list-unit-files", "--no-legend", "--no-pager")
    if any(line.startswith("minipc-proxy-") for line in records.splitlines()):
        handover = root / "legacy-backup/handover.json"
        if handover.exists() and json.loads(handover.read_text()).get("phase") == "accepted":
            return
        raise RuntimeError("Existing MiniPC service requires migration. Use --stage to prepare the package")


def group(user):
    return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name


def mac_identity(name):
    dscl = privileged("dscl")

    def ds(*args):
        return native.execute([dscl, ".", *args]).stdout

    groups = dict(line.rsplit(None, 1) for line in ds("-list", "/Groups", "PrimaryGroupID").splitlines())
    users = dict(line.rsplit(None, 1) for line in ds("-list", "/Users", "UniqueID").splitlines())
    if name not in groups:
        occupied = set(groups.values()) | set(users.values())
        number = next((str(n) for n in range(450, 500) if str(n) not in occupied), None)
        if number is None:
            raise RuntimeError("No available local system identity number")
        ds("-create", "/Groups/" + name)
        ds("-create", "/Groups/" + name, "PrimaryGroupID", number)
        groups[name] = number
    if name not in users:
        number = groups[name]
        if number in users.values():
            raise RuntimeError("Service uid is already assigned")
        record = "/Users/" + name
        ds("-create", record)
        for key, value in (("UniqueID", number), ("PrimaryGroupID", number),
                           ("UserShell", "/usr/bin/false"), ("NFSHomeDirectory", "/var/empty"),
                           ("IsHidden", "1"), ("Password", "*")):
            ds("-create", record, key, value)


def identity(name, linux):
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        if linux:
            system_command("useradd", "--system", "--user-group", "--no-create-home",
                           "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin", name)
        else:
            mac_identity(name)
        account = pwd.getpwnam(name)
    primary = grp.getgrgid(account.pw_gid)
    if (account.pw_uid == 0 or primary.gr_gid == 0 or primary.gr_name != name
            or primary.gr_mem not in ([], [name])
            or account.pw_shell not in ("/usr/sbin/nologin", "/bin/false", "/usr/bin/false")
            or any(user.pw_name != name and (user.pw_gid == account.pw_gid or user.pw_uid == account.pw_uid)
                   for user in pwd.getpwall())
            or any(name in entry.gr_mem and entry.gr_gid != primary.gr_gid for entry in grp.getgrall())):
        raise RuntimeError(f"Existing service account is not a restricted private identity: {name}")


def check_service_files(account, executable, ca):
    check = ("import os, ssl, sys\n"
             "trust = ssl.create_default_context(cafile=sys.argv[2])\n"
             "sys.exit(not(os.access(sys.argv[1], os.X_OK) and trust.get_ca_certs()))")
    process = native.child(["/usr/bin/python3", "-IS", "-c", check, executable, str(ca)], account)
    try:
        if process.wait(timeout=15):
            raise RuntimeError("Service account cannot execute its backend or read a usable trust bundle")
    finally:
        native.stop_child(process)


def definitions(service, templates):
    host = service.host
    launcher = str(host.package / "outbound-proxy")
    files = {}
    if host.system == "Linux":
        files[SYSTEMD / "outbound-proxy-stunnel.socket"] = (templates / "transport.socket").read_bytes()
    for role, user, command in zip(("tinyproxy", "stunnel"), (host.proxy_user, host.tls_user),
                                    service.commands(service.runtime / "active")):
        if host.system == "Linux":
            template = (templates / "pair.service.in").read_text()
            text = (template.replace("@ROLE@", role).replace("@USER@", user)
                    .replace("@GROUP@", group(user)).replace("@COMMAND@", " ".join(command)))
            if role == "tinyproxy":
                relation = "BindsTo=outbound-proxy-stunnel.socket\nAfter=outbound-proxy-stunnel.socket"
                io = "StandardOutput=null\nRestart=on-failure\nRestartSec=2"
                unit = "outbound-proxy-tinyproxy.service"
            else:
                relation = "PartOf=outbound-proxy-stunnel.socket"
                io = "StandardInput=socket\nStandardOutput=socket"
                unit = "outbound-proxy-stunnel@.service"
            files[SYSTEMD / unit] = text.replace("@RELATION@", relation).replace("@IO@", io).encode()
        else:
            if role == "tinyproxy":
                activation = {"RunAtLoad": True, "KeepAlive": True}
            else:
                activation = {"inetdCompatibility": {"Wait": False}, "Sockets": {"Listener": {
                    "SockNodeName": "127.0.0.1", "SockServiceName": str(native.TUNNEL_PORT),
                    "SockType": "stream", "SockFamily": "IPv4"}}}
            files[service.manager.jobs / (LABEL + role + ".plist")] = native.launch_job(
                role, command, UserName=user, GroupName=group(user), **activation)
    for action in ("boot", "tick", "recover"):
        if host.system == "Linux":
            retry = "" if action == "tick" else "Restart=on-failure\nRestartSec=15s"
            template = (templates / "operator.service.in").read_text()
            text = template.replace("@ACTION@", action).replace("@LAUNCHER@", launcher).replace("@RETRY@", retry)
            files[SYSTEMD / f"outbound-proxy-{action}.service"] = text.encode()
        elif action != "tick":
            options = {"RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 15}
            if action == "boot":
                options["Disabled"] = True
            files[DAEMONS / (LABEL + action + ".plist")] = native.launch_job(action, [launcher, action], **options)
    if host.system == "Linux":
        files[SYSTEMD / "outbound-proxy-panel.service"] = (templates / "panel.service.in").read_bytes().replace(
            b"@LAUNCHER@", launcher.encode())
        timer = SYSTEMD / "outbound-proxy-tick.timer"
        template = (templates / "tick.timer.in").read_bytes()
        interval = b"60"
        if timer.is_file() and not timer.is_symlink():
            match = re.search(rb"^OnActiveSec=([1-9][0-9]*)s$", timer.read_bytes(), re.M)
            if match:
                interval = match[1]
        files[timer] = template.replace(b"@INTERVAL@", interval)
    else:
        files[DAEMONS / (LABEL + "panel.plist")] = native.launch_job(
            "panel", [launcher, "panel-serve"], RunAtLoad=True, KeepAlive=True)
    return files


def check_alias(link, target):
    if link.is_symlink() and link.readlink() == target:
        return
    if link.exists() or link.is_symlink():
        raise RuntimeError(f"CLI path already belongs to another installation: {link}")


def check_path_entry(path, package):
    if not os.path.lexists(path):
        return
    from .uninstall import regular
    if regular(path) != (str(package) + "\n").encode():
        raise RuntimeError(f"Existing CLI path entry belongs to another installation: {path}")


def alias(link, target):
    check_alias(link, target)
    if not link.is_symlink():
        link.symlink_to(target)


def check_verifier():
    from .pairing import Rejected, verify
    try:
        trusted(Path("/usr/bin/openssl"))
    except FileNotFoundError:
        raise RuntimeError("Missing system OpenSSL executable: /usr/bin/openssl") from None
    # Public P-256 signature of the fixed self-test message.
    public = ("MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEXWWrcoh9clBF0VAmIH31bbxrzhQPH5zL"
              "ItTBRCZnmmAEu6h2MK11EsU1080DqIloHyIPrB08jJxc0tomy5j0Aw==")
    signature = ("MEUCIQCYl1PViC6vE4dUpdvSev1L2qBLRt9ONVhpDvEwsc+qXQIg"
                 "AyVTNkZ923Nt2C0ZLlCPXQLC/iFXPaMeuE6wbJvNmro=")
    try:
        verify(public, signature, "outbound-proxy verifier self-test")
    except (OSError, subprocess.SubprocessError, Rejected):
        raise RuntimeError("System OpenSSL cannot verify browser pairing signatures") from None


def check_saved_port(store):
    # Older installations could legitimately have saved 18890 while forwarding was off.
    ids = set()
    if (store.root / "current").exists():
        ids.add(store.current())
    pending = store.pending()
    if pending:
        ids.update((pending["previous"], pending["candidate"]))
    for identifier in ids:
        store.read(identifier)  # Includes reserved-port checks on both sides of recovery.


def check_legacy_port(root):
    config = root / "verified/tinyproxy.conf"
    if config.exists() and re.search(rf"^\s*Port\s+{PANEL_PORT}\s*(?:#.*)?$", config.read_text(), re.I | re.M):
        raise RuntimeError(f"Legacy forwarding uses reserved panel port {PANEL_PORT}")


def panel_pid(service):
    manager = service.manager
    if manager.linux:
        unit = "outbound-proxy-panel.service"
        result = manager.ctl("show", "-p", "LoadState", "-p", "FragmentPath", "-p", "DropInPaths", "-p", "MainPID", unit, check=False)
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if properties.get("DropInPaths"):
            raise RuntimeError("Native panel has unrecognized systemd overrides")
        if properties.get("LoadState") == "not-found":
            # An override can already exist on disk for an as-yet uninstalled base unit.
            roots = manager.ctl("show", "-p", "UnitPath", "--value").stdout.split()
            if not roots:
                raise RuntimeError("Cannot inspect systemd unit directories")
            for base in roots:
                for suffix in (unit, "outbound-proxy-.service", "outbound-.service", "service"):
                    if any((Path(base) / (suffix + ".d")).glob("*.conf")):
                        raise RuntimeError("Native panel has unrecognized systemd overrides")
            return None
        if result.returncode or properties.get("FragmentPath") != str(SYSTEMD / unit):
            raise RuntimeError("Cannot verify the registered systemd panel")
        pid = int(properties["MainPID"])
        if pid:
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            except FileNotFoundError:
                return None
            if argv[-2:] != [os.fsencode(service.host.package / "outbound-proxy"), b"panel-serve"]:
                raise RuntimeError("Native panel service runs an unexpected command")
        return pid or None
    result = manager.ctl("print", "system/" + LABEL + "panel", check=False)
    if result.returncode == 113:
        return None
    definition = DAEMONS / (LABEL + "panel.plist")
    if result.returncode or f"path = {definition}" not in (line.strip() for line in result.stdout.splitlines()):
        raise RuntimeError("Cannot verify the registered launchd panel")
    arguments = re.search(r"^\s*arguments = \{\n(.*?)^\s*\}", result.stdout, re.M | re.S)
    if not arguments or arguments[1].split() != [str(service.host.package / "outbound-proxy"), "panel-serve"]:
        raise RuntimeError("Native panel service runs an unexpected command")
    match = re.search(r"^\s*pid = (\d+)$", result.stdout, re.M)
    return int(match[1]) if match else None


def panel_socket(system, pid, port=PANEL_PORT):
    """Classify all TCP listeners on the port and attribute the loopback socket to the native job."""
    if system == "Linux":
        loopback = f"{int.from_bytes(socket.inet_aton('127.0.0.1'), sys.byteorder):08X}"
        inodes = set()
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            if table.name == "tcp6" and not table.exists():
                continue
            for row in table.read_text().splitlines()[1:]:
                fields = row.split()
                address, number = fields[1].split(":")
                if fields[3] != "0A" or int(number, 16) != port:
                    continue
                if address != loopback:
                    return "conflict"
                inodes.add(f"socket:[{fields[9]}]")
        if not inodes:
            return "free"
        if pid:
            try:
                for fd in Path(f"/proc/{pid}/fd").iterdir():
                    try:
                        inodes.discard(os.readlink(fd))
                    except FileNotFoundError:
                        continue
            except (FileNotFoundError, PermissionError):
                return "conflict"
        return "conflict" if inodes else "managed"
    result = native.execute([privileged("lsof"), "-nP", "-a", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"], check=False)
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return "free"
    if result.returncode or result.stderr:
        raise RuntimeError("Cannot inspect the panel listener")
    owners = [line for line in result.stdout.splitlines() if line.startswith("p")]
    endpoints = [line for line in result.stdout.splitlines() if line.startswith("n")]
    if owners == [f"p{pid}"] and endpoints == [f"n127.0.0.1:{port}"]:
        return "managed"
    return "conflict"


def require_panel_port(service):
    state = panel_socket(service.host.system, panel_pid(service))
    if state == "free":
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", PANEL_PORT))
                return
            except OSError:
                pass
    elif state == "managed":
        return
    raise RuntimeError(f"Another listener occupies browser panel port {PANEL_PORT}")


def start_panel(service):
    manager = service.manager
    if manager.linux:
        manager.ctl("enable", "outbound-proxy-panel.service")
        manager.ctl("restart", "outbound-proxy-panel.service")
    else:
        manager.unload("panel")
        manager.ctl("enable", "system/" + LABEL + "panel")
        manager.ctl("bootstrap", "system", str(DAEMONS / (LABEL + "panel.plist")))
    for _ in range(40):
        pid = panel_pid(service)
        owner = panel_socket(service.host.system, pid)
        if owner == "conflict":
            raise RuntimeError(f"Unexpected listener on browser panel port {PANEL_PORT}")
        if owner == "managed":
            connection = http.client.HTTPConnection("127.0.0.1", PANEL_PORT, timeout=1)
            try:
                connection.request("GET", "/")
                if connection.getresponse().status == 200:
                    if panel_pid(service) != pid or panel_socket(service.host.system, pid) != "managed":
                        raise RuntimeError("Native panel listener changed during the readiness check")
                    return
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
        time.sleep(0.125)
    raise RuntimeError("The registered browser panel did not become ready")


def validate_registrations(root, files):
    from .uninstall import owns_definition
    for path, expected in files.items():
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"Unsafe native registration: {path}")
        current = path.read_bytes()
        if current != expected and not owns_definition(root, path, current):
            raise RuntimeError(f"Existing native registration belongs to another installation: {path}")


def install(staging):
    root = Path("/etc/outbound-proxy")
    host = Host.discover()
    service = Native(root, host=host)
    linux = host.system == "Linux"
    # Isolated Python skips user and global site initialization. Check its active library paths.
    trusted(Path(sys.executable))
    for library in sys.path[1:]:
        path = Path(library)
        trusted(path if path.exists() else path.parent)
    check_verifier()
    if linux:
        system_command("systemctl", "show", "--property=Version")
        check_legacy(root, system_command, staging)
        if staging:
            check_legacy_port(legacy.ROOT)
        privileged("useradd")
    else:
        system_command("launchctl", "print", "system")
        privileged("dscl")
        privileged("lsof")
    require_panel_port(service)
    directory(root, 0o700)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise RuntimeError("State directory requires a private root-owned directory")
    store = Store(root)
    with store.lifecycle_lock():
        with store.locked(maintenance=True):
            store.allow_install()
            check_saved_port(store)
            require_panel_port(service)
            if linux:
                directory(SYSTEMD)
                directory(Path("/usr/local/bin"))
                check_alias(Path("/usr/local/bin/outbound-proxy"), host.package / "outbound-proxy")
            else:
                directory(DAEMONS)
                directory(Path("/etc/paths.d"))
                check_path_entry(Path("/etc/paths.d/outbound-proxy"), host.package)
            directory(host.package)
            directory(host.package / "releases")
            directory(service.runtime.parent, 0o711)
            directory(service.runtime, 0o711)
            directory(service.manager.jobs)
            for user, program in ((host.proxy_user, host.tinyproxy), (host.tls_user, host.stunnel)):
                identity(user, linux)
                check_service_files(pwd.getpwnam(user), program, host.ca)
            repository = Path(__file__).resolve().parents[2]
            native.TEMPLATES = repository / "scripts/native"
            files = definitions(service, native.TEMPLATES)
            validate_registrations(root, files)
            stage(host.package, repository)
            from .uninstall import record_install
            record_install(root, host, files, (host.package / "current").resolve())
            for path, data in files.items():
                replace_file(path, data)
            if linux:
                alias(Path("/usr/local/bin/outbound-proxy"), host.package / "outbound-proxy")
                system_command("systemctl", "daemon-reload")
                system_command("systemctl", "enable", "outbound-proxy-recover.service")
            else:
                replace_file(Path("/etc/paths.d/outbound-proxy"), (str(host.package) + "\n").encode())
                service.manager.ctl("enable", "system/" + LABEL + "recover")
            store.finish_install()
        # The panel and pairing code take the operator lock themselves.
        start_panel(service)
        from .pairing import print_code
        print_code(root, required=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true", help="Prepare standalone files alongside the legacy service")
    options = parser.parse_args()
    try:
        os.umask(0o077)
        install(options.stage)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, RuntimeError) else "Check platform prerequisites and directory access"
        raise SystemExit(message) from None
    print(f"Installed outbound-proxy. Open http://127.0.0.1:{PANEL_PORT}/. Use outbound-proxy ui pair in an operator terminal for a pairing code.")
