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
#
# Because the bare name says "the default" rather than a release, it stops
# meaning what it did whenever DEFAULT_BASE moves. So the release a container
# was actually built on is recorded on it as user.aiab_base, and aiab.cli
# rebuilds any container whose marker disagrees with the release now being
# asked for; base_from_container_name dates the ones built before the marker.

from __future__ import annotations

import csv
import datetime
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

# The release used when a directory has recorded no base of its own.
DEFAULT_BASE = "26.04"

# The default before it moved to 26.04. Containers built back then carry no
# user.aiab_base marker, so this is what an unmarked one was built on — see
# base_from_container_name, and the rebuild checks in aiab.cli.
LEGACY_DEFAULT_BASE = "24.04"

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


@dataclass(frozen=True)
class _Row:
    """One release from distro-info-data. Dates are None when absent."""

    series: str
    version: str
    created: datetime.date | None
    released: datetime.date | None
    eol: datetime.date | None

    def exists(self, today: datetime.date) -> bool:
        """True once the release has been opened for development."""
        return self.created is not None and self.created <= today

    def in_devel(self, today: datetime.date) -> bool:
        return self.exists(today) and (self.released is None or self.released > today)

    def supported(self, today: datetime.date) -> bool:
        """True while in standard support — the csv's 'eol' column.

        Deliberately not eol-esm: ESM covers security updates for a paid
        subscription, which isn't what you want a fresh dev container on.
        """
        return self.exists(today) and (self.eol is None or self.eol >= today)


def _distro_info_rows() -> list[_Row]:
    """The releases distro-info-data lists, in file order.

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
                _Row(
                    series=series,
                    version=version,
                    created=_date(row.get("created") or ""),
                    released=_date(row.get("release") or ""),
                    eol=_date(row.get("eol") or ""),
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
    unreleased = [row.version for row in _distro_info_rows() if row.in_devel(today)]
    return unreleased[-1] if unreleased else None


def supported(today: datetime.date | None = None) -> list[tuple[str, str]]:
    """(version, codename) for the releases still in standard support.

    For `aiab base`'s listing, so it names what you'd actually want to build
    on rather than every Ubuntu ever. Falls back to the whole built-in table
    when distro-info-data isn't there to date-filter with — that table is
    short and hand-maintained, so it's already roughly this list.
    """
    if today is None:
        today = datetime.date.today()
    rows = [row for row in _distro_info_rows() if row.supported(today)]
    if rows:
        return [(row.version, row.series) for row in rows]
    return sorted((v, c) for c, v in CODENAMES.items())


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
    for row in _distro_info_rows():
        if row.series == v:
            return row.version
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


def base_from_container_name(name: str, agent: str) -> str:
    """The release a template was built on, inferred from its name alone.

    For templates that predate the user.aiab_base marker, where the name is
    the only record left. An '<agent>-base-<token>' name spells its release
    out in the token; the bare agent name means whatever the default was when
    it was built, which can only be LEGACY_DEFAULT_BASE — anything built under
    the current default is marked. A name that is neither gets the same
    answer, since it is no younger than the marker either.
    """
    m = re.fullmatch(rf"{re.escape(agent)}-base-(\d{{2}})(\d{{2}})", name)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return LEGACY_DEFAULT_BASE


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
