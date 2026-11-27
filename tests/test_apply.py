import runpy
from pathlib import Path

import pytest


parse = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))["parse_upstream"]


def test_parse_encoded_password():
    assert parse("https://operator:p%21@proxy.example:8118\n") == (
        "proxy.example", 8118, "operator", "p!"
    )


def test_https_default_port():
    implicit = parse("https://u:p@proxy.example")
    assert implicit == parse("https://u:p@proxy.example:443") == ("proxy.example", 443, "u", "p")


@pytest.mark.parametrize("value", [
    "https://u:p@proxy.example:",
    "https://u:p@proxy.example:bad",
    "https://u:p@proxy.example:65536",
    "http://u:p@proxy.example:8118",
    "https://proxy.example:8118",
    "https://u:p@proxy.example:0",
    "https://u:p%0a@proxy.example:8118",
    "https://u:p@proxy.example:8118\nsecond",
    "https://u:p@proxy.example:8118/path",
    "https://u:p%40q@proxy.example:8118",
])
def test_reject_invalid_upstream(value):
    with pytest.raises(ValueError, match="Invalid upstream HTTPS URI") as error:
        parse(value)
    assert value not in str(error.value)


@pytest.mark.parametrize("failure", [None, "candidate", "activation", "rollback"])
def test_activate_generation(tmp_path, failure):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    current = tmp_path / "verified"
    current.symlink_to("previous")
    calls = []

    def probe_candidate(generation):
        calls.append(("probe", generation))
        if failure == "candidate":
            raise RuntimeError("private candidate details")

    def activate(generation):
        calls.append(("activate", generation))
        if failure in ("activation", "rollback") and generation == "new":
            raise RuntimeError("private activation details")
        if failure == "rollback" and generation == "previous":
            raise RuntimeError("private rollback details")
        assert current.readlink() == Path("previous")

    rotate = module["rotate"]
    if failure:
        with pytest.raises(RuntimeError, match=failure) as error:
            rotate(current, "new", probe_candidate, activate)
        assert "private" not in str(error.value)
        assert current.readlink() == Path("previous")
    else:
        rotate(current, "new", probe_candidate, activate)
        assert current.readlink() == Path("new")
    expected = [("probe", "new")]
    if failure != "candidate":
        expected.append(("activate", "new"))
    if failure in ("activation", "rollback"):
        expected.append(("activate", "previous"))
    assert calls == expected


def test_render_matching_pair():
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    tinyproxy, stunnel = module["render_pair"](
        ("proxy.example", 8118, "operator", "p!"),
        ("127.0.0.1", "172.30.40.1"), ("127.0.0.1", "172.30.40.0/28"),
        18888, 18889,
    )
    assert "Listen 172.30.40.1\n" in tinyproxy
    assert "Allow 172.30.40.0/28\n" in tinyproxy
    assert "Upstream http operator:p!@127.0.0.1:18889\n" in tinyproxy
    assert "accept = 127.0.0.1:18889\n" in stunnel
    assert "verifyChain = yes\n" in stunnel
    assert "checkHost = proxy.example\n" in stunnel
    assert "p!" not in stunnel


def test_retry_after_failed_activation(tmp_path):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    verified = tmp_path / "verified"
    verified.symlink_to("previous")
    calls = []

    def candidate(generation):
        calls.append(("candidate", generation))

    def activate(generation):
        calls.append(("activate", generation))
        if generation == "new" and calls.count(("activate", "new")) == 1:
            raise RuntimeError("failed live probe")

    with pytest.raises(RuntimeError, match="Proxy activation failed"):
        module["rotate"](verified, "new", candidate, activate)
    assert verified.readlink() == Path("previous")
    module["rotate"](verified, "new", candidate, activate)
    assert verified.readlink() == Path("new")
    assert calls == [
        ("candidate", "new"),
        ("activate", "new"),
        ("activate", "previous"),
        ("candidate", "new"),
        ("activate", "new"),
    ]


@pytest.mark.parametrize("statuses, switched", [
    ([True], False),
    ([False, True], False),
    ([False, False], True),
])
def test_failover_only_after_confirmed_failure(monkeypatch, statuses, switched):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    check = module["check_failover"]
    primary = ("first.example", 443, "u", "p")
    reserve = ("second.example", 443, "u", "p")
    switches = []

    def probe(_):
        if not statuses.pop(0):
            raise RuntimeError("failed probe")

    monkeypatch.setitem(check.__globals__, "probe", probe)
    monkeypatch.setitem(check.__globals__, "apply_upstream", switches.append)
    monkeypatch.setattr(module["time"], "sleep", lambda _: None)
    check([primary, reserve], reserve)
    assert not statuses
    assert switches == ([primary] if switched else [])


def test_parse_ordered_list():
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    parse_list = module["parse_upstreams"]
    first = "https://u:p@first.example:443"
    second = "https://u:p@second.example:443"
    assert len(parse_list(first)) == 1
    assert [item[0] for item in parse_list(f"{first}, {second}\n")] == ["first.example", "second.example"]
    for invalid in (f"{first},{first}", f"{first},", f"{first}\n,{second}", f"{first},{second},{first}"):
        with pytest.raises(ValueError):
            parse_list(invalid)


def test_failed_alternate_preserves_verified_generation(monkeypatch, tmp_path):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    check = module["check_failover"]
    first = ("first.example", 443, "u", "p")
    second = ("second.example", 443, "u", "p")
    verified = tmp_path / "verified"
    verified.symlink_to("current")

    def fail(_):
        raise RuntimeError("probe failed")

    monkeypatch.setitem(check.__globals__, "ROOT", tmp_path)
    monkeypatch.setitem(check.__globals__, "probe", fail)
    monkeypatch.setitem(check.__globals__, "verify_candidate", fail)
    monkeypatch.setitem(check.__globals__, "activate_generation", lambda _: pytest.fail("unexpected activation"))
    monkeypatch.setattr(module["time"], "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="candidate verification failed"):
        check([first, second], first)
    assert verified.readlink() == Path("current")


def test_large_upstream_file_keeps_every_entry(monkeypatch, tmp_path):
    import os
    import stat

    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    hosts = [f"proxy-{i}.example" for i in range(200)]
    source = ",".join(f"https://u:p@{host}:443" for host in hosts) + "\n"
    path = tmp_path / "upstream.url"
    path.write_text(source)
    path.chmod(0o600)
    real_fstat = os.fstat

    def root_owned(fd):
        fields = list(real_fstat(fd))
        fields[stat.ST_UID] = 0
        return os.stat_result(fields)

    with monkeypatch.context() as patch:
        patch.setattr(module["os"], "fstat", root_owned)
        assert module["read_source"](path) == source
    assert [entry[0] for entry in module["parse_upstreams"](source)] == hosts


@pytest.mark.parametrize("outcome", ["success", "all_failed", "activation_failed"])
def test_ordered_failover_after_multiple_failures(monkeypatch, tmp_path, outcome):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/minipc_proxy_apply.py"))
    check = module["check_failover"]
    entries = [(f"proxy-{i}.example", 443, "u", "p") for i in range(6)]
    active = entries[2]
    verified = tmp_path / "verified"
    verified.symlink_to("previous")
    attempts = []
    activations = []

    def active_probe(_):
        raise RuntimeError("active probe failed")

    def candidate_probe(entry):
        attempts.append(entry)
        if outcome == "all_failed" or entry != entries[4]:
            raise RuntimeError("failed isolated probe")

    def activate(generation):
        activations.append(generation)
        if outcome == "activation_failed" and generation != "previous":
            raise RuntimeError("failed live probe")

    for key, value in dict(ROOT=tmp_path, probe=active_probe, verify_candidate=candidate_probe,
                           activate_generation=activate).items():
        monkeypatch.setitem(check.__globals__, key, value)
    monkeypatch.setattr(module["time"], "sleep", lambda _: None)
    expected = [entries[i] for i in (0, 1, 3, 4)]
    if outcome == "success":
        check(entries, active)
        assert activations == [str(verified.readlink())]
    else:
        with pytest.raises(RuntimeError, match="candidate verification failed" if outcome == "all_failed" else "activation failed"):
            check(entries, active)
        assert verified.readlink() == Path("previous")
        if outcome == "all_failed":
            expected.append(entries[5])
            assert activations == []
        else:
            assert len(activations) == 2 and activations[-1] == "previous"
    assert attempts == expected
