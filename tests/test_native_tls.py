"""Run the installed forwarding daemons against an entirely loopback HTTPS fixture."""

import base64
import contextlib
import os
from pathlib import Path
import pwd
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def issue_certificate(directory, name, dns_names):
    config = directory / f"{name}.cnf"
    config.write_text(
        "[req]\nprompt=no\ndistinguished_name=subject\nx509_extensions=server\n"
        f"[subject]\nCN={dns_names[0]}\n[server]\nbasicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment\n"
        f"subjectAltName={','.join('DNS:' + dns for dns in dns_names)}\n"
    )
    key = directory / f"{name}.key"
    cert = directory / f"{name}.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-config", str(config), "-keyout", str(key), "-out", str(cert)],
                   capture_output=True, check=True)
    cert.chmod(0o644)
    return cert, key


class LocalServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client can withdraw an invalid TLS handshake during a negative case.
        if not isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


def receive_headers(stream):
    result = b""
    while b"\r\n\r\n" not in result:
        chunk = stream.recv(4096)
        if not chunk:
            raise ConnectionError("Incomplete fixture request")
        result += chunk
        if len(result) > 32768:
            raise ConnectionError("Oversized fixture request")
    return result.split(b"\r\n")


class Target(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.context.wrap_socket(self.request, server_side=True) as tls:
            tls.settimeout(5)
            if receive_headers(tls)[0].startswith(b"GET / HTTP/"):
                tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")


class Upstream(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.context.wrap_socket(self.request, server_side=True) as tls:
            tls.settimeout(5)
            headers = receive_headers(tls)
            expected = base64.b64encode(b"fixture:fixture-pass")
            authorized = any(header.split(b":", 1)[0].lower() == b"proxy-authorization"
                             and header.partition(b":")[2].strip() == b"Basic " + expected
                             for header in headers[1:])
            self.server.observations.append((headers[0], authorized))
            if not authorized:
                tls.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                return
            if not headers[0].startswith(b"CONNECT example.com:443 HTTP/"):
                tls.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            with socket.create_connection(self.server.target, timeout=5) as target:
                tls.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                while True:
                    ready, _, _ = select.select([tls, target], [], [], 5)
                    if not ready:
                        break
                    for source in ready:
                        chunk = source.recv(16384)
                        if not chunk:
                            return
                        destination = target if source is tls else tls
                        destination.sendall(chunk)


@contextlib.contextmanager
def serving(handler, cert, key, target=None):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    with LocalServer(("127.0.0.1", 0), handler) as server:
        server.context = context
        server.target = target
        server.observations = []
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                                  daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


def fixture_adapter(root, ca):
    from outbound_proxy.native import Host, Native
    account = pwd.getpwuid(os.geteuid()).pw_name if os.geteuid() else "nobody"
    host = Host("Linux" if sys.platform.startswith("linux") else "Darwin", shutil.which("tinyproxy"),
                shutil.which("stunnel"), shutil.which("curl"), ca, account, account)
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True, mode=0o711)
    return Native(root, host=host, runtime=runtime)


@pytest.fixture
def tls_directory():
    # Root-run tests delegate the daemons to nobody, who needs directory traversal.
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        directory.chmod(0o711)
        yield directory


def test_real_tls_forwarding(tls_directory):
    for program in ("openssl", "tinyproxy", "stunnel", "curl"):
        if shutil.which(program) is None:
            pytest.skip(f"TLS integration needs {program}")
    cert, key = issue_certificate(tls_directory, "fixture", ["localhost", "example.com"])
    issuer, _ = issue_certificate(tls_directory, "untrusted", ["example.invalid"])
    with serving(Target, cert, key) as target, serving(Upstream, cert, key, target.server_address) as upstream:
        port = upstream.server_address[1]
        uri = f"https://fixture:fixture-pass@localhost:{port}"
        adapter = fixture_adapter(tls_directory, cert)
        adapter.check(uri)
        assert len(upstream.observations) == 1
        assert upstream.observations[0][1]
        with pytest.raises((ValueError, RuntimeError)):
            adapter.check(uri.replace("fixture-pass", "wrong-password"))
        assert upstream.observations[-1][1] is False
        before = len(upstream.observations)
        with pytest.raises((ValueError, RuntimeError)):
            adapter.check(uri.replace("localhost", "127.0.0.1"))
        with pytest.raises((ValueError, RuntimeError)):
            fixture_adapter(tls_directory, issuer).check(uri)
        assert len(upstream.observations) == before
    with pytest.raises((ValueError, RuntimeError)):
        adapter.check(uri)
