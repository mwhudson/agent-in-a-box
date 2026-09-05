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
# aiab.notify - desktop notifications for the questions `aiab monitor` is
# waiting on an answer to.
#
# The monitor's usual home is a ten-line tmux pane under the agent, which is
# easy not to be looking at: a parked host sat there unanswered until the
# proxy gave up on it (aiab.netproxy._ASK_TIMEOUT, 60s) and the agent saw a
# connection failure instead of an answer. This raises the same question on
# the desktop, where a notification is hard to miss — and, because notify-send
# can attach action buttons to one, lets the decision be made from the
# notification itself without going back to the terminal at all.
#
# It is deliberately best-effort, and silent when it can't work:
#
#   * without notify-send on PATH (it's in libnotify-bin, which isn't
#     installed by default) nothing is raised and the monitor behaves exactly
#     as it did before — the pane and the terminal bell are the whole UI;
#   * notify-send failing — no session bus over ssh, no notification daemon —
#     is equally quiet, because its stderr is discarded rather than dumped
#     into the pane.
#
# So this only ever *adds* a way to answer. Every decision it can take is one
# the pane can take too, and the pane stays authoritative: a click here is
# routed back through the same aiab.netwatch call the buttons use.
#
# Nothing here imports textual — action callbacks arrive on a reader thread,
# and it's the caller's business to get them onto its own thread (see
# aiab.monitor_tui, which hands them to textual's App.call_from_thread).

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

# What the notification identifies itself as, and the themed icon it asks for.
# An icon name the theme doesn't have just means no icon, so this is safe.
_APP_NAME = "aiab"
_ICON = "network-transmit-receive"

# How long to give `gdbus` to withdraw a notification. It's a local call to an
# already-running daemon, so this only bounds a hang.
_CLOSE_TIMEOUT = 5.0

# Where the session bus lives when the environment doesn't say (see _bus_env).
_BUS_SOCKET = Path(f"/run/user/{os.getuid()}/bus")

# Called with (key, action) when a notification button is clicked. Runs on the
# reader thread for that notification, not the caller's.
ActionCallback = Callable[[str, str], None]


def _bus_env() -> dict[str, str]:
    """The environment to run notify-send in, with a session bus if possible.

    The monitor is normally a tmux pane, and tmux gives a new pane the
    environment of whichever client created the *session*. For a tmux server
    that outlives a logout that address can be stale or missing, which would
    leave notifications quietly broken in exactly the setup they matter most
    in. The bus socket is at a predictable path, so fall back to that instead
    of trusting the inherited value blindly.
    """
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and _BUS_SOCKET.exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={_BUS_SOCKET}"
    return env


class _Live:
    """One notification currently on screen: its notify-send and its id.

    The id arrives on notify-send's stdout (--print-id) and is what lets
    close() withdraw the notification through the daemon. Whether it turns up
    before notify-send exits depends on that process's stdio buffering, so it
    stays None if it doesn't — see Notifier.close for what that costs.
    """

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self.ident: str | None = None


class Notifier:
    """Desktop notifications with buttons, at most one live per key.

    A key names the question being asked — aiab.monitor_tui uses the parked
    host — and is what ties the three operations together: notify() raises a
    notification for one, an action on it comes back through the callback
    under that key, and close() withdraws it once the question has been
    answered elsewhere or stopped mattering.
    """

    def __init__(self, on_action: ActionCallback) -> None:
        self._on_action = on_action
        # Resolved once: PATH isn't going to change under a running monitor,
        # and `enabled` is really "was notify-send installed when we started".
        self._send = shutil.which("notify-send")
        self._gdbus = shutil.which("gdbus")
        self._env = _bus_env()
        self._lock = threading.Lock()
        self._live: dict[str, _Live] = {}

    @property
    def enabled(self) -> bool:
        """Whether notifications can be raised at all (notify-send present)."""
        return self._send is not None

    def notify(
        self,
        key: str,
        summary: str,
        body: str,
        actions: Sequence[tuple[str, str]] = (),
    ) -> None:
        """Raise a notification for key, with (action, label) buttons.

        A second call for a key already on screen is ignored, so a caller can
        re-assert a pending question every poll without stacking up banners.
        With no actions it is a statement rather than a question: nothing to
        click, and close() is the only thing that takes it down.
        """
        if self._send is None:
            return
        with self._lock:
            if key in self._live:
                return
        cmd = [
            self._send,
            "--app-name",
            _APP_NAME,
            "--icon",
            _ICON,
            # Critical outlives the few seconds an ordinary banner is given,
            # and expiry is ours to manage: the question is worth interrupting
            # for, and close() is what takes the notification down.
            "--urgency",
            "critical",
            "--expire-time",
            "0",
            # Prints the notification id, which close() needs. With actions
            # attached notify-send then stays alive until one is clicked (or
            # we terminate it) and prints the one that was.
            "--print-id",
        ]
        for action, label in actions:
            cmd += ["--action", f"{action}={label}"]
        cmd += ["--", summary, body]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=self._env,
            )
        except OSError:
            return
        live = _Live(proc)
        with self._lock:
            self._live[key] = live
        threading.Thread(target=self._read, args=(key, live), daemon=True).start()

    def _read(self, key: str, live: _Live) -> None:
        """Read one notify-send's output to its end, then report the click.

        Output is the id (from --print-id) and then, if the user pressed a
        button, that action's name. The verdict is only delivered if this
        notification is still the live one for its key: if close() got there
        first the question has already been answered in the pane, and a click
        that raced with the withdrawal shouldn't overrule it.

        Only a click retires the notification here. notify-send exiting does
        not mean the banner is gone — one raised with no actions exits
        immediately and stays on screen — so otherwise the entry is left for
        close() to withdraw.
        """
        action: str | None = None
        if live.proc.stdout is not None:
            for line in live.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if live.ident is None and line.isdigit():
                    live.ident = line
                else:
                    action = line
        live.proc.wait()
        with self._lock:
            current = self._live.get(key) is live
            if current and action is not None:
                # Acting on a notification closes it, so there is nothing
                # left for close() to withdraw.
                del self._live[key]
        if current and action is not None:
            self._on_action(key, action)

    def close(self, key: str) -> None:
        """Withdraw the notification for key, if one is up.

        Terminating notify-send ends its wait, but whether the daemon then
        drops the banner is the daemon's business, so ask it directly when the
        id made it out in time. If it didn't, the worst case is a stale banner
        whose buttons no longer reach anyone — which the summary's phrasing
        has to survive, since a click on it is simply lost.
        """
        with self._lock:
            live = self._live.pop(key, None)
        if live is None:
            return
        if live.ident is not None and self._gdbus is not None:
            self._close_by_id(live.ident)
        with contextlib.suppress(OSError):
            live.proc.terminate()

    def close_all(self) -> None:
        """Withdraw every notification still up (the monitor is going away)."""
        with self._lock:
            keys = list(self._live)
        for key in keys:
            self.close(key)

    def _close_by_id(self, ident: str) -> None:
        assert self._gdbus is not None
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [
                    self._gdbus,
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.Notifications",
                    "--object-path",
                    "/org/freedesktop/Notifications",
                    "--method",
                    "org.freedesktop.Notifications.CloseNotification",
                    ident,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._env,
                timeout=_CLOSE_TIMEOUT,
                check=False,
            )
