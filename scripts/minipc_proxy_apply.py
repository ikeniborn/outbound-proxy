#!/usr/bin/env python3
"""Validate and apply the MiniPC outbound proxy configuration."""

import argparse
import fcntl
import os
import tempfile
import re
import socket
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit


def parse_upstream(source):
    """Return a DNS name, port, username, and password for the proxy pair."""
    message = "Invalid upstream HTTPS URI"
    uri = source.removesuffix("\n")
    if not uri.isascii() or any(not 33 <= ord(char) <= 126 for char in uri):
        raise ValueError(message)

    try:
        parsed = urlsplit(uri)
        port = parsed.port
        if (
            parsed.scheme != "https"
            or port == 0
            or parsed.netloc.endswith(":")
            or not parsed.username
            or not parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", parsed.hostname or "")
        ):
            raise ValueError(message)
        username, password = (
            unquote(part, errors="strict") for part in (parsed.username, parsed.password)
        )
        # Tinyproxy uses colon and at-sign as upstream credential delimiters.
        if any(not re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=/-]+", value)
               for value in (username, password)):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    return parsed.hostname, 443 if port is None else port, username, password


def select_generation(pointer, generation):
    """Replace a generation pointer atomically and sync its directory."""
    if generation is None:
        pointer.unlink(missing_ok=True)
    else:
        with tempfile.TemporaryDirectory(dir=pointer.parent) as temporary:
            candidate = Path(temporary) / pointer.name
            candidate.symlink_to(generation)
            candidate.replace(pointer)
    directory = os.open(pointer.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def rotate(verified, generation, prepare, activate):
    """Activate a probed generation under the caller's exclusive apply lock."""
    previous = str(verified.readlink()) if verified.is_symlink() else None
    try:
        prepare(generation)
    except Exception:
        raise RuntimeError("Proxy candidate preparation failed") from None

    try:
        activate(generation)
        select_generation(verified, generation)
    except Exception:
        try:
            select_generation(verified, previous)
            activate(previous)
        except Exception:
            raise RuntimeError("Proxy rollback failed after activation error") from None
        raise RuntimeError("Proxy activation failed") from None


def render_pair(upstream, listeners, allowed, proxy_port, tunnel_port, *,
                user="nobody", group="nogroup", ca="/etc/ssl/certs/ca-certificates.crt", inetd=False):
    """Render configuration for the matched HTTP and TLS services."""
    host, port, login, password = upstream
    tinyproxy = (
        f"Port {proxy_port}\n"
        + "".join(f"Listen {address}\n" for address in listeners)
        + "".join(f"Allow {source}\n" for source in allowed)
        + f"User {user}\nGroup {group}\nTimeout 600\nMaxClients 100\nLogLevel Critical\n"
        + f"Upstream http {login}:{password}@127.0.0.1:{tunnel_port}\n"
    )
    service = "client = yes\n" if inetd else f"[upstream]\nclient = yes\naccept = 127.0.0.1:{tunnel_port}\n"
    stunnel = (
        f"foreground = yes\npid =\n{service}connect = {host}:{port}\n"
        f"sni = {host}\nverifyChain = yes\ncheckHost = {host}\n"
        f"CAfile = {ca}\n"
    )
    return tinyproxy, stunnel


ROOT = Path("/etc/minipc-proxy")
RUNTIME = Path("/run/minipc-proxy")
GATEWAY = "192.168.70.1"
SUBNET = "192.168.70.0/28"
PORT = 18888
TUNNEL_PORT = 18889
UNITS = ("minipc-proxy-stunnel.service", "minipc-proxy-tinyproxy.service")


def read_source(path):
    with opener_without_symlinks(path) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("Upstream source must be a root-owned regular file with mode 0600")
        return source.read()


def opener_without_symlinks(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    return os.fdopen(descriptor, encoding="ascii")


def write_pair(directory, pair):
    tinyproxy, stunnel = pair
    for name, contents in (("tinyproxy.conf", tinyproxy), ("stunnel.conf", stunnel)):
        with (directory / name).open("x") as config:
            config.write(contents)
            config.flush()
            os.fsync(config.fileno())
    for path in (directory, directory.parent):
        descriptor = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def wait_for_listener(port):
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Proxy listener did not become ready")


def probe(port):
    result = subprocess.run(
        ["curl", "-q", "--silent", "--noproxy", "", "--max-time", "15",
         "--proxy", f"http://127.0.0.1:{port}", "--output", "/dev/null",
         "--write-out", "%{http_connect} %{http_code}", "https://example.com/"],
        capture_output=True, check=True, text=True,
    )
    if result.stdout != "200 200":
        raise RuntimeError("Proxy probe failed")


def verify_candidate(upstream):
    with tempfile.TemporaryDirectory(dir=RUNTIME) as temporary:
        directory = Path(temporary)
        with socket.socket() as proxy_socket, socket.socket() as tunnel_socket:
            for listener in (proxy_socket, tunnel_socket):
                listener.bind(("127.0.0.1", 0))
            proxy_port = proxy_socket.getsockname()[1]
            tunnel_port = tunnel_socket.getsockname()[1]
        loopback = ("127.0.0.1",)
        write_pair(directory, render_pair(upstream, loopback, loopback, proxy_port, tunnel_port))
        processes = []
        try:
            for command in (
                ["stunnel", str(directory / "stunnel.conf")],
                ["tinyproxy", "-d", "-c", str(directory / "tinyproxy.conf")],
            ):
                processes.append(subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            wait_for_listener(tunnel_port)
            wait_for_listener(proxy_port)
            probe(proxy_port)
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("Candidate service exited")
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def activate_generation(generation):
    active = RUNTIME / "active"
    select_generation(active, generation)
    if generation is None:
        command = ["systemctl", "stop", *UNITS]
    else:
        command = ["systemctl", "restart", *UNITS]
    subprocess.run(command, check=True, capture_output=True, timeout=90)
    if generation is not None:
        wait_for_listener(PORT)
        probe(PORT)


def parse_upstreams(source):
    """Read an ordered list of distinct upstreams."""
    line = source.removesuffix("\n")
    if "\n" in line or "\r" in line:
        raise ValueError("Invalid upstream list")
    entries = line.split(",")
    upstreams = [parse_upstream(entry.strip(" ")) for entry in entries]
    if len(set(upstreams)) != len(upstreams):
        raise ValueError("Duplicate upstream")
    return upstreams


def production_pair(upstream):
    return render_pair(
        upstream, ("127.0.0.1", GATEWAY), ("127.0.0.1", SUBNET), PORT, TUNNEL_PORT,
    )


def verified_upstream(upstreams):
    verified = ROOT / "verified"
    if not verified.is_symlink():
        raise RuntimeError("Apply the proxy list before enabling failover")
    current = tuple((verified / name).read_text() for name in ("tinyproxy.conf", "stunnel.conf"))
    for upstream in upstreams:
        if production_pair(upstream) == current:
            return upstream
    raise RuntimeError("Verified proxy is outside the configured list. Apply the new list first")


class CandidateVerificationError(RuntimeError):
    """An upstream failed its isolated probe before activation."""


def apply_upstream(upstream):
    try:
        verify_candidate(upstream)
    except (RuntimeError, subprocess.SubprocessError):
        raise CandidateVerificationError("Proxy candidate verification failed") from None

    generation = Path(tempfile.mkdtemp(prefix="generation-", dir=ROOT))

    def prepare(path):
        write_pair(Path(path), production_pair(upstream))

    rotate(ROOT / "verified", str(generation), prepare, activate_generation)


def select_failover(upstreams, active, active_ok, try_alternate):
    """Return the first verified alternate after two failed stable probes."""
    if active_ok():
        return None
    time.sleep(3)
    if active_ok():
        return None
    if len(upstreams) == 1:
        raise CandidateVerificationError("Active proxy failed twice and no alternate is configured")
    for alternate in upstreams:
        if alternate != active and try_alternate(alternate):
            return alternate
    raise CandidateVerificationError("Proxy candidate verification failed for all alternates")


def check_failover(upstreams, active):
    def active_ok():
        try:
            probe(PORT)
        except (RuntimeError, subprocess.SubprocessError):
            return False
        return True

    def try_alternate(alternate):
        try:
            apply_upstream(alternate)
        except CandidateVerificationError:
            return False
        return True

    if select_failover(upstreams, active, active_ok, try_alternate) is not None:
        print("Outbound proxy switched after confirmed failure")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-failover", action="store_true", help="Switch only after active proxy failure")
    options = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Run proxy apply as root")
    os.umask(0o077)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    with (RUNTIME / "apply.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        upstreams = parse_upstreams(read_source(ROOT / "upstream.url"))
        if options.check_failover:
            check_failover(upstreams, verified_upstream(upstreams))
        else:
            apply_upstream(upstreams[0])
            print("Outbound proxy activated and verified")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Outbound proxy apply failed") from None
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
