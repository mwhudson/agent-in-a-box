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
# aiab.netwatch - the console behind `aiab monitor`.
#
# Tails the filtering-proxy logs for a directory's session containers (one
# log per agent, under aiab.netproxy.PROXY_DIR) and watches the directory's
# pending queue. While a watch session runs it keeps a watcher.pid file in
# the pending dir; that file is what tells the proxy it may *park* a request
# to an unknown host instead of refusing it outright. Each parked host shows
# up as a pending file here; the user's decision turns into a plain
# aiab.state mutation, which every parked handler notices on its next poll —
# so there is no other channel between the two processes.
#
# There are two front ends over the shared plumbing in this module: the
# richer one in aiab.monitor_tui (textual; buttons you can click, plus a
# mounts view), and the
# plain keystroke loop at the bottom of this file, which is what you get
# when textual isn't installed (or with `aiab monitor --plain`).
#
# `aiab run` opens this in a tmux pane below the agent automatically (when
# the directory is restricted and tmux is available); it also works stand-
# alone in any terminal.

from __future__ import annotations

import contextlib
import os
import select
import sys
import termios
import time
import tty
from collections.abc import Iterator
from pathlib import Path

from . import StrPath
from . import agents
from . import netproxy
from . import profiles
from . import state
from .lxd import container_name_for_dir, dir_slug

# Per-directory queues of parked hosts, keyed like the other per-directory
# state (see aiab.state). Each file is one undecided host, written by the
# proxy and removed by it when the request resolves; watcher.pid marks an
# attached watch session.
_PENDING_BASE: Path = Path.home() / ".local" / "share" / "aiab" / "pending"

_WATCHER_PID = "watcher.pid"

# How long a [t]emporary allow lasts.
_TEMP_ALLOW_SECS = 15 * 60

POLL_INTERVAL = 0.3

# The decisions a watch UI can take for a parked host. The plain console
# binds them to keystrokes (_KEYS below), the textual UI to buttons and
# bindings; both record them through apply_decision().
ALLOW = "allow"
TEMP = "temp"
DENY = "deny"
SKIP = "skip"


def pending_dir(directory: StrPath) -> Path:
    """The pending-queue dir for a directory (shared by proxy and watcher)."""
    return _PENDING_BASE / dir_slug(str(Path(directory).resolve()))


def pending_hosts(pdir: Path) -> set[str]:
    """The hosts currently parked in a pending dir."""
    try:
        return {p.name for p in pdir.iterdir()} - {_WATCHER_PID}
    except OSError:
        return set()


@contextlib.contextmanager
def attached(pdir: Path) -> Iterator[None]:
    """Mark a watch session as attached to a pending dir, for the duration.

    The watcher.pid file written here is what switches the proxy from
    fail-fast 403s to parking unknown hosts (see aiab.netproxy).
    """
    pdir.mkdir(parents=True, exist_ok=True)
    pid_file = pdir / _WATCHER_PID
    pid_file.write_text(f"{os.getpid()}\n")
    try:
        yield
    finally:
        # Only remove the marker if it is still ours — a second watch session
        # may have taken over the queue.
        with contextlib.suppress(OSError, ValueError):
            if int(pid_file.read_text()) == os.getpid():
                pid_file.unlink()


def apply_decision(
    work_dir: Path, host: str, action: str, *, agent: str | None = None
) -> str:
    """Record one watch decision; return a line describing what happened.

    Parked requests poll the policy themselves, so an ALLOW/DENY here is all
    it takes to release them; SKIP records nothing and leaves the request to
    time out. agent scopes the rule to one agent (default: all agents), as
    with the underlying state.add_network_allow/deny.
    """
    suffix = f" [{agent}]" if agent else ""
    if action == ALLOW:
        state.add_network_allow(work_dir, host, None, agent=agent)
        return f"allowed {host}{suffix}"
    if action == TEMP:
        state.add_network_allow(
            work_dir, host, time.time() + _TEMP_ALLOW_SECS, agent=agent
        )
        return f"allowed {host} for 15m{suffix}"
    if action == DENY:
        state.add_network_deny(work_dir, host, agent=agent)
        return f"denied {host}{suffix}"
    return f"skipped {host} (request will time out)"


class _LogTail:
    """Incrementally read new lines from one proxy log (which may not exist yet)."""

    def __init__(self, path: Path, label: str) -> None:
        self.path = path
        self.label = label
        try:
            self.pos = path.stat().st_size  # start at the end: new lines only
        except OSError:
            self.pos = 0

    def read_new(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.pos:
            self.pos = 0  # truncated/replaced; start over
        if size == self.pos:
            return []
        with self.path.open() as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        return [f"[{self.label}] {line}" for line in data.splitlines()]


def log_tails(work_dir: Path) -> list[_LogTail]:
    """One tail per session the directory could be running.

    That's every agent, plus every isolated profile of one — those get their
    own container, and so their own proxy log. Logs that don't exist (yet)
    just stay silent.
    """
    return [
        _LogTail(
            netproxy.PROXY_DIR / f"{container_name_for_dir(work_dir, prefix)}.log",
            prefix,
        )
        for agent in agents.AGENT_NAMES
        for prefix in profiles.session_prefixes(agent)
    ]


def _print_rules(policy: state.NetworkPolicy, suffix: str = "") -> None:
    """Print a policy's allow/deny lists (all-agents then per-agent) as
    one-line summaries, each tagged with ``suffix`` (e.g. ' (global)')."""
    if policy["allow"]:
        print(f"  allowed{suffix}: " + ", ".join(a["domain"] for a in policy["allow"]))
    if policy["deny"]:
        print(f"  denied{suffix}:  " + ", ".join(policy["deny"]))
    for name, bucket in sorted(policy.get("agents", {}).items()):
        tag = f"{suffix} [{name}]"
        if bucket["allow"]:
            print(f"  allowed{tag}: " + ", ".join(a["domain"] for a in bucket["allow"]))
        if bucket["deny"]:
            print(f"  denied{tag}:  " + ", ".join(bucket["deny"]))


def _print_policy(work_dir: Path) -> None:
    policy = state.get_network(work_dir)
    print(f"Watching network access for {work_dir} (mode: {policy['mode']})")
    _print_rules(policy)
    _print_rules(state.get_global_network(), suffix=" (global)")


def _read_key(timeout: float) -> str | None:
    """Return one keystroke within timeout, or None (stdin is in cbreak mode)."""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    return sys.stdin.read(1)


_KEYS = {"a": ALLOW, "t": TEMP, "d": DENY, "s": SKIP}


def watch(work_dir: Path) -> int:
    """Run the plain keystroke watch loop for a directory; return an exit code.

    This is the fallback front end; `aiab monitor` prefers the textual one
    (aiab.monitor_tui) when textual is installed.
    """
    if not sys.stdin.isatty():
        print("aiab monitor needs an interactive terminal", file=sys.stderr)
        return 1

    pdir = pending_dir(work_dir)
    tails = log_tails(work_dir)

    _print_policy(work_dir)
    print("Keys: [a]llow  [t]emp allow 15m  [d]eny  [s]kip  [q]uit")

    queued: list[str] = []
    active: str | None = None
    known: set[str] = set()

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        with attached(pdir):
            while True:
                for tail in tails:
                    for line in tail.read_new():
                        print(line)

                # Pick up newly parked hosts. `known` mirrors what is
                # currently on disk, so a host whose request timed out and
                # later retries (its file vanishing and reappearing) gets
                # prompted again.
                present = pending_hosts(pdir)
                for name in sorted(present - known):
                    if name != active and name not in queued:
                        queued.append(name)
                known = present

                if active is None and queued:
                    active = queued.pop(0)
                    # \a rings the terminal bell, which tmux turns into a
                    # visual alert on the window — the "decision pending"
                    # signal.
                    print(f"\a==> {active} ? [a/t/d/s] ", end="", flush=True)

                key = _read_key(POLL_INTERVAL)
                if key is None:
                    continue
                if active is not None:
                    action = _KEYS.get(key)
                    if action is None:
                        continue  # ignore other keys while a prompt is up
                    print(apply_decision(work_dir, active, action))
                    active = None
                elif key in ("q", "\x03", "\x04"):  # q, ^C, ^D
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
