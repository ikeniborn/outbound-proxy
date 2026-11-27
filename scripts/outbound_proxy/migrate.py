"""Import the MiniPC proxy and transfer its existing listeners to the new service."""
import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import minipc_proxy_apply as legacy
from .native import system_command

LEGACY = Path("/etc/minipc-proxy")
SYSTEM = Path("/etc/systemd/system")
OLD_RUN = Path("/run/minipc-proxy")
STATE = Path("/etc/outbound-proxy")
NETWORK = "minipc-proxy-network.service"
TLS, PROXY = legacy.UNITS
TIMER = "minipc-proxy-failover.timer"
WORKER = "minipc-proxy-failover.service"
UNITS = (NETWORK, TLS, PROXY, WORKER, TIMER)
DROPIN = "90-outbound-proxy-handover.conf"


def import_catalog(path, source, enabled, failover, interval):
    parsed = legacy.parse_upstreams(source)
    entries = source.strip().split(",")
    verified = (path / "verified").resolve(strict=True)
    if verified.parent != path.resolve():
        raise RuntimeError("Unexpected verified configuration location")
    pair = tuple((verified / name).read_text() for name in ("tinyproxy.conf", "stunnel.conf"))
    names = [f"proxy-{i + 1}" for i in range(len(entries))]
    selected = [name for name, upstream in zip(names, parsed) if legacy.production_pair(upstream) == pair]
    if len(selected) != 1:
        raise RuntimeError("Cannot uniquely identify the verified upstream")
    return {"upstreams": [{"name": n, "uri": u.strip()} for n, u in zip(names, entries)],
            "selected": selected[0], "enabled": enabled, "failover": failover,
            "docker": True, "port": legacy.PORT, "interval": interval}


def handover_state(source, staged, expected_selected=None):
    if staged["enabled"]:
        raise RuntimeError("Disable standalone forwarding before migration")
    target = source
    if staged["upstreams"]:
        named = {legacy.parse_upstream(item["uri"]): item["name"] for item in staged["upstreams"]}
        legacy_names = {legacy.parse_upstream(item["uri"]): item["name"] for item in source["upstreams"]}
        if named.keys() != legacy_names.keys():
            raise RuntimeError("Staged upstreams must match the complete legacy catalog and credentials")
        if any(staged[key] != source[key] for key in ("port", "docker", "interval", "failover")):
            raise RuntimeError("Staged forwarding settings differ from the legacy service")
        selected = next(named[key] for key, name in legacy_names.items() if name == source["selected"])
        if staged["selected"] != selected:
            raise RuntimeError("Select the staged upstream matching the current legacy route")
        target = dict(staged, enabled=True)
    if expected_selected is not None and target["selected"] != expected_selected:
        raise RuntimeError("Verified selection differs from the requested handover route")
    return target


def timer_interval(properties):
    match = re.search(r'OnUnitInactiveUSec=([^;}]+)', properties)
    if not match:
        raise ValueError("Cannot determine legacy timer interval")
    raw = match.group(1).strip()
    units = {"w": 604800, "d": 86400, "h": 3600, "min": 60, "s": 1}
    parts = re.findall(r'(\d+)(min|[wdhs])', raw)
    if not parts or "".join(a + b for a, b in parts) != raw.replace(" ", ""):
        raise ValueError("Legacy interval requires a whole number of seconds")
    seconds = sum(int(n) * units[u] for n, u in parts)
    if seconds < 1:
        raise ValueError("Invalid legacy interval")
    return seconds


def command(*args, input_text=None):
    return system_command(*args, data=input_text)


def prop(unit, key):
    return command("systemctl", "show", unit, "--property=" + key, "--value").strip()


def snapshot():
    settings = {}
    for unit in UNITS:
        if prop(unit, "LoadState") != "loaded":
            raise RuntimeError("Expected legacy unit is missing")
        enabled = prop(unit, "UnitFileState")
        active = prop(unit, "ActiveState")
        if enabled not in ("static", "enabled", "disabled") or active not in ("active", "inactive"):
            raise RuntimeError("Legacy unit requires operator review")
        settings[unit] = {"enabled": enabled, "active": active == "active"}
    if not (settings[PROXY]["active"] and settings[TLS]["active"] and settings[NETWORK]["active"]):
        raise RuntimeError("Expected serving legacy proxy and bridge before handover")
    if settings[PROXY]["enabled"] != "enabled" or settings[TIMER]["active"] != (settings[TIMER]["enabled"] == "enabled"):
        raise RuntimeError("Legacy runtime and boot settings require operator review")
    for name in ("tinyproxy.conf", "stunnel.conf"):
        active = OLD_RUN / "active" / name
        if active.is_file() and active.read_bytes() != (LEGACY / "verified" / name).read_bytes():
            raise RuntimeError("Running configuration differs from verified legacy generation")
    state = import_catalog(LEGACY, legacy.read_source(LEGACY / "upstream.url"), True,
                           settings[TIMER]["active"], timer_interval(prop(TIMER, "TimersMonotonic")))
    return state, settings


def write_record(target, record):
    with tempfile.NamedTemporaryFile(dir=target.parent, mode="w", delete=False) as f:
        json.dump(record, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, target)
    sync_dir(target.parent)


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def rule_path(unit):
    return SYSTEM / (unit + ".d") / DROPIN


def block_legacy(backup):
    # Any already queued legacy CLI reads this file only after obtaining its lock.
    (LEGACY / "upstream.url").rename(LEGACY / "upstream.url.before-outbound-proxy")
    sync_dir(LEGACY)
    for unit in UNITS:
        path = rule_path(unit)
        path.parent.mkdir(exist_ok=True)
        sync_dir(SYSTEM)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
            f.write("[Unit]\nConditionPathExists=!" + str(backup / "handover.json") + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(f.name, path)
        finally:
            os.unlink(f.name)
        sync_dir(path.parent)
    command("systemctl", "daemon-reload")
    command("systemctl", "stop", TIMER, WORKER, PROXY, TLS, NETWORK)


def backup_legacy(directory):
    shutil.copytree(LEGACY, directory / "legacy", symlinks=True)
    for unit in UNITS:
        (directory / unit).write_text(command("systemctl", "cat", unit))
    firewall = command("nft", "list", "table", "inet", "minipc_proxy")
    (directory / "firewall.nft").write_text(firewall)
    # Read the saved pair from the backup itself, even when the source used an absolute link.
    generation = (LEGACY / "verified").resolve(strict=True)
    for name in ("tinyproxy.conf", "stunnel.conf"):
        if (directory / "legacy" / generation.name / name).read_bytes() != (generation / name).read_bytes():
            raise RuntimeError("Backup validation failed")
    for base, _, files in os.walk(directory, topdown=False):
        for name in files:
            path = Path(base, name)
            if not path.is_symlink():
                with path.open("rb") as f:
                    os.fsync(f.fileno())
        sync_dir(base)


def restore(ops, directory, journal):
    # Converge all new native effects before returning the port to the legacy pair.
    if journal["phase"] != "restoring":
        write_record(directory / "standalone-before-rollback.json", ops.get_state())
    journal["phase"] = "restoring"
    write_record(directory / "handover.json", journal)
    ops.import_state(journal["old_state"])
    for unit in UNITS:
        path = rule_path(unit)
        if path.exists():
            expected = "[Unit]\nConditionPathExists=!" + str(directory / "handover.json") + "\n"
            if path.read_text() != expected:
                raise RuntimeError("Legacy drop-in changed during migration")
            path.unlink()
            sync_dir(path.parent)
    source = LEGACY / "upstream.url"
    held = LEGACY / "upstream.url.before-outbound-proxy"
    if held.exists():
        if source.exists():
            with tempfile.NamedTemporaryFile(dir=directory, prefix="source-before-rollback-", delete=False) as saved:
                saved.write(source.read_bytes())
                saved.flush()
                os.fsync(saved.fileno())
        held.replace(source)
        sync_dir(LEGACY)
    # Recreate the verified pair from the protected copy, including after a reboot.
    saved_generation = directory / "legacy" / journal["verified_name"]
    recovery = Path(tempfile.mkdtemp(prefix="generation-restored-", dir=LEGACY))
    legacy.write_pair(recovery, [(saved_generation / name).read_text() for name in ("tinyproxy.conf", "stunnel.conf")])
    legacy.select_generation(LEGACY / "verified", str(recovery))
    legacy.select_generation(OLD_RUN / "active", str(recovery))
    command("systemctl", "daemon-reload")
    table = (directory / "firewall.nft").read_text()
    command("systemctl", "start", NETWORK)
    command("nft", "-f", "-", input_text="table inet minipc_proxy\ndelete table inet minipc_proxy\n" + table)
    for unit in (TLS, PROXY, TIMER):
        old = journal["units"][unit]
        if old["active"]:
            command("systemctl", "restart" if unit in (TLS, PROXY) else "start", unit)
    legacy.wait_for_listener(legacy.TUNNEL_PORT)
    legacy.wait_for_listener(legacy.PORT)
    probe_consumers(journal["members"])
    journal["phase"] = "restored"
    write_record(directory / "handover.json", journal)


def bridge_members():
    network = json.loads(command("docker", "network", "inspect", "minipc-outbound-proxy"))[0]
    members = sorted(item["Name"] for item in network["Containers"].values())
    if not members:
        raise RuntimeError("Expected at least one dedicated bridge consumer")
    return members


def probe_host():
    code = command("curl", "-q", "--silent", "--noproxy", "", "--max-time", "20",
                   "--proxy", f"http://127.0.0.1:{legacy.PORT}", "--output", "/dev/null",
                   "--write-out", "%{http_connect} %{http_code}", "https://example.com/")
    if code != "200 200":
        raise RuntimeError("Host proxy probe failed")


def probe_consumers(members):
    probe_host()
    if members != bridge_members():
        raise RuntimeError("Bridge membership changed during migration")
    for name in members:
        pid = command("docker", "inspect", "--format", "{{.State.Pid}}", name).strip()
        if not pid.isdigit() or int(pid) <= 1:
            raise RuntimeError("Expected proxy consumer is not running")
        result = command("nsenter", "--target", pid, "--net", "--", "/usr/bin/curl", "-q",
                         "--silent", "--noproxy", "", "--max-time", "20", "--proxy",
                         f"http://{legacy.GATEWAY}:{legacy.PORT}", "--output", "/dev/null",
                         "--write-out", "%{http_connect} %{http_code}", "https://example.com/")
        if result != "200 200":
            raise RuntimeError("Container proxy probe failed")


def migrate(ops, phase, expected_selected=None):
    if expected_selected is not None and phase != "cutover":
        raise ValueError("--expect-selected applies to cutover only")
    if sys.platform != "linux":
        raise RuntimeError("MiniPC migration requires Linux")
    directory = STATE / "legacy-backup"
    journal_path = directory / "handover.json"
    OLD_RUN.mkdir(mode=0o700, exist_ok=True)
    if OLD_RUN.is_symlink() or OLD_RUN.stat().st_uid != os.geteuid() or OLD_RUN.stat().st_mode & 0o022:
        raise RuntimeError("Unsafe legacy runtime directory")
    fd = os.open(OLD_RUN / "apply.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as old_lock:
        # Do not wait on a scheduled job that is about to be stopped by cutover.
        try:
            fcntl.flock(old_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Legacy apply is active. Retry after it finishes") from None
        with ops.exclusive():
            if phase == "inspect":
                if journal_path.exists():
                    journal = json.loads(journal_path.read_text())
                    return {"migration": journal["phase"], "backup": str(directory)}
                state, _ = snapshot()
                state = handover_state(state, ops.get_state())
                return {"migration": "ready", "selected": state["selected"], "entries": len(state["upstreams"]), "members": bridge_members(),
                        "port": state["port"], "failover": state["failover"], "interval": state["interval"]}
            if phase in ("accept", "rollback"):
                journal = json.loads(journal_path.read_text())
                if journal["phase"] not in ("pending", "serving", "accepted", "restoring"):
                    raise RuntimeError("No handover awaiting acceptance or rollback")
                if phase == "accept":
                    if journal["phase"] == "accepted":
                        return {"migration": "accepted", "backup": str(directory)}
                    if journal["phase"] in ("pending", "restoring"):
                        raise RuntimeError("Interrupted cutover requires rollback")
                    try:
                        effective = ops.run("status")
                        if not (effective["running"] and effective["boot_enabled"] and
                                effective["enabled"] and effective["port"] == legacy.PORT and effective["docker"]):
                            raise RuntimeError("Standalone service does not own the expected route")
                        prior_catalog = journal.get("old_state", {}).get("upstreams")
                        if prior_catalog and ops.get_state()["upstreams"] != prior_catalog:
                            raise RuntimeError("Prepared catalog changed before acceptance")
                        if effective.get("recovery_pending") or any(
                                effective.get(k) != v for k, v in journal.get("expected", {}).items()):
                            raise RuntimeError("Standalone selection or schedule changed before acceptance")
                        probe_consumers(journal["members"])
                    except Exception:
                        restore(ops, directory, journal)
                        raise RuntimeError("Acceptance failed. The legacy service was restored") from None
                    journal["phase"] = "accepted"
                    write_record(journal_path, journal)
                else:
                    restore(ops, directory, journal)
                return {"migration": journal["phase"], "backup": str(directory)}
            if phase != "cutover":
                raise ValueError("Unknown migration phase")
            if directory.exists():
                if not journal_path.is_file() or json.loads(journal_path.read_text()).get("phase") != "restored":
                    raise RuntimeError("Inspect or roll back the existing handover before retrying")
                # Preserve the previous rollback copy when retrying a restored migration.
                directory.rename(STATE / ("legacy-backup-restored-" + str(time.time_ns())))
                sync_dir(STATE)
            if (LEGACY / "upstream.url.before-outbound-proxy").exists() or any(rule_path(unit).exists() for unit in UNITS):
                raise RuntimeError("Existing migration backup or unit override requires review")
            ops.run("recover")
            health = ops.native.health()
            if health["running"] is not False or health["boot_enabled"] is not False:
                raise RuntimeError("Standalone forwarding and boot startup must be stopped before migration")
            old_state = ops.get_state()
            source, units = snapshot()
            state = handover_state(source, old_state, expected_selected)
            selected = next(e["uri"] for e in state["upstreams"] if e["name"] == state["selected"])
            ops.native.check(selected)
            members = bridge_members()
            probe_consumers(members)
            expected = {key: state[key] for key in ("selected", "failover", "interval")}
            record = {"phase": "pending", "expected": expected, "old_state": old_state, "units": units, "members": members,
                      "verified_name": (LEGACY / "verified").resolve(strict=True).name}
            with tempfile.TemporaryDirectory(dir=STATE, prefix=".legacy-backup-") as staged:
                prepared = Path(staged) / "backup"
                prepared.mkdir(mode=0o700)
                backup_legacy(prepared)
                write_record(prepared / "handover.json", record)
                prepared.rename(directory)
                sync_dir(STATE)
            probe_host()
            start = time.monotonic()
            try:
                block_legacy(directory)
                command("nft", "delete", "table", "inet", "minipc_proxy")
                ops.import_state(state)
                probe_host()
                duration = time.monotonic() - start
                probe_consumers(members)
            except Exception:
                restore(ops, directory, record)
                raise RuntimeError("Cutover failed. The legacy service was restored") from None
            record.update(phase="serving", cutover_seconds=round(duration, 3))
            write_record(journal_path, record)
            return {"migration": "serving", "cutover_seconds": record["cutover_seconds"],
                    "backup": str(directory)}
