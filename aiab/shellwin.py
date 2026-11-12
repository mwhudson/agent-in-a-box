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
# aiab.shellwin - the container shell's place in the agent's tmux layout.
#
# `aiab run --shell` could always open a shell inside a session container, but
# only as a session of its own: something to start in a spare terminal, not
# something to flick to and back from while the agent works. This is the other
# half — the tmux side of "take me to a shell in this container" — and both
# `aiab shell` (aiab.cli) and the monitor pane's Shell button
# (aiab.monitor_tui) are callers of it.
#
# One shell process, two places it can be:
#
#   * its own window, the default. Full height for the shell, and stock tmux
#     navigates it — `C-b w` lists it, `C-b l` toggles agent to shell and back.
#     It joins a window list aiab already maintains: a session group per
#     container with a window per agent (see the tmux control plane in
#     aiab.cli).
#   * a ten-line pane in the agent's own window (`aiab shell --pane`), between
#     the agent and the monitor. That is for keeping the monitor *visible*
#     while working in the shell: the monitor is a split of the agent's window
#     (aiab.cli._monitor_pane) and tmux cannot show one pane in two windows,
#     so the only way to see both is to be in the window the monitor is in.
#     A popup cannot do this — tmux does not update panes while one is up, so
#     the monitor behind it would be a frozen snapshot.
#
# Asking again moves it back, and the same process travels either way: a pane
# keeps its pid across `join-pane` and `break-pane` (verified on tmux 3.6), so
# whatever you were doing in the shell survives the move. "Parked in its own
# window" and "the shell window" are therefore the same state, which is also
# the closest tmux has to a pane that is hidden by default: a window's panes
# run while you are looking elsewhere, and there is no hidden pane *within* a
# window.
#
# Identity lives on the pane, not the window. tmux can select a window by name
# (`new-window -S`), but a name is not an identity — two directories' shells
# both want to be called `shell`, and in a tmux session of your own a window
# called that might be one you named yourself. A window id is no better here:
# `break-pane` makes a *new* window every time, so a mark on the window would
# be lost on the first move. So the mark is a tmux user option on the pane,
# which is what the shell actually is, and it survives both moves.
#
# Scope is the asking session, never the group. Group members share one window
# list, so *creating* the window reaches every terminal, but selecting it moves
# only the pointer of the session it is told about — which is the point of the
# group: each terminal looks where it likes. Every command below is therefore
# told its session explicitly, and a window target is always qualified with it:
# a bare window id resolves against whichever session tmux used last, which in
# a group is not reliably the caller's.
#
# What the pane runs is `aiab run --shell ... --no-tmux`, the existing session
# path. That is what makes the shell see the same mounts, environment, network
# policy and git guard as the agent, and hold the per-container lock, so the
# idle-stopper cannot take the container down while you are standing in it.
# `--no-tmux` is the part that matters here: without it the shell would split a
# *second* monitor pane under itself. Two monitors are not in themselves a
# problem — aiab.netwatch.attached takes its marker lock shared precisely so
# concurrent ones work — but notification dedup is per-process, so every parked
# host would raise two desktop banners. Network decisions made from the shell
# belong to the monitor that is already running, or to `aiab net`.

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import agents

# The window a shell gets when it has one to itself. Short on purpose: it sits
# in the status line next to the agent's own window, and it is a label rather
# than an identity (see MARK).
WINDOW_NAME = "shell"

# tmux user option set on the shell's *pane*, naming the container whose shell
# it is. Set by whoever creates the pane rather than by the shell itself: a
# container can take seconds to start, and a pane left unmarked for that long
# is one a second ask would miss, opening a duplicate.
MARK = "@aiab_shell"

# How many lines the shell takes when it is a pane in the agent's window. The
# same ten the monitor pane gets (see aiab.cli._monitor_pane): enough for a
# command and its output, little enough to leave the agent the window.
PANE_HEIGHT = 10

# The agent's own pane within its window: the first one, which the monitor was
# split off and which a joined shell is placed under.
_AGENT_PANE = "0"

# What find() asks tmux for, one line per pane in the session.
_FORMAT = (
    f"#{{pane_id}}\t#{{window_id}}\t#{{window_index}}\t#{{window_panes}}\t#{{{MARK}}}"
)
_FIELDS = 5

# What show() did, for the caller to report.
OPENED = "opened"
SWITCHED = "switched"
JOINED = "joined"
PARKED = "parked"


class Error(Exception):
    """tmux could not do something this module needed."""


@dataclass(frozen=True)
class Shell:
    """Where a container's shell pane is right now."""

    pane: str
    window: str
    index: str | None
    # Whether it has its window to itself, i.e. it is "the shell window"
    # rather than a pane joined into the agent's.
    alone: bool


@dataclass(frozen=True)
class Outcome:
    """What an ask did, and where the shell ended up."""

    action: str
    index: str | None

    def describe(self) -> str:
        """One line for a terminal or the monitor's log."""
        where = f" (C-b {self.index})" if self.index else ""
        if self.action == OPENED:
            return f"opened a shell window{where}"
        if self.action == SWITCHED:
            return f"switched to the shell window{where}"
        if self.action == JOINED:
            return "pulled the shell into this window"
        return f"put the shell back in its own window{where}"


def _tmux(*args: str) -> str:
    """Run one tmux command and return its output, or raise Error.

    Unlike the questions in aiab.focus, nothing here is optional: a caller
    asking for a shell wants one, so a failure has to be reportable rather
    than quietly meaning "cannot tell".
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


def _ask(field: str) -> str:
    """Ask tmux about the caller's own place in the layout.

    Anchored to TMUX_PANE, which tmux sets for every process it starts in a
    pane, because an unanchored `display-message` answers about the *current*
    window of the *most recently used* session — neither of which need be
    ours. The monitor pane is the case that proves it: it lives in the agent's
    window, and asking it to put a shell "here" has to mean that window even
    when the terminal is off looking at another one.

    Without TMUX_PANE the caller is not in a pane but in a tmux key binding
    (`bind-key ... run-shell 'aiab shell'`), where there is a client and the
    unanchored answer is the right one: the window that client is looking at,
    which is the one whose keyboard was just used.
    """
    pane = os.environ.get("TMUX_PANE")
    target = ["-t", pane] if pane else []
    return _tmux("display-message", "-p", *target, field).strip()


def session() -> str | None:
    """The tmux session the caller is in, or None if it isn't in one.

    The TMUX check is not just a shortcut: asked from outside, tmux answers
    `display-message` about whichever session its server used last, and acting
    on that would open a shell in some session nobody is looking at.

    A pane in a session *group* reports the session that created its window,
    which need not be the one the terminal in front of you is attached to.
    That is the only session tmux will name from a pane, and it is the right
    one for the single-terminal case this is nearly always in; the cost of
    being wrong is a shell that appears in the group's shared window list
    without your view moving to it.
    """
    if not os.environ.get("TMUX"):
        return None
    return _ask("#{session_id}") or None


def current_window() -> str:
    """The window the caller is in, as a window id."""
    window = _ask("#{window_id}")
    if not window:
        raise Error("tmux did not say which window this is")
    return window


def find(session_id: str, container: str) -> Shell | None:
    """Where `container`'s shell is in `session_id`, or None if it has none.

    `-s` asks about every pane in the session rather than the current window,
    and in a session group that reaches every member's windows, since the list
    is shared — so a shell opened from another terminal is one this finds
    rather than duplicates.
    """
    for line in _tmux("list-panes", "-s", "-t", session_id, "-F", _FORMAT).splitlines():
        fields = line.split("\t")
        if len(fields) != _FIELDS:
            continue
        pane, window, index, panes, mark = fields
        if mark != container:
            continue
        return Shell(
            pane=pane,
            window=window,
            index=index or None,
            alone=panes.strip() == "1",
        )
    return None


def command(
    work_dir: Path,
    agent: str,
    profile: str | None = None,
    argv0: str | None = None,
) -> list[str]:
    """The `aiab run --shell` command line a shell pane runs.

    `argv0` is how to spell a re-invocation of aiab; it defaults to the
    launcher in the checkout this package lives in, which is how aiab is
    installed (see the README), and a caller that knows the path the user
    actually invoked should pass that instead.

    --for is passed explicitly rather than left to the pane's working
    directory, so the shell is for the directory that was asked about even if
    something later changes where the pane started.
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


def _create(
    session_id: str,
    work_dir: Path,
    container: str,
    run_cmd: list[str],
    as_pane: bool,
) -> Outcome:
    """Start the shell, as a pane here or as a window of its own."""
    if as_pane:
        # Split the agent's pane rather than whatever is active, so the shell
        # lands under the agent and above the monitor however you got here.
        out = _tmux(
            "split-window",
            "-v",
            "-l",
            str(PANE_HEIGHT),
            "-P",
            "-F",
            "#{pane_id}\t#{window_index}",
            "-t",
            f"{current_window()}.{_AGENT_PANE}",
            "-c",
            str(work_dir),
            shlex.join(run_cmd),
        )
    else:
        # The trailing colon means "this session, next free index"; without it
        # the target would name an existing window and tmux would refuse.
        out = _tmux(
            "new-window",
            "-P",
            "-F",
            "#{pane_id}\t#{window_index}",
            "-t",
            f"{session_id}:",
            "-n",
            WINDOW_NAME,
            "-c",
            str(work_dir),
            shlex.join(run_cmd),
        )
    pane, _, index = out.strip().partition("\t")
    if not pane:
        raise Error("tmux did not say which pane it created")
    _tmux("set-option", "-p", "-t", pane, MARK, container)
    return Outcome(action=JOINED if as_pane else OPENED, index=index or None)


def _to_window(session_id: str, shell: Shell) -> Outcome:
    """Show the shell full-window: switch to it, or break it out first."""
    if shell.alone:
        # Qualified with our session: a bare window id would move whichever
        # session tmux used last (see the module comment).
        _tmux("select-window", "-t", f"{session_id}:{shell.window}")
        return Outcome(action=SWITCHED, index=shell.index)
    # It is a pane in somebody's window — the agent's. Take it out into a
    # window of its own; without -d that window is selected, which is what
    # was asked for.
    index = _tmux(
        "break-pane", "-s", shell.pane, "-n", WINDOW_NAME, "-P", "-F", "#{window_index}"
    ).strip()
    return Outcome(action=SWITCHED, index=index or None)


def _toggle_pane(shell: Shell) -> Outcome:
    """Pull the shell into this window, or push it back out if it is here."""
    window = current_window()
    if shell.window != window:
        _tmux(
            "join-pane",
            "-s",
            shell.pane,
            "-t",
            f"{window}.{_AGENT_PANE}",
            "-l",
            str(PANE_HEIGHT),
        )
        return Outcome(action=JOINED, index=None)
    if shell.alone:
        # We are *in* the shell's own window, so there is no agent window here
        # to pull it into and nothing to push it out of. tmux would treat the
        # break as a no-op rather than an error, which would leave us claiming
        # to have moved something.
        raise Error(
            "the shell already has this window to itself — ask from the "
            "agent's window to pull it in there"
        )
    # -d so taking it out of sight doesn't take our view with it.
    index = _tmux(
        "break-pane",
        "-d",
        "-s",
        shell.pane,
        "-n",
        WINDOW_NAME,
        "-P",
        "-F",
        "#{window_index}",
    ).strip()
    return Outcome(action=PARKED, index=index or None)


def show(
    work_dir: Path,
    container: str,
    agent: str,
    *,
    as_pane: bool = False,
    profile: str | None = None,
    argv0: str | None = None,
) -> Outcome:
    """Show a shell for `container`, starting one if it has none.

    With `as_pane`, the shell is a pane in the window the caller is looking at
    — and asking again while it is there puts it back in a window of its own.
    Otherwise it is shown full-window, which for a shell currently joined into
    the agent's window means taking it back out.

    Raises Error when the caller isn't inside tmux — there is nowhere to put a
    pane then, and what to do instead (hand over this terminal, say so in a log
    pane) is the caller's call, not ours.
    """
    session_id = session()
    if session_id is None:
        raise Error("not inside a tmux session")
    found = find(session_id, container)
    if found is None:
        return _create(
            session_id,
            work_dir,
            container,
            command(work_dir, agent, profile, argv0),
            as_pane,
        )
    return _toggle_pane(found) if as_pane else _to_window(session_id, found)
