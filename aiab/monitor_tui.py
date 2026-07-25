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
# The `aiab monitor` control console (its shared network plumbing — the
# pending queue, the proxy-log tails and the decision recording — lives in
# aiab.netwatch). It is a general session control panel with five tabs,
# selected from the buttons in the header (or the 1/2/3/4/5 hotkeys):
#
#   * Network (default): proxy logs scroll in the middle, and every parked host
#     gets a row of Allow / 15m / Deny / Skip buttons above the footer, so a
#     decision is a mouse click as well as a keystroke;
#   * Domains: every domain already allowed or denied for the directory, each a
#     row whose Allow / 15m / Deny / × buttons re-decide it on the spot — so a
#     past Deny can be flipped to Allow with one click — plus an input to allow
#     a new domain up front. Writes the same aiab.state the parked-host
#     decisions do;
#   * Mounts: the recorded mounts for the directory, each a row with a
#     read-only/read-write toggle and a remove button, plus an input to add
#     a new one (with filesystem path completion). Edits mutate aiab.state and,
#     when a session container is around, take effect live on it — the same
#     thing `aiab mount`/`aiab unmount` do;
#   * Ports: lists TCP ports the container is listening on above the threshold
#     and prompts to forward them to the host;
#   * Limits: the directory's CPU and memory limits, each editable inline with
#     a Set button. Changes are saved to aiab.state and take effect on the next
#     `aiab run` for the directory.
#
# Textual asks the terminal for mouse tracking itself, and tmux forwards mouse
# input to the pane, so the buttons work inside the tmux layout `aiab run` sets
# up (which also turns the tmux `mouse` option on in the sessions it creates,
# so a click lands here even while the agent pane has focus).
#
# textual is a hard requirement (>= 0.32, for RichLog and the modern app API —
# Ubuntu's python3-textual 0.1.x is too old; see docs/install.md). aiab.cli
# still imports this module lazily, so that only `aiab monitor` pays the cost
# of loading it.

from __future__ import annotations

import contextlib
import io
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.suggester import Suggester
from textual.widget import Widget
from textual.widgets import Button, Input, RichLog, Static

from . import PROJECT, WORK_PREFIX
from . import agents
from . import netproxy
from . import netwatch
from . import profiles
from . import state
from .lxd import Container, Lxd

# Ports below this threshold are skipped when scanning the container's socket
# table — they're nearly always system services (sshd, DNS, etc.) rather than
# something the agent started.
_MIN_FORWARD_PORT = 1024

# Ports to exclude even if they're above the threshold — our own proxy port
# is always listening inside restricted containers.
_EXCLUDED_PORTS: frozenset[int] = frozenset({netproxy.PROXY_PORT})

_VIEWS = ("network", "domains", "mounts", "ports", "limits")


def _read_listening_ports(init_pid: int) -> set[int]:
    """Return ports with TCP LISTEN sockets in a container's network namespace.

    Reads /proc/<init_pid>/net/tcp and tcp6, which are scoped to the
    container's network namespace because init_pid is the container's PID 1.
    State 0A in the hex table is TCP_LISTEN; the port is the last four hex
    digits of the local_address field.
    """
    ports: set[int] = set()
    for name in ("tcp", "tcp6"):
        try:
            text = Path(f"/proc/{init_pid}/net/{name}").read_text()
        except OSError:
            continue
        for line in text.splitlines()[1:]:  # skip the header row
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            try:
                port = int(parts[1].split(":")[1], 16)
            except (IndexError, ValueError):
                continue
            if port >= _MIN_FORWARD_PORT and port not in _EXCLUDED_PORTS:
                ports.add(port)
    return ports


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


class DecisionRow(Horizontal):
    """One already-decided domain and (if editable) the buttons to re-decide
    or drop it.

    Covers three scopes (see aiab.state.NetworkPolicy / get_global_network):
    this directory's all-agents rules (agent=None, editable), this
    directory's per-agent overlay (agent=<name>, editable, tagged), and the
    global policy shared by every directory (editable=False, tagged) — shown
    read-only so a rule set globally (aiab net --global) isn't invisible
    here, which would otherwise let a user conclude no rule exists and try to
    add a contradictory local one.
    """

    def __init__(
        self,
        domain: str,
        kind: str,
        expires: float | None = None,
        *,
        agent: str | None = None,
        editable: bool = True,
    ) -> None:
        super().__init__()
        self.domain = domain
        self.kind = kind  # netwatch.ALLOW or netwatch.DENY
        self.expires = expires
        self.agent = agent
        self.editable = editable

    def compose(self) -> ComposeResult:
        if self.kind == netwatch.DENY:
            badge, badge_class = "denied", "badge-deny"
        elif self.expires is not None:
            badge, badge_class = "15m", "badge-allow"
        else:
            badge, badge_class = "allowed", "badge-allow"
        yield Static(self.domain, classes="domain")
        yield Static(badge, classes=f"badge {badge_class}")
        if self.agent:
            yield Static(f"[{self.agent}]", classes="scope-tag")
        if not self.editable:
            yield Static("(global)", classes="scope-tag scope-global")
            return
        yield Button("Allow", name=netwatch.ALLOW, classes=netwatch.ALLOW)
        yield Button("15m", name=netwatch.TEMP, classes=netwatch.TEMP)
        yield Button("Deny", name=netwatch.DENY, classes=netwatch.DENY)
        yield Button("×", name="remove", classes="remove")

    # Enter/Leave bubble up from the child statics and buttons, so the row sees
    # them whenever the mouse is anywhere along it. Leave fires when crossing
    # between children too, so only drop the highlight once the mouse has really
    # left the row's region (which is_mouse_over reports for the whole row).
    def on_enter(self, event: events.Enter) -> None:
        self.add_class("hovered")

    def on_leave(self, event: events.Leave) -> None:
        if not self.is_mouse_over:
            self.remove_class("hovered")


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


class PortPendingRow(Horizontal):
    """A newly detected listening port waiting for a forwarding decision."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port

    def compose(self) -> ComposeResult:
        yield Static(f":{self.port}", classes="port")
        yield Button("Forward", name="forward", classes="forward")
        yield Button("Ignore", name="ignore", classes="ignore")


class PortForwardRow(Horizontal):
    """A port actively forwarded from the host into the container."""

    def __init__(self, port: int) -> None:
        super().__init__()
        self.port = port

    def compose(self) -> ComposeResult:
        yield Static(f"localhost:{self.port}", classes="port-label")
        yield Button("×", name="remove", classes="remove")


class LimitRow(Horizontal):
    """One resource limit field: its name, an editable value, and a Set button."""

    def __init__(self, field: str, value: str) -> None:
        super().__init__()
        self.field = field
        self._value = value

    def compose(self) -> ComposeResult:
        yield Static(self.field, classes="limit-label")
        yield Input(self._value, id=f"limit-{self.field}", classes="limit-input")
        yield Button("Set", name="set", classes="limit-set")


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
    .tab {
        height: 1;
        min-width: 9;
        border: none;
        margin: 0;
    }
    .tab.active {
        background: $accent;
        text-style: bold;
    }
    .tab.flash {
        background: $warning;
        color: $text;
        text-style: bold;
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
    #domains {
        height: 1fr;
    }
    #domain-list {
        height: 1fr;
    }
    DecisionRow {
        height: 1;
    }
    DecisionRow.hovered {
        background: $panel-lighten-2;
    }
    DecisionRow .domain {
        width: 1fr;
        padding: 0 1;
    }
    DecisionRow .badge {
        width: 9;
        padding: 0 1;
        text-style: bold;
    }
    DecisionRow .badge-allow {
        color: $success;
    }
    DecisionRow .badge-deny {
        color: $error;
    }
    DecisionRow Button {
        height: 1;
        min-width: 7;
        border: none;
        margin: 0 1 0 0;
    }
    DecisionRow Button.allow {
        background: $success-darken-2;
    }
    DecisionRow Button.temp {
        background: $success-darken-3;
    }
    DecisionRow Button.deny {
        background: $error-darken-2;
    }
    DecisionRow .remove {
        min-width: 3;
        background: $error-darken-2;
    }
    DecisionRow .scope-tag {
        width: auto;
        padding: 0 1;
        color: $text-muted;
    }
    DecisionRow .scope-global {
        text-style: italic;
    }
    #add-domain-row {
        height: 1;
    }
    #add-domain-btn {
        height: 1;
        min-width: 7;
        border: none;
        margin: 0 1 0 0;
        background: $success-darken-2;
    }
    #add-domain {
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
    #ports {
        height: 1fr;
    }
    #port-list {
        height: 1fr;
    }
    #port-pending {
        height: auto;
        max-height: 60%;
    }
    PortForwardRow {
        height: 1;
    }
    PortForwardRow .port-label {
        width: 1fr;
        padding: 0 1;
    }
    PortForwardRow Button {
        height: 1;
        min-width: 3;
        border: none;
        margin: 0 1 0 0;
    }
    PortForwardRow .remove {
        min-width: 3;
        background: $error-darken-2;
    }
    PortPendingRow {
        height: 1;
    }
    PortPendingRow .port {
        width: 1fr;
        padding: 0 1;
        text-style: bold;
    }
    PortPendingRow Button {
        height: 1;
        min-width: 9;
        border: none;
        margin: 0 1 0 0;
    }
    PortPendingRow .forward {
        background: $success-darken-2;
    }
    PortPendingRow .ignore {
        background: $error-darken-3;
    }
    #limits {
        height: 1fr;
    }
    #limit-list {
        height: auto;
    }
    LimitRow {
        height: 1;
    }
    LimitRow .limit-label {
        width: 10;
        padding: 0 1;
        text-style: bold;
    }
    LimitRow .limit-input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
    }
    LimitRow .limit-set {
        height: 1;
        min-width: 5;
        border: none;
        margin: 0 1 0 0;
        background: $success-darken-2;
    }
    """

    BINDINGS = [
        Binding("a", f"decide('{netwatch.ALLOW}')", "allow"),
        Binding("t", f"decide('{netwatch.TEMP}')", "allow 15m"),
        Binding("d", f"decide('{netwatch.DENY}')", "deny"),
        Binding("s", f"decide('{netwatch.SKIP}')", "skip"),
        Binding("1", "select_view('network')", "network", show=False),
        Binding("2", "select_view('domains')", "domains", show=False),
        Binding("3", "select_view('mounts')", "mounts", show=False),
        Binding("m", "select_view('mounts')", "mounts", show=False),
        Binding("4", "select_view('ports')", "ports", show=False),
        Binding("p", "select_view('ports')", "ports", show=False),
        Binding("5", "select_view('limits')", "limits", show=False),
        Binding("l", "select_view('limits')", "limits", show=False),
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
        # Which tab fills the middle of the pane.
        self._view = "network"
        # Whether the Network tab is currently lit in its flash cycle (toggled
        # while a decision is pending and another tab has focus).
        self._flash_on = False
        # Port-forwarding state: the cached init PID of the session container
        # (fetched lazily from lxc info, reset when the proc entry vanishes),
        # ports seen on the last poll, ports the user chose to ignore this
        # session, and ports currently forwarded to the host.
        self._init_pid: int | None = None
        self._known_ports: set[int] = set()
        self._ignored_ports: set[int] = set()
        self._forwarded_ports: set[int] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Button("Network", id="tab-network", classes="tab")
            yield Button("Domains", id="tab-domains", classes="tab")
            yield Button("Mounts", id="tab-mounts", classes="tab")
            yield Button("Ports", id="tab-ports", classes="tab")
            yield Button("Limits", id="tab-limits", classes="tab")
            yield Static(id="policy")
        yield RichLog(id="log")
        with Vertical(id="domains"):
            yield VerticalScroll(id="domain-list")
            with Horizontal(id="add-domain-row"):
                yield Button("+ Allow", id="add-domain-btn")
                yield Input(id="add-domain", placeholder="domain to allow")
        with Vertical(id="mounts"):
            yield VerticalScroll(id="mount-list")
            with Horizontal(id="add-row"):
                yield Button("+ Add", id="add-mount")
                yield Input(
                    id="add-path",
                    placeholder="path to mount (read-only)",
                    suggester=PathSuggester(),
                )
        with Vertical(id="ports"):
            yield VerticalScroll(id="port-list")
            yield VerticalScroll(id="port-pending")
        with Vertical(id="limits"):
            yield Vertical(id="limit-list")
        yield VerticalScroll(id="pending")

    def on_mount(self) -> None:
        self._select_view("network")
        self._refresh_policy()
        self.set_interval(netwatch.POLL_INTERVAL, self._poll)
        # A slow blink of the Network tab, so a parked host pulls focus back
        # while you are over on Domains or Mounts.
        self.set_interval(0.6, self._flash_tab)

    # -- network view --

    def _refresh_policy(self) -> None:
        policy = state.get_network(self.work_dir)
        self.query_one("#policy", Static).update(
            f"{self.work_dir} · mode: {policy['mode']}"
        )

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
        self._check_ports()

    def _decide(self, row: PendingRow, action: str) -> None:
        self._handled.add(row.host)
        message = netwatch.apply_decision(self.work_dir, row.host, action)
        row.remove()
        self._pending_count -= 1
        self.query_one("#log", RichLog).write(message)
        self._refresh_policy()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Let the a/t/d/s keys decide only on the network tab, when pending."""
        if action == "decide":
            if self._view == "network" and self._pending_count > 0:
                return True
            return None
        return True

    def action_decide(self, action: str) -> None:
        """Apply a keystroke decision to the oldest pending host."""
        rows = self.query(PendingRow)
        if rows:
            self._decide(rows.first(), action)

    # -- tab switching --

    def action_select_view(self, view: str) -> None:
        self._select_view(view)

    def _select_view(self, view: str) -> None:
        self._view = view
        self.query_one("#log").display = view == "network"
        self.query_one("#pending").display = view == "network"
        self.query_one("#domains").display = view == "domains"
        self.query_one("#mounts").display = view == "mounts"
        self.query_one("#ports").display = view == "ports"
        self.query_one("#limits").display = view == "limits"
        for name in _VIEWS:
            self.query_one(f"#tab-{name}", Button).set_class(name == view, "active")
        if view == "domains":
            self._refresh_domains()
        elif view == "mounts":
            self._refresh_mounts()
        elif view == "ports":
            self._refresh_ports()
        elif view == "limits":
            self._refresh_limits()
        self._flash_tab()  # stop flashing the moment the Network tab is opened
        self.refresh_bindings()

    def _flash_tab(self) -> None:
        """Blink the Network tab while a host is parked and another tab shows."""
        if self._pending_count > 0 and self._view != "network":
            self._flash_on = not self._flash_on
        else:
            self._flash_on = False
        self.query_one("#tab-network", Button).set_class(self._flash_on, "flash")

    # -- domains view --

    @staticmethod
    def _decision_rows(
        policy: state.NetworkPolicy, *, editable: bool
    ) -> list[DecisionRow]:
        """DecisionRows for one policy's all-agents and per-agent rules.

        Allowed domains first, then denied; alphabetical within each group so
        rows are easy to find and stay put when a flip moves a domain between
        the groups (the policy lists themselves are in decision order).
        """
        rows = [
            DecisionRow(a["domain"], netwatch.ALLOW, a["expires"], editable=editable)
            for a in sorted(policy["allow"], key=lambda a: a["domain"])
        ] + [
            DecisionRow(d, netwatch.DENY, editable=editable)
            for d in sorted(policy["deny"])
        ]
        for agent_name, bucket in sorted(policy.get("agents", {}).items()):
            rows += [
                DecisionRow(
                    a["domain"],
                    netwatch.ALLOW,
                    a["expires"],
                    agent=agent_name,
                    editable=editable,
                )
                for a in sorted(bucket["allow"], key=lambda a: a["domain"])
            ] + [
                DecisionRow(d, netwatch.DENY, agent=agent_name, editable=editable)
                for d in sorted(bucket["deny"])
            ]
        return rows

    def _refresh_domains(self) -> None:
        domain_list = self.query_one("#domain-list", VerticalScroll)
        # This directory's rules (editable here) first, then the global ones
        # (aiab.net --global) — shown read-only, since editing them from a
        # single directory's view would be surprising.
        rows = self._decision_rows(
            state.get_network(self.work_dir), editable=True
        ) + self._decision_rows(state.get_global_network(), editable=False)
        self._replace_rows(domain_list, DecisionRow, rows)

    def _decide_domain(self, row: DecisionRow, action: str) -> None:
        self._write_log(
            netwatch.apply_decision(self.work_dir, row.domain, action, agent=row.agent)
        )
        self._refresh_domains()
        self._refresh_policy()

    def _add_domain(self, raw: str) -> None:
        domain = raw.strip()
        if not domain:
            return
        self._write_log(netwatch.apply_decision(self.work_dir, domain, netwatch.ALLOW))
        self._refresh_domains()
        self._refresh_policy()

    def _remove_domain(self, row: DecisionRow) -> None:
        # Allow and deny are disjoint, so the domain is in at most one list;
        # drop it from both so the host is parked and re-prompted next time.
        state.remove_network_allow(self.work_dir, row.domain, agent=row.agent)
        state.remove_network_deny(self.work_dir, row.domain, agent=row.agent)
        tag = f" [{row.agent}]" if row.agent else ""
        self._write_log(f"removed {row.domain}{tag}")
        self._refresh_domains()
        self._refresh_policy()

    # -- mounts view --

    def _refresh_mounts(self) -> None:
        mount_list = self.query_one("#mount-list", VerticalScroll)
        rows = [
            MountRow(m["source"], m["readonly"])
            for m in state.get_mounts(self.work_dir)
        ]
        self._replace_rows(mount_list, MountRow, rows)

    def _containers(self) -> list[Container]:
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
                for prefix in profiles.session_prefixes(agent)
                for container in [self._lxd.container_for_dir(self.work_dir, prefix)]
                if container.exists()
            ]
        except OSError:  # lxc not installed / not reachable
            return []

    def _write_log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _replace_rows(
        self, container: Widget, row_type: type[Widget], rows: Sequence[Widget]
    ) -> None:
        """Replace the row widgets in a list-like container."""
        for row in container.query(row_type):
            row.remove()
        for row in rows:
            container.mount(row)

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

    # -- ports view --

    def _get_init_pid(self) -> int | None:
        """Return the session container's init PID, fetched lazily and cached.

        Resets the cache when the proc entry for the stored PID disappears
        (container restarted), so a new lookup picks up the new PID.
        """
        if self._init_pid is not None:
            if Path(f"/proc/{self._init_pid}/net/tcp").exists():
                return self._init_pid
            # Container restarted; clear stale state so the new process is
            # detected fresh and pending rows for old ports are cleaned up.
            self._init_pid = None
            self._known_ports.clear()
        containers = self._containers()
        if not containers:
            return None
        try:
            pid = containers[0].init_pid()
            if pid:
                self._init_pid = pid
            return pid
        except Exception:
            return None

    def _check_ports(self) -> None:
        """Detect newly opened listening ports and prompt to forward them."""
        init_pid = self._get_init_pid()
        if init_pid is None:
            return
        current = _read_listening_ports(init_pid)
        new_ports = (
            current - self._known_ports - self._ignored_ports - self._forwarded_ports
        )
        gone_ports = self._known_ports - current
        self._known_ports = current

        pending = self.query_one("#port-pending", VerticalScroll)
        rows = {row.port: row for row in pending.query(PortPendingRow)}

        for port in gone_ports:
            if port in rows:
                rows[port].remove()
                self._write_log(f"port :{port} closed before a decision was made")
            elif port in self._forwarded_ports:
                self._write_log(f"port :{port} closed (forward still active)")

        for port in sorted(new_ports):
            pending.mount(PortPendingRow(port))
            self._write_log(f"port :{port} opened — forward to host? (Ports tab)")

        if new_ports:
            self.bell()

    def _refresh_ports(self) -> None:
        port_list = self.query_one("#port-list", VerticalScroll)
        rows = [PortForwardRow(port) for port in sorted(self._forwarded_ports)]
        self._replace_rows(port_list, PortForwardRow, rows)

    def _forward_port(self, row: PortPendingRow) -> None:
        port = row.port
        self._forwarded_ports.add(port)
        self._apply_to_containers(
            f"forward port {port}",
            lambda c, p=port: c.add_proxy_device(
                f"fwd-{p}",
                listen=f"tcp:127.0.0.1:{p}",
                connect=f"tcp:127.0.0.1:{p}",
                bind="host",
            ),
        )
        row.remove()
        self.query_one("#port-list", VerticalScroll).mount(PortForwardRow(port))
        self._write_log(f"forwarding localhost:{port} → container:{port}")

    def _remove_forward(self, row: PortForwardRow) -> None:
        port = row.port
        self._forwarded_ports.discard(port)
        self._apply_to_containers(
            f"remove forward for port {port}",
            lambda c, p=port: c.remove_device(f"fwd-{p}"),
        )
        row.remove()
        self._write_log(f"removed forwarding for port {port}")

    def _ignore_port(self, row: PortPendingRow) -> None:
        self._ignored_ports.add(row.port)
        row.remove()

    # -- limits view --

    def _refresh_limits(self) -> None:
        limit_list = self.query_one("#limit-list", Vertical)
        limits = state.get_limits(self.work_dir)
        rows = [
            LimitRow("cpu", str(limits["cpu"])),
            LimitRow("memory", limits["memory"]),
        ]
        self._replace_rows(limit_list, LimitRow, rows)

    def _apply_limit(self, field: str, value: str) -> None:
        limits = state.get_limits(self.work_dir)
        try:
            if field == "cpu":
                limits["cpu"] = state.parse_cpu(value)
            elif field == "memory":
                limits["memory"] = state.parse_memory(value)
            else:
                return
        except ValueError as e:
            self._write_log(str(e))
            return
        state.set_limits(self.work_dir, limits)
        self._apply_to_containers(
            f"set {field}={value}",
            lambda c, lim=limits: c.apply_limits(lim["cpu"], lim["memory"]),
        )
        self._write_log(f"set {field}={value}")
        self._refresh_limits()

    def on_unmount(self) -> None:
        """Remove host-side port-forwarding proxy devices when the monitor exits."""
        for port in list(self._forwarded_ports):
            self._apply_to_containers(
                f"remove forward for port {port}",
                lambda c, p=port: c.remove_device(f"fwd-{p}"),
            )

    # -- shared event handling --

    def _focus_add_input(self, button_id: str) -> bool:
        if button_id == "add-mount":
            self.query_one("#add-path", Input).focus()
            return True
        if button_id == "add-domain-btn":
            self.query_one("#add-domain", Input).focus()
            return True
        return False

    def _handle_row_button(self, button: Button) -> None:
        row = button.parent
        if isinstance(row, PendingRow):
            self._decide(row, button.name or netwatch.SKIP)
        elif isinstance(row, DecisionRow):
            if button.name == "remove":
                self._remove_domain(row)
            else:
                self._decide_domain(row, button.name or netwatch.ALLOW)
        elif isinstance(row, MountRow):
            if button.name == "remove":
                self._remove_mount(row)
            else:
                self._toggle_mode(row)
        elif isinstance(row, PortPendingRow):
            if button.name == "forward":
                self._forward_port(row)
            else:
                self._ignore_port(row)
        elif isinstance(row, PortForwardRow):
            self._remove_forward(row)
        elif isinstance(row, LimitRow):
            self._apply_limit(row.field, row.query_one(Input).value.strip())

    def _handle_input(self, event: Input.Submitted) -> None:
        input_id = event.input.id
        if input_id == "add-path":
            value = event.value.strip()
            if value:
                self._add_mount(value, readonly=True)  # default new mounts to ro
            event.input.value = ""
        elif input_id == "add-domain":
            self._add_domain(event.value)
            event.input.value = ""
        elif input_id and input_id.startswith("limit-"):
            row = event.input.parent
            if isinstance(row, LimitRow):
                self._apply_limit(row.field, event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if button.id and button.id.startswith("tab-"):
            self._select_view(button.id.removeprefix("tab-"))
            return
        if button.id and self._focus_add_input(button.id):
            return
        self._handle_row_button(button)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._handle_input(event)


def monitor(work_dir: Path, container_name: str | None = None) -> int:
    """Run the textual monitor UI for a directory; return an exit code."""
    if not sys.stdin.isatty():
        print("aiab monitor needs an interactive terminal", file=sys.stderr)
        return 1
    with netwatch.attached(netwatch.pending_dir(work_dir)):
        MonitorApp(work_dir, container_name).run()
    return 0
