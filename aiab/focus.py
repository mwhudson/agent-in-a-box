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
# aiab.focus - whether the agent's terminal has your attention.
#
# aiab.attention gets a wait out of the container and aiab.monitor_tui turns
# one that has gone on long enough into a desktop notification. This answers
# the question that decides whether such a notification should still be up:
# are you *there*? A banner about an agent waiting is worth raising while you
# are somewhere else, and is noise the moment you look at the terminal it is
# about.
#
# Neither agent can answer it. Claude Code's hook events and opencode's plugin
# events both stop at "you answered" (UserPromptSubmit, chat.message); there is
# no focus or keystroke event in either, and Claude's own focus tracking never
# leaves its TUI. Which is fine, because the question was never really the
# agent's: the notification is raised on the host, and the terminal that would
# have your attention is on the host too. tmux, which `aiab run` already wraps
# the agent in (see aiab.cli), knows the answer and will say:
#
#   * `#{client_flags}` carries `focused` for a client whose terminal window
#     has focus, and `#{window_id}` for that same client is the window it is
#     looking at *now* — so "you are looking at this agent" is: some client
#     whose current window is ours, flagged focused;
#   * `#{client_activity}` is when that client last sent input. It advances on
#     a keystroke and not on pane output — an agent streaming a reply does not
#     move it — which makes it the "you pressed a key" half.
#
# Both rest on tmux's `focus-events`, which is off by default: with it off tmux
# never asks the terminal to report focus, and every client stays flagged
# focused for as long as it is attached. So it gets turned on — and it has to
# be on *before* the terminal attaches. tmux asks a terminal to start
# reporting focus while it is working out what that terminal can do, which it
# does once, just after a client attaches. Verified on tmux 3.6: with the
# option on beforehand the client is sent `\033[?1004h` as soon as it answers
# tmux's capability queries; turned on afterwards nothing is ever sent to that
# client, and neither `refresh-client` nor re-setting `terminal-features`
# makes tmux reconsider. So enable() is called by `aiab run` before it attaches
# a terminal (aiab.cli._reexec_under_tmux), and not — as it once was — by the
# monitor the first time it had a wait to weigh up, which is long past the only
# moment that would have worked.
#
# The option is server-wide, which is the only scope tmux has for it, so this
# also reaches a tmux server that was the user's rather than one `aiab run`
# started. That is a small thing to change under someone (tmux only forwards
# focus events to a pane that asked for them, which is what every editor
# already does) and it is deliberately not turned back off afterwards, because
# "off" is not ours to restore — the user may have set it, and another aiab
# session may be relying on it.
#
# A terminal that isn't reporting focus after all is the case to be careful
# about: `focused` would be stuck on, every wait would look like one you were
# already looking at, and nothing would ever be announced. It is not
# hypothetical — a client that attached before the option went on is exactly
# that, and it is what a run joining a tmux session you already had gets. tmux
# cannot be asked either: `#{client_termfeatures}` lists `focus` for any
# terminal tmux believes *can* report it, whether or not it was ever asked to,
# so it says nothing about this client. What does is the flag moving: focus is
# believed only once some client has actually been seen unfocused. Until then
# look() says it cannot tell, and its caller does what it did before this
# module existed.

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

# One line per attached client: whether its terminal has focus, when it last
# sent input, and which window it is showing.
_FORMAT = "#{client_flags}\t#{client_activity}\t#{window_id}"
_FIELDS = 3

# The flag a focused client carries.
_FOCUSED = "focused"

# How stale the previous reading may be for a keystroke to still be this
# question's. look() is called on the monitor's poll while a wait is in flight
# and not at all between waits, so a longer gap than a few polls means the
# baseline belongs to some earlier wait, and a bump across it says nothing
# about whether you were there for this one.
_STALE = 2.0


@dataclass(frozen=True)
class Look:
    """What one poll could tell about the terminal showing the agent.

    `focused` is None when there is nothing to go on: no tmux, no focus
    reporting, or a terminal whose focus flag has never been shown to mean
    anything. That is different from False, which is a terminal that is
    genuinely elsewhere.

    `typed` is whether a key reached that terminal since the previous look —
    only ever True alongside `focused`, since a key you pressed somewhere else
    is not you dealing with this.
    """

    focused: bool | None
    typed: bool = False


# What every look says when the question cannot be answered at all.
UNKNOWN = Look(focused=None)


def _tmux(*args: str) -> str | None:
    """Run one tmux command, returning its output or None if it failed.

    Every use here is a question aiab can do without: no tmux, no server, no
    such pane — all of them mean "cannot tell", which Look already has a way
    to say.
    """
    try:
        result = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _on() -> bool:
    """Whether tmux's focus reporting is on at all."""
    shown = _tmux("show-options", "-s", "focus-events")
    return shown is not None and shown.split()[-1:] == ["on"]


def enable() -> bool:
    """Turn tmux's focus reporting on, and say whether it is now on.

    Call this before attaching a terminal to the agent's session: it is the
    last moment tmux will pass the request on to that terminal (see the module
    comment). Idempotent, and deliberately never undone.

    The read-back is what matters: an old tmux that doesn't know the option
    leaves us with a focus flag that would lie, and it is better to know that
    here than to silently stop announcing waits.
    """
    _tmux("set-option", "-s", "focus-events", "on")
    return _on()


class Focus:
    """Whether the window holding this pane has your attention.

    Constructed by aiab.monitor_tui, which runs in a tmux pane below the agent
    (aiab.cli._monitor_pane), so "this pane's window" is the agent's window —
    and, because `aiab run` shares one window list across a session group, a
    client of any session in the group counts as long as that window is the
    one it is showing.

    Only ever asks questions: whether focus can be reported at all was
    settled before the terminal attached (see enable()), and a monitor that
    never has a wait to weigh up asks tmux nothing at all.
    """

    def __init__(self, pane: str | None = None) -> None:
        # The pane we are in. tmux sets TMUX_PANE for every process it starts.
        self._pane = pane if pane is not None else os.environ.get("TMUX_PANE")
        self._window: str | None = None
        # Whether tmux can answer at all: None until the first look() tries.
        self._ready: bool | None = None
        # Whether this terminal's focus flag has been shown to mean something.
        self._trusted = False
        # The previous reading, for spotting a keystroke: the client's input
        # time, whether it was focused, and when we looked.
        self._activity: int | None = None
        self._focused: bool | None = None
        self._when = 0.0

    def _setup(self) -> bool:
        """Resolve our window and check focus reporting is on; True if both.

        Turning it on here would be too late — the terminal attached long
        before the first wait (see the module comment) — so this only reads the
        option. Off means no client will ever report focus, and there is
        nothing to read.
        """
        if self._ready is None:
            self._ready = False
            if self._pane:
                window = _tmux(
                    "display-message", "-p", "-t", self._pane, "#{window_id}"
                )
                if window and window.strip():
                    self._window = window.strip()
                    self._ready = _on()
        return self._ready

    def _clients(self) -> list[tuple[str, int]] | None:
        """Every attached client currently showing our window."""
        out = _tmux("list-clients", "-F", _FORMAT)
        if out is None:
            return None
        rows = []
        for line in out.splitlines():
            fields = line.split("\t")
            if len(fields) != _FIELDS:
                continue
            flags, activity, window = fields
            if window != self._window:
                continue
            try:
                rows.append((flags, int(activity)))
            except ValueError:
                continue
        return rows

    def look(self) -> Look:
        """Ask tmux where you are, relative to the agent's window."""
        if not self._setup():
            return UNKNOWN
        rows = self._clients()
        if rows is None:
            return UNKNOWN
        now = time.monotonic()
        previous, previous_activity, when = self._focused, self._activity, self._when
        self._when = now
        if not rows:
            # The window isn't on anybody's screen. No focus reporting is
            # needed to be sure of that, and it doesn't make the flag on some
            # future client any more trustworthy — so this doesn't grant trust
            # the way an observed unfocus does.
            self._focused, self._activity = False, None
            return Look(focused=False)
        focused = any(_FOCUSED in flags for flags, _ in rows)
        activity = max(activity for _, activity in rows)
        self._focused, self._activity = focused, activity
        if any(_FOCUSED not in flags for flags, _ in rows):
            # A client that isn't flagged focused is one whose terminal is
            # reporting focus rather than being assumed to have it. One
            # sighting settles it for good — a terminal that reports focus
            # goes on reporting it for as long as it is attached — and any
            # client will do, since the option they turn on is the server's.
            self._trusted = True
        if not self._trusted:
            return UNKNOWN
        # A focus change is itself input as far as tmux is concerned — the
        # terminal sends an escape sequence for it, and that moves the
        # client's activity time. So a bump only counts as a keystroke when
        # the focus either side of it was the same, and when the reading it is
        # measured against is recent enough to belong to this wait.
        typed = (
            focused
            and previous == focused
            and previous_activity is not None
            and activity > previous_activity
            and now - when <= _STALE
        )
        return Look(focused=focused, typed=typed)
