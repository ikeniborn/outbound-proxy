import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from outbound_proxy import native


def test_platform_and_dependencies(tmp_path):
    assert native.platform_name("Linux", "ID=debian\n") == "Linux"
    assert native.platform_name("Linux", 'ID="ubuntu"\n') == "Linux"
    assert native.platform_name("Darwin", "") == "Darwin"
    with pytest.raises(RuntimeError, match="Debian|Ubuntu"):
        native.platform_name("Linux", "ID=fedora")
    with pytest.raises(RuntimeError, match="tinyproxy"):
        native.executable("tinyproxy", [tmp_path])


def test_privileged_path_refuses_writable_component(tmp_path):
    with pytest.raises(RuntimeError, match="ownership|writable"):
        native.trusted(tmp_path)


def state(**updates):
    return dict({"upstreams": [{"name": "one", "uri": "https://u:p@proxy.example:443"}],
                 "selected": "one", "port": 19999, "enabled": True,
                 "failover": True, "docker": True, "interval": 120}, **updates)


class Effects:
    def __init__(self):
        self.events = []
        self.failure = None

    def __getattr__(self, name):
        def effect(*args):
            self.events.append((name, *args))
            if name == self.failure:
                raise RuntimeError("native registration failed")
        return effect


def adapter(tmp_path, system="Linux"):
    host = SimpleNamespace(system=system)
    result = native.Native(tmp_path, host=host, runtime=tmp_path/"runtime")
    effects = Effects()
    result.manager = effects
    result.access = effects
    result.publish = effects.publish
    result.probe = effects.probe
    result.verify_pair = effects.verify_pair
    return result, effects


def test_access_precedes_listener_and_is_removed_after_shutdown(tmp_path):
    service, effects = adapter(tmp_path)
    service.apply(state())
    events = [event[0] for event in effects.events]
    assert events.index("prepare") < events.index("publish") < events.index("start_pair") < events.index("probe")
    assert ("schedule", True, 120) in effects.events
    assert ("boot", True) in effects.events
    effects.events.clear()
    service.apply(state(enabled=False))
    assert effects.events.index(("stop_pair",)) < effects.events.index(("withdraw",))
    assert ("schedule", False, 120) in effects.events
    assert ("boot", False) in effects.events


def test_registration_failure_is_observable(tmp_path):
    service, effects = adapter(tmp_path)
    effects.failure = "boot"
    with pytest.raises(RuntimeError, match="registration"):
        service.apply(state(docker=False))


def test_macos_rejects_docker_before_changing_system(tmp_path):
    service, effects = adapter(tmp_path, "Darwin")
    with pytest.raises(RuntimeError, match="Linux|macOS"):
        service.apply(state())
    assert not effects.events


def test_repeat_package_stage_preserves_settings_and_resolves_templates(tmp_path):
    from outbound_proxy import installer
    repository = Path(__file__).resolve().parents[1]
    package = tmp_path / "install"
    package.mkdir()
    statefile = tmp_path / "settings"
    statefile.write_text("previous verified settings")
    installer.stage(package, repository)
    first = (package / "outbound-proxy").read_bytes()
    installer.stage(package, repository)
    assert first == (package / "outbound-proxy").read_bytes()
    assert (package / "current/native/job.plist").is_file()
    assert (package / "current/outbound_proxy/web").is_dir()
    assert statefile.read_text() == "previous verified settings"


def test_stage_installation_accepts_existing_legacy_units_only_explicitly(tmp_path):
    from outbound_proxy import installer
    def inventory(*args):
        return "minipc-proxy-stunnel.service enabled"
    with pytest.raises(RuntimeError, match="migration"):
        installer.check_legacy(tmp_path, inventory, False)
    installer.check_legacy(tmp_path, inventory, True)
    backup = tmp_path / "legacy-backup"
    backup.mkdir()
    (backup/"handover.json").write_text('{"phase":"accepted"}')
    installer.check_legacy(tmp_path, inventory, False)


def test_launch_job_restricts_brew_executable_and_boot_default(monkeypatch):
    import plistlib
    from outbound_proxy import installer
    templates = Path(__file__).resolve().parents[1] / "scripts/native"
    monkeypatch.setattr(native, "TEMPLATES", templates)
    host = native.Host("Darwin", "/opt/homebrew/bin/tinyproxy", "/opt/homebrew/bin/stunnel",
                       "/usr/bin/curl", Path("/etc/ssl/cert.pem"), "_outboundproxy", "_outboundtls")
    service = native.Native("/etc/outbound-proxy", host=host)
    monkeypatch.setattr(installer, "group", lambda name: name)
    files = installer.definitions(service, templates)
    pair = plistlib.loads(files[service.manager.jobs/"local.outbound-proxy.tinyproxy.plist"])
    assert pair["UserName"] == "_outboundproxy"
    assert pair["ProgramArguments"][0] == "/opt/homebrew/bin/tinyproxy"
    tls = plistlib.loads(files[service.manager.jobs/"local.outbound-proxy.stunnel.plist"])
    assert tls["inetdCompatibility"] == {"Wait": False}
    assert tls["Sockets"]["Listener"]["SockNodeName"] == "127.0.0.1"
    assert "StandardOutPath" not in tls
    boot = plistlib.loads(files[native.DAEMONS/"local.outbound-proxy.boot.plist"])
    assert boot["Disabled"] and boot["ProgramArguments"][-1] == "boot"


def test_schedule_change_preserves_running_pair(tmp_path):
    service, effects = adapter(tmp_path)
    service.schedule(False, 120)
    assert effects.events == [("schedule", False, 120)]


@pytest.mark.parametrize("identity, expected", [({}, "nobody"), ({"user": "outbound-proxy"}, "outbound-proxy")])
def test_tinyproxy_runs_as_service_identity_with_independent_upstream_login(identity, expected):
    tiny, _ = native.legacy.render_pair(("example.test", 443, "login", "secret"),
                                        ("127.0.0.1",), ("127.0.0.1",), 18888, 18889, **identity)
    assert f"User {expected}\n" in tiny
    assert "Upstream http login:secret@" in tiny


def test_unmatched_systemd_glob_must_not_refuse_clean_install(tmp_path):
    from outbound_proxy import installer

    def unit_files(*args):
        if "minipc-proxy-*" in args:
            raise RuntimeError("systemctl returns code 1 for an unmatched unit glob")
        return "unrelated.service enabled enabled\nssh.service enabled enabled\n"

    installer.check_legacy(tmp_path, unit_files, False)


def test_native_owns_transport_socket_while_service_reads_one_connection(tmp_path, monkeypatch):
    from outbound_proxy import installer
    templates = Path(__file__).resolve().parents[1] / "scripts/native"
    host = native.Host("Linux", "/usr/bin/tinyproxy", "/usr/bin/stunnel", "/usr/bin/curl",
                       Path("/etc/ssl/certs/ca-certificates.crt"), "outbound-proxy", "outbound-tls")
    monkeypatch.setattr(installer, "group", lambda name: name)
    files = installer.definitions(native.Native(tmp_path, host=host), templates)
    listener = files[native.SYSTEMD/"outbound-proxy-stunnel.socket"].decode()
    assert "ListenStream=127.0.0.1:18889" in listener and "Accept=yes" in listener
    transport = files[native.SYSTEMD/"outbound-proxy-stunnel@.service"].decode()
    assert "StandardInput=socket" in transport and "User=outbound-tls" in transport
    front = files[native.SYSTEMD/"outbound-proxy-tinyproxy.service"].decode()
    assert "BindsTo=outbound-proxy-stunnel.socket" in front
    assert "After=outbound-proxy-stunnel.socket" in front


def test_manager_reports_bus_failure(monkeypatch, tmp_path):
    manager = native.Manager(SimpleNamespace(system="Linux"), tmp_path)
    monkeypatch.setattr(manager, "ctl", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus"))
    with pytest.raises(RuntimeError):
        manager.running("tinyproxy")
    with pytest.raises(RuntimeError):
        manager.boot_enabled()


def test_stage_rejects_foreign_launcher_before_switching_package(tmp_path):
    from outbound_proxy import installer
    package = tmp_path / "package"
    package.mkdir()
    (package/"outbound-proxy").symlink_to("/home/other/untrusted-launcher")
    with pytest.raises(RuntimeError):
        installer.stage(package, Path(__file__).resolve().parents[1])
    assert not (package/"current").is_symlink()


def test_equal_unit_content_does_not_preserve_unsafe_permissions(tmp_path):
    unit = tmp_path / "unsafe.service"
    unit.write_bytes(b"same")
    unit.chmod(0o666)
    assert native.replace_file(unit, b"same") is True
    assert unit.stat().st_mode & 0o777 == 0o644


def test_preflight_requires_a_readable_ca_bundle_for_service_identity(tmp_path):
    import os
    import pwd
    from outbound_proxy import installer
    owner = pwd.getpwuid(os.geteuid())
    bad = tmp_path / "ca.pem"
    bad.write_text("unusable trust data")
    with pytest.raises(RuntimeError, match="trust bundle"):
        installer.check_service_files(owner, "/usr/bin/true", bad)


def test_installed_panel_is_independent_and_starts_at_boot(monkeypatch, tmp_path):
    import plistlib
    from outbound_proxy import installer
    templates = Path(__file__).resolve().parents[1] / "scripts/native"
    monkeypatch.setattr(native, "TEMPLATES", templates)
    monkeypatch.setattr(installer, "group", lambda name: name)
    for system in ("Linux", "Darwin"):
        host = native.Host(system, "/bin/tinyproxy", "/bin/stunnel", "/usr/bin/curl",
                           Path("/etc/ssl/cert.pem"), "front", "tls")
        files = installer.definitions(native.Native(tmp_path, host=host), templates)
        if system == "Linux":
            panel = files[native.SYSTEMD/"outbound-proxy-panel.service"].decode()
            assert f"ExecStart={host.package}/outbound-proxy panel-serve\n" in panel
            assert "WantedBy=multi-user.target" in panel and "Restart=" in panel
            assert "tinyproxy" not in panel and "stunnel" not in panel
        else:
            panel = plistlib.loads(files[native.DAEMONS/"local.outbound-proxy.panel.plist"])
            assert panel["RunAtLoad"] and panel["KeepAlive"]
            assert panel["ProgramArguments"] == [str(host.package/"outbound-proxy"), "panel-serve"]


def test_saved_panel_port_is_rejected_before_registration_even_if_disabled(tmp_path):
    from outbound_proxy import installer
    from outbound_proxy.store import Store, defaults, write_json
    store = Store(tmp_path)
    with store.locked():
        generation = store.stage(defaults())
        store.commit(generation)
        write_json(store.path(generation)/"state.json", dict(defaults(), port=18890))
        with pytest.raises(RuntimeError, match="18890"):
            installer.check_saved_port(store)


@pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux procfs socket attribution")
def test_panel_only_recognizes_listening_socket_of_manager_pid():
    import socket
    import os
    from outbound_proxy import installer
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert installer.panel_socket("Linux", os.getpid(), port) == "managed"
        assert installer.panel_socket("Linux", 1, port) == "conflict"
        assert installer.panel_socket("Linux", None, port) == "conflict"
    assert installer.panel_socket("Linux", None, port) == "free"
    with socket.socket() as wildcard:
        wildcard.bind(("0.0.0.0", port))
        wildcard.listen()
        assert installer.panel_socket("Linux", os.getpid(), port) == "conflict"


def test_platform_can_verify_browser_signature():
    from outbound_proxy import installer
    installer.check_verifier()


def test_panel_port_is_reserved_for_saved_settings_and_import():
    from outbound_proxy.store import defaults, validate
    with pytest.raises(RuntimeError, match="reserved"):
        validate(dict(defaults(), port=18890))


def test_staging_rejects_reserved_legacy_port_without_transferring_ownership(tmp_path):
    from outbound_proxy import installer
    active = tmp_path / "generation-private"
    active.mkdir()
    (tmp_path / "verified").symlink_to(active.name)
    config = active / "tinyproxy.conf"
    original = 'Port 18890\nUpstream http test-user:test-secret@127.0.0.1:18889\n'
    config.write_text(original)
    with pytest.raises(RuntimeError, match="18890") as failure:
        installer.check_legacy_port(tmp_path)
    assert "test-secret" not in str(failure.value)
    assert config.read_text() == original and (tmp_path/"verified").readlink() == Path(active.name)


def test_port_collision_on_pending_generation_refuses_install(tmp_path):
    from outbound_proxy import installer
    from outbound_proxy.store import Store, defaults, write_json
    store = Store(tmp_path)
    with store.locked():
        old = store.current()
        candidate = store.stage(defaults())
        store.write_pending(old, candidate, "forwarding")
        write_json(store.path(candidate)/"state.json", dict(defaults(), port=18890))
        with pytest.raises(RuntimeError, match="18890"):
            installer.check_saved_port(store)
        assert store.current() == old and store.pending()["candidate"] == candidate


def test_macos_attribution_refuses_foreign_pid_and_exposed_listener(monkeypatch):
    from outbound_proxy import installer
    monkeypatch.setattr(installer, "privileged", lambda name: "/usr/sbin/lsof")
    for pid, addresses, expected in [
        (23, ["127.0.0.1:18890"], "managed"), (24, ["127.0.0.1:18890"], "conflict"),
        (23, ["*:18890"], "conflict"), (23, ["127.0.0.1:18890", "[::1]:18890"], "conflict"),
    ]:
        record = "\n".join([f"p{pid}", *(f"n{address}" for address in addresses)])
        monkeypatch.setattr(native, "execute", lambda *args, **kwargs:
                            SimpleNamespace(returncode=0, stdout=record, stderr=""))
        assert installer.panel_socket("Darwin", 23) == expected


def test_verifier_preflight_names_missing_dependency(monkeypatch):
    from outbound_proxy import installer
    def missing(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(installer, "trusted", missing)
    with pytest.raises(RuntimeError, match="OpenSSL"):
        installer.check_verifier()


def test_removal_can_read_old_reserved_port_but_new_generations_reject_it(tmp_path):
    from outbound_proxy.store import Store, defaults, write_json
    store = Store(tmp_path)
    with store.locked():
        old = store.stage(defaults())
        previous = {**defaults(), "port": 18890, "enabled": True, "selected": "old",
                    "upstreams": [{"name": "old", "uri": "https://user:secret@old.example:443"}]}
        write_json(store.path(old)/"state.json", previous)
        assert store.read(old, allow_panel_port=True) == previous
        with pytest.raises(RuntimeError, match="reserved"):
            store.stage({**previous, "enabled": False})
        with pytest.raises(RuntimeError, match="reserved"):
            store.read(old)


def test_stage_refuses_foreign_current_and_path_alias(tmp_path):
    from outbound_proxy import installer
    package = tmp_path / "package"
    package.mkdir()
    foreign = tmp_path/"other"
    foreign.mkdir()
    (package/"current").symlink_to(foreign)
    with pytest.raises(RuntimeError):
        installer.stage(package, Path(__file__).resolve().parents[1])
    assert (package/"current").readlink() == foreign
    file = tmp_path/"paths.d-entry"
    file.write_text(str(foreign) + "\n")
    with pytest.raises(RuntimeError):
        installer.check_path_entry(file, package)
    assert file.read_text() == str(foreign) + "\n"


def test_reinstall_refuses_effective_panel_overrides_before_restart():
    from outbound_proxy import installer

    class Overridden:
        linux = True

        def ctl(self, *args, **options):
            fields = {"LoadState": "loaded", "MainPID": "123",
                      "FragmentPath": "/etc/systemd/system/outbound-proxy-panel.service",
                      "DropInPaths": "/etc/systemd/system/outbound-proxy-panel.service.d/override.conf"}
            return SimpleNamespace(returncode=0, stdout="\n".join(f"{k}={fields[k]}" for k in fields if k in args))

    with pytest.raises(RuntimeError):
        installer.panel_pid(SimpleNamespace(manager=Overridden()))


def test_launchd_job_with_an_unrelated_program_is_not_the_installed_panel():
    from outbound_proxy import installer
    description = ("system/local.outbound-proxy.panel = {\n"
                   "\tpath = /Library/LaunchDaemons/local.outbound-proxy.panel.plist\n"
                   "\targuments = {\n\t\t/usr/local/bin/other\n\t}\n\tpid = 42\n}")
    manager = SimpleNamespace(linux=False, ctl=lambda *args, **kwargs:
                              SimpleNamespace(returncode=0, stdout=description))
    host = SimpleNamespace(package=Path("/Library/PrivilegedHelperTools/outbound-proxy"))
    with pytest.raises(RuntimeError):
        installer.panel_pid(SimpleNamespace(manager=manager, host=host))


def test_first_install_detects_orphaned_systemd_panel_override(tmp_path):
    from outbound_proxy import installer
    override = tmp_path/"outbound-proxy-panel.service.d"/"override.conf"
    override.parent.mkdir()
    override.write_text("[Service]\nExecStart=/usr/bin/other\n")

    class EmptyUnit:
        linux = True

        def ctl(self, *args, **kwargs):
            if "UnitPath" in args:
                return SimpleNamespace(returncode=0, stdout=str(tmp_path))
            return SimpleNamespace(returncode=4, stdout="LoadState=not-found\nMainPID=0\n")

    with pytest.raises(RuntimeError, match="override"):
        installer.panel_pid(SimpleNamespace(manager=EmptyUnit()))


def test_panel_readiness_rechecks_ownership_after_http_success(monkeypatch):
    from outbound_proxy import installer
    class HTTP:
        def request(self, method, path):
            pass

        def getresponse(self):
            return SimpleNamespace(status=200)

        def close(self):
            pass

    service = SimpleNamespace(manager=SimpleNamespace(linux=True, ctl=lambda *args: None),
                              host=SimpleNamespace(system="Linux"))
    calls = iter(["managed", "conflict", "conflict"])
    monkeypatch.setattr(installer, "panel_pid", lambda owner: 42)
    monkeypatch.setattr(installer, "panel_socket", lambda *args: next(calls))
    monkeypatch.setattr(installer.http.client, "HTTPConnection", lambda *args, **options: HTTP())
    with pytest.raises(RuntimeError, match="listener"):
        installer.start_panel(service)
