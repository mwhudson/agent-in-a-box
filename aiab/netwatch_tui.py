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
# aiab.netwatch_tui - the textual front end for `aiab net watch`.
#
# The richer of the two watch consoles (the shared plumbing and the plain
# keystroke fallback live in aiab.netwatch): the proxy logs scroll in the
# middle, and every parked host gets a row of Allow / 15m / Deny / Skip
# buttons above the footer — so a decision is a mouse click as well as a
# keystroke. Textual asks the terminal for mouse tracking itself, and tmux
# forwards mouse input to the pane, so the buttons work inside the tmux
# layout `aiab run` sets up (which also turns the tmux `mouse` option on in
# the sessions it creates, so a click lands here even while the agent pane
# has focus).
#
# aiab.cli imports this module lazily and falls back to the plain console
# when the import fails — because textual isn't installed, or is too old to
# have RichLog (Ubuntu's python3-textual 0.1.x predates the modern API).

from __future__ import annotations

import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Footer, RichLog, Static

from . import netwatch
from . import state


class PendingRow(Horizontal):
    """One parked host and its decision buttons."""

    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host

    def compose(self) -> ComposeResult:
        yield Static(self.host, classes="host")
        yield Button("Allow", name=netwatch.ALLOW, classes=netwatch.ALLOW)
        yield Button("15m", name=netwatch.TEMP, classes=netwatch.TEMP)
        yield Button("Deny", name=netwatch.DENY, classes=netwatch.DENY)
        yield Button("Skip", name=netwatch.SKIP, classes=netwatch.SKIP)


class WatchApp(App[None]):
    """Tail the proxy logs and prompt, with buttons, for parked hosts."""

    # No command palette: it would only crowd the footer of a 10-line pane.
    ENABLE_COMMAND_PALETTE = False

    # Compact styling: the usual home is a 10-line tmux pane, so the policy
    # header is one cropped line and the buttons lose their borders (which
    # is what makes a Button one cell tall).
    CSS = """
    #policy {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    #log {
        height: 1fr;
        padding: 0 1;
    }
    #pending {
        height: auto;
        max-height: 60%;
    }
    PendingRow {
        height: 1;
    }
    PendingRow .host {
        width: 1fr;
        padding: 0 1;
        text-style: bold;
    }
    PendingRow Button {
        height: 1;
        min-width: 7;
        border: none;
        margin: 0 1 0 0;
    }
    PendingRow Button.allow {
        background: $success-darken-2;
    }
    PendingRow Button.temp {
        background: $success-darken-3;
    }
    PendingRow Button.deny {
        background: $error-darken-2;
    }
    """

    BINDINGS = [
        Binding("a", f"decide('{netwatch.ALLOW}')", "allow"),
        Binding("t", f"decide('{netwatch.TEMP}')", "allow 15m"),
        Binding("d", f"decide('{netwatch.DENY}')", "deny"),
        Binding("s", f"decide('{netwatch.SKIP}')", "skip"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, work_dir: Path) -> None:
        super().__init__()
        self.work_dir = work_dir
        self.pdir = netwatch.pending_dir(work_dir)
        self.tails = netwatch.log_tails(work_dir)
        # Hosts already decided here whose pending file is still on disk (the
        # proxy removes it within a poll); don't re-prompt for those.
        self._handled: set[str] = set()
        # Number of undecided hosts currently shown; tracked separately from
        # the DOM so check_action can use it even when remove() is still
        # pending (Widget.remove is async).
        self._pending_count: int = 0

    def compose(self) -> ComposeResult:
        yield Static(id="policy")
        yield RichLog(id="log")
        yield VerticalScroll(id="pending")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_policy()
        self.set_interval(netwatch.POLL_INTERVAL, self._poll)

    def _refresh_policy(self) -> None:
        policy = state.get_network(self.work_dir)
        parts = [f"{self.work_dir}", f"mode: {policy['mode']}"]
        if policy["allow"]:
            parts.append("allow: " + ", ".join(a["domain"] for a in policy["allow"]))
        if policy["deny"]:
            parts.append("deny: " + ", ".join(policy["deny"]))
        self.query_one("#policy", Static).update(" · ".join(parts))

    def _poll(self) -> None:
        log = self.query_one("#log", RichLog)
        for tail in self.tails:
            for line in tail.read_new():
                log.write(line)

        # Mirror the pending dir into the rows: new file, new row; file gone
        # (request resolved or timed out), row gone. A handled host's file
        # vanishing also clears it from _handled, so a later retry of a
        # skipped or timed-out host prompts again.
        present = netwatch.pending_hosts(self.pdir)
        self._handled &= present
        pending = self.query_one("#pending", VerticalScroll)
        rows = {row.host: row for row in pending.query(PendingRow)}
        removed = [h for h, row in rows.items() if h not in present]
        for host in removed:
            rows[host].remove()
        new = sorted(present - rows.keys() - self._handled)
        for host in new:
            pending.mount(PendingRow(host))
        if new:
            # tmux turns the bell into a visual alert on the window — the
            # "decision pending" signal.
            self.bell()
        self._pending_count += len(new) - len(removed)
        if new or removed:
            self.refresh_bindings()
        self._refresh_policy()

    def _decide(self, row: PendingRow, action: str) -> None:
        self._handled.add(row.host)
        message = netwatch.apply_decision(self.work_dir, row.host, action)
        row.remove()
        self._pending_count -= 1
        self.query_one("#log", RichLog).write(message)
        self._refresh_policy()
        self.refresh_bindings()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        row = event.button.parent
        if isinstance(row, PendingRow):
            self._decide(row, event.button.name or netwatch.SKIP)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide decision bindings from the footer when nothing is pending."""
        if action == "decide":
            return True if self._pending_count > 0 else None
        return True

    def action_decide(self, action: str) -> None:
        """Apply a keystroke decision to the oldest pending host."""
        rows = self.query(PendingRow)
        if rows:
            self._decide(rows.first(), action)


def watch(work_dir: Path) -> int:
    """Run the textual watch UI for a directory; return an exit code."""
    if not sys.stdin.isatty():
        print("aiab net watch needs an interactive terminal", file=sys.stderr)
        return 1
    with netwatch.attached(netwatch.pending_dir(work_dir)):
        WatchApp(work_dir).run()
    return 0
