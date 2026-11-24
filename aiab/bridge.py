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
# aiab.bridge - asking the host to do something for the agent, and waiting
# for the answer.
#
# aiab.attention already gets word from the container to the host, but only
# one way and only eventually: the agent leaves a file, and `aiab monitor`
# notices it on its next poll. Some things an agent wants from the host need
# an answer, and need it now — the clipboard is the first: Claude Code runs
# `wl-paste` when you paste an image and gives up after two seconds. So this
# is the other shape of channel: a request and its reply, over a unix socket.
#
# The socket lives in the directory's state dir, under SUBDIR, so it appears
# inside the container at the same place every other piece of shared state
# does (STATE_MOUNT) and nothing new is mounted. A unix socket in a
# bind-mounted directory connects across a container boundary because the
# container shares the host's kernel; the raw.idmap the session runs under
# makes the container user the socket's owner, so the directory can be 0700.
#
# It is served by the `aiab run` that started the session, from a thread, for
# as long as that run lasts — not by `aiab monitor`, which is optional. Each
# run has its own socket (named for its pid), because an agent and an
# `aiab shell` for one directory are two runs against one state dir, and
# neither should take the other's channel away when it exits. The session is
# told which one is its own through ENV_VAR.
#
# What crosses it is deliberately small: a named operation from HANDLERS with
# a few arguments, and some bytes each way. The agent gets exactly the
# operations listed there and nothing else of the host's — in particular the
# clipboard no longer means handing it the compositor's socket (which is what
# aiab used to do), with every window and input method that comes with it.
#
# The wire format, one request per connection: a JSON line
# {"op": ..., "args": {...}}, then the request's data until the client shuts
# its side down; back comes a JSON line {"ok": true} or {"ok": false,
# "error": ...}, then the reply's data until the server closes. The container
# side of it is bridge_client.py, which is pushed in by install().

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from . import STATE_MOUNT, StrPath
from .lxd import Container
from .state import dir_state_dir

# Which socket is this session's, as the container sees it.
ENV_VAR = "AIAB_BRIDGE"

# The sockets' directory inside the per-directory state dir.
SUBDIR = "bridge"
_CONTAINER_DIR = f"{STATE_MOUNT}/{SUBDIR}"

# Where the client goes in the container, and the commands it stands in for.
# /usr/local/bin is ahead of /usr/bin on the session's PATH, so the shims win
# over a real wl-clipboard that an older template still has installed.
_CLIENT_SOURCE = Path(__file__).with_name("bridge_client.py")
CLIENT_PATH = "/usr/local/lib/aiab/bridge-client"
SHIMS = ("wl-copy", "wl-paste")
_SHIM_DIR = "/usr/local/bin"

# Bounds on one request. The clipboard is the biggest thing that crosses, and
# a screenshot is a few megabytes; the limit only stops a runaway writer.
_MAX_HEADER = 64 * 1024
_MAX_DATA = 64 * 1024 * 1024
# How long a host-side command may take. wl-paste against a live compositor
# answers in milliseconds; this bounds a hang, not a slow answer.
_TIMEOUT = 10.0
# How often the accept loop looks up to see whether it has been told to stop.
_ACCEPT_POLL = 0.5


class BridgeError(Exception):
    """A request the host could not carry out; the message goes back as-is."""


# A handler takes the request's arguments and data and returns the reply data.
Handler = Callable[[Mapping[str, Any], bytes], bytes]


# --------------------------------------------------------------------------
# the clipboard
# --------------------------------------------------------------------------


def _wl_tool(name: str) -> str:
    """The host's wl-copy/wl-paste, or a BridgeError saying why there isn't one.

    The session's environment is the host's, so WAYLAND_DISPLAY says whether
    there is a compositor to talk to, exactly as it would for any other program
    run from this terminal.
    """
    if not os.environ.get("WAYLAND_DISPLAY"):
        raise BridgeError("the host has no Wayland session (WAYLAND_DISPLAY unset)")
    path = shutil.which(name)
    if path is None:
        raise BridgeError(
            f"{name} is not installed on the host (apt install wl-clipboard)"
        )
    return path


def _flags(args: Mapping[str, Any], names: Mapping[str, str]) -> list[str]:
    """The boolean flags named in args, as wl-clipboard options."""
    return [flag for key, flag in names.items() if args.get(key) is True]


def _mime_type(args: Mapping[str, Any]) -> list[str]:
    t = args.get("type")
    if t is None:
        return []
    if not isinstance(t, str) or not t:
        raise BridgeError("type must be a non-empty string")
    return ["--type", t]


def clipboard_paste(args: Mapping[str, Any], data: bytes) -> bytes:
    """wl-paste on the host: the clipboard's contents, or its list of types."""
    argv = [_wl_tool("wl-paste")]
    argv += _flags(
        args,
        {
            "primary": "--primary",
            "list_types": "--list-types",
            "no_newline": "--no-newline",
        },
    )
    argv += _mime_type(args)
    try:
        r = subprocess.run(argv, capture_output=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BridgeError("wl-paste timed out on the host")
    if r.returncode != 0:
        raise BridgeError(
            r.stderr.decode(errors="replace").strip() or "wl-paste failed"
        )
    return r.stdout


def clipboard_copy(args: Mapping[str, Any], data: bytes) -> bytes:
    """wl-copy on the host, with the request's data as what to copy.

    wl-copy forks a child that stays behind to serve the selection until
    something else replaces it, and that child inherits wl-copy's stdout and
    stderr. A pipe there would never see EOF, so neither is one: stdout goes
    nowhere and stderr to a file, which is read once the parent has exited.
    """
    argv = [_wl_tool("wl-copy")]
    argv += _flags(
        args,
        {
            "primary": "--primary",
            "trim_newline": "--trim-newline",
            "paste_once": "--paste-once",
            "clear": "--clear",
        },
    )
    argv += _mime_type(args)
    with tempfile.TemporaryFile() as err:
        try:
            r = subprocess.run(
                argv,
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=err,
                timeout=_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise BridgeError("wl-copy timed out on the host")
        if r.returncode != 0:
            err.seek(0)
            msg = err.read().decode(errors="replace").strip()
            raise BridgeError(msg or "wl-copy failed")
    return b""


# What a session may ask for. Adding an operation here is adding to what every
# agent can do on the host, so each one should be as narrow as these.
HANDLERS: dict[str, Handler] = {
    "clipboard.paste": clipboard_paste,
    "clipboard.copy": clipboard_copy,
}


# --------------------------------------------------------------------------
# the server
# --------------------------------------------------------------------------


def _recv_all(conn: socket.socket, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            raise BridgeError("request too large")
        chunks.append(chunk)


def _handle(conn: socket.socket, handlers: Mapping[str, Handler]) -> None:
    """Serve one request on an accepted connection, then close it."""
    with conn:
        conn.settimeout(_TIMEOUT)
        try:
            raw = _recv_all(conn, _MAX_HEADER + _MAX_DATA)
            line, sep, data = raw.partition(b"\n")
            if not sep or len(line) > _MAX_HEADER:
                raise BridgeError("malformed request")
            try:
                request = json.loads(line)
            except ValueError:
                raise BridgeError("malformed request")
            if not isinstance(request, dict):
                raise BridgeError("malformed request")
            op, args = request.get("op"), request.get("args", {})
            handler = handlers.get(op) if isinstance(op, str) else None
            if handler is None:
                raise BridgeError(f"unknown operation {op!r}")
            if not isinstance(args, dict):
                raise BridgeError("malformed request")
            reply = handler(args, data)
        except BridgeError as e:
            header, reply = {"ok": False, "error": str(e)}, b""
        except OSError:
            return  # the client went away; there is nobody to tell
        else:
            header = {"ok": True}
        with contextlib.suppress(OSError):
            conn.sendall(json.dumps(header).encode() + b"\n" + reply)


class Server:
    """One run's socket and the thread serving it.

    The socket is bound through an O_PATH handle on its directory, by way of
    /proc/self/fd, because the state dir sits deep enough under a home
    directory that the full path can overrun the 108 bytes a sockaddr_un has
    room for.
    """

    def __init__(
        self,
        host_dir: Path,
        container_dir: str,
        handlers: Mapping[str, Handler] = HANDLERS,
    ) -> None:
        self.host_dir = host_dir
        self.name = f"{os.getpid()}.sock"
        self.container_path = f"{container_dir}/{self.name}"
        self._handlers = handlers
        self._stop = threading.Event()
        self._dirfd = -1
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def _at(self, name: str) -> str:
        return f"/proc/self/fd/{self._dirfd}/{name}"

    def _reap_stale(self) -> None:
        """Remove sockets left by runs that died without cleaning up.

        Nothing listening is the test: a pid could have been reused, a refused
        connection can't lie.
        """
        for entry in os.listdir(self._at("")):
            if not entry.endswith(".sock") or entry == self.name:
                continue
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(self._at(entry))
            except (ConnectionRefusedError, FileNotFoundError):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self._at(entry))
            except OSError:
                pass
            finally:
                probe.close()

    def start(self) -> None:
        self.host_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._dirfd = os.open(self.host_dir, os.O_PATH | os.O_DIRECTORY)
        self._reap_stale()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self._at(self.name))
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self._at(self.name))
        sock.listen()
        sock.settimeout(_ACCEPT_POLL)
        self._sock = sock
        self._thread = threading.Thread(
            target=self._serve, name="aiab-bridge", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.setblocking(True)
            threading.Thread(
                target=_handle, args=(conn, self._handlers), daemon=True
            ).start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._sock is not None:
            self._sock.close()
        if self._dirfd >= 0:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._at(self.name))
            os.close(self._dirfd)
            self._dirfd = -1


@contextlib.contextmanager
def serve(directory: StrPath) -> Iterator[dict[str, str]]:
    """Serve this run's bridge for the duration; yield the env that finds it."""
    server = Server(dir_state_dir(directory) / SUBDIR, _CONTAINER_DIR)
    server.start()
    try:
        yield {ENV_VAR: server.container_path}
    finally:
        server.close()


# --------------------------------------------------------------------------
# the container side
# --------------------------------------------------------------------------


def install_script() -> str:
    """The shell that puts the client and its shims in place in a container."""
    links = " && ".join(f"ln -sf {CLIENT_PATH} {_SHIM_DIR}/{shim}" for shim in SHIMS)
    return (
        f"mkdir -p {os.path.dirname(CLIENT_PATH)} && "
        f"cat > {CLIENT_PATH}.new && chmod 755 {CLIENT_PATH}.new && "
        f"mv {CLIENT_PATH}.new {CLIENT_PATH} && {links}"
    )


def install(container: Container) -> None:
    """Put the bridge client in a session container, as wl-copy and wl-paste.

    Written on every run rather than baked into the template, for the reason
    aiab.attention.install gives: one exec, and existing session containers
    always get the version this checkout ships.
    """
    container.exec(["sh", "-c", install_script()], input=_CLIENT_SOURCE.read_bytes())
