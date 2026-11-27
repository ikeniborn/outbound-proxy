from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from outbound_proxy.store import Store, OperationError, defaults, write_json


def fixture(tmp_path, system="Linux"):
    from outbound_proxy.uninstall import Removal, record_install
    root = tmp_path / "catalog"
    store = Store(root)
    with store.locked():
        state = defaults()
        state.update(upstreams=[{"name": "selected", "uri": "https://u:fixture@up.example:443"}],
                     selected="selected", enabled=True, docker=True, failover=True)
        store.commit(store.stage(state))
    host = SimpleNamespace(system=system, package=tmp_path / "package",
                           runtime=tmp_path / "runtime/config")
    release = host.package / "releases/tmp12345678"
    release.mkdir(parents=True)
    for directory in (host.package, release.parent, release):
        directory.chmod(0o755)
    (release / "outbound-proxy").write_text("launcher")
    (host.package / "current").symlink_to("releases/tmp12345678")
    (host.package / "outbound-proxy").symlink_to("current/outbound-proxy")
    registration = tmp_path / "jobs"
    registration.mkdir()
    binary = tmp_path / "bin" / "outbound-proxy"
    binary.parent.mkdir()
    if system == "Linux":
        binary.symlink_to(host.package / "outbound-proxy")
        panel = registration / "outbound-proxy-panel.service"
    else:
        binary.write_text(str(host.package) + "\n")
        binary.chmod(0o644)
        panel = registration / "local.outbound-proxy.panel.plist"
    panel.write_bytes(b"panel-definition")
    panel.chmod(0o644)
    record_install(root, host, {panel: panel.read_bytes()}, release)
    backup = root / "legacy-backup"
    backup.mkdir(mode=0o700)
    write_json(backup / "handover.json", {"phase": "accepted", "kept": "legacy"})
    (root / "panel-pairing").mkdir(mode=0o700)
    write_json(root / "panel-pairing/records.json", {"key": "pairing fixture"})
    cleanup = Removal(store, host, registration, binary)
    return cleanup, release, panel


class Units:
    def __init__(self, panel):
        self.panel = panel
        self.active = True
        self.calls = []
        self.disabled = set()

    def __call__(self, verb, *args, check=True):
        self.calls.append((verb, *args))
        stdout, code = "", 0
        if verb == "show":
            unit = args[-1]
            found = self.panel.exists() and unit == self.panel.name
            stdout = ("LoadState=loaded\nFragmentPath=" + str(self.panel) + "\nDropInPaths=\nActiveState="
                      + ("active" if self.active else "inactive") + "\n") if found else (
                          "LoadState=not-found\nFragmentPath=\nDropInPaths=\nActiveState=inactive\n")
        elif verb == "stop":
            self.active = False
        elif verb == "print":
            if args[-1] == "system/local.outbound-proxy.panel" and self.active:
                stdout = "system/local.outbound-proxy.panel = {\n path = " + str(self.panel) + "\n}"
            else:
                code = 113
        elif verb == "disable":
            self.disabled.add(args[-1].removeprefix("system/"))
        elif verb == "print-disabled":
            stdout = "\n".join('"' + label + '" => disabled' for label in self.disabled)
        elif verb == "bootout":
            self.active = False
        elif verb not in ("list-units", "disable", "daemon-reload"):
            raise AssertionError(verb)
        return SimpleNamespace(stdout=stdout, stderr="", returncode=code)


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_removal_revokes_pairing_preserves_backup_catalog_and_foreign_files(tmp_path, system):
    removal, release, panel = fixture(tmp_path, system)
    units = Units(panel)
    removal.manager.ctl = units
    removal.check_listeners = lambda *args: None
    foreign = release / "foreign-note"
    foreign.write_text("keep")
    backup = removal.store.root / "legacy-backup/handover.json"
    original = backup.read_bytes()
    removal.run()
    removal.run()
    assert not units.active and not panel.exists()
    assert not (release / "outbound-proxy").exists() and not removal.command_file.exists()
    assert not (removal.store.root / "panel-pairing/records.json").exists()
    assert backup.read_bytes() == original and foreign.read_text() == "keep"
    with pytest.raises(OperationError):
        with removal.store.locked():
            pytest.fail("stale command reactivated removed installation")
    with removal.store.locked(maintenance=True):
        state = removal.store.read(removal.store.current())
        assert state["selected"] == "selected" and state["failover"] and not state["enabled"]
        assert state["upstreams"][0]["uri"] == "https://u:fixture@up.example:443"
        removal.store.allow_install()
        removal.store.finish_install()
    with removal.store.locked():
        assert not removal.store.read(removal.store.current())["enabled"]


@pytest.mark.parametrize("phase", ["pending", "serving", "restoring", "unknown"])
def test_unresolved_migration_stops_before_any_native_change(tmp_path, phase):
    removal, release, panel = fixture(tmp_path)
    removal.manager.ctl = Units(panel)
    write_json(removal.store.root / "legacy-backup/handover.json", {"phase": phase})
    with pytest.raises(OperationError, match="migrate"):
        removal.run()
    assert not removal.manager.ctl.calls
    assert (release / "outbound-proxy").exists() and panel.exists()
    with removal.store.locked():
        pass


def test_failed_cleanup_retries_from_checkout_after_quiescing_and_revocation(tmp_path, monkeypatch):
    removal, release, panel = fixture(tmp_path)
    removal.manager.ctl = Units(panel)
    removal.check_listeners = lambda *args: None
    with monkeypatch.context() as patch:
        patch.setattr(removal, "remove_program", lambda: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(OSError):
            removal.run()
    assert not panel.exists()
    assert not (removal.store.root / "panel-pairing/records.json").exists()
    with pytest.raises(OperationError):
        with removal.store.locked(maintenance=True):
            removal.store.allow_install()
    removal.run()
    assert not (release / "outbound-proxy").exists()


def test_collision_at_native_registration_refuses_before_stopping_foreign_unit(tmp_path):
    removal, release, panel = fixture(tmp_path)
    panel.write_text("a foreign replacement")
    removal.manager.ctl = Units(panel)
    with pytest.raises(OperationError):
        removal.run()
    assert not any(command[0] in ("stop", "disable") for command in removal.manager.ctl.calls)
    assert panel.read_text() == "a foreign replacement"


def test_disable_unloaded_macos_boot_registration(tmp_path):
    removal, _, panel = fixture(tmp_path, "Darwin")
    boot = removal.registration / "local.outbound-proxy.boot.plist"
    boot.write_bytes(b"owned-boot")
    boot.chmod(0o644)
    from outbound_proxy.uninstall import record_install
    record_install(removal.store.root, removal.host, {boot: boot.read_bytes()},
                   (removal.host.package / "current").resolve())
    units = Units(panel)
    removal.manager.ctl = units
    removal.check_listeners = lambda *args: None
    removal.run()
    assert ("disable", "system/local.outbound-proxy.boot") in units.calls


def test_reject_plist_replacement_with_different_program(tmp_path):
    import plistlib
    removal, _, panel = fixture(tmp_path, "Darwin")
    panel.write_bytes(plistlib.dumps({
        "Label": panel.stem,
        "ProgramArguments": [str(removal.host.package / "outbound-proxy"), "panel-serve"],
        "Program": "/foreign/program",
    }))
    removal.manager.ctl = Units(panel)
    with pytest.raises(OperationError):
        removal.run()
    assert panel.exists() and removal.manager.ctl.active


def test_staged_removal_preserves_unowned_firewall_and_drains_jobs(tmp_path):
    import threading
    removal, _, panel = fixture(tmp_path)
    units = Units(panel)
    removal.manager.ctl = units
    removal.check_listeners = lambda *args: None
    removal.access.run = lambda *a, **k: pytest.fail("touched firewall without ownership")
    original = removal.quiesce
    def quiesce(jobs):
        with pytest.raises(OperationError):
            with removal.store.locked():
                pytest.fail("new operations must be rejected during removal")
        original(jobs)
    removal.quiesce = quiesce
    import concurrent.futures
    started = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with removal.store.locked():
            def remove():
                started.set()
                removal.run()
            done = pool.submit(remove)
            assert started.wait(2)
            assert not done.done()
            state = removal.store.read(removal.store.current())
            state.update(port=23345, enabled=False)
            removal.store.commit(removal.store.stage(state))
        done.result(5)
    with removal.store.locked(maintenance=True):
        assert removal.store.read(removal.store.current())["port"] == 23345


def test_stale_loaded_fragment_after_unlink_can_finish_removal(tmp_path):
    removal, _, panel = fixture(tmp_path)
    panel.unlink()
    class StaleUnits(Units):
        def __init__(self, path):
            super().__init__(path)
            self.reloaded = False

        def __call__(self, verb, *args, **kw):
            if verb == "show" and args[-1] == panel.name and not self.reloaded:
                return SimpleNamespace(stdout=f"LoadState=loaded\nFragmentPath={panel}\nDropInPaths=\nActiveState=inactive\n",
                                       returncode=0)
            if verb == "disable":
                pytest.fail("Can't disable an already missing unit file")
            if verb == "daemon-reload":
                self.reloaded = True
            return super().__call__(verb, *args, **kw)

    removal.manager.ctl = StaleUnits(panel)
    removal.check_listeners = lambda *args: None
    removal.run()
    assert removal.manager.ctl.reloaded


@pytest.mark.parametrize("enabled", [True, False])
def test_saved_pre_panel_port_resets_to_default_before_reinstall(tmp_path, enabled):
    removal, _, panel = fixture(tmp_path)
    removal.manager.ctl = Units(panel)
    removal.check_listeners = lambda *args: None
    with removal.store.locked():
        state = removal.store.read(removal.store.current())
        state.update(port=18890, enabled=enabled)
        write_json(removal.store.path(removal.store.current()) / "state.json", state)
    assert removal.run() is True
    with removal.store.locked(maintenance=True):
        removal.store.allow_install()
        removal.store.finish_install()
    with removal.store.locked():
        state = removal.store.read(removal.store.current())
    assert state["port"] == 18888 and not state["enabled"] and state["selected"] == "selected"


def test_orphaned_boot_link_removed_even_if_unit_file_already_missing(tmp_path):
    removal, _, panel = fixture(tmp_path)
    panel.unlink()
    wants = removal.registration / "multi-user.target.wants"
    wants.mkdir(mode=0o755)
    owned = wants / panel.name
    owned.symlink_to("../" + panel.name)
    foreign = wants / "unrelated.service"
    foreign.symlink_to("../unrelated.service")
    removal.manager.ctl = Units(panel)
    removal.check_listeners = lambda *args: None
    removal.run()
    assert not owned.is_symlink() and foreign.is_symlink()


def test_ownership_of_gateway_only_listener_is_checked_without_a_connection(tmp_path, monkeypatch):
    from outbound_proxy import uninstall
    removal, _, _ = fixture(tmp_path)
    proc = tmp_path / "proc"
    (proc / "net").mkdir(parents=True)
    # Linux /proc/net/tcp exposes the dedicated gateway as little-endian IPv4.
    (proc / "net/tcp").write_text("sl local_address rem_address st ...\n"
                                 "0: 0146A8C0:49C8 00000000:0000 0A 0:0 00:0 0 1000 0 1234\n")
    process = proc / "123"
    (process / "fd").mkdir(parents=True)
    (process / "fd/2").symlink_to("socket:[1234]")
    (process / "cgroup").write_text("0::/system.slice/test.service")
    (process / "cmdline").write_text("/usr/bin/tinyproxy -c /etc/minipc-proxy/current")
    monkeypatch.setattr(uninstall, "PROC", proc, raising=False)
    removal.check_listeners(18888)
    (process / "cmdline").write_text("tinyproxy -c " + str(removal.host.runtime / "active/tinyproxy.conf"))
    with pytest.raises(OperationError, match="listener"):
        removal.check_listeners(18888)
