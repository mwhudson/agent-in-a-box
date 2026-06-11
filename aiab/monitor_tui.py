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
# aiab.monitor_tui - the textual front end for `aiab monitor`.
#
# The richer of the two control consoles (the shared network plumbing and the
# plain keystroke fallback live in aiab.netwatch). It is a general session
# control panel with two interchangeable views, swapped from a header button
# (or the `m` hotkey):
#
#   * the network view (default): proxy logs scroll in the middle, and every
#     parked host gets a row of Allow / 15m / Deny / Skip buttons above the
#     footer, so a decision is a mouse click as well as a keystroke;
#   * the mounts view: the recorded mounts for the directory, each a row with
#     a read-only/read-write toggle and a remove button, plus an input to add
#     a new one (with filesystem path completion). Edits mutate aiab.state and,
#     when a session container is around, take effect live on it — the same
#     thing `aiab mount`/`aiab unmount` do.
#
# Textual asks the terminal for mouse tracking itself, and tmux forwards mouse
# input to the pane, so the buttons work inside the tmux layout `aiab run` sets
# up (which also turns the tmux `mouse` option on in the sessions it creates,
# so a click lands here even while the agent pane has focus).
#
# aiab.cli imports this module lazily and falls back to the plain console when
# the import fails — because textual isn't installed, or is too old to have
# RichLog (Ubuntu's python3-textual 0.1.x predates the modern API).

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.suggester import Suggester
from textual.widgets import Button, Footer, Input, RichLog, Static

from . import PROJECT, WORK_PREFIX
from . import agents
from . import netwatch
from . import state
from .lxd import Lxd


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


class MountRow(Horizontal):
    """One recorded mount: its path, a read-only/read-write toggle, remove."""

    def __init__(self, source: str, readonly: bool) -> None:
        super().__init__()
        self.source = source
        self.readonly = readonly

    def compose(self) -> ComposeResult:
        yield Static(self.source, classes="mount-path")
        yield Button("ro" if self.readonly else "rw", name="mode", classes="mode")
        yield Button("×", name="remove", classes="remove")


class PathSuggester(Suggester):
    """Inline path completion: complete an input value against the filesystem.

    Walks the directory named by the value's leading path and offers the first
    matching entry, keeping the value's original (un-expanded) prefix so the
    suggestion still starts with what the user typed — which is what textual's
    Input needs to render it as ghost text. Directories gain a trailing slash
    so a Tab/right-arrow accept leads straight into the next segment.
    """

    def __init__(self) -> None:
        # Paths are case-sensitive, and the filesystem can change under us, so
        # don't cache completions.
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        if not value:
            return None
        expanded = os.path.expanduser(value)
        head, sep, tail = expanded.rpartition("/")
        directory = (head + sep) if sep else "."
        try:
            entries = sorted(os.listdir(directory))
        except OSError:
            return None
        prefix = value[: len(value) - len(tail)]  # original text before `tail`
        for name in entries:
            # Offer hidden entries only once the user has typed the leading dot.
            if name.startswith(".") and not tail.startswith("."):
                continue
            if name.startswith(tail):
                suggestion = prefix + name
                if os.path.isdir(os.path.join(directory, name)):
                    suggestion += "/"
                return suggestion
        return None


class MonitorApp(App[None]):
    """Tail the proxy logs and prompt for parked hosts; manage mounts."""

    # No command palette: it would only crowd the footer of a 10-line pane.
    ENABLE_COMMAND_PALETTE = False

    # Compact styling: the usual home is a 10-line tmux pane, so the header is
    # one cropped line and the buttons lose their borders (which is what makes
    # a Button one cell tall).
    CSS = """
    #header {
        height: 1;
        background: $panel;
    }
    #policy {
        width: 1fr;
        height: 1;
        padding: 0 1;
    }
    #view-toggle {
        height: 1;
        min-width: 9;
        border: none;
        margin: 0;
    }
    #log {
        height: 1fr;
        padding: 0 1;
    }
    #mounts {
        height: 1fr;
    }
    #mount-list {
        height: 1fr;
    }
    MountRow {
        height: 1;
    }
    MountRow .mount-path {
        width: 1fr;
        padding: 0 1;
    }
    MountRow Button {
        height: 1;
        min-width: 4;
        border: none;
        margin: 0 1 0 0;
    }
    MountRow .remove {
        min-width: 3;
        background: $error-darken-2;
    }
    #add-row {
        height: 1;
    }
    #add-mount {
        height: 1;
        min-width: 7;
        border: none;
        margin: 0 1 0 0;
        background: $success-darken-2;
    }
    #add-path {
        height: 1;
        border: none;
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
        Binding("m", "toggle_mounts", "mounts"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, work_dir: Path, container_name: str | None = None) -> None:
        super().__init__()
        self.work_dir = work_dir
        # The session container whose live mounts a mounts edit should touch
        # (the agent `aiab run` opened this pane for). None when run stand-alone,
        # in which case an edit falls back to every existing container for the
        # directory. The Lxd handle is built lazily, only when a mount op needs
        # it, so the network view never reaches for LXD.
        self.container_name = container_name
        self._lxd: Lxd | None = None
        self.pdir = netwatch.pending_dir(work_dir)
        self.tails = netwatch.log_tails(work_dir)
        # Hosts already decided here whose pending file is still on disk (the
        # proxy removes it within a poll); don't re-prompt for those.
        self._handled: set[str] = set()
        # Number of undecided hosts currently shown; tracked separately from
        # the DOM so check_action can use it even when remove() is still
        # pending (Widget.remove is async).
        self._pending_count: int = 0
        # Which view fills the middle of the pane.
        self._showing_mounts = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(id="policy")
            yield Button("Mounts", id="view-toggle")
        yield RichLog(id="log")
        with Vertical(id="mounts"):
            yield VerticalScroll(id="mount-list")
            with Horizontal(id="add-row"):
                yield Button("+ Add", id="add-mount")
                yield Input(
                    id="add-path",
                    placeholder="path to mount (read-only)",
                    suggester=PathSuggester(),
                )
        yield VerticalScroll(id="pending")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#mounts").display = False
        self._refresh_policy()
        self.set_interval(netwatch.POLL_INTERVAL, self._poll)

    # -- network view --

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

    # -- mounts view --

    def action_toggle_mounts(self) -> None:
        self._showing_mounts = not self._showing_mounts
        self.query_one("#log").display = not self._showing_mounts
        self.query_one("#mounts").display = self._showing_mounts
        self.query_one("#view-toggle", Button).label = (
            "Network" if self._showing_mounts else "Mounts"
        )
        if self._showing_mounts:
            self._refresh_mounts()

    def _refresh_mounts(self) -> None:
        mount_list = self.query_one("#mount-list", VerticalScroll)
        for row in mount_list.query(MountRow):
            row.remove()
        for m in state.get_mounts(self.work_dir):
            mount_list.mount(MountRow(m["source"], m["readonly"]))

    def _containers(self) -> list:
        """The session containers a mounts edit should apply to, live.

        The named one when `aiab run` handed us a container, otherwise every
        agent container that currently exists for the directory (so a stand-
        alone monitor still reaches running sessions). An empty list — no LXD,
        nothing running — just means the edit is recorded for the next run.
        """
        if self._lxd is None:
            self._lxd = Lxd(PROJECT)
        try:
            if self.container_name is not None:
                container = self._lxd.container(self.container_name)
                return [container] if container.exists() else []
            return [
                container
                for agent in agents.AGENT_NAMES
                for container in [self._lxd.container_for_dir(self.work_dir, agent)]
                if container.exists()
            ]
        except OSError:  # lxc not installed / not reachable
            return []

    def _write_log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _apply_to_containers(self, describe: str, op) -> None:
        """Run a device op on each target container, logging its output.

        The Container methods narrate to stderr; capture that so it lands in
        the log pane instead of corrupting the live display, and report a
        failure there too rather than tearing the app down.
        """
        captured = io.StringIO()
        try:
            with contextlib.redirect_stderr(captured):
                for container in self._containers():
                    op(container)
        except Exception as exc:  # an lxc call failed; keep the UI alive
            self._write_log(f"{describe} failed: {exc}")
        for line in captured.getvalue().splitlines():
            self._write_log(line)

    def _add_mount(self, raw_path: str, readonly: bool) -> None:
        path = Path(raw_path).expanduser()
        try:
            source = str(path.resolve(strict=True))
        except OSError:
            self._write_log(f"no such directory: {raw_path}")
            return
        if not Path(source).is_dir():
            self._write_log(f"not a directory: {source}")
            return
        state.set_mount(self.work_dir, source, readonly)
        self._apply_to_containers(
            f"mount {source}",
            lambda c: c.add_device(source, work_prefix=WORK_PREFIX, readonly=readonly),
        )
        self._refresh_mounts()

    def _toggle_mode(self, row: MountRow) -> None:
        readonly = not row.readonly
        state.set_mount(self.work_dir, row.source, readonly)
        self._apply_to_containers(
            f"remount {row.source}",
            lambda c: c.add_device(
                row.source, work_prefix=WORK_PREFIX, readonly=readonly
            ),
        )
        self._refresh_mounts()

    def _remove_mount(self, row: MountRow) -> None:
        state.remove_mount(self.work_dir, row.source)
        self._apply_to_containers(
            f"unmount {row.source}",
            lambda c: c.remove_dir_device(row.source),
        )
        self._refresh_mounts()

    # -- shared event handling --

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if button.id == "view-toggle":
            self.action_toggle_mounts()
            return
        if button.id == "add-mount":
            self.query_one("#add-path", Input).focus()
            return
        row = button.parent
        if isinstance(row, PendingRow):
            self._decide(row, button.name or netwatch.SKIP)
        elif isinstance(row, MountRow):
            if button.name == "remove":
                self._remove_mount(row)
            else:
                self._toggle_mode(row)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "add-path":
            value = event.value.strip()
            if value:
                self._add_mount(value, readonly=True)  # default new mounts to ro
            event.input.value = ""


def monitor(work_dir: Path, container_name: str | None = None) -> int:
    """Run the textual monitor UI for a directory; return an exit code."""
    if not sys.stdin.isatty():
        print("aiab monitor needs an interactive terminal", file=sys.stderr)
        return 1
    with netwatch.attached(netwatch.pending_dir(work_dir)):
        MonitorApp(work_dir, container_name).run()
    return 0
