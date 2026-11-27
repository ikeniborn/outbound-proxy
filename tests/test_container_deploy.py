"""Deployment checks use Docker/nft command recordings and never touch the host."""

import contextlib
import importlib.util
import json
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("docker_deploy", ROOT / "scripts/docker_deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)

IMAGE = deploy.REPOSITORY + "@sha256:" + "a" * 64
LOCAL = dict(image=IMAGE, port=18888, clients=False, inventory=False,
             lan=None, subnet=None, interface=None)
LAN = dict(LOCAL, lan="192.168.1.7", subnet="192.168.1.0/26", interface="eth0")


def args(action="install", **changes):
    values = dict(action=action, port=None, clients=None, inventory=None, lan=None,
                  subnet=None, local_only=False, image=None)
    return types.SimpleNamespace(**dict(values, **changes))


class DeploymentTests(unittest.TestCase):
    def test_base_bindings_and_client_inventory_override(self):
        base = (ROOT / "compose.yaml").read_text()
        self.assertIn('127.0.0.1:18890:18890', base)
        self.assertIn('127.0.0.1:${OP_HOST_PORT:-18888}:18888', base)
        self.assertNotIn("docker.sock", base)
        self.assertNotIn("0.0.0.0", base)
        with patch.object(deploy, "run") as run:
            deploy.compose(LOCAL, "owner", "up")
            service = json.loads(run.call_args.kwargs["data"])["services"]["proxy"]
            self.assertNotIn("networks", service)
            self.assertNotIn("volumes", service)
            deploy.compose(dict(LOCAL, clients=True, inventory=True), "owner", "up")
            service = json.loads(run.call_args.kwargs["data"])["services"]["proxy"]
            self.assertEqual(service["environment"]["OP_CLIENT_IP"], deploy.CLIENT_IP)
            self.assertEqual(service["cap_add"], ["SETUID", "SETGID"])
            self.assertTrue(service["volumes"][0]["read_only"])
            self.assertFalse(service["volumes"][0]["bind"]["create_host_path"])

    def test_digest_pinning_and_ambiguity(self):
        tag = deploy.REPOSITORY + ":test"
        with patch.object(deploy, "run", side_effect=["", json.dumps([{"RepoDigests": [IMAGE]}])]):
            self.assertEqual(deploy.resolve_image(tag), IMAGE)
        with patch.object(deploy, "run", side_effect=["", '[{"RepoDigests": []}]']):
            with self.assertRaisesRegex(deploy.Error, "immutable"):
                deploy.resolve_image(tag)

    def test_local_image_requires_the_exact_existing_id_without_pull(self):
        image = "sha256:" + "b" * 64
        with patch.object(deploy, "run", return_value=image) as run:
            self.assertEqual(deploy.resolve_image(image), image)
            run.assert_called_once_with("docker", "image", "inspect", "--format", "{{.Id}}", image)
        with patch.object(deploy, "run", return_value="sha256:" + "c" * 64):
            with self.assertRaisesRegex(deploy.Error, "unavailable"):
                deploy.resolve_image(image)

    def test_lan_policy_requires_interface_scope_and_address(self):
        rules = deploy.linux_lan.firewall(LAN, "owner")
        self.assertIn('iifname "eth0" ip saddr 192.168.1.0/26 ip daddr 192.168.1.7 tcp dport 18888 '
                      'dnat to 172.29.240.2:18888', rules)
        self.assertIn('ip daddr 172.29.240.2 tcp dport { 18888, 18890 } drop', rules)
        self.assertIn('iifname != "eth0" drop', rules)
        self.assertIn('ip saddr != 192.168.1.0/26 drop', rules)
        with patch.object(deploy, "run", return_value=json.dumps([
            {"ifname": "eth0", "addr_info": [{"local": "192.168.1.7", "prefixlen": 24}]}
        ])):
            self.assertEqual(deploy.lan_interface(LAN["lan"], LAN["subnet"]), "eth0")
            with self.assertRaises(deploy.Error):
                deploy.lan_interface(LAN["lan"], "192.168.0.0/16")

    def test_reserved_bridge_rejects_existing_host_route(self):
        with patch.object(deploy, "run", side_effect=["", '[{"dst":"172.29.0.0/16","dev":"tun0"}]']):
            with self.assertRaisesRegex(deploy.Error, "overlaps"):
                deploy.check_routes(False)

    def test_reject_foreign_firewall_without_mutation(self):
        existing = {"nftables": [{"table": {"family": "ip", "name": deploy.linux_lan.TABLE, "comment": "foreign"}}]}
        with patch.object(deploy, "run", return_value=json.dumps(existing)) as run:
            with self.assertRaisesRegex(deploy.Error, "another installation"):
                deploy.install_policy(LAN, "owner", True)
            self.assertTrue(all("-f" not in call.args for call in run.call_args_list))

    def test_boot_recovery_withholds_rules_during_pending_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deployment.json"
            path.write_text(json.dumps({"owner": "one", "active": LAN, "pending": LOCAL}))
            with patch.object(deploy.linux_lan, "apply_policy") as apply:
                deploy.linux_lan.restore_on_boot(path)
                apply.assert_called_once_with(None, "one", deploy.linux_lan.run)
            path.write_text(json.dumps({"owner": "one", "active": LAN}))
            with patch.object(deploy.linux_lan, "apply_policy") as apply, \
                 patch.object(deploy.linux_lan, "lan_interface", return_value="eth0"):
                deploy.linux_lan.restore_on_boot(path)
                self.assertEqual([call.args[0] for call in apply.call_args_list], [None, LAN])
            with patch.object(deploy.linux_lan, "apply_policy") as apply, \
                 patch.object(deploy.linux_lan, "lan_interface", return_value="another"):
                with self.assertRaisesRegex(deploy.Error, "different interface"):
                    deploy.linux_lan.restore_on_boot(path)
                apply.assert_called_once_with(None, "one", deploy.linux_lan.run)

    def test_boot_unit_is_scoped_repeatable_and_removable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(deploy, "run", return_value="not-found") as run:
            folder = Path(directory)
            unit = folder / "probe.service"
            state = folder / "deployment.json"
            with patch.object(deploy, "UNIT", unit):
                deploy.boot_policy(LAN, "one", state, True)
                before = unit.read_bytes()
                self.assertIn(b"Type=oneshot", before)
                self.assertIn(b"CAP_NET_ADMIN", before)
                helper = folder / "restore-lan.py"
                compile(helper.read_text(), str(helper), "exec")
                deploy.boot_policy(LAN, "one", state, True)
                self.assertEqual(unit.read_bytes(), before)
                with self.assertRaisesRegex(deploy.Error, "independently owned"):
                    deploy.boot_policy(None, "foreign", state, True)
                deploy.boot_policy(None, "one", state, True)
                deploy.boot_policy(None, "one", state, True)
                self.assertFalse(unit.exists())
                self.assertFalse(helper.exists())
                self.assertEqual(sum(c.args[1] == "disable" for c in run.call_args_list), 1)

    def test_foreign_resources_rejected_and_retained_catalog_accepted(self):
        for name, output in (("catalog", ["outbound-proxy-catalog", '[{"Labels":{}}]']),
                             ("ingress", ["", "outbound-proxy-ingress", '[{"Labels":{}}]'])):
            with self.subTest(name=name), patch.object(deploy, "run", side_effect=output):
                with self.assertRaisesRegex(deploy.Error, "another installation"):
                    deploy.verify_resources("one")
        with patch.object(deploy, "run", side_effect=["outbound-proxy-catalog",
                          json.dumps([{"Labels": {deploy.OWNER: "one"}}]), ""]):
            deploy.verify_resources("one")

    def test_reject_lan_on_desktop_before_mutation(self):
        with patch.object(deploy, "run") as run:
            with self.assertRaisesRegex(deploy.Error, "unavailable on Docker Desktop"):
                deploy.configure(args(lan=LAN["lan"], subnet=LAN["subnet"]), LOCAL, False)
            run.assert_not_called()

    def test_occupied_port_stops_before_image_pull(self):
        with patch.object(deploy, "available_port", side_effect=deploy.Error("occupied")), \
             patch.object(deploy, "resolve_image") as pull:
            with self.assertRaisesRegex(deploy.Error, "occupied"):
                deploy.configure(args(port=19999, image=IMAGE), LOCAL, True)
            pull.assert_not_called()

    def test_reconfigure_rolls_back_image_ports_and_policy(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            path = Path(directory) / "deployment.json"
            path.write_text(json.dumps({"owner": "one", "active": LAN, "previous": None}))
            candidate = dict(LAN, port=19999)
            for name, value in (("local_engine", True), ("run", ""), ("configure", candidate),
                                ("preflight_loopback", None), ("check_routes", None), ("boot_policy", None)):
                stack.enter_context(patch.object(deploy, name, return_value=value))
            compose = stack.enter_context(patch.object(deploy, "compose"))
            verify = stack.enter_context(patch.object(deploy, "verify", side_effect=[deploy.Error("health failed"), None]))
            policy = stack.enter_context(patch.object(deploy, "install_policy"))
            with self.assertRaisesRegex(deploy.Error, "health failed"):
                deploy.deploy(args("reconfigure"), path)
            state = json.loads(path.read_text())
            self.assertEqual(state["active"], LAN)
            self.assertNotIn("pending", state)
            self.assertEqual([call.args[0] for call in verify.call_args_list], [candidate, LAN])
            self.assertEqual(compose.call_args.args, (LAN, "one", "up", "-d", "--pull", "never"))
            self.assertEqual(policy.call_args.args, (LAN, "one", True))

    def test_rejected_canary_preserves_prior_service(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            path = Path(directory) / "deployment.json"
            initial = {"owner": "one", "active": LOCAL, "previous": None}
            path.write_text(json.dumps(initial))
            for name, value in (("local_engine", False), ("run", ""), ("configure", LOCAL)):
                stack.enter_context(patch.object(deploy, name, return_value=value))
            stack.enter_context(patch.object(deploy, "preflight_loopback", side_effect=deploy.Error("isolation failed")))
            compose = stack.enter_context(patch.object(deploy, "compose"))
            policy = stack.enter_context(patch.object(deploy, "install_policy"))
            with self.assertRaisesRegex(deploy.Error, "isolation"):
                deploy.deploy(args("reconfigure"), path)
            self.assertEqual(json.loads(path.read_text()), initial)
            self.assertEqual(compose.call_args.args[-2:], ("config", "--quiet"))
            policy.assert_not_called()

    def test_remove_retains_volume_and_can_run_twice(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(deploy, "local_engine", return_value=False), \
             patch.object(deploy, "run", return_value=""), patch.object(deploy, "compose") as compose:
            path = Path(directory) / "deployment.json"
            path.write_text(json.dumps({"owner": "one", "active": LOCAL, "previous": LAN}))
            deploy.deploy(args("remove"), path)
            deploy.deploy(args("remove"), path)
            compose.assert_called_once_with(LOCAL, "one", "down")
            self.assertIsNone(json.loads(path.read_text())["active"])
