"""macOS PF command-contract tests. No host firewall or Docker daemon is used."""

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import docker_deploy as deploy
import docker_pf as pf

OWNER = "1a613759-07d3-45ad-a65d-bb73b64c6b2b"
LAN = {"interface": "en0", "lan": "192.0.2.10", "subnet": "192.0.2.0/26", "port": 19999}
IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
    status: active"""


class MacPFTests(unittest.TestCase):
    def test_subnet_must_be_on_an_active_lan_interface(self):
        run = Mock(return_value=IFCONFIG)
        self.assertEqual(pf.lan_interface(LAN["lan"], LAN["subnet"], run), "en0")
        for address, cidr in [(LAN["lan"], "192.0.2.0/23"), ("192.0.2.1", LAN["subnet"])]:
            with self.assertRaises(deploy.Error):
                pf.lan_interface(address, cidr, run)
        run.assert_called_with("/sbin/ifconfig", "-a")

    def test_require_enabled_pf_unskipped_interface_and_actual_root_rdr_hook(self):
        for replies in [["Status: Disabled"], ["Status: Enabled", "en0 (skip)"],
                        ["Status: Enabled", "en0", 'rdr-anchor "com.apple/*" all']]:
            run = Mock(side_effect=replies)
            with self.assertRaises(deploy.Error):
                pf.check_hook(run, "en0")
            self.assertTrue(all("-f" not in call.args for call in run.call_args_list))

    def test_only_replaces_private_translation_and_verifies_readback(self):
        name = pf.anchor(OWNER)
        source = pf.rule(LAN)
        sanitized = source.replace(" port 19999 ->", " port = 19999 ->")
        run = Mock(side_effect=["Status: Enabled", "en0", 'rdr-anchor "outbound-proxy/*" all',
                               "com.apple", "", "", name, sanitized])
        pf.apply(LAN, OWNER, (), run)
        writes = [call for call in run.call_args_list if "-f" in call.args]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0].args[:5], ("/usr/bin/sudo", "--", "/sbin/pfctl", "-a", name))
        self.assertTrue(all("-N" in call.args and call.kwargs["data"] == source + "\n" for call in writes))
        self.assertEqual(source, "rdr on en0 inet proto tcp from 192.0.2.0/26 to 192.0.2.10 "
                         "port 19999 -> 127.0.0.1 port 19999")
        run = Mock(side_effect=[name, sanitized, "", "", "com.apple"])
        pf.apply(None, OWNER, [LAN], run)
        self.assertTrue(all(call.kwargs["data"] == "\n" for call in run.call_args_list if "-f" in call.args))

    def test_refuse_foreign_anchor_contents_and_local_only_uses_no_sudo(self):
        run = Mock(side_effect=[pf.anchor(OWNER), 'rdr-anchor "unknown/*" all'])
        with self.assertRaisesRegex(deploy.Error, "unrecognized"):
            pf.apply(None, OWNER, [LAN], run)
        run.reset_mock()
        pf.apply(None, OWNER, (), run)
        run.assert_not_called()
        with patch.object(deploy.platform, "system", return_value="Darwin"), \
             patch.object(deploy.os, "geteuid", return_value=0), patch.object(deploy, "run") as docker:
            with self.assertRaisesRegex(deploy.Error, "signed-in"):
                deploy.local_engine()
            docker.assert_not_called()

    def test_boot_fragment_is_opt_in_owned_and_safely_replaced(self):
        with tempfile.TemporaryDirectory() as dirname:
            directory = Path(dirname)
            anchor = pf.anchor(OWNER)
            file = directory / f"outbound-proxy-{OWNER}"
            main = directory / "pf.conf"
            main.write_text(f'load anchor "{anchor}" from "{file}"\n')
            marker = "# " + anchor + "\n"
            run = Mock()
            with patch.object(pf, "FRAGMENTS", directory), patch.object(pf, "MAIN_RULES", main):
                pf.persist(LAN, OWNER, [], run)
                run.assert_not_called()
                file.write_text(marker)
                with patch.object(Path, "lstat", return_value=types.SimpleNamespace(st_mode=0o100644, st_uid=123)):
                    with self.assertRaisesRegex(deploy.Error, "root-owned"):
                        pf.persist(LAN, OWNER, [], run)
                def emulate(*args, data=None):
                    self.assertEqual(args[:2], ("/usr/bin/sudo", "--"))
                    if args[2] == "/usr/bin/tee":
                        Path(args[3]).write_text(data)
                    if args[2] == "/bin/mv":
                        os.replace(args[-2], args[-1])
                    return ""
                with patch.object(Path, "lstat", return_value=types.SimpleNamespace(st_mode=0o100644, st_uid=0)):
                    pf.persist(LAN, OWNER, [], emulate)
                    self.assertEqual(file.read_text(), marker + pf.rule(LAN) + "\n")
                    pf.persist(None, OWNER, [LAN], emulate)
                    self.assertEqual(file.read_text(), marker)
                    file.write_text(marker + "foreign")
                    with self.assertRaisesRegex(deploy.Error, "another configuration"):
                        pf.persist(None, OWNER, [LAN], emulate)
