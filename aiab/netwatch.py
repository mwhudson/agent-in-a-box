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
# aiab.netwatch - the plumbing behind `aiab monitor`.
#
# Locates the filtering-proxy logs for a directory's session containers (one
# log per agent, under aiab.netproxy.PROXY_DIR) and manages the directory's
# pending queue. While a watch session runs it holds a lock file in the
# pending dir; that lock is what tells the proxy it may *park* a request
# to an unknown host instead of refusing it outright. Each parked host shows
# up as a pending file here; the user's decision turns into a plain
# aiab.state mutation, which every parked handler notices on its next poll —
# so there is no other channel between the two processes.
#
# The user interface over this plumbing lives in aiab.monitor_tui (textual).
# This module deliberately holds no UI of its own, so the queue helpers and
# the decision recording can be tested without driving a terminal.
#
# `aiab run` opens the monitor in a tmux pane below the agent automatically
# (when the directory is restricted and tmux is available); it also works
# stand-alone in any terminal.

from __future__ import annotations

import contextlib
import fcntl
import time
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
# proxy and removed by it when the request resolves.
_PENDING_BASE: Path = Path.home() / ".local" / "share" / "aiab" / "pending"

# Entries in a pending dir that are bookkeeping rather than parked hosts: the
# lock an attached monitor holds, and the pid file older versions used for the
# same job (still filtered so a leftover one never shows up as a host).
_NON_HOST_ENTRIES = frozenset({netproxy.WATCHER_LOCK, "watcher.pid"})

# How long a [t]emporary allow lasts.
_TEMP_ALLOW_SECS = 15 * 60

POLL_INTERVAL = 0.3

# The decisions a watch UI can take for a parked host. aiab.monitor_tui binds
# them to buttons and keystrokes; both routes record them through
# apply_decision().
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
        return {p.name for p in pdir.iterdir()} - _NON_HOST_ENTRIES
    except OSError:
        return set()


@contextlib.contextmanager
def attached(pdir: Path) -> Iterator[None]:
    """Mark a watch session as attached to a pending dir, for the duration.

    Holding this is what switches the proxy from fail-fast 403s to parking
    unknown hosts (see aiab.netproxy.watcher_attached). The lock is taken
    *shared*, so every monitor of a directory holds it at once — one per
    concurrent `aiab run` there — and the marker only clears when the last of
    them exits.

    That sharing is the point. The pid file this replaces held one writer, so
    two monitors overwrote each other and whichever left first cleared the
    marker, dropping the survivors back to fail-fast 403s with no sign that
    parking had stopped working. The OS also releases the lock on process
    death, so a crashed monitor needs no cleanup.
    """
    pdir.mkdir(parents=True, exist_ok=True)
    with (pdir / netproxy.WATCHER_LOCK).open("w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        yield


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
