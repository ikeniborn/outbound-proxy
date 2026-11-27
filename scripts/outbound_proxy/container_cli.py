"""Send local operator commands to the active container supervisor."""

import json
import os
import socket
import sys

from .cli import Parser, credential
from .container import CONTROL
from .container_inventory import drop_to_panel
from .store import PANEL_PORT


def main():
    parser = Parser(prog="outbound-proxy")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "list", "enable", "disable", "recover", "tick", "ui"):
        commands.add_parser(action)
    for action in ("add", "update", "use", "remove", "check"):
        commands.add_parser(action).add_argument("name")
    commands.add_parser("port").add_argument("value", type=int)
    for action in ("docker", "failover"):
        commands.add_parser(action).add_argument("value", choices=("on", "off"))
    fields = vars(parser.parse_args())
    if fields["action"] == "ui":
        print(f"http://127.0.0.1:{PANEL_PORT}/")
        return 0
    try:
        if os.geteuid() == 0 and os.environ.get("OP_INVENTORY") == "1":
            drop_to_panel()
        if fields["action"] in ("add", "update"):
            fields["uri"] = credential(sys.stdin)
            if fields["action"] == "update" and not fields["uri"]:
                del fields["uri"]
        if fields["action"] in ("docker", "failover"):
            fields["value"] = fields["value"] == "on"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(120)
            sock.connect(str(CONTROL))
            sock.sendall(json.dumps(fields).encode() + b"\n")
            with sock.makefile("rb") as stream:
                result = json.load(stream)
        if "error" in result:
            print(result["error"], file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError):
        print("Container command failed. Check the local supervisor and input.", file=sys.stderr)
        return 1
