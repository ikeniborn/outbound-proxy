"""Exercise the browser boundary over an actual loopback socket."""

import base64
import http.client
import io
import json
import shutil
import socket
import subprocess
import tempfile
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_docker_access import HostCommands
from test_store import Host, operations

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from outbound_proxy.cli import dispatch
from outbound_proxy.docker_access import Access
from outbound_proxy.ui import ManagementServer

FIXTURE_SECRET = "not-for-browser-output"


@pytest.fixture(autouse=True)
def panel_port(monkeypatch, tmp_path):
    host = HostCommands()
    host.network["Containers"] = {}
    monkeypatch.setattr(Host, "access", Access(tmp_path, host), raising=False)
    # Keep the same origin across restarts within a test, even on an installed host.
    from outbound_proxy import ui
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    monkeypatch.setattr(ui, "PANEL_PORT", port)
    return port


@contextmanager
def listening(ops):
    with ManagementServer(ops) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join()


def request(server, path, data=None, **headers):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    method = "GET" if data is None else "POST"
    if method == "POST":
        headers.setdefault("Origin", server.origin)
        headers.setdefault("Content-Type", "application/json")
    try:
        connection.request(method, path, headers=headers,
                                body=None if data is None else json.dumps(data).encode())
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def keypair():
    private = subprocess.check_output(["/usr/bin/openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout"])
    public = subprocess.check_output(["/usr/bin/openssl", "pkey", "-pubout", "-outform", "DER"], input=private)
    return private, base64.b64encode(public).decode()


def pair(server, public, code=None):
    from outbound_proxy.pairing import issue_code
    code = code or issue_code(server.ops.store.root)
    return request(server, "/api/pair", {"code": code, "public": public})


def proof(server, pair_id, private):
    status, _, body = request(server, "/api/challenge", {"id": pair_id})
    assert status == 200
    challenge = json.loads(body)["challenge"]
    with tempfile.NamedTemporaryFile() as f:
        f.write(private)
        f.flush()
        signature = subprocess.check_output(["/usr/bin/openssl", "dgst", "-sha256", "-sign", f.name],
                                            input=challenge.encode())
    return {"id": pair_id, "challenge": challenge, "signature": base64.b64encode(signature).decode()}


def login(server, paired=None):
    if paired is None:
        private, public = keypair()
        status, _, body = pair(server, public)
        assert status == 200 and "csrf" not in json.loads(body)
        paired = json.loads(body)["id"], private
    status, headers, body = request(server, "/api/login", proof(server, *paired))
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"]
    assert "SameSite=Strict" in headers["Set-Cookie"]
    return {"Cookie": headers["Set-Cookie"].split(";")[0],
            "X-CSRF-Token": json.loads(body)["csrf"]}


def test_local_assets_and_private_state(tmp_path, panel_port):
    with listening(operations(tmp_path)) as server:
        assert server.server_address == ("127.0.0.1", panel_port)
        for path in ("/", "/app.js", "/style.css"):
            status, headers, body = request(server, path)
            assert status == 200 and "no-store" in headers["Cache-Control"]
        assert request(server, "/api/state")[0] == 403
        assert request(server, "/", Host="local.invalid")[0] == 403


def test_codes_expire_rotate_and_are_consumed_once_under_concurrency(tmp_path, monkeypatch):
    from outbound_proxy import pairing
    private, public = keypair()
    with listening(operations(tmp_path)) as server:
        first = pairing.issue_code(tmp_path)
        second = pairing.issue_code(tmp_path)
        assert pair(server, public, first)[0] == 403
        with monkeypatch.context() as patch:
            patch.setattr(pairing, "wall_time", lambda: 1e20)
            assert pair(server, public, second)[0] == 403
        payload = {"code": second, "public": public}
        assert request(server, "/api/pair", payload, Origin="null")[0] == 403
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: pair(server, public, second)[0], range(2)))
        assert sorted(statuses) == [200, 403]
        record = tmp_path / "panel-pairing/records.json"
        assert first not in record.read_text() and second not in record.read_text()
        assert record.stat().st_mode & 0o077 == 0


def test_bound_challenges_replay_expiry_restart_and_revocation(tmp_path, monkeypatch):
    from outbound_proxy import ui
    ops = operations(tmp_path)
    private, public = keypair()
    other, other_public = keypair()
    with listening(ops) as server:
        one = json.loads(pair(server, public)[2])["id"]
        two = json.loads(pair(server, other_public)[2])["id"]
        wrong = proof(server, one, other)
        assert request(server, "/api/login", wrong)[0] == 403
        correct = proof(server, one, private)
        assert request(server, "/api/login", {**correct, "id": two})[0] == 403
        correct = proof(server, one, private)
        assert request(server, "/api/login", correct, Origin="null")[0] == 403
        assert request(server, "/api/login", correct)[0] == 200
        assert request(server, "/api/login", correct)[0] == 403
        expired = proof(server, one, private)
        headers = login(server, (one, private))
        with monkeypatch.context() as patch:
            patch.setattr(ui, "monotonic", lambda: 1e20)
            assert request(server, "/api/login", expired)[0] == 403
            assert request(server, "/api/state", **headers)[0] == 403
        pending = proof(server, one, private)
    with listening(ops) as restarted:
        assert request(restarted, "/api/login", pending)[0] == 403
        assert request(restarted, "/api/state", **headers)[0] == 403
        headers = login(restarted, (one, private))
        assert request(restarted, "/api/state", **headers)[0] == 200
    shutil.rmtree(tmp_path / "panel-pairing")
    with listening(ops) as reinstalled:
        assert request(reinstalled, "/api/challenge", {"id": one})[0] == 403


def test_reject_unauthorized_actions_without_changes(tmp_path):
    ops = operations(tmp_path)
    before = ops.run("status")
    with listening(ops) as server:
        auth = login(server)
        for headers in ({}, {"Cookie": auth["Cookie"]}, {"X-CSRF-Token": auth["X-CSRF-Token"]},
                        {**auth, "Origin": "https://elsewhere.test"},
                        {**auth, "Host": "127.0.0.1:80"}):
            assert request(server, "/api/action", {"action": "enable"}, **headers)[0] == 403
            assert ops.run("status") == before
        for action in ({"action": "enable", "name": "edge"}, {"action": "port", "value": True},
                       {"action": "docker", "value": "false"}, {"action": "shell"},
                       {"action": "use", "name": ["edge"]}, [], {"action": "add", "name": "edge"}):
            assert request(server, "/api/action", action, **auth)[0] == 400
            assert ops.run("status") == before


def test_cli_and_browser_share_catalog_service_state_and_redacted_errors(tmp_path, capsys):
    host = Host()
    cli = operations(tmp_path, host)
    web = operations(tmp_path, host)
    credential = f"https://operator:{FIXTURE_SECRET}@proxy.example"
    dispatch(["add", "edge"], cli, io.StringIO(credential))
    dispatch(["use", "edge"], cli)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    dispatch(["port", str(port)], cli)
    with listening(web) as server:
        auth = login(server)
        status, headers, body = request(server, "/api/state", **auth)
        assert status == 200
        initial = json.loads(body)
        assert initial["port"] == port
        assert initial["upstreams"] == [{"name": "edge", "host": "proxy.example", "port": 443}]
        assert initial["selected"] == "edge"
        assert FIXTURE_SECRET.encode() not in body
        for action in ("enable", "disable", "enable"):
            status, headers, body = request(server, "/api/action", {"action": action}, **auth)
            assert status == 200
            assert json.loads(body)["running"] == (action == "enable")
            dispatch(["status"], cli)
            assert cli.run("status")["boot_enabled"] == (action == "enable")
        status, _, body = request(server, "/api/action", {
            "action": "update", "name": "edge", "uri": credential.replace("proxy.example", "new.example")
        }, **auth)
        assert status == 200
        assert cli.run("list")["upstreams"] == [{"name": "edge", "host": "new.example", "port": 443}]
        for action in ({"action": "update", "name": "edge"},
                       {"action": "check", "name": "edge"},
                       {"action": "failover", "value": True}):
            assert request(server, "/api/action", action, **auth)[0] == 200
        assert cli.run("status")["failover"]
        status, headers, body = request(server, "/api/action", {
            "action": "update", "name": "edge", "uri": FIXTURE_SECRET
        }, **auth)
        assert status == 409
        assert FIXTURE_SECRET.encode() not in body
        output = capsys.readouterr()
        assert FIXTURE_SECRET not in output.err + output.out


def test_unexpected_operation_exception_stays_private(capfd, tmp_path):
    class Broken:
        store = operations(tmp_path).store
        def run(self, **_):
            raise RuntimeError(FIXTURE_SECRET)

    with listening(Broken()) as server:
        status, _, body = request(server, "/api/state", **login(server))
        assert status == 500
        assert FIXTURE_SECRET.encode() not in body
        output = capfd.readouterr()
        assert FIXTURE_SECRET not in output.err + output.out


def test_cookie_alone_cannot_restore_session_read_or_mutate(tmp_path):
    ops = operations(tmp_path)
    with listening(ops) as server:
        auth = login(server)
        cookie = {"Cookie": auth["Cookie"]}
        assert request(server, "/api/session", {}, **cookie)[0] == 404
        assert request(server, "/api/state", **cookie)[0] == 403
        assert request(server, "/api/action", {"action": "failover", "value": True}, **cookie)[0] == 403
        assert not ops.run("status")["failover"]
        assert request(server, "/api/state", **auth)[0] == 200


def test_terminal_pairing_code_install_repeat_and_unattended(tmp_path, monkeypatch, capsys):
    from outbound_proxy import pairing
    from unittest.mock import patch
    import pytest

    with patch.object(pairing, "open", side_effect=OSError, create=True):
        pairing.print_code(tmp_path, required=False)
        assert not (tmp_path / "panel-pairing").exists()
        with pytest.raises(RuntimeError, match="terminal"):
            pairing.print_code(tmp_path)

    terminal = tmp_path / "terminal"
    monkeypatch.setattr(pairing, "open", lambda *_args, **_kwargs: terminal.open("a"), raising=False)
    pairing.print_code(tmp_path, required=False)
    initial = terminal.read_text()
    code = initial.strip().split()[-1]
    pairing.print_code(tmp_path, required=False)
    assert terminal.read_text() == initial
    with listening(operations(tmp_path)) as server:
        assert pair(server, keypair()[1], code)[0] == 200
    pairing.print_code(tmp_path, required=False)
    assert terminal.read_text() == initial
    pairing.print_code(tmp_path)
    assert terminal.read_text().strip().split()[-1] != code
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_real_browser_reopens_and_is_revoked(tmp_path):
    api = pytest.importorskip("playwright.sync_api")
    from outbound_proxy.pairing import issue_code

    state = tmp_path / "state"
    ops = operations(state)
    network = ops.native.access.run.network
    network["Containers"]["one"] = {"Name": "sample-worker", "IPv4Address": "192.168.70.4/28"}
    with api.sync_playwright() as playwright:
        browser = shutil.which("google-chrome") or shutil.which("chromium") or playwright.chromium.executable_path
        if not Path(browser).is_file():
            pytest.skip("Chromium is required for the browser integration test")

        def profile(name):
            return playwright.chromium.launch_persistent_context(str(tmp_path / name), executable_path=browser,
                                                                 headless=True)

        with listening(ops) as server, profile("operator") as operator:
            page = operator.pages[0]
            page.goto(server.origin)
            page.get_by_label("Pairing code").fill(issue_code(state))
            page.get_by_role("button", name="Pair browser").click()
            page.get_by_text("Stopped", exact=True).wait_for(timeout=5000)
            assert not page.get_by_label("Docker bridge access", exact=True).is_checked()
            page.get_by_role("row").filter(has_text="sample-worker").get_by_text("192.168.70.4").wait_for()
            network["Containers"].clear()
            page.get_by_role("button", name="Refresh status").click()
            page.get_by_text("Attached containers (0)", exact=True).wait_for()
            assert not page.get_by_text("sample-worker").count()
            assert page.evaluate("async () => !(await storedKey()).privateKey.extractable")
            page.reload()
            page.get_by_text("Stopped", exact=True).wait_for(timeout=5000)
            with profile("guest") as guest:
                another = guest.pages[0]
                another.goto(server.origin)
                assert another.evaluate("async () => (await fetch('/api/state')).status") == 403
                assert another.get_by_role("heading", name="Pair this browser").is_visible()
        with listening(ops) as restarted, profile("operator") as retained:
            page = retained.pages[0]
            page.goto(restarted.origin)
            page.get_by_text("Stopped", exact=True).wait_for(timeout=5000)
        shutil.rmtree(state / "panel-pairing")
        with listening(ops) as reinstalled, profile("operator") as revoked:
            page = revoked.pages[0]
            page.goto(reinstalled.origin)
            page.get_by_role("status").get_by_text("Pairing or sign-in failed.", exact=False).wait_for(timeout=5000)
            assert not page.get_by_role("heading", name="Upstreams").is_visible()


def test_expiry_is_rechecked_after_blocking_verification_and_body_read(tmp_path, monkeypatch):
    from outbound_proxy import pairing, ui
    private, public = keypair()
    ops = operations(tmp_path)
    with listening(ops) as server:
        pair_id = json.loads(pair(server, public)[2])["id"]
        signed = proof(server, pair_id, private)
        original_verify = pairing.verify

        def slow_verification(*args):
            original_verify(*args)
            monkeypatch.setattr(ui, "monotonic", lambda: 1e20)

        with monkeypatch.context() as patch:
            patch.setattr(pairing, "verify", slow_verification)
            assert request(server, "/api/login", signed)[0] == 403
        monkeypatch.undo()
        auth = login(server, (pair_id, private))
        read = ui.Handler.read_object

        def slow_read(handler):
            data = read(handler)
            monkeypatch.setattr(ui, "monotonic", lambda: 1e20)
            return data

        monkeypatch.setattr(ui.Handler, "read_object", slow_read)
        assert request(server, "/api/action", {"action": "failover", "value": True}, **auth)[0] == 403
        assert not ops.run("status")["failover"]
