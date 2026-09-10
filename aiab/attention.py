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
# aiab.attention - telling the host when an agent is waiting on you.
#
# An agent that has finished its turn, or stopped to ask something, is waiting
# — and if the terminal isn't the window you're looking at, that wait costs
# whatever it costs before you notice. This is the other half of the desktop
# notifications aiab.notify raises for parked hosts: same Notifier, same pane,
# a different question.
#
# The agent runs in the container and the notification has to be raised on the
# host, so something has to cross the boundary. The channel is one the
# container already has: the directory's state dir, mounted read-write at
# STATE_MOUNT (see aiab.state.dir_state_dir). The agent writes a file named for
# the session under STATE_MOUNT/attention, and `aiab monitor` — which is on the
# host, and already polling — reads it. Nothing else is opened up; in
# particular the host's D-Bus session bus stays outside the container, where a
# notification daemon is the least of what it would hand an agent.
#
# Getting the agent to write that file is the part that isn't shared: the
# mechanism belongs to the agent, so there is one per agent (see MECHANISMS,
# and Agent.attention for which one an agent uses).
#
# HOOKS (Claude Code) is a *managed settings* drop-in, written into the session
# container at DROP_IN_PATH. That matters for staying out of the user's way:
# managed settings are a separate source from ~/.claude/settings.json, and hook
# lists across sources are concatenated rather than overridden, so these hooks
# neither displace the user's own nor can be displaced by them. The drop-in
# directory means aiab doesn't have to own
# /etc/claude-code/managed-settings.json either. Three hooks:
#
#   * Stop — the turn ended, so the agent is now waiting for a prompt;
#   * Notification — it stopped mid-turn to ask for something, or (idle_prompt)
#     is telling us again about the turn that already ended;
#   * UserPromptSubmit / SessionEnd — you answered, or the session is over.
#
# PLUGIN (opencode) is agent-config/opencode/plugins/attention.js, overlaid
# into opencode's global plugin directory like the rest of the versioned config
# this repo ships (see aiab.agents). It watches the same three moments —
# session.idle, permission.updated, chat.message/dispose — but because it is
# one static file serving every session of that agent, the file it writes is
# named by ENV_VAR rather than baked in.
#
# Nothing on the container side waits 15 seconds. The hooks only record *since
# when* the agent has been waiting, and the host decides when that has gone on
# long enough (DELAY) — so "and I hadn't noticed" is a host-side policy that
# can change without touching a container, and the agent's own idle threshold
# is left alone.
#
# What the file says is a wait's whole identity on the host side: its mtime is
# when the wait started, and its one line is why. A wait whose reason changes
# is a new question and aiab.monitor_tui weighs it up afresh — which is why
# recording is idempotent (see _record), so an agent repeating itself about a
# wait already recorded does not restart its countdown or forget that you have
# already dealt with it.

from __future__ import annotations

import json
import shlex
from pathlib import Path

from . import STATE_MOUNT, StrPath
from .lxd import Container
from .state import dir_state_path

# How long an agent has to have been waiting before the host says anything.
# Long enough not to fire while you are still reading the answer, short enough
# to be the reason you look up.
DELAY = 15.0

# How an agent is made to report a wait. Named here rather than in aiab.agents
# because what each one means is this module's business; the registry only says
# which one an agent uses.
HOOKS = "hooks"  # Claude Code: a managed-settings drop-in (DROP_IN_PATH).
PLUGIN = "plugin"  # opencode: a plugin, overlaid with the versioned config.
MECHANISMS = (HOOKS, PLUGIN)

# Where the PLUGIN mechanism is told to write. One plugin file serves every
# session, so the session's own file has to be named for it in the environment.
ENV_VAR = "AIAB_ATTENTION"

# Where the waiting-session files live: a directory inside the per-directory
# state dir, so it is the same place on both sides of the mount.
_SUBDIR = "attention"
_CONTAINER_DIR = f"{STATE_MOUNT}/{_SUBDIR}"

# The managed-settings drop-in carrying the hooks. Claude Code merges every
# *.json in this directory into its managed settings in sorted order, so the
# numeric prefix keeps aiab's slot predictable next to anything else that
# lands there — and aiab never has to own managed-settings.json itself.
_DROP_IN_DIR = "/etc/claude-code/managed-settings.d"
DROP_IN_PATH = f"{_DROP_IN_DIR}/50-aiab-attention.json"

# What each hook records as the reason, and what the host says because of it.
_WAITING_FOR_PROMPT = "Waiting for your next prompt"
_WAITING_FOR_ANSWER = "Waiting for a response"

# Notification types that mean the agent stopped mid-turn and needs an answer
# before it can go on. Matched as a regex against the notification's type, so
# the quieter ones (auth_success, agent_completed) are left out — and so
# worker_permission_prompt is covered by the substring already here.
_NOTIFY_TYPES = (
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
)

# Claude's own nudge that you have been sitting at an idle prompt for a while
# (a minute, at the time of writing). It is not a question: it is the turn
# that already ended saying so again, so it records the same reason Stop did,
# which _record then makes a no-op. Recorded at all only because it also
# covers the wait nobody announced — a session you attached to and never
# typed at, where no Stop of ours ever ran.
_IDLE_TYPE = "idle_prompt"


def attention_dir(directory: StrPath) -> Path:
    """The host side of the waiting-session directory for a project dir.

    Named, not created: the monitor asks for this on every poll, so it has to
    stay free of the state dir's lock-and-write (see state.dir_state_path).
    install() is what creates it.
    """
    return dir_state_path(directory) / _SUBDIR


def waiting(directory: StrPath) -> dict[str, tuple[float, str]]:
    """Sessions waiting on the user: key -> (waiting since, reason).

    The key is the session's home key ('claude', 'claude@openrouter'), so it
    names the agent the notification should be about. A file being rewritten
    as we read it is not worth locking against: the reason falls back to a
    generic one and the next poll, 0.3s later, gets it right.
    """
    result: dict[str, tuple[float, str]] = {}
    try:
        entries = list(attention_dir(directory).iterdir())
    except OSError:
        return result
    for entry in entries:
        try:
            since = entry.stat().st_mtime
            reason = entry.read_text().strip()
        except OSError:
            continue
        result[entry.name] = (since, reason or _WAITING_FOR_ANSWER)
    return result


def _file(key: str) -> str:
    """The container-side path of one session's waiting file, shell-quoted."""
    return shlex.quote(f"{_CONTAINER_DIR}/{key}")


def _record(key: str, reason: str) -> str:
    """A shell command recording that `key` is waiting, for `reason`.

    A wait already recorded for the same reason is left exactly as it is. The
    file's mtime is *since when*, and the host reads a change as a new
    question (see the module comment), but an agent will happily say the same
    thing twice about one wait — Claude's idle nudge on top of the Stop that
    already recorded it, a permission prompt raised again. Rewriting the file
    then would restart the countdown to a notification and lose the fact that
    you had already looked.
    """
    return (
        f"mkdir -p {shlex.quote(_CONTAINER_DIR)}; "
        f'[ "$(cat {_file(key)} 2>/dev/null)" = {shlex.quote(reason)} ] || '
        f"printf '%s\\n' {shlex.quote(reason)} > {_file(key)}"
    )


def _clear(key: str) -> str:
    """A shell command recording that `key` is no longer waiting."""
    return f"rm -f {_file(key)}"


def _hook(command: str, matcher: str | None = None) -> dict[str, object]:
    entry: dict[str, object] = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def drop_in(key: str) -> str:
    """The managed-settings drop-in reporting one session's waits."""
    settings = {
        "hooks": {
            "Stop": [_hook(_record(key, _WAITING_FOR_PROMPT))],
            "Notification": [
                _hook(_record(key, _WAITING_FOR_ANSWER), "|".join(_NOTIFY_TYPES)),
                _hook(_record(key, _WAITING_FOR_PROMPT), _IDLE_TYPE),
            ],
            "UserPromptSubmit": [_hook(_clear(key))],
            "SessionEnd": [_hook(_clear(key))],
        }
    }
    return json.dumps(settings, indent=2) + "\n"


def install(container: Container, directory: StrPath, key: str, mechanism: str) -> None:
    """Prepare a session container to report its waits, and clear any stale one.

    The HOOKS drop-in is written on every run rather than baked into the
    template: the template is only rebuilt on `aiab upgrade-templates`, and a
    session container outlives that, so a template-time install would reach
    existing sessions only after a container was recreated. It is one small
    file, so writing it each time costs a single exec and is always the version
    this checkout ships. PLUGIN needs no write at all — the plugin arrives as
    an overlay, mounted fresh from this checkout every run.

    The stale clear matters because the file is the container's to remove: a
    session container that was killed rather than exited leaves its last wait
    behind, and without this the next run would inherit it and notify about a
    wait that ended days ago.
    """
    attention_dir(directory).mkdir(parents=True, exist_ok=True)
    if mechanism == HOOKS:
        container.exec(
            ["sh", "-c", f"mkdir -p {_DROP_IN_DIR} && cat > {DROP_IN_PATH}"],
            input=drop_in(key).encode(),
        )
    (attention_dir(directory) / key).unlink(missing_ok=True)


def env(key: str, mechanism: str) -> dict[str, str]:
    """Environment a session needs to report its waits.

    Only PLUGIN wants anything: the plugin is one file shared by every session
    of that agent, so which file to write is the session's to say. The HOOKS
    drop-in is generated per session and has the name in it already.
    """
    if mechanism != PLUGIN:
        return {}
    return {ENV_VAR: f"{_CONTAINER_DIR}/{key}"}


def is_ask(reason: str) -> bool:
    """Whether a wait is the agent stopped for an answer, not just finished.

    The difference is worth having because looking settles one and not the
    other: a turn that has ended has nothing more to say, so seeing it is the
    end of it, while a question you looked at and walked away from is still a
    question (see monitor_tui.MonitorApp._check_attention).

    Anything not recognisably the end of a turn counts as an ask, so a reason
    this host doesn't know — an older container's wording, or the generic one
    waiting() falls back to for a file caught half-written — errs towards
    saying something rather than towards silence.
    """
    return reason != _WAITING_FOR_PROMPT


def summary(key: str) -> str:
    """The notification title for a session that has waited long enough."""
    return f"aiab: {key} is waiting"


def body(reason: str, directory: StrPath) -> str:
    """The notification body: why it is waiting, and where."""
    return f"{reason} in {Path(directory).name}."
