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
# aiab.release - the Ubuntu release a template container is built on.
#
# A "base" is an Ubuntu release written as its version ("24.04") — the
# canonical form aiab stores (see aiab.state) and names containers after.
# Codenames ("noble") are accepted on input and normalised to the version.
#
# One agent can have a template per release. The default release's template
# keeps the bare agent name (e.g. "claude"), so existing templates aren't
# orphaned and the common case stays tidy; an alternate release gets an
# "<agent>-base-<token>" template (e.g. "claude-base-2204"), where the token
# is the version without its dot — container names can't contain dots, and a
# four-digit token can't be mistaken for the eight-hex-char directory hash that
# ends a session container's name (see aiab.lxd.container_name_for_dir).

from __future__ import annotations

import re
from collections.abc import Collection

# The release used when a directory has recorded no base of its own.
DEFAULT_BASE = "24.04"

# Codename -> version for the releases we know about. Extend freely; any NN.NN
# version is accepted even when it's not listed here, so this table only needs
# to carry the codenames we want to resolve.
CODENAMES: dict[str, str] = {
    "focal": "20.04",
    "jammy": "22.04",
    "noble": "24.04",
    "oracular": "24.10",
    "plucky": "25.04",
    "questing": "25.10",
    "resolute": "26.04",
}

# A version is YY.MM — always two digits, a dot, two digits.
_VERSION_RE = re.compile(r"\d{2}\.\d{2}")


def normalize(value: str) -> str:
    """Return the canonical version ("24.04") for a release name or version.

    Accepts a codename ("noble") or a version ("24.04"); raises ValueError
    with a helpful message for anything else.
    """
    v = value.strip().lower()
    if v in CODENAMES:
        return CODENAMES[v]
    if _VERSION_RE.fullmatch(v):
        return v
    known = ", ".join(sorted(CODENAMES))
    raise ValueError(
        f"unknown release {value!r}; use a version like 24.04 "
        f"or a codename ({known})"
    )


def image_for(base: str) -> str:
    """The LXD image alias for a canonical base, e.g. 'ubuntu-daily:24.04'.

    The daily remote rather than the release remote: its images are rebuilt
    continuously, so a fresh template has far less to pull in when it updates
    itself, and the in-development release is available there before it's
    published to the 'ubuntu:' remote.
    """
    return f"ubuntu-daily:{base}"


def _token(base: str) -> str:
    # Container names can't contain dots; "24.04" -> "2404".
    return base.replace(".", "")


def base_container_name(agent: str, base: str) -> str:
    """Template container name for an agent on a given canonical base.

    The default base keeps the bare agent name; others get a distinct
    '<agent>-base-<token>' name that can't collide with a session container
    (those are '<agent>-<basename>-<hash>', see aiab.lxd.container_name_for_dir).
    """
    if base == DEFAULT_BASE:
        return agent
    return f"{agent}-base-{_token(base)}"


def is_base_container_name(name: str, agent_names: Collection[str]) -> bool:
    """True if name is a template container (default or alternate-base).

    The alternate-base pattern requires a four-digit token, which the
    eight-char hex hash ending a session container's name can't match.
    """
    if name in agent_names:
        return True
    return any(
        re.fullmatch(rf"{re.escape(agent)}-base-\d{{4}}", name) for agent in agent_names
    )


def base_names_for_agent(agent: str, instance_names: Collection[str]) -> list[str]:
    """Existing template container names for an agent, default first.

    Used by `aiab upgrade-templates` to find every template an agent has,
    whatever release each was built on.
    """
    names = [agent] if agent in instance_names else []
    pat = re.compile(rf"{re.escape(agent)}-base-\d{{4}}")
    names += sorted(n for n in instance_names if pat.fullmatch(n))
    return names
