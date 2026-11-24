#!/usr/bin/python3 -I
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
# The container side of aiab.bridge: wl-copy and wl-paste, done by asking the
# host to run its own.
#
# This file is not imported by aiab. aiab.bridge.install() copies it into each
# session container and links it in as both commands, and it tells which it is
# being from argv[0]. So it runs on whatever python3 the container's release
# has, with nothing but the standard library — keep it that way.
#
# Only the options that make sense without a compositor of our own are
# accepted; anything else is refused loudly rather than silently ignored, so a
# caller relying on it finds out. --foreground is accepted and means nothing:
# wl-copy on the host does its own backgrounding.

import argparse
import json
import os
import socket
import sys

ENV_VAR = "AIAB_BRIDGE"
TIMEOUT = 15.0


def _parse(prog: str, argv: list) -> tuple:
    """The (op, args, data) a command line asks for."""
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument("-p", "--primary", action="store_true")
    p.add_argument("-t", "--type")
    if prog == "wl-paste":
        p.add_argument("-l", "--list-types", action="store_true")
        p.add_argument("-n", "--no-newline", action="store_true")
        a = p.parse_args(argv)
        args = {
            "primary": a.primary,
            "type": a.type,
            "list_types": a.list_types,
            "no_newline": a.no_newline,
        }
        return "clipboard.paste", args, b""
    p.add_argument("-n", "--trim-newline", action="store_true")
    p.add_argument("-o", "--paste-once", action="store_true")
    p.add_argument("-c", "--clear", action="store_true")
    p.add_argument("-f", "--foreground", action="store_true")
    p.add_argument("text", nargs="*")
    a = p.parse_args(argv)
    args = {
        "primary": a.primary,
        "type": a.type,
        "trim_newline": a.trim_newline,
        "paste_once": a.paste_once,
        "clear": a.clear,
    }
    if a.clear:
        data = b""
    elif a.text:
        data = " ".join(a.text).encode()
    else:
        data = sys.stdin.buffer.read()
    return "clipboard.copy", args, data


def request(path: str, op: str, args: dict, data: bytes) -> bytes:
    """Send one request and return the reply's data; raise on a refusal."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(path)
        s.sendall(json.dumps({"op": op, "args": args}).encode() + b"\n" + data)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        s.close()
    line, _, reply = b"".join(chunks).partition(b"\n")
    header = json.loads(line)
    if not header.get("ok"):
        raise RuntimeError(header.get("error") or "the host refused")
    return reply


def main() -> int:
    prog = os.path.basename(sys.argv[0])
    if prog not in ("wl-copy", "wl-paste"):
        print(f"{prog}: run as wl-copy or wl-paste", file=sys.stderr)
        return 2
    op, args, data = _parse(prog, sys.argv[1:])
    path = os.environ.get(ENV_VAR)
    if not path:
        print(
            f"{prog}: no clipboard in this session ({ENV_VAR} unset)", file=sys.stderr
        )
        return 1
    try:
        reply = request(path, op, args, data)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"{prog}: {e}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
