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
# aiab.profiles - named bundles of settings layered onto a directory's own.
#
# A profile is a reusable set of settings that isn't tied to one project
# directory: environment variables to inject, extra domains to allow in
# restricted mode, and whether the session gets its own credential store. It is
# selected per run (`aiab run --profile NAME <agent>`), never recorded against
# a directory, so the same directory can be used with and without one.
#
# Profiles come from two places:
#   - built-ins defined here (BUILTIN), which may carry a `prepare` hook
#     because they are code; and
#   - user profiles recorded by `aiab profile add` (see aiab.state), which are
#     plain data.
# Built-in names are reserved: `aiab profile add` refuses them, so a lookup
# never has to choose between two definitions of the same name.
#
# Scoping: a profile may name the agents it applies to. `openrouter` only makes
# sense for `claude` (it points the Anthropic-shaped client at a different
# endpoint), while a profile that only tightens the network applies to any
# agent. An empty/absent `agents` list means "any agent"; running a profile
# against an agent it doesn't list is an error rather than a silent no-op.
#
# Isolation: `isolated` decides whether a profile forks the agent's *identity*
# as well as its settings. With it set, the session gets its own credential
# store (~/.local/share/aiab/<agent>@<profile>/home) and its own session
# container, so an OpenRouter token never lands in the same ~/.claude as an
# Anthropic login. Without it, the profile layers settings onto the agent's
# normal home and container — what a network-tightening profile wants, since
# re-authenticating to enter a stricter mode would defeat the point. The
# *template* container stays keyed by agent either way: a profile never changes
# how the agent is installed, so there is nothing to build twice.

from __future__ import annotations

import getpass
import json
import re
import sys
from pathlib import Path
from typing import Callable, NotRequired, TypedDict

from . import StrPath, state

# Model the openrouter profile selects by default.
DEFAULT_OR_MODEL: str = "anthropic/claude-sonnet-4-6"


class Profile(TypedDict):
    """One profile's settings. All fields optional; absent means "no opinion"."""

    # Shown by `aiab profile list`.
    description: NotRequired[str]
    # Agents this profile applies to; absent or empty means any agent.
    agents: NotRequired[list[str]]
    # Fork the credential store and session container (see module docstring).
    isolated: NotRequired[bool]
    # Environment variables injected into the agent process.
    env: NotRequired[dict[str, str]]
    # Extra domains allowed while the directory is in restricted mode.
    allow: NotRequired[list[str]]


# Profile names share a namespace with agent names inside container names, and
# must survive being embedded in one, so hold them to the same shape as an
# agent name: lowercase alphanumerics and hyphens.
_NAME_RE: re.Pattern[str] = re.compile(r"[a-z0-9][a-z0-9-]*")


def _ensure_openrouter_key(home_dir: StrPath) -> None:
    """Prompt for an OpenRouter API key on first use and record it.

    Only the key is written here — it's a secret, so it belongs in the
    profile's credential store rather than in profiles.json. The endpoint and
    model come from the profile's `env` and need no prompting.
    """
    settings_path = Path(home_dir) / ".claude" / "settings.json"
    if settings_path.exists():
        return

    print("OpenRouter key not found — setting up now.", file=sys.stderr)
    print(file=sys.stderr)

    api_key = getpass.getpass("OpenRouter API key (sk-or-...): ").strip()
    if not api_key:
        sys.exit("Error: API key is required")

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with settings_path.open("w") as f:
        json.dump({"env": {"ANTHROPIC_AUTH_TOKEN": api_key}}, f, indent=2)
        f.write("\n")
    print(f"Wrote OpenRouter key to {settings_path}", file=sys.stderr)
    print(file=sys.stderr)


# Built-in profiles. Keys are profile names.
BUILTIN: dict[str, Profile] = {
    "openrouter": Profile(
        description="Claude Code pointed at OpenRouter instead of the Claude API",
        agents=["claude"],
        # The OpenRouter token must not mix with a claude.ai login.
        isolated=True,
        env={
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            # Not ANTHROPIC_MODEL: env outranks the `model` settings field, but
            # `/model` persists a switch *into* that field, so setting
            # ANTHROPIC_MODEL makes an in-session switch revert on next launch.
            # The custom-model-option variable leaves the field free and adds
            # the entry to the /model picker, which otherwise lists only
            # Anthropic model names this endpoint won't accept.
            "ANTHROPIC_CUSTOM_MODEL_OPTION": DEFAULT_OR_MODEL,
            "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME": "OpenRouter",
        },
        allow=["openrouter.ai"],
    ),
}

# One-time setup run against a built-in profile's credential store before the
# session starts. Keyed separately from BUILTIN so the profile itself stays
# plain data, comparable with a user profile.
PREPARE: dict[str, Callable[[StrPath], None]] = {
    "openrouter": _ensure_openrouter_key,
}


def valid_name(name: str) -> bool:
    """True if name is usable as a profile name (and in a container name)."""
    return bool(_NAME_RE.fullmatch(name))


def get(name: str) -> Profile | None:
    """Return a profile by name, built-in or user-defined; None if unknown.

    Built-in names are reserved (`aiab profile add` refuses them), so the two
    sources can't both define a name and the order of these lookups is not a
    precedence decision.
    """
    builtin = BUILTIN.get(name)
    if builtin is not None:
        return builtin
    recorded = state.get_profile(name)
    if recorded is None:
        return None
    return Profile(**recorded)


def names() -> list[str]:
    """Return every known profile name, built-in and user-defined, sorted."""
    return sorted({*BUILTIN, *state.list_profiles()})


def session_prefixes(agent: str) -> list[str]:
    """Every container prefix a directory could have sessions under, for agent.

    That's the bare agent name plus one per *isolated* profile that applies to
    it — a non-isolated profile shares the agent's container, so it adds no
    name. Callers that enumerate a directory's containers (`aiab list --for`,
    the monitor, the netwatch log tails) need this rather than the agent name
    alone, or they silently miss profile sessions.
    """
    found = [agent]
    for name in names():
        profile = get(name)
        if profile and profile.get("isolated") and applies_to(profile, agent):
            found.append(container_prefix(agent, name, True))
    return found


def applies_to(profile: Profile, agent: str) -> bool:
    """True if profile is usable with agent (no `agents` list means any)."""
    allowed = profile.get("agents")
    return not allowed or agent in allowed


def home_key(agent: str, profile: str | None, isolated: bool) -> str:
    """Key naming the credential store for a run: the agent, or agent@profile.

    '@' keeps the isolated store visibly distinct from a plain agent's, and is
    fine in a directory name; container names use container_prefix() instead,
    which is held to hostname characters.
    """
    return f"{agent}@{profile}" if profile and isolated else agent


def container_prefix(agent: str, profile: str | None, isolated: bool) -> str:
    """Session-container prefix for a run: the agent, or agent-profile.

    An isolated profile must not share a session container with the plain
    agent — different credentials and environment — so it gets its own name.
    Hyphen-joined because LXD instance names are hostnames: letters, digits and
    hyphens only, so the '@' used by home_key() isn't available here.
    """
    return f"{agent}-{profile}" if profile and isolated else agent
