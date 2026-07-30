# Copyright (C) 2026 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# aiab.netproxy - the filtering HTTP(S) proxy behind `aiab net restrict`.
#
# When a directory's network mode is 'restricted', `aiab run` masks the
# container's NIC and spawns one of these per session container, listening on
# an abstract unix socket on the host (abstract because snap-confined LXD
# can't reach filesystem paths in the user's home; a peer-credential check
# stands in for socket file permissions). An LXD proxy device forwards
# 127.0.0.1:PROXY_PORT inside the container to that socket, and the agent is
# launched with HTTP_PROXY/HTTPS_PROXY pointing at it — so the only egress is
# proxy-aware traffic to allowed domains.
#
# A request's host is matched (exactly, or as a subdomain) against the
# agent's API domains (passed on the command line, always allowed) and the
# directory's recorded allow/deny lists, most specific rule winning. The
# policy is re-read from aiab.state on every request, so `aiab net
# allow`/`deny`/`open` take effect immediately without restarting anything.
# Denied requests get a 403 naming the host, and are logged to stderr (which
# `aiab run` redirects to a per-container log file).
#
# A host in neither list is normally refused too — but while an `aiab monitor`
# session is attached (it holds a lock file in --pending-dir, see
# watcher_attached), the request is instead *parked*: the proxy drops a pending
# file for the monitor to prompt the user about, and polls the policy until the
# decision lands or the wait times out. Concurrent requests for the same host
# all poll the same policy, so one answer releases them all.
#
# Run as:
#   python3 -m aiab.netproxy --socket PATH --dir DIR --agent NAME
#       [--pending-dir DIR] [--profile NAME]

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import re
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

from . import agents
from . import profiles
from . import state

# Where the proxy listens inside the container (forwarded by the LXD proxy
# device); the conventional HTTP proxy port.
PROXY_PORT: int = 3128

# Host-side per-container proxy files (pidfile and log), maintained by
# `aiab run`; `aiab monitor` tails the logs from here too.
PROXY_DIR: Path = Path.home() / ".local" / "share" / "aiab" / "proxy"

_MAX_HEADER_BYTES = 65536
_CONNECT_TIMEOUT = 15.0

# How a parked request waits for a watch session's decision: poll the policy
# at this interval, give up (403) after this long. The timeout also bounds
# how long a request can hold a handler thread when nobody answers.
_ASK_POLL = 0.5
_ASK_TIMEOUT = 60.0

# Hosts must look like a domain name (or IPv4 literal) to be parked: the host
# string becomes the pending file's name, so anything with a slash (or other
# oddity) from a malicious request line must not reach the filesystem.
_SAFE_HOST_RE = re.compile(r"[a-z0-9.-]+")

# evaluate() verdicts.
ALLOW = "allow"
DENY = "deny"
ASK = "ask"


def evaluate(
    host: str,
    api_domains: list[str],
    policy: state.NetworkPolicy,
    global_policy: state.NetworkPolicy | None = None,
) -> str:
    """Classify a host against a policy: ALLOW, DENY, or ASK.

    API domains always win. Otherwise the most specific (longest) matching
    rule across the allow and deny lists decides, so e.g. an allow for
    api.x.com pokes a hole in a deny for x.com. A host matching neither
    list is ASK: the caller chooses between refusing it outright and parking
    the request for an interactive decision.

    A global_policy supplies allow/deny rules shared by every directory. The
    directory's own rules take precedence: on an equal-length match a local
    rule beats a global one, but a longer global rule still beats a shorter
    local one (the most-specific-wins rule is preserved across both scopes).
    """
    host = host.lower().rstrip(".")
    if policy["mode"] != state.MODE_RESTRICTED:
        return ALLOW

    def matches(domain: str) -> bool:
        return host == domain or host.endswith("." + domain)

    if any(matches(d) for d in api_domains):
        return ALLOW

    # (domain, verdict, is_local); local rules win ties via the score below.
    rules = [(a["domain"], ALLOW, True) for a in policy["allow"]]
    rules += [(d, DENY, True) for d in policy["deny"]]
    if global_policy is not None:
        rules += [(a["domain"], ALLOW, False) for a in global_policy["allow"]]
        rules += [(d, DENY, False) for d in global_policy["deny"]]

    verdict = ASK
    best = (-1, False)  # (match length, is_local)
    for domain, kind, is_local in rules:
        score = (len(domain), is_local)
        if matches(domain) and score > best:
            best = score
            verdict = kind
    return verdict


# The marker an `aiab monitor` holds while attached to a pending dir. Its name
# lives here because this module is its only reader; aiab.netwatch.attached()
# writes it and imports the name from here (netwatch imports netproxy, not the
# other way round).
WATCHER_LOCK = "watcher.lock"


def watcher_attached(pending_dir: Path) -> bool:
    """Return True if any `aiab monitor` session is attached to pending_dir.

    Probes the lock netwatch.attached() holds *shared*, so a non-blocking
    exclusive attempt fails while at least one monitor has it. Several monitors
    can be attached at once — one per concurrent `aiab run` in the directory —
    and parking stays on until the last of them exits.

    A lock rather than the pid file this replaces: that was single-holder, so
    whichever monitor happened to exit first removed the marker and silently
    dropped the others back to fail-fast 403s. A lock is also self-healing,
    since the OS releases it when a process dies, so there is no liveness probe
    and nothing stale to clean up after a crash.
    """
    try:
        fd = os.open(pending_dir / WATCHER_LOCK, os.O_RDWR)
    except OSError:
        return False  # no lock file, so nothing has ever attached
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


class ProxyServer(socketserver.ThreadingUnixStreamServer):
    """A unix-socket HTTP proxy enforcing one directory+agent's allowlist."""

    daemon_threads = True

    def __init__(
        self,
        socket_path: str,
        work_dir: Path,
        agent: str,
        pending_dir: Path | None = None,
        profile: str | None = None,
    ) -> None:
        self.work_dir = work_dir
        self.agent = agent
        # The always-allowed defaults: the shared baseline plus this agent's
        # own API/auth/telemetry domains, sourced from the agent registry
        # rather than passed in (see aiab.agents). A profile's `allow` list
        # joins them rather than the user rules, for the same reason the
        # agent's do — a profile that points the agent at a different endpoint
        # would be useless if reaching it needed a separate `aiab net allow`.
        defaults = agents.BASELINE_DOMAINS + agents.get(agent).api_domains
        if profile:
            entry = profiles.get(profile)
            if entry:
                defaults = defaults + list(entry.get("allow", []))
        self.api_domains = [d.lower() for d in defaults]
        self.pending_dir = pending_dir
        super().__init__(socket_path, _Handler)

    def decide(self, host: str) -> str:
        return evaluate(
            host,
            self.api_domains,
            state.network_for_agent(self.work_dir, self.agent),
            state.global_for_agent(self.agent),
        )

    def verify_request(self, request: Any, client_address: Any) -> bool:
        """Accept connections from root (LXD's forkproxy) and our own uid only.

        Abstract sockets have no file permissions, so this peer-credential
        check is what stops other local users from using the proxy.
        """
        creds = request.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")
        )
        _pid, uid, _gid = struct.unpack("iII", creds)
        if uid not in (0, os.getuid()):
            _log(f"refused connection from uid {uid}")
            return False
        return True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints to stderr too, but unframed; keep our log format
        # so a crashing handler is obvious next to the ALLOW/DENY lines.
        _log(f"error handling request:\n{traceback.format_exc().rstrip()}")


class _Handler(socketserver.StreamRequestHandler):
    server: ProxyServer

    def handle(self) -> None:
        head = self._read_head()
        if head is None:
            return
        request_line, header_lines = head
        try:
            method, target, version = request_line.split(maxsplit=2)
        except ValueError:
            self._respond(400, "Bad Request", "malformed request line")
            return

        if method.upper() == "CONNECT":
            host, _, port_str = target.rpartition(":")
            port = int(port_str) if port_str.isdigit() else 443
            host = host or target
            upstream_head = b""
        else:
            # Plain HTTP in absolute-URI form (e.g. apt, http:// fetches).
            url = urlsplit(target)
            if not url.hostname:
                self._respond(400, "Bad Request", "expected an absolute URI")
                return
            host = url.hostname
            port = url.port or 80
            # Rewrite to origin-form for the upstream server, and force the
            # connection closed so the relay below terminates.
            path = url.path or "/"
            if url.query:
                path += "?" + url.query
            headers = [
                line
                for line in header_lines
                if not line.lower().startswith(("proxy-connection:", "connection:"))
            ]
            headers.append("Connection: close")
            upstream_head = (
                "\r\n".join([f"{method} {path} {version}", *headers]) + "\r\n\r\n"
            ).encode("latin-1")

        host = host.strip("[]")  # tolerate bracketed IPv6 literals
        verdict = self.server.decide(host)
        if verdict == ASK:
            verdict = self._await_decision(host)
        if verdict != ALLOW:
            _log(f"DENY {method} {host}:{port}")
            self._respond(
                403,
                "Forbidden",
                f"aiab: access to {host} is not allowed; "
                f"run 'aiab net allow {host}' on the host to permit it",
            )
            return

        _log(f"ALLOW {method} {host}:{port}")
        try:
            upstream = socket.create_connection((host, port), timeout=_CONNECT_TIMEOUT)
        except OSError as exc:
            _log(f"FAIL {method} {host}:{port}: {exc}")
            self._respond(502, "Bad Gateway", f"could not reach {host}:{port}: {exc}")
            return

        with upstream:
            upstream.settimeout(None)
            if upstream_head:
                upstream.sendall(upstream_head)
            else:
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
            _relay(self.connection, upstream)

    def _await_decision(self, host: str) -> str:
        """Park an undecided request while a watch session asks the user.

        Drops a pending file (named after the host) for `aiab monitor` to
        pick up, then polls the policy until an allow/deny lands, the watcher
        goes away, or the wait times out — all of which resolve to DENY
        except an explicit allow. Without a live watcher attached this
        returns DENY immediately, preserving the old fail-fast behaviour.
        """
        pending_dir = self.server.pending_dir
        host = host.lower().rstrip(".")
        if (
            pending_dir is None
            or not _SAFE_HOST_RE.fullmatch(host)
            or not watcher_attached(pending_dir)
        ):
            return DENY
        pending = pending_dir / host
        try:
            pending.write_text(f"{time.time()}\n")
        except OSError:
            return DENY
        _log(f"ASK {host}")
        try:
            deadline = time.time() + _ASK_TIMEOUT
            while time.time() < deadline:
                time.sleep(_ASK_POLL)
                verdict = self.server.decide(host)
                if verdict != ASK:
                    return verdict
                if not watcher_attached(pending_dir):
                    return DENY
            return DENY
        finally:
            with contextlib.suppress(OSError):
                pending.unlink()

    def _read_head(self) -> tuple[str, list[str]] | None:
        """Read the request line and headers; None on EOF/overflow."""
        lines: list[str] = []
        total = 0
        while True:
            raw = self.rfile.readline(_MAX_HEADER_BYTES)
            total += len(raw)
            if not raw or total > _MAX_HEADER_BYTES:
                return None
            line = raw.decode("latin-1").rstrip("\r\n")
            if not line:
                break
            lines.append(line)
        if not lines:
            return None
        return lines[0], lines[1:]

    def _respond(self, code: int, reason: str, body: str) -> None:
        payload = (body + "\r\n").encode()
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("latin-1")
        with contextlib.suppress(OSError):
            self.wfile.write(head + payload)


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Shovel bytes both ways until both directions are closed."""
    t = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
    t.start()
    _pipe(upstream, client)
    t.join()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aiab.netproxy")
    parser.add_argument(
        "--socket",
        required=True,
        help="unix socket to listen on; @name for an abstract socket",
    )
    parser.add_argument(
        "--dir", required=True, help="project directory whose policy applies"
    )
    parser.add_argument(
        "--agent",
        required=True,
        help="agent being served; selects its defaults and per-agent rules",
    )
    parser.add_argument(
        "--pending-dir",
        default=None,
        help="dir where undecided hosts are parked for 'aiab monitor'",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="profile in force; its allow list joins the always-allowed defaults",
    )
    args = parser.parse_args(argv)

    # An abstract socket (@name) lives in the network namespace rather than
    # the filesystem: nothing to unlink or chmod (verify_request stands in
    # for file permissions), and snap-confined LXD can dial it even though
    # its mount namespace can't see our home directory.
    abstract = args.socket.startswith("@")
    if abstract:
        address = "\0" + args.socket[1:]
    else:
        address = args.socket
        Path(address).unlink(missing_ok=True)

    def _terminate(signo: int, frame: FrameType | None) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _terminate)

    pending_dir = None
    if args.pending_dir:
        pending_dir = Path(args.pending_dir)
        pending_dir.mkdir(parents=True, exist_ok=True)

    server = ProxyServer(address, Path(args.dir), args.agent, pending_dir, args.profile)
    if not abstract:
        os.chmod(address, 0o600)
    _log(
        f"proxy listening on {args.socket} for {args.dir} ({args.agent}"
        f"{'/' + args.profile if args.profile else ''}; "
        f"api domains: {', '.join(server.api_domains) or 'none'})"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if not abstract:
            Path(address).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
