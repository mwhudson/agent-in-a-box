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
# STATE_MOUNT (see aiab.state.dir_state_dir). Claude Code hooks write a file
# named for the session under STATE_MOUNT/attention, and `aiab monitor` — which
# is on the host, and already polling — reads it. Nothing else is opened up;
# in particular the host's D-Bus session bus stays outside the container, where
# a notification daemon is the least of what it would hand an agent.
#
# The hooks are delivered as a Claude Code *managed settings* drop-in, written
# into the session container at STATE_DROP_IN. That matters for staying out of
# the user's way: managed settings are a separate source from
# ~/.claude/settings.json, and hook lists across sources are concatenated
# rather than overridden, so these hooks neither displace the user's own nor
# can be displaced by them. The drop-in directory means aiab doesn't have to
# own /etc/claude-code/managed-settings.json either.
#
# Three hooks, and the timing is deliberately *not* theirs:
#
#   * Stop — the turn ended, so the agent is now waiting for a prompt;
#   * Notification — it stopped mid-turn to ask for something;
#   * UserPromptSubmit / SessionEnd — you answered, or the session is over.
#
# Nothing here waits 15 seconds. The hooks only record *since when* the agent
# has been waiting, and the host decides when that has gone on long enough
# (DELAY) — so "and I hadn't noticed" is a host-side policy that can change
# without touching a container, and Claude Code's own idle threshold is left
# alone.

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

# Notification types worth interrupting for: the agent has stopped and needs
# something from you. Matched as a regex against the notification's type, so
# the quieter ones (auth_success, agent_completed) are left out.
_NOTIFY_TYPES = (
    "permission_prompt",
    "idle_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
)


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
    """A shell command recording that `key` is waiting, for `reason`."""
    return (
        f"mkdir -p {shlex.quote(_CONTAINER_DIR)} && "
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
                _hook(_record(key, _WAITING_FOR_ANSWER), "|".join(_NOTIFY_TYPES))
            ],
            "UserPromptSubmit": [_hook(_clear(key))],
            "SessionEnd": [_hook(_clear(key))],
        }
    }
    return json.dumps(settings, indent=2) + "\n"


def install(container: Container, directory: StrPath, key: str) -> None:
    """Install the hooks in a session container and clear any stale wait.

    Written on every run rather than baked into the template: the template is
    only rebuilt on `aiab upgrade-templates`, and a session container outlives
    that, so a template-time install would reach existing sessions only after
    a container was recreated. It is one small file, so writing it each time
    costs a single exec and is always the version this checkout ships.

    The stale clear matters because the file is the container's to remove: a
    session container that was killed rather than exited leaves its last wait
    behind, and without this the next run would inherit it and notify about a
    wait that ended days ago.
    """
    attention_dir(directory).mkdir(parents=True, exist_ok=True)
    container.exec(
        ["sh", "-c", f"mkdir -p {_DROP_IN_DIR} && cat > {DROP_IN_PATH}"],
        input=drop_in(key).encode(),
    )
    (attention_dir(directory) / key).unlink(missing_ok=True)


def summary(key: str) -> str:
    """The notification title for a session that has waited long enough."""
    return f"aiab: {key} is waiting"


def body(reason: str, directory: StrPath) -> str:
    """The notification body: why it is waiting, and where."""
    return f"{reason} in {Path(directory).name}."
