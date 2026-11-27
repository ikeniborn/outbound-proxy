import multiprocessing
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

URI = "https://operator:synthetic-secret@one.example:8443"
SECOND = "https://other:other-secret@two.example:7443"


class Host:
    docker_supported = True

    def __init__(self):
        self.live = None

    def health(self):
        enabled = bool(self.live and self.live["enabled"])
        return {"running": enabled, "boot_enabled": enabled}

    def apply(self, state):
        self.live = state

    def schedule(self, enabled, interval):
        self.scheduled = enabled

    def check(self, uri):
        pass


def operations(root, native=None):
    from outbound_proxy.operations import Operations
    return Operations(root, native or Host())


def test_initial_catalog_and_preferences_are_private(tmp_path):
    root = tmp_path / "catalog"
    manager = operations(root)
    view = manager.run("status")
    assert view["port"] == 18888
    assert view["interval"] == 60
    assert view["selected"] is view["last_check"] is None
    for key in ("enabled", "docker", "failover", "running", "boot_enabled", "recovery_pending"):
        assert view[key] is False
    manager.run("add", "first", URI)
    manager.run("add", "second", SECOND)
    manager.run("use", "second")
    view = operations(root).run("list")
    assert view["selected"] == "second"
    assert view["upstreams"] == [
        {"name": "first", "host": "one.example", "port": 8443},
        {"name": "second", "host": "two.example", "port": 7443},
    ]
    assert "operator" not in repr(view) and "secret" not in repr(view)
    for path in [root, *root.rglob("*")]:
        assert path.stat().st_mode & 0o077 == 0


def test_rejected_mutations_and_secret_retention(tmp_path):
    from outbound_proxy.operations import OperationError
    manager = operations(tmp_path)
    manager.run("add", "first", URI)
    manager.run("add", "second", SECOND)
    manager.run("use", "first")
    before = manager.run("status")
    for args in [("add", "second", URI), ("add", "other", URI.replace("secret", "secre%74")),
                 ("update", "second", URI), ("remove", "first"),
                 ("add", "../escape", URI), ("add", "new", "http://secret@host:1"),
                 ("use", "missing")]:
        with pytest.raises(OperationError) as error:
            manager.run(*args)
        assert "secret" not in str(error.value)
        assert manager.run("status") == before
    assert manager.run("update", "first", "") == before
    new_uri = URI.replace("one.example", "changed.example")
    manager.run("update", "first", new_uri)
    assert manager.run("remove", "second")["upstreams"] == [
        {"name": "first", "host": "changed.example", "port": 8443}
    ]


def add_in_process(root, index):
    operations(root).run("add", f"item-{index}", f"https://u:p@item-{index}.example:443")


def test_independent_writers_do_not_lose_entries(tmp_path):
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=add_in_process, args=(tmp_path, n)) for n in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert {item["name"] for item in operations(tmp_path).run("list")["upstreams"]} == {
        "item-0", "item-1", "item-2", "item-3"
    }


def test_reject_insecure_catalog_or_replaced_secret_file(tmp_path):
    from outbound_proxy.operations import OperationError
    root = tmp_path / "catalog"
    root.mkdir(mode=0o755)
    with pytest.raises(OperationError):
        operations(root).run("list")
    root.chmod(0o700)
    link = tmp_path / "linked"
    link.symlink_to(root)
    with pytest.raises(OperationError):
        operations(link).run("list")
    manager = operations(root)
    manager.run("add", "first", URI)
    secret_file, = [path for path in root.rglob("*.json") if URI in path.read_text()]
    secret_file.chmod(0o644)
    with pytest.raises(OperationError):
        manager.run("status")
    target = tmp_path / "elsewhere"
    secret_file.rename(target)
    target.chmod(0o600)
    secret_file.symlink_to(target)
    with pytest.raises(OperationError):
        manager.run("status")
