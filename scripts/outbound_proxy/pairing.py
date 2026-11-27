"""Durable public keys and single-use operator pairing codes."""

import base64
import hashlib
import secrets
import subprocess
import tempfile
from pathlib import Path
from time import time as wall_time

from .store import PANEL_PORT, Store, private, read_json, sync_dir, write_json

ORIGIN = f"http://127.0.0.1:{PANEL_PORT}"
CODE_SECONDS = 300
# SubjectPublicKeyInfo for an uncompressed P-256 point (two 32-byte coordinates).
P256_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")


class Rejected(ValueError):
    """An invalid or expired authentication request."""


def records(root):
    directory = Path(root) / "panel-pairing"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        private(directory.lstat(), directory=True)
    else:
        sync_dir(directory.parent)
    path = directory / "records.json"
    try:
        return path, read_json(path)
    except FileNotFoundError:
        return path, {"code": None, "keys": {}}


def issue_code(root, *, only_if_needed=False):
    with Store(root).locked():
        path, data = records(root)
        current = data["code"]
        if only_if_needed and (data["keys"] or current and current["until"] > wall_time()):
            return None
        code = secrets.token_urlsafe(24)
        data["code"] = {"digest": hashlib.sha256(code.encode()).hexdigest(), "until": wall_time() + CODE_SECONDS}
        write_json(path, data)
        return code


def print_code(root, required=True):
    try:
        terminal = open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        if required:
            raise RuntimeError("Pairing requires a controlling operator terminal.") from None
        return
    with terminal:
        code = issue_code(root, only_if_needed=not required)
        if code:
            print(f"Open {ORIGIN}/\nPairing code (valid for 5 minutes): {code}", file=terminal, flush=True)


def decode(value, limit):
    if not isinstance(value, str) or len(value) > (4 * ((limit + 2) // 3)):
        raise Rejected()
    try:
        result = base64.b64decode(value, validate=True)
    except ValueError:
        raise Rejected() from None
    if len(result) > limit:
        raise Rejected()
    return result


def register(root, code, public):
    der = decode(public, 91)
    if (not isinstance(code, str) or not code.isascii() or len(code) != 32
            or len(der) != 91 or not der.startswith(P256_PREFIX)):
        raise Rejected()
    with Store(root).locked():
        path, data = records(root)
        current = data["code"]
        if (not current or current["until"] <= wall_time()
                or not secrets.compare_digest(hashlib.sha256(code.encode()).hexdigest(), current["digest"])):
            raise Rejected()
        identifier = hashlib.sha256(der).hexdigest()
        data["keys"][identifier] = base64.b64encode(der).decode("ascii")
        data["code"] = None
        write_json(path, data)
        return identifier


def public_key(root, identifier):
    if not isinstance(identifier, str) or len(identifier) != 64:
        raise Rejected()
    with Store(root).locked():
        _, data = records(root)
        public = data["keys"].get(identifier)
        if not public:
            raise Rejected()
        return public


def verify(public, signature, challenge):
    der = decode(signature, 72)
    if len(der) < 8:
        raise Rejected()
    with tempfile.TemporaryDirectory(prefix="outbound-proof-") as temporary:
        key = Path(temporary) / "public.pem"
        sig = Path(temporary) / "signature.der"
        key.write_bytes(b"-----BEGIN PUBLIC KEY-----\n"
                        + base64.encodebytes(decode(public, 91)) + b"-----END PUBLIC KEY-----\n")
        sig.write_bytes(der)
        result = subprocess.run(["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(key),
                                 "-signature", str(sig)], input=challenge.encode("ascii"),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                cwd="/", env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, timeout=3)
        if result.returncode:
            raise Rejected()
