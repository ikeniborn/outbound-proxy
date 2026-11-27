"""CLI contract and safe credential input."""
import io
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_launcher_help():
    result = subprocess.run([sys.executable, "-I", str(SCRIPTS / "outbound-proxy"), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert "failover" in result.stdout


@pytest.mark.parametrize(("args", "expected"), [
    (["docker", "off"], {"value": False}),
    (["port", "19999"], {"value": 19999}),
    (["use", "second"], {"name": "second"}),
    (["add", "first"], {"name": "first", "uri": "https://operator:fixture@proxy.test:443"}),
])
def test_cli_dispatch(args, expected, capsys):
    from outbound_proxy.cli import dispatch

    class Service:
        def run(self, action, **fields):
            assert action == args[0]
            assert fields == expected
            return {"selected": None}

    dispatch(args, Service(), io.StringIO("https://operator:fixture@proxy.test:443\n"))
    assert "fixture" not in capsys.readouterr().out


def test_cli_refuses_uri_argument(capsys):
    from outbound_proxy.cli import dispatch
    with pytest.raises(SystemExit):
        dispatch(["add", "first", "https://operator:fixture@proxy.test:443"], None)
    assert "fixture" not in capsys.readouterr().err


def test_ui_prints_stable_address_without_starting_a_server(monkeypatch, capsys):
    from outbound_proxy import ui
    from outbound_proxy.cli import dispatch

    monkeypatch.setattr(ui, "serve", lambda ops: pytest.fail("address display started a server"))
    dispatch(["ui"], object())
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:18890/"


def test_pair_is_an_explicit_ui_subcommand():
    from outbound_proxy.cli import parser

    args = parser().parse_args(["ui", "pair"])
    assert (args.action, args.ui_action) == ("ui", "pair")
