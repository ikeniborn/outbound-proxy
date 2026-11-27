"""Check container boundaries and real forwarding with loopback-only fixtures."""

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading

import pytest

from test_native_tls import Target, Upstream, fixture_adapter, issue_certificate, serving, tls_directory
from test_store import Host
from test_ui import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from outbound_proxy import container_runtime, store, ui
from outbound_proxy.container import LocalPanel
from outbound_proxy.container_inventory import Inventory
from outbound_proxy.container_runtime import ContainerOperations, ContainerRuntime, Network
from outbound_proxy.store import OperationError


def settings(**extra):
    env = {"OP_MANAGEMENT_IP": "127.0.0.1", "OP_MANAGEMENT_SOURCES": "127.0.0.1/32",
           "OP_FORWARD_IP": "127.0.0.2", "OP_FORWARD_SOURCES": "127.0.0.1/32",
           "OP_HOST_PORT": "23345"}
    return Network.configured({**env, **extra})


@contextmanager
def panel(tmp_path, monkeypatch):
    host = Host()
    host.network = settings()
    ops = ContainerOperations(tmp_path, host, Inventory())
    monkeypatch.setattr(ui, "PANEL_PORT", 0)
    with LocalPanel(ops, host.network) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def test_local_session_and_published_endpoints(tmp_path, monkeypatch):
    with panel(tmp_path, monkeypatch) as server:
        assert server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, 32) == b"lo\0"
        assert not server.verify_request(None, ("127.0.0.3", 1024))
        assert request(server, "/api/session", {}, Host="remote.invalid")[0] == 403
        assert request(server, "/api/session", {}, Origin="null")[0] == 403
        assert request(server, "/api/pair", {})[0] == 404
        assert request(server, "/api/state")[0] == 403
        status, headers, body = request(server, "/api/session", {})
        assert status == 200 and "HttpOnly" in headers["Set-Cookie"]
        auth = {"Cookie": headers["Set-Cookie"].split(";")[0], "X-CSRF-Token": json.loads(body)["csrf"]}
        assert request(server, "/api/action", {"action": "enable"})[0] == 403
        assert request(server, "/api/action", {"action": "failover", "value": True},
                       Origin="http://another.invalid", **auth)[0] == 403
        status, _, body = request(server, "/api/state", **auth)
        view = json.loads(body)
        assert status == 200 and not view["enabled"] and not view["failover"]
        assert view["deployment"]["endpoints"] == ["127.0.0.1:23345"]
        assert view["bridge"]["status"] == "unavailable"
        assert request(server, "/api/action", {"action": "port", "value": 23456}, **auth)[0] == 409


def test_container_browser_opens_without_pairing(tmp_path, monkeypatch):
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as playwright, panel(tmp_path, monkeypatch) as server:
        browser = shutil.which("google-chrome") or playwright.chromium.executable_path
        if not Path(browser).is_file():
            pytest.skip("Chromium is required for browser integration")
        with playwright.chromium.launch(executable_path=browser) as browser:
            page = browser.new_page()
            page.goto(server.origin)
            page.get_by_text("Stopped", exact=True).wait_for()
            assert page.get_by_text("127.0.0.1:23345").is_visible()
            assert not page.get_by_label("Pairing code").is_visible()
            assert not page.get_by_role("button", name="Save port").is_visible()
            page.get_by_label("Automatic failover").check()
            page.locator('#panel[aria-busy="false"]').wait_for()
            page.reload()
            page.get_by_text("Stopped", exact=True).wait_for()
            assert page.get_by_label("Automatic failover").is_checked()


def test_no_broad_panel_acl_or_overlapping_client_listener():
    with pytest.raises(ValueError):
        settings(OP_MANAGEMENT_SOURCES="172.29.240.0/28")
    with pytest.raises(ValueError):
        settings(OP_CLIENT_IP="127.0.0.2", OP_CLIENT_SUBNET="127.0.0.0/8")


def test_real_pipeline_restart_selection_and_private_children(tls_directory, monkeypatch):
    for binary in ("stunnel", "tinyproxy", "curl", "openssl"):
        if not shutil.which(binary):
            pytest.skip(f"Real pipeline requires {binary}")
    cert, key = issue_certificate(tls_directory, "fixture", ["localhost", "example.com"])
    fixture = fixture_adapter(tls_directory, cert)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(container_runtime, "FORWARD_PORT", port)
    defaults = store.defaults
    monkeypatch.setattr(store, "defaults", lambda: {**defaults(), "port": port})
    network = settings(OP_CLIENT_IP="127.0.0.3", OP_CLIENT_SUBNET="127.0.0.0/8")
    runtime = ContainerRuntime(network, fixture.runtime, host=fixture.host)
    root = tls_directory / "state"
    ops = ContainerOperations(root, runtime, Inventory())
    with serving(Target, cert, key) as target, serving(Upstream, cert, key, target.server_address) as upstream:
        uri = f"https://fixture:fixture-pass@localhost:{upstream.server_address[1]}"
        try:
            ops.run("add", name="ok", uri=uri)
            ops.run("use", name="ok")
            ops.run("add", name="wrong", uri=uri.replace("fixture-pass", "wrong-password"))
            assert ops.run("enable")["running"] and runtime.interval is None
            forwarded = subprocess.run([fixture.host.curl, "-q", "--silent", "--noproxy", "",
                "--max-time", "5", "--cacert", str(cert), "--proxy", f"http://{network.forward}:{port}",
                "https://example.com/"], capture_output=True, check=True)
            assert forwarded.stdout == b"ok"
            with socket.create_connection((network.forward, port), timeout=2,
                                          source_address=("127.0.0.55", 0)) as denied:
                denied.sendall(b"CONNECT example.com:443 HTTP/1.0\r\n\r\n")
                assert b"403" in denied.recv(1024).splitlines()[0]
            with pytest.raises(OSError):
                socket.create_connection((network.client, port), timeout=1)
            with pytest.raises(OperationError):
                ops.run("use", name="wrong")
            assert ops.run("status")["selected"] == "ok"
            assert "fixture-pass" not in repr(ops.run("docker", value=True))
            with socket.create_connection((network.client, port), timeout=1):
                pass
            ops.run("failover", value=True)
            assert runtime.interval == 60
            with serving(Upstream, cert, key, target.server_address) as standby:
                ops.run("add", name="standby", uri=uri.rsplit(":", 1)[0] + f":{standby.server_address[1]}")
                probe = runtime.probe
                calls = []

                def active_health(port):
                    if len(calls) < 2:
                        calls.append(port)
                        raise RuntimeError("Current route is unavailable")
                    return probe(port)

                with monkeypatch.context() as patch:
                    patch.setattr(runtime, "probe", active_health)
                    assert ops.run("tick")["selected"] == "standby"
                assert standby.observations[-1][1]
                ops.run("use", name="ok")
            runtime.close()
            host_only = ContainerRuntime(settings(), fixture.runtime, host=fixture.host)
            runtime = host_only
            restarted = ContainerOperations(root, host_only, Inventory())
            after = restarted.run("boot")
            assert after["running"] and not after["docker"] and not after["docker_supported"]
            with socket.create_connection((network.forward, port), timeout=1):
                pass
            with pytest.raises(OSError):
                socket.create_connection((network.client, port), timeout=1)
            with restarted.exclusive():
                assert restarted.get_state()["docker"] is False
            assert restarted.run("disable")["running"] is False
            assert runtime.interval is None
            for ip in ("127.0.0.1", network.forward, network.client):
                with pytest.raises(OSError):
                    socket.create_connection((ip, port), timeout=1)
        finally:
            runtime.close()


def test_operator_selected_inventory_network():
    from outbound_proxy.container_inventory import inventory_network
    assert inventory_network({}) == "outbound-proxy-clients"
    assert inventory_network({"OP_INVENTORY_NETWORK": "minipc-outbound-proxy"}) == "minipc-outbound-proxy"
    with pytest.raises(ValueError):
        inventory_network({"OP_INVENTORY_NETWORK": "../containers/json"})


def test_inventory_only_requests_fixed_network_and_projects_response(monkeypatch):
    from outbound_proxy import container_inventory as inventory
    calls = []
    record = {"Name": inventory.NETWORK, "Driver": "bridge", "Internal": True,
              "IPAM": {"Config": [{"Subnet": "172.29.241.0/28", "Gateway": "172.29.241.1"}]},
              "Containers": {"secret-id": {"Name": "worker", "IPv4Address": "172.29.241.4/28"}},
              "Labels": {"private-label": "private-value"}}

    class Daemon:
        status = 200

        def request(self, method, path):
            calls.append((method, path))

        def getresponse(self):
            return self

        def read(self, limit):
            return json.dumps(record).encode()

        def close(self):
            pass

    monkeypatch.setattr(inventory, "DockerConnection", lambda _: Daemon())
    result = inventory.inspect_network()
    assert calls == [("GET", "/networks/outbound-proxy-clients")]
    assert result["members"] == [{"name": "worker", "ip": "172.29.241.4"}]
    assert "secret-id" not in repr(result) and "private-value" not in repr(result)


def test_cli_uses_running_supervisor_and_redacts_input(tmp_path, monkeypatch, capsys):
    from contextlib import ExitStack
    from types import SimpleNamespace
    from outbound_proxy import container_cli, container_inventory
    from outbound_proxy.container import Command, ControlServer, start_server

    host = Host()
    host.network = settings()
    path = tmp_path / "control.sock"
    monkeypatch.setattr(container_cli, "CONTROL", path)
    with ExitStack() as stack:
        server = ControlServer(str(path), Command)
        server.ops = ContainerOperations(tmp_path / "catalog", host, Inventory())
        start_server(stack, server)
        monkeypatch.setattr(sys, "argv", ["outbound-proxy", "add", "example"])
        calls = []
        with monkeypatch.context() as bootstrap:
            bootstrap.setattr(container_cli, "os", SimpleNamespace(
                environ={"OP_INVENTORY": "1"}, geteuid=lambda: 0))
            bootstrap.setattr(container_inventory, "os", SimpleNamespace(
                setgroups=lambda gids: calls.append(("groups", gids)),
                setgid=lambda gid: calls.append(("gid", gid)),
                setuid=lambda uid: calls.append(("uid", uid))))

            def read_credential(_):
                assert calls == [("groups", []), ("gid", 10001), ("uid", 10001)]
                return "https://operator:secret@example.invalid"

            bootstrap.setattr(container_cli, "credential", read_credential)
            assert container_cli.main() == 0
        view = capsys.readouterr().out
        assert "secret" not in view and json.loads(view)["upstreams"][0]["host"] == "example.invalid"
        monkeypatch.setattr(sys, "argv", ["outbound-proxy", "status"])
        assert container_cli.main() == 0
        assert json.loads(capsys.readouterr().out)["upstreams"][0]["name"] == "example"


def test_explicit_stop_recovers_after_client_network_revocation(tmp_path):
    class Offline(Host):
        docker_supported = False
        network = settings()

        def apply(self, state):
            if state["enabled"]:
                raise RuntimeError("Committed listener is unavailable")
            return super().apply(state)

    host = Offline()
    ops = ContainerOperations(tmp_path, host, Inventory())
    with ops.exclusive():
        previous = ops.store.current()
        state = ops.get_state()
        state.update(upstreams=[{"name": "edge", "uri": "https://fixture:private@upstream.invalid"}],
                     selected="edge", port=24444, enabled=True, docker=True)
        committed = ops.store.stage(state)
        ops.store.commit(committed)
        ops.store.write_pending(previous, committed, "forwarding")
    with pytest.raises(OperationError):
        ops.run("boot")
    assert ops.run("status")["recovery_pending"]
    result = ops.run("disable")
    assert result["running"] is False and not result["recovery_pending"]
    assert result["port"] == 18888 and result["selected"] == "edge"
    with ops.exclusive():
        assert ops.get_state()["docker"] is False
