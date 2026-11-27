"""MiniPC import rules and handover failure boundaries."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import minipc_proxy_apply as legacy


def legacy_tree(path, active=1):
    entries = ["https://operator:one@one.test:443", "https://operator:two@two.test:443"]
    (path / "upstream.url").write_text(",".join(entries) + "\n")
    config = path / "generation-real"
    config.mkdir()
    legacy.write_pair(config, legacy.production_pair(legacy.parse_upstream(entries[active])))
    (path / "verified").symlink_to(config)
    return entries


def test_import_selects_verified_second(tmp_path):
    from outbound_proxy.migrate import import_catalog
    entries = legacy_tree(tmp_path)
    state = import_catalog(tmp_path, (tmp_path / "upstream.url").read_text(), True, True, 125)
    assert state["selected"] == "proxy-2"
    assert state["upstreams"] == [{"name": f"proxy-{i}", "uri": uri} for i, uri in enumerate(entries, 1)]
    assert state["interval"] == 125


def test_unmatched_pair_stops_import(tmp_path):
    from outbound_proxy.migrate import import_catalog
    entries = legacy_tree(tmp_path)
    with pytest.raises(RuntimeError, match="verified"):
        import_catalog(tmp_path, entries[0], True, False, 60)


def test_interval_uses_inactive_delay():
    from outbound_proxy.migrate import timer_interval
    assert timer_interval("{ OnBootUSec=1min ; next_elapse=1min }\n{ OnUnitInactiveUSec=2min 5s ; next_elapse=infinity }") == 125
    with pytest.raises(ValueError):
        timer_interval("{ OnBootUSec=1min }")


def test_failed_acceptance_restores_prior_owner(tmp_path, monkeypatch):
    import contextlib
    import json
    from outbound_proxy import migrate as m

    class Service:
        exclusive = staticmethod(contextlib.nullcontext)

        def run(self, action):
            assert action == "status"
            return {"running": True, "boot_enabled": True, "enabled": True, "port": legacy.PORT, "docker": True}

    backup = tmp_path / "legacy-backup"
    backup.mkdir()
    (backup / "handover.json").write_text(json.dumps({"phase": "serving", "members": ["consumer"]}))
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr(m, "OLD_RUN", tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path)
    recovered = []
    monkeypatch.setattr(m, "restore", lambda ops, path, journal: recovered.append(path))

    def failed(members):
        raise RuntimeError("Consumer did not reconnect")

    monkeypatch.setattr(m, "probe_consumers", failed)
    with pytest.raises(RuntimeError, match="restored"):
        m.migrate(Service(), "accept")
    assert recovered == [backup]


def test_interrupted_cutover_requires_rollback(tmp_path, monkeypatch):
    import contextlib
    from outbound_proxy import migrate as m

    class Service:
        exclusive = staticmethod(contextlib.nullcontext)

    backup = tmp_path / "legacy-backup"
    backup.mkdir()
    (backup / "handover.json").write_text('{"phase":"pending"}')
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr(m, "OLD_RUN", tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path)
    with pytest.raises(RuntimeError, match="rollback"):
        m.migrate(Service(), "accept")


def test_recovery_rebuilds_saved_configuration_after_reboot(tmp_path, monkeypatch):
    from outbound_proxy import migrate as m
    import json
    import shutil

    old = tmp_path / "old"
    old.mkdir()
    legacy_tree(old)
    backup = tmp_path / "saved"
    shutil.copytree(old, backup / "legacy", symlinks=True)
    (backup / "firewall.nft").write_text("table inet minipc_proxy {}")
    (old / "upstream.url").rename(old / "upstream.url.before-outbound-proxy")
    runtime = tmp_path / "run"
    runtime.mkdir()
    units = tmp_path / "units"
    units.mkdir()
    monkeypatch.setattr(m, "LEGACY", old)
    monkeypatch.setattr(m, "OLD_RUN", runtime)
    monkeypatch.setattr(m, "SYSTEM", units)
    commands = []
    monkeypatch.setattr(m, "command", lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(m.legacy, "wait_for_listener", lambda port: commands.append(("ready", port)))
    monkeypatch.setattr(m, "probe_consumers", lambda members: commands.append(("consumers", tuple(members))))

    class Service:
        def get_state(self):
            return {"upstreams": []}

        def import_state(self, state):
            assert json.loads((backup / "handover.json").read_text())["phase"] == "restoring"

    journal = {"phase": "serving", "old_state": {}, "members": ["fixture"], "verified_name": "generation-real",
               "units": {u: {"active": True} for u in m.UNITS}}
    m.restore(Service(), backup, journal)
    assert journal["phase"] == "restored"
    assert (runtime / "active").resolve() == (old / "verified").resolve()
    assert (old / "verified/tinyproxy.conf").read_bytes() == (backup / "legacy/generation-real/tinyproxy.conf").read_bytes()
    assert (old / "upstream.url").exists()
    assert commands[-1] == ("consumers", ("fixture",))


def test_prepared_catalog_maps_verified_route_and_preserves_order(tmp_path):
    from outbound_proxy import migrate as m
    entries = legacy_tree(tmp_path)
    old = m.import_catalog(tmp_path, ",".join(entries), True, True, 60)
    prepared = dict(old, enabled=False, selected="ft4", upstreams=[
        {"name": "ft4", "uri": entries[1].removesuffix(":443")},
        {"name": "ft3", "uri": entries[0]}
    ])
    result = m.handover_state(old, prepared, "ft4")
    assert result == dict(prepared, enabled=True)
    assert not prepared["enabled"]
    for changed in (dict(selected="ft3"), dict(enabled=True), dict(port=19999), dict(failover=False),
                    dict(upstreams=[*prepared["upstreams"][:1], {"name":"ft3", "uri": entries[0].replace("one@", "other@")}])):
        with pytest.raises(RuntimeError):
            m.handover_state(old, dict(prepared, **changed), "ft4")
    with pytest.raises(RuntimeError):
        m.handover_state(old, prepared, "other")
