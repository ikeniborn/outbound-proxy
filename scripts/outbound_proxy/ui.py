"""Loopback-only browser management and proof-of-possession sessions."""

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic

from . import pairing
from .store import PANEL_PORT, OperationError

WEB = Path(__file__).with_name("web")
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}
CHALLENGE_SECONDS = 60
SESSION_SECONDS = 1800


def valid_action(data):
    action = data.get("action")
    if action in ("enable", "disable"):
        return data.keys() == {"action"}
    if action in ("port", "docker", "failover"):
        if data.keys() != {"action", "value"}:
            return False
        value = data["value"]
        if action == "port":
            return type(value) is int and 1024 <= value <= 65535
        return type(value) is bool
    if action not in ("add", "update", "use", "remove", "check"):
        return False
    if not isinstance(data.get("name"), str) or not data["name"]:
        return False
    if action in ("add", "update") and data.keys() == {"action", "name", "uri"}:
        return isinstance(data["uri"], str) and bool(data["uri"])
    return action != "add" and data.keys() == {"action", "name"}


def matches(value, expected):
    return (bool(expected) and isinstance(value, str) and value.isascii()
            and secrets.compare_digest(value, expected))


class ManagementServer(ThreadingHTTPServer):
    # Drain in-flight requests when the service closes its listener.
    daemon_threads = False

    def __init__(self, ops):
        super().__init__(("127.0.0.1", PANEL_PORT), Handler)
        self.ops = ops
        self.authority = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.authority}"
        self.cookie_name = f"outbound_proxy_{self.server_port}"
        self.challenges = {}
        self.sessions = {}
        self.auth_lock = threading.Lock()

    def prune_expired(self):
        for cache in (self.challenges, self.sessions):
            for key in [key for key, record in cache.items() if record[0] <= monotonic()]:
                del cache[key]

    def authenticate(self, path, data):
        root = self.ops.store.root
        if path == "/api/pair" and data.keys() == {"code", "public"}:
            return {"id": pairing.register(root, data["code"], data["public"])}, None
        with self.auth_lock:
            self.prune_expired()
            if path == "/api/challenge" and data.keys() == {"id"}:
                pairing.public_key(root, data["id"])
                if len(self.challenges) >= 256:
                    raise pairing.Rejected()
                challenge = f"Outbound Proxy sign-in {self.origin} {data['id']} {secrets.token_urlsafe(32)}"
                self.challenges[challenge] = (monotonic() + CHALLENGE_SECONDS, data["id"])
                return {"challenge": challenge}, None
            if path == "/api/login" and data.keys() == {"id", "challenge", "signature"}:
                if not isinstance(data["challenge"], str):
                    raise pairing.Rejected()
                issued = self.challenges.pop(data["challenge"], None)
                if not issued or issued[1] != data["id"]:
                    raise pairing.Rejected()
                public = pairing.public_key(root, data["id"])
                pairing.verify(public, data["signature"], data["challenge"])
                if issued[0] <= monotonic():
                    raise pairing.Rejected()
                session, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                self.sessions[session] = (monotonic() + SESSION_SECONDS, token)
                cookie = (f"{self.cookie_name}={session}; Path=/api/; HttpOnly; "
                          f"SameSite=Strict; Max-Age={SESSION_SECONDS}")
                return {"csrf": token}, cookie
        raise pairing.Rejected()

    def handle_error(self, request, client_address):
        # An exception may contain request data. Never send it to the terminal.
        pass


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, format, *args):
        pass

    def reply(self, status, body, content_type="application/json", cookie=None):
        if content_type == "application/json":
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'self'; connect-src 'self'; form-action 'none'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        # The base implementation echoes malformed requests and unknown methods.
        self.reply(code, {"error": "Request rejected."})

    def authenticated(self):
        server = self.server
        cookies = [item.strip() for line in self.headers.get_all("Cookie", [])
                   for item in line.split(";")]
        values = [item.partition("=")[2] for item in cookies
                  if item.partition("=")[0] == server.cookie_name]
        csrf = self.headers.get_all("X-CSRF-Token", [])
        if len(values) != 1 or len(csrf) != 1:
            return False
        with server.auth_lock:
            current = server.sessions.get(values[0])
            return bool(current and current[0] > monotonic() and matches(csrf[0], current[1]))

    def host_ok(self):
        return self.headers.get_all("Host", []) == [self.server.authority]

    def do_GET(self):
        if not self.host_ok():
            return self.send_error(403)
        if self.path == "/api/state":
            if not self.authenticated():
                return self.send_error(403)
            return self.run_operation({"action": "status"})
        if self.path not in ASSETS:
            return self.send_error(404)
        name, media_type = ASSETS[self.path]
        self.reply(200, (WEB / name).read_bytes(), media_type)

    def read_object(self):
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
                or len(lengths[0]) > 5 or self.headers.get("Transfer-Encoding")
                or self.headers.get_all("Content-Type", []) != ["application/json"]):
            raise ValueError
        size = int(lengths[0])
        if not 0 < size <= 16384:
            raise ValueError

        def unique_keys(items):
            result = dict(items)
            if len(result) != len(items):
                raise ValueError
            return result

        raw = self.rfile.read(size)
        if len(raw) != size:
            raise ValueError
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)
        if not isinstance(data, dict):
            raise ValueError
        return data

    def do_POST(self):
        server = self.server
        if not self.host_ok() or self.headers.get_all("Origin", []) != [server.origin]:
            return self.send_error(403)
        if self.path not in ("/api/pair", "/api/challenge", "/api/login", "/api/action"):
            return self.send_error(404)
        if self.path == "/api/action" and not self.authenticated():
            return self.send_error(403)
        try:
            data = self.read_object()
        except (ValueError, RecursionError, OSError):
            return self.send_error(400)
        if self.path != "/api/action":
            try:
                reply, cookie = server.authenticate(self.path, data)
            except pairing.Rejected:
                return self.send_error(403)
            except Exception:
                return self.reply(503, {"error": "Browser authorization is unavailable."})
            return self.reply(200, reply, cookie=cookie)
        if not self.authenticated():
            return self.send_error(403)
        if not valid_action(data):
            return self.send_error(400)
        self.run_operation(data)

    def run_operation(self, data):
        try:
            # Both interfaces consume the common, credential-free public view.
            view = self.server.ops.run(**data)
        except OperationError as error:
            return self.reply(409, {"error": str(error)})
        except Exception:
            return self.reply(500, {"error": "Operation failed. Check service status."})
        view["bridge"] = self.server.ops.native.access.status() if view["docker_supported"] else None
        self.reply(200, view)


def serve(ops):
    with ManagementServer(ops) as server:
        server.serve_forever()
