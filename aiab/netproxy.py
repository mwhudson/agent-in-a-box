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
# Allowing a name is not the last word: because the proxy dials out from the
# *host*, an allowed name that resolves to loopback, the LAN or a link-local
# address would hand the container a route to things the masked NIC exists to
# keep away from it. So the resolved address is checked too, and the
# connection goes to the address that was checked — see connect_external().
#
# A host in neither list is normally refused too — but while an `aiab monitor`
# session is attached (it holds a lock file in --pending-dir, see
# watcher_attached), the request is instead *parked*: the proxy drops a pending
# file for the monitor to prompt the user about, and polls the policy until the
# decision lands or the wait times out. Concurrent requests for the same host
# all poll the same policy, so one answer releases them all.
#
# Every byte of an allowed request also passes through _pipe(), which makes
# this the one place that can see how much traffic a session is actually
# moving. The tunnel counts its two directions into a _UsageMeter, keyed by
# host, which periodically writes the totals to --usage-file for `aiab
# monitor` to display (see aiab.netwatch.read_usage). Like the pending queue,
# that file is the whole channel between the two processes.
#
# Run as:
#   python3 -m aiab.netproxy --socket PATH --dir DIR --agent NAME
#       [--pending-dir DIR] [--profile NAME] [--usage-file PATH]

from __future__ import annotations

import argparse
import contextlib
import fcntl
import ipaddress
import json
import os
import re
import select
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
import traceback
from collections.abc import Callable
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

# How often a transferring tunnel's byte counters are written out. The file
# is also written when a tunnel ends and when the proxy exits, so this only
# bounds how stale a *live* transfer's numbers can look in `aiab monitor`.
_USAGE_FLUSH_INTERVAL = 2.0

# evaluate() verdicts.
ALLOW = "allow"
DENY = "deny"
ASK = "ask"


def host_matches(host: str, domain: str) -> bool:
    """Does host fall under domain -- as the domain itself, or a subdomain?"""
    return host == domain or host.endswith("." + domain)


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
        return host_matches(host, domain)

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


def is_external_address(addr: str) -> bool:
    """Is this resolved address one that genuinely leaves this machine?

    Policy is matched on the *name*, but the proxy connects from the host, so
    a name the policy allowed that resolves inward would reach the host's own
    loopback services or the user's LAN — neither of which the container can
    otherwise touch, its NIC being masked. Anything not routable off-box is
    therefore refused regardless of how the name was allowed.

    Unparseable addresses are refused too: this is the last check before a
    connection, so the safe answer to "I don't recognise this" is no.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    # ::ffff:127.0.0.1 is loopback wearing an IPv6 costume; judge the v4 address
    # it actually denotes.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # includes 169.254.169.254, the cloud metadata host
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


class InternalAddress(Exception):
    """Raised when every address a host resolves to points back inward."""

    def __init__(self, host: str, addresses: list[str]) -> None:
        super().__init__(host)
        self.host = host
        self.addresses = addresses


def connect_external(host: str, port: int) -> socket.socket:
    """Connect to host:port, refusing addresses that don't leave the machine.

    Deliberately not socket.create_connection(): the address has to be vetted
    *and then* connected to, rather than the name being resolved a second time
    once the check has passed. Otherwise a name answering differently on the
    second lookup walks straight past the check.

    Addresses are tried in resolution order, skipping the ones that fail
    is_external_address(). InternalAddress is raised only when nothing
    survived the filter, so a host with a mix of addresses still reaches its
    external ones rather than failing on a stray inward answer.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    blocked: list[str] = []
    last: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in infos:
        addr = str(sockaddr[0])
        if not is_external_address(addr):
            blocked.append(addr)
            continue
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(_CONNECT_TIMEOUT)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            sock.close()
            last = exc
    if last is not None:
        raise last
    if blocked:
        raise InternalAddress(host, blocked)
    raise OSError(f"no addresses for {host}")


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


def usage_path(container_name: str) -> Path:
    """Where a container's proxy writes its byte counters.

    Alongside the .log and .pid files `aiab run` maintains for the same
    proxy, and named the same way, so `aiab monitor` can find all three from
    a container name it reconstructs itself (see aiab.netwatch).
    """
    return PROXY_DIR / f"{container_name}.bytes"


class _UsageMeter:
    """Per-host byte counters for one proxy, published to a file.

    Counts what the tunnels actually carried, which for a CONNECT is
    ciphertext: the figure is bytes on the wire (TLS framing and all), and it
    can only ever be attributed to a host, never to a URL within it.

    Hosts matching the agent's own API domains are flagged, because the
    interesting traffic is usually the *other* kind -- apt, npm, pip, a git
    clone -- and an agent streaming completions all day would otherwise bury
    it in the total.

    Counters live for the proxy's lifetime, which is the session's: a new
    `aiab run` starts a new file. Writing is rate-limited by
    _USAGE_FLUSH_INTERVAL and done under the lock, so the handler threads
    doing the counting share one small file rather than one write each.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        # host -> [bytes up, bytes down, is an api domain]
        self._hosts: dict[str, list[int]] = {}
        self._last_write = 0.0

    def count(self, host: str, api: bool, *, up: int = 0, down: int = 0) -> None:
        """Add bytes in one direction for a host, flushing if it's time."""
        if self.path is None:
            return
        with self._lock:
            entry = self._hosts.setdefault(host, [0, 0, int(api)])
            entry[0] += up
            entry[1] += down
            self._maybe_write(time.time() >= self._last_write + _USAGE_FLUSH_INTERVAL)

    def flush(self) -> None:
        """Write the counters out now (at the end of a tunnel, or at exit)."""
        if self.path is None:
            return
        with self._lock:
            self._maybe_write(True)

    def _maybe_write(self, due: bool) -> None:
        """Publish the counters if due. Caller holds the lock."""
        if not due or self.path is None:
            return
        self._last_write = time.time()
        payload = {
            "hosts": {
                host: {"up": up, "down": down, "api": bool(api)}
                for host, (up, down, api) in self._hosts.items()
            }
        }
        # Written whole and renamed into place: a monitor polling the file
        # must never catch a half-written one and read it as "no traffic".
        # A failure here is not worth disturbing a working session for --
        # the numbers are a readout, not part of enforcement.
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()


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
        usage_file: Path | None = None,
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
        self.usage = _UsageMeter(usage_file)
        super().__init__(socket_path, _Handler)

    def is_api_domain(self, host: str) -> bool:
        """Is this host one of the agent's own API endpoints?

        Matched the same way evaluate() matches them, so what the meter
        labels 'agent traffic' is exactly what the policy waves through.
        """
        host = host.lower().rstrip(".")
        return any(host_matches(host, d) for d in self.api_domains)

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
            upstream = connect_external(host, port)
        except InternalAddress as exc:
            # Allowed by name, but it points back at the host or the LAN. A 403
            # rather than a 502, because this is a policy refusal and not a
            # network failure: the name resolved fine.
            addresses = ", ".join(exc.addresses)
            _log(f"DENY {method} {host}:{port}: internal address ({addresses})")
            self._respond(
                403,
                "Forbidden",
                f"aiab: {host} resolves to {addresses}, which is not reachable "
                f"from a container; allowing a name does not grant access to "
                f"the host or its network",
            )
            return
        except OSError as exc:
            _log(f"FAIL {method} {host}:{port}: {exc}")
            self._respond(502, "Bad Gateway", f"could not reach {host}:{port}: {exc}")
            return

        # Meter what the tunnel carries, so `aiab monitor` can show where a
        # session's traffic is going (the host is as fine-grained as this
        # gets: a CONNECT body is ciphertext).
        meter = self.server.usage
        api = self.server.is_api_domain(host)

        def count_up(n: int) -> None:
            meter.count(host, api, up=n)

        def count_down(n: int) -> None:
            meter.count(host, api, down=n)

        with upstream:
            upstream.settimeout(None)
            if upstream_head:
                upstream.sendall(upstream_head)
                count_up(len(upstream_head))
            else:
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
            _relay(self.connection, upstream, count_up, count_down)
        meter.flush()  # the tunnel's final numbers, without waiting for a tick

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


# How long one direction of a tunnel may sit silent *after the other has
# finished* before the proxy gives up on it (see _relay). It is an idle
# timeout in that half-closed state, not a cap on the tunnel: a byte in
# either direction resets it, so a slow or streaming response survives as
# long as it keeps saying something.
_HALF_CLOSE_GRACE = 120.0


def _pipe(
    src: socket.socket,
    dst: socket.socket,
    done: threading.Event,
    count: Callable[[int], None] | None = None,
) -> None:
    """Shovel one direction of a tunnel, then half-close the far side.

    ``count``, when given, is told the size of each chunk forwarded -- this
    being the one point every proxied byte passes through (see _UsageMeter).

    Reads wait in poll() rather than in a blocking recv(), so a direction
    whose peer has gone silent can still notice ``done`` — set by whichever
    direction finishes first — and give up instead of blocking forever.
    ``src`` itself stays a blocking socket, so the sendall() the other
    direction does on it keeps its ordinary semantics (a settimeout() here
    would make that one raise mid-stream after a slow reader).
    """
    poller = select.poll()
    poller.register(src, select.POLLIN)
    try:
        while True:
            if not poller.poll(_HALF_CLOSE_GRACE * 1000):
                if done.is_set():
                    return  # the other direction ended and this one is dead
                continue  # an idle tunnel is fine while both ends are live
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
            if count is not None:
                count(len(data))
    except OSError:
        pass
    finally:
        done.set()
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def _relay(
    client: socket.socket,
    upstream: socket.socket,
    count_up: Callable[[int], None] | None = None,
    count_down: Callable[[int], None] | None = None,
) -> None:
    """Shovel bytes both ways until both directions are closed.

    Either direction ending starts the clock on the other, because a tunnel
    whose peer went away *without closing* — a killed client, a server that
    holds an idle socket open — leaves the surviving direction blocked on a
    read that will never return. That pins the client socket, the upstream
    socket and two threads for the life of the proxy, which outlives the
    session that opened the tunnel; enough of them and the proxy hits its
    file-descriptor limit and starts failing every request, policy lookups
    included.
    """
    done = threading.Event()
    t = threading.Thread(
        target=_pipe, args=(client, upstream, done, count_up), daemon=True
    )
    t.start()
    _pipe(upstream, client, done, count_down)
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
    parser.add_argument(
        "--usage-file",
        default=None,
        help="file to publish per-host byte counters to for 'aiab monitor'",
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

    usage_file = Path(args.usage_file) if args.usage_file else None
    server = ProxyServer(
        address, Path(args.dir), args.agent, pending_dir, args.profile, usage_file
    )
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
        # SIGTERM lands here (_terminate raises SystemExit): publish the final
        # counters before going away, so a session's last transfer isn't lost
        # between flush intervals.
        server.usage.flush()
        server.server_close()
        if not abstract:
            Path(address).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
