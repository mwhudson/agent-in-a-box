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
# aiab.shellwin - the container shell's own tmux window.
#
# `aiab run --shell` could always open a shell inside a session container, but
# only as a session of its own: something to start in a spare terminal, not
# something to flick to and back from while the agent works. This is the other
# half — the tmux side of "take me to a shell in this container" — and both
# `aiab shell` (aiab.cli) and the monitor pane's Shell button
# (aiab.monitor_tui) are callers of it.
#
# A window, not another split. `aiab run` already spends ten lines of the
# agent's window on the monitor pane (aiab.cli._monitor_pane) and the agent's
# TUI wants the rest, so a third split would leave all three cramped. Windows
# are also what aiab's tmux layout is already made of: a session group per
# container, a window per agent, one shared window list, and a per-session
# current-window pointer (see the tmux control plane in aiab.cli). A shell
# window joins that list, which means stock tmux navigates it — `C-b w` lists
# it and `C-b l` toggles agent to shell and back, with nothing new to learn.
#
# Switch *or* create, keyed by the container. tmux can select a window by name
# (`new-window -S`), but a name is not an identity: two directories' shells
# both want to be called `shell`, and in a tmux session of your own a window
# called that might be one you named yourself. So the window carries a tmux
# user option naming the container whose shell it holds, and that — not the
# name — is what a later ask matches on. The name is left as the short, plain
# `shell` that belongs in a status line.
#
# Scope is the asking session, never the group. Group members share one window
# list, so *creating* the window reaches every terminal, but selecting it moves
# only the pointer of the session it is told about — which is the point of the
# group: each terminal looks where it likes. Every command below is therefore
# told its session explicitly, and a window target is always qualified with it:
# a bare window id resolves against whichever session tmux used last, which in
# a group is not reliably the caller's (verified on tmux 3.6).
#
# What the window runs is `aiab run --shell ... --no-tmux`, the existing
# session path. That is what makes the shell see the same mounts, environment,
# network policy and git guard as the agent, and hold the per-container lock,
# so the idle-stopper cannot take the container down while you are standing in
# it. `--no-tmux` is the part that matters here: without it the shell window
# would split a *second* monitor pane under itself, and two monitors polling
# one parked-host directory would both answer the same prompt. Network
# decisions made from the shell belong to the agent window's monitor, or to
# `aiab net`.

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import agents

# The window's name. Short on purpose: it sits in the status line next to the
# agent's own window, and it is a label rather than an identity (see MARK).
WINDOW_NAME = "shell"

# tmux user option set on the window, naming the container whose shell it
# holds. Set by whoever creates the window rather than by the shell itself: a
# container can take seconds to start, and a window left unmarked for that long
# is one a second ask would miss, opening a duplicate.
MARK = "@aiab_shell"

# What list_windows() asks tmux for, one line per window in the session.
_FORMAT = f"#{{window_id}}\t#{{window_index}}\t#{{{MARK}}}"
_FIELDS = 3


class Error(Exception):
    """tmux could not do something this module needed."""


@dataclass(frozen=True)
class Window:
    """The shell window an ask arrived at.

    `index` is the window's number in the session, for telling the user which
    `C-b <n>` reaches it; `created` distinguishes a window this ask opened
    from one it merely switched to.
    """

    id: str
    index: str | None
    created: bool


def _tmux(*args: str) -> str:
    """Run one tmux command and return its output, or raise Error.

    Unlike the questions in aiab.focus, nothing here is optional: a caller
    asking for a shell window wants it, so a failure has to be reportable
    rather than quietly meaning "cannot tell".
    """
    try:
        result = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise Error(f"could not run tmux: {e}") from e
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exited {result.returncode}"
        raise Error(f"tmux {args[0]}: {detail}")
    return result.stdout


def session() -> str | None:
    """The tmux session the caller is in, or None if it isn't in one.

    The TMUX check is not just a shortcut: asked from outside, tmux answers
    `display-message` about whichever session its server used last, and acting
    on that would open a shell window in some session nobody is looking at.

    A pane in a session *group* reports the session that created its window,
    which need not be the one the terminal in front of you is attached to.
    That is the only session tmux will name from a pane, and it is the right
    one for the single-terminal case this is nearly always in; the cost of
    being wrong is a window that appears in the group's shared list without
    your view moving to it.
    """
    if not os.environ.get("TMUX"):
        return None
    return _tmux("display-message", "-p", "#{session_id}").strip() or None


def find(session_id: str, container: str) -> Window | None:
    """The window in `session_id` already holding `container`'s shell, if any.

    In a session group this sees every member's windows, since the list is
    shared — so a shell opened from another terminal is one this finds rather
    than duplicates.
    """
    for line in _tmux("list-windows", "-t", session_id, "-F", _FORMAT).splitlines():
        fields = line.split("\t")
        if len(fields) != _FIELDS:
            continue
        window_id, index, mark = fields
        if mark == container:
            return Window(id=window_id, index=index or None, created=False)
    return None


def command(
    work_dir: Path,
    agent: str,
    profile: str | None = None,
    argv0: str | None = None,
) -> list[str]:
    """The `aiab run --shell` command line a shell window runs.

    `argv0` is how to spell a re-invocation of aiab; it defaults to the
    launcher in the checkout this package lives in, which is how aiab is
    installed (see the README), and a caller that knows the path the user
    actually invoked should pass that instead.

    --for is passed explicitly rather than left to the window's working
    directory, so the shell is for the directory that was asked about even if
    something later changes where the window started.
    """
    cmd = [
        argv0 or str(agents.REPO_ROOT / "bin" / "aiab"),
        "run",
        "--shell",
        "--no-tmux",
        f"--for={work_dir}",
    ]
    if profile:
        cmd.append(f"--profile={profile}")
    cmd.append(agent)
    return cmd


def show(
    work_dir: Path,
    container: str,
    agent: str,
    profile: str | None = None,
    argv0: str | None = None,
) -> Window:
    """Show a shell for `container`: its window if it has one, else a new one.

    Raises Error when the caller isn't inside tmux — there is no window to
    switch to then, and what to do instead (hand over this terminal, say so in
    a log pane) is the caller's call, not ours.
    """
    session_id = session()
    if session_id is None:
        raise Error("not inside a tmux session")

    existing = find(session_id, container)
    if existing is not None:
        # Qualified with our session: a bare window id would move whichever
        # session tmux used last (see the module comment).
        _tmux("select-window", "-t", f"{session_id}:{existing.id}")
        return existing

    # The trailing colon means "this session, next free index"; without it the
    # target would name an existing window and tmux would refuse.
    out = _tmux(
        "new-window",
        "-P",
        "-F",
        "#{window_id}\t#{window_index}",
        "-t",
        f"{session_id}:",
        "-n",
        WINDOW_NAME,
        "-c",
        str(work_dir),
        shlex.join(command(work_dir, agent, profile, argv0)),
    )
    window_id, _, index = out.strip().partition("\t")
    if not window_id:
        raise Error("tmux did not say which window it created")
    _tmux("set-option", "-w", "-t", window_id, MARK, container)
    return Window(id=window_id, index=index or None, created=True)
