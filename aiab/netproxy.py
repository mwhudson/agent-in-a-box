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
# A request's host is allowed if it matches (exactly, or as a subdomain) one
# of the agent's API domains (passed on the command line) or one of the
# directory's recorded allows. The policy is re-read from aiab.state on every
# request, so `aiab net allow`/`deny`/`open` take effect immediately without
# restarting anything. Denied requests get a 403 naming the host, and are
# logged to stderr (which `aiab run` redirects to a per-container log file).
#
# Run as:
#   python3 -m aiab.netproxy --socket PATH --dir DIR [--api-domain D]...

from __future__ import annotations

import argparse
import contextlib
import os
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

from . import state

# Where the proxy listens inside the container (forwarded by the LXD proxy
# device); the conventional HTTP proxy port.
PROXY_PORT: int = 3128

_MAX_HEADER_BYTES = 65536
_CONNECT_TIMEOUT = 15.0


def _log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


class ProxyServer(socketserver.ThreadingUnixStreamServer):
    """A unix-socket HTTP proxy enforcing one directory's allowlist."""

    daemon_threads = True

    def __init__(
        self, socket_path: str, work_dir: Path, api_domains: list[str]
    ) -> None:
        self.work_dir = work_dir
        self.api_domains = [d.lower() for d in api_domains]
        super().__init__(socket_path, _Handler)

    def allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        policy = state.get_network(self.work_dir)
        if policy["mode"] != state.MODE_RESTRICTED:
            return True
        domains = self.api_domains + [a["domain"] for a in policy["allow"]]
        return any(host == d or host.endswith("." + d) for d in domains)

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
        if not self.server.allowed(host):
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
        "--api-domain",
        action="append",
        default=[],
        help="domain that is always allowed (repeatable)",
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

    server = ProxyServer(address, Path(args.dir), args.api_domain)
    if not abstract:
        os.chmod(address, 0o600)
    _log(
        f"proxy listening on {args.socket} for {args.dir} "
        f"(api domains: {', '.join(args.api_domain) or 'none'})"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if not abstract:
            Path(address).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
