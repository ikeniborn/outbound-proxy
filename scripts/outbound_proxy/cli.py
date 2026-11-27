"""Local operator command line for the outbound proxy."""
import argparse
import getpass
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path("/etc/outbound-proxy")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse can include an unrecognized argument containing credentials.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments. See --help. Supply credentials on standard input.\n")


def parser():
    p = Parser(prog="outbound-proxy", description="Manage local outbound forwarding.")
    cmds = p.add_subparsers(dest="action", required=True)
    for action in ("list", "status", "enable", "disable", "panel-serve", "boot", "tick", "recover"):
        cmds.add_parser(action)
    cmds.add_parser("ui", help="Show the browser panel address or issue a pairing code").add_argument(
        "ui_action", nargs="?", choices=("pair",))
    for action in ("add", "update", "use", "check", "remove"):
        cmds.add_parser(action).add_argument("name")
    cmds.add_parser("port").add_argument("value", type=int)
    for action in ("docker", "failover"):
        cmds.add_parser(action).add_argument("value", choices=("on", "off"))
    migration = cmds.add_parser("migrate")
    migration.add_argument("--expect-selected", metavar="NAME", help="Require this verified route on cutover")
    migration.add_argument("phase", choices=("inspect", "cutover", "accept", "rollback"))
    return p


def credential(stream):
    if stream.isatty():
        return getpass.getpass("Authenticated HTTPS URI: ")
    if stream is sys.stdin:
        info = os.fstat(stream.fileno())
        if stat.S_ISREG(info.st_mode) and (info.st_mode & 0o077 or info.st_uid not in {0, int(os.getenv("SUDO_UID", "0"))}):
            raise ValueError("Use an owner-only credential file")
    value = stream.read(65537)
    if len(value) > 65536:
        raise ValueError("Input exceeds the credential size limit")
    return value.removesuffix("\n")


def dispatch(args, ops=None, stream=None):
    fields = vars(parser().parse_args(args))
    action = fields.pop("action")
    if ops is None:
        if os.geteuid() != 0:
            raise PermissionError("Administrative access required")
        from .native import Native
        from .operations import Operations
        os.environ["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
        os.umask(0o077)
        ops = Operations(ROOT, Native(ROOT))
    if action == "ui":
        from .store import PANEL_PORT
        print(f"http://127.0.0.1:{PANEL_PORT}/")
        if fields["ui_action"] == "pair":
            from .pairing import print_code
            print_code(ops.store.root)
        return
    if action == "panel-serve":
        from .ui import serve
        return serve(ops)
    if action == "migrate":
        from .migrate import migrate
        view = migrate(ops, fields["phase"], fields["expect_selected"])
    else:
        if action in ("add", "update"):
            fields["uri"] = credential(stream or sys.stdin)
        if action in ("docker", "failover"):
            fields["value"] = fields["value"] == "on"
        view = ops.run(action, **fields)
    print(json.dumps(view, ensure_ascii=True, indent=2))


def main():
    try:
        dispatch(sys.argv[1:])
    except KeyboardInterrupt:
        return 130
    except PermissionError:
        print("Administrative access or a protected file is required.", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as exc:
        # The service layer raises redacted operator errors.
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("Proxy operation failed. Check native service state and permissions.", file=sys.stderr)
        return 1
    return 0
