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
# Codenames ("noble") and "devel" are accepted on input and normalised to the
# version, using the host's distro-info-data where it's available.
#
# One agent can have a template per release. The default release's template
# keeps the bare agent name (e.g. "claude"), so existing templates aren't
# orphaned and the common case stays tidy; an alternate release gets an
# "<agent>-base-<token>" template (e.g. "claude-base-2204"), where the token
# is the version without its dot — container names can't contain dots, and a
# four-digit token can't be mistaken for the eight-hex-char directory hash that
# ends a session container's name (see aiab.lxd.container_name_for_dir).

from __future__ import annotations

import csv
import datetime
import re
from collections.abc import Collection
from pathlib import Path

# The release used when a directory has recorded no base of its own.
DEFAULT_BASE = "24.04"

# The release table every Ubuntu host already has: distro-info-data is
# Priority: important and a dependency of python3-apt, and Debian ships the
# same file. Reading the csv directly keeps the codename list current (and
# lets 'devel' resolve) without depending on python3-distro-info.
DISTRO_INFO_CSV = Path("/usr/share/distro-info/ubuntu.csv")

# Codename -> version, used when distro-info-data is absent or too old to know
# the release being asked for. Any NN.NN version is accepted whether or not
# either source lists it, so this only needs to cover the common cases.
CODENAMES: dict[str, str] = {
    "focal": "20.04",
    "jammy": "22.04",
    "noble": "24.04",
    "oracular": "24.10",
    "plucky": "25.04",
    "questing": "25.10",
    "resolute": "26.04",
    "stonking": "26.10",
}

# A version is YY.MM — always two digits, a dot, two digits.
_VERSION_RE = re.compile(r"\d{2}\.\d{2}")


def _date(value: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value.strip())
    except ValueError:
        return None


def _distro_info_rows() -> (
    list[tuple[str, str, datetime.date | None, datetime.date | None]]
):
    """(series, version, created, released) from distro-info-data, in file order.

    Empty when the data file is missing or unreadable — every caller falls
    back to CODENAMES, so a host without distro-info-data still works. Rows
    whose version isn't a plain YY.MM (the pre-6.06 releases) are dropped:
    there are no container images for those anyway.
    """
    try:
        text = DISTRO_INFO_CSV.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for row in csv.DictReader(text.splitlines()):
        series = (row.get("series") or "").strip()
        # The csv marks LTS releases in the version: "26.04 LTS" -> "26.04".
        version = (row.get("version") or "").strip().split(" ")[0]
        if series and _VERSION_RE.fullmatch(version):
            rows.append(
                (
                    series,
                    version,
                    _date(row.get("created") or ""),
                    _date(row.get("release") or ""),
                )
            )
    return rows


def devel_version(today: datetime.date | None = None) -> str | None:
    """The version of the in-development release, or None if not known.

    "In development" is the last release distro-info-data lists that has been
    opened but hasn't reached its release date — the rule python3-distro-info
    applies, without the dependency.
    """
    if today is None:
        today = datetime.date.today()
    unreleased = [
        version
        for _series, version, created, released in _distro_info_rows()
        if created is not None
        and created <= today
        and (released is None or released > today)
    ]
    return unreleased[-1] if unreleased else None


def normalize(value: str) -> str:
    """Return the canonical version ("24.04") for a release name or version.

    Accepts a codename ("noble"), a version ("24.04"), or "devel" for the
    release currently in development; raises ValueError with a helpful
    message for anything else.

    "devel" is resolved to a version here, at the point of input, so what
    gets stored (and named after, and compared against) is a fixed release
    rather than an alias that would silently mean the next one in six months.
    """
    v = value.strip().lower()
    if v == "devel":
        devel = devel_version()
        if devel is None:
            raise ValueError(
                "cannot resolve 'devel': no in-development release found in "
                f"{DISTRO_INFO_CSV} (is distro-info-data installed and current?)"
            )
        return devel
    for series, version, _created, _released in _distro_info_rows():
        if series == v:
            return version
    if v in CODENAMES:
        return CODENAMES[v]
    if _VERSION_RE.fullmatch(v):
        return v
    raise ValueError(
        f"unknown release {value!r}; use a version like 24.04, a codename "
        f"like noble, or devel"
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
