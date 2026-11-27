import socket

import pytest

from test_store import Host, URI, SECOND, operations


class Native(Host):
    def __init__(self):
        super().__init__()
        self.rejected_uris = set()
        self.bad_port = None
        self.unavailable = False
        self.stable_outcomes = []
        self.interrupt = False

    def check(self, uri):
        if uri in self.rejected_uris:
            raise RuntimeError(uri)

    def apply(self, state):
        self.live = state
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt()
        if state["port"] == self.bad_port or self.unavailable:
            raise RuntimeError(URI)

    def probe(self, port):
        assert self.live["port"] == port
        if not self.stable_outcomes.pop(0):
            raise RuntimeError(URI)


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def configured(tmp_path):
    host = Native()
    manager = operations(tmp_path, host)
    manager.run("add", "one", URI)
    manager.run("add", "two", SECOND)
    manager.run("use", "one")
    manager.run("port", value=available_port())
    return manager, host


def test_manual_check_and_disabled_selection(tmp_path):
    manager, host = configured(tmp_path)
    view = manager.run("check", "two")
    assert view["last_check"]["name"] == "two"
    assert view["last_check"]["outcome"] == "passed"
    assert view["last_check"]["time"]
    assert view["selected"] == "one"
    host.rejected_uris.add(SECOND)
    with pytest.raises(RuntimeError) as error:
        manager.run("use", "two")
    assert "secret" not in str(error.value)
    assert manager.run("status")["last_check"]["outcome"] == "failed"
    assert not manager.run("status")["running"]


def test_rollback_failed_stable_probe_and_active_credentials(tmp_path):
    manager, host = configured(tmp_path)
    assert manager.run("enable")["running"]
    previous_port = host.live["port"]
    host.bad_port = available_port()
    with pytest.raises(RuntimeError, match="Proxy change failed"):
        manager.run("port", value=host.bad_port)
    assert host.live["port"] == manager.run("status")["port"] == previous_port
    host.rejected_uris.add(SECOND.replace("two.example", "bad.example"))
    with pytest.raises(RuntimeError):
        manager.run("update", "one", SECOND.replace("two.example", "bad.example"))
    assert host.live["upstreams"][0]["uri"] == URI
    assert manager.run("status")["upstreams"][0]["host"] == "one.example"


def test_conflicting_ports_while_stopped(tmp_path):
    manager, host = configured(tmp_path)
    previous_port = manager.run("status")["port"]
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        for port in [sock.getsockname()[1], 18889, 18890, 1023, 65536, True]:
            with pytest.raises(RuntimeError):
                manager.run("port", value=port)
    assert manager.run("status")["port"] == previous_port


def test_failover_two_checks_ordered_scan_and_disable(tmp_path, monkeypatch):
    monkeypatch.setattr("minipc_proxy_apply.time.sleep", lambda seconds: None)
    manager, host = configured(tmp_path)
    manager.run("add", "three", SECOND.replace("two.example", "three.example"))
    manager.run("failover", value=True)
    manager.run("enable")
    host.stable_outcomes = [False, True]
    assert manager.run("tick")["selected"] == "one"
    host.stable_outcomes = [False, False]
    host.rejected_uris.add(SECOND)
    assert manager.run("tick")["selected"] == "three"
    host.stable_outcomes = [True]
    assert manager.run("tick")["selected"] == "three"
    stopped = manager.run("disable")
    assert not stopped["running"] and not stopped["boot_enabled"] and stopped["failover"]
    assert manager.run("tick")["selected"] == "three"


def test_reconcile_interrupted_activation_and_degraded_rollback(tmp_path):
    manager, host = configured(tmp_path)
    manager.run("enable")
    host.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        manager.run("use", "two")
    resumed = operations(tmp_path, host)
    status = resumed.run("status")
    assert status["recovery_pending"] and status["selected"] == "one"
    assert resumed.run("recover")["recovery_pending"] is False
    assert host.live["selected"] == "one"
    host.unavailable = True
    with pytest.raises(RuntimeError, match="recovery|rollback"):
        resumed.run("use", "two")
    assert resumed.run("status")["recovery_pending"]
    host.unavailable = False
    assert resumed.run("boot")["selected"] == host.live["selected"] == "one"


def test_interrupt_after_commit_recovers_new_state(tmp_path, monkeypatch):
    manager, host = configured(tmp_path)
    manager.run("enable")
    with monkeypatch.context() as patch:
        def interrupt():
            raise KeyboardInterrupt()
        patch.setattr(manager.store, "clear_pending", interrupt)
        with pytest.raises(KeyboardInterrupt):
            manager.run("use", "two")
    resumed = operations(tmp_path, host)
    assert resumed.run("status")["recovery_pending"]
    assert resumed.run("recover")["selected"] == host.live["selected"] == "two"


def test_failure_writing_journal_leaves_services_untouched(tmp_path, monkeypatch):
    manager, host = configured(tmp_path)
    manager.run("enable")
    original = host.live
    def refuse(*args):
        raise OSError("disk full")
    monkeypatch.setattr(manager.store, "write_pending", refuse)
    with pytest.raises(RuntimeError):
        manager.run("use", "two")
    assert host.live is original
    assert manager.run("status")["selected"] == "one"


def test_import_state_verifies_and_restores_on_error(tmp_path):
    manager, host = configured(tmp_path)
    manager.run("enable")
    with manager.exclusive():
        candidate = manager.get_state()
        previous_port = candidate["port"]
        candidate.update(selected="two", interval=120, docker=True, failover=True)
        view = manager.import_state(candidate)
        assert view["selected"] == "two" and view["interval"] == 120 and view["docker"]
        candidate["port"] = available_port()
        host.bad_port = candidate["port"]
        with pytest.raises(RuntimeError):
            manager.import_state(candidate)
    assert host.live["port"] == previous_port and host.live["selected"] == "two"


def test_docker_unavailable_while_stopped(tmp_path):
    manager, host = configured(tmp_path)
    host.docker_supported = False
    with pytest.raises(RuntimeError):
        manager.run("docker", value=True)
    assert manager.run("status")["docker"] is False


@pytest.mark.parametrize("action", ["port", "docker"])
def test_live_listener_changes_verify_candidate_before_touching_services(tmp_path, action):
    setting = available_port() if action == "port" else True
    manager, host = configured(tmp_path)
    manager.run("enable")
    previous = host.live
    host.rejected_uris.add(URI)
    with pytest.raises(RuntimeError, match="verification"):
        manager.run(action, value=setting)
    assert host.live is previous


def test_boot_interruption_retains_recovery_evidence(tmp_path):
    manager, host = configured(tmp_path)
    manager.run("enable")
    host.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        manager.run("boot")
    resumed = operations(tmp_path, host)
    assert resumed.run("status")["recovery_pending"]
    assert resumed.run("recover")["running"]


def test_failed_live_credential_activation_restores_prior_secret(tmp_path):
    class CredentialRejectingHost(Native):
        def apply(self, state):
            super().apply(state)
            if state["upstreams"][0]["uri"] == URI.replace("secret", "replacement"):
                raise RuntimeError("Stable route failed")

    configured(tmp_path)
    host = CredentialRejectingHost()
    manager = operations(tmp_path, host)
    manager.run("enable")
    with pytest.raises(RuntimeError):
        manager.run("update", "one", URI.replace("secret", "replacement"))
    with manager.exclusive():
        assert manager.get_state()["upstreams"][0]["uri"] == URI
    assert host.live["upstreams"][0]["uri"] == URI


def test_pointer_failure_reconciles_to_side_that_committed(tmp_path, monkeypatch):
    manager, host = configured(tmp_path)
    manager.run("enable")
    commit = manager.store.commit
    for after_write, expected in [(False, "one"), (True, "two")]:
        def fail(candidate):
            if after_write:
                commit(candidate)
            raise OSError("disk failure")
        with monkeypatch.context() as patch:
            patch.setattr(manager.store, "commit", fail)
            with pytest.raises(RuntimeError):
                manager.run("use", "two")
        status = manager.run("status")
        assert not status["recovery_pending"]
        assert status["selected"] == host.live["selected"] == expected


def test_failover_preference_recovery_keeps_original_service_running(tmp_path):
    manager, host = configured(tmp_path)
    manager.run("enable")
    previous = host.live
    def interrupted_schedule(enabled, interval):
        host.scheduled = enabled
        raise KeyboardInterrupt()
    host.schedule = interrupted_schedule
    with pytest.raises(KeyboardInterrupt):
        manager.run("failover", value=True)
    host.schedule = lambda enabled, interval: setattr(host, "scheduled", enabled)
    status = manager.run("recover")
    assert not status["recovery_pending"] and not status["failover"] and not host.scheduled
    host.rejected_uris.add(URI)
    assert manager.run("failover", value=True)["failover"]
    assert not manager.run("failover", value=False)["failover"]
    assert host.live is previous and not host.scheduled


def test_boot_after_interrupted_timer_change_starts_committed_forwarders(tmp_path):
    manager, host = configured(tmp_path)
    manager.run("enable")
    def interrupt(enabled, interval):
        raise KeyboardInterrupt()
    host.schedule = interrupt
    with pytest.raises(KeyboardInterrupt):
        manager.run("failover", value=True)
    host.schedule = lambda enabled, interval: None
    host.live = None
    status = manager.run("boot")
    assert status["running"] and not status["failover"] and not status["recovery_pending"]


def test_status_keeps_pending_visible_if_native_health_query_fails(tmp_path):
    manager, host = configured(tmp_path)
    manager.run("enable")
    host.interrupt = True
    with pytest.raises(KeyboardInterrupt):
        manager.run("use", "two")
    def unavailable():
        raise RuntimeError(URI)
    host.health = unavailable
    view = manager.run("status")
    assert view["selected"] == "one" and view["recovery_pending"]
    assert view["running"] is view["boot_enabled"] is None
    assert "secret" not in repr(view)


def test_migration_can_restore_disabled_catalog_with_broken_pending_route(tmp_path):
    manager, host = configured(tmp_path)
    with manager.exclusive():
        stopped = manager.get_state()
        manager.run("enable")
        committed = manager.store.current()
        manager.store.write_pending(committed, committed, "forwarding")
        host.rejected_uris.add(URI)
        applied = []

        def stop_only(state):
            applied.append(state["enabled"])
            if state["enabled"]:
                raise RuntimeError("Broken old route")
            host.live = state

        host.apply = stop_only
        manager.import_state(stopped)
        assert applied == [False]
        assert manager.store.pending() is None
        assert manager.get_state() == stopped
