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
# aiab.state - persistent per-directory mount records.
#
# The extra directories a user mounts into a project directory's containers
# (via `aiab mount` or `aiab run --also`) are recorded here, keyed by the
# project directory's real path, so they:
#   (a) reach every agent started for that directory, not just the ones that
#       happened to exist when the mount was added, and
#   (b) survive deleting and recreating a directory's container.
# `aiab run` replays the recorded mounts each time it brings a container up.
#
# State is a single JSON file:
#   { "<dir real path>": [ {"source": "<host path>", "readonly": bool}, ... ] }

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from . import StrPath

_PATH = Path.home() / ".local" / "share" / "aiab" / "mounts.json"


class Mount(TypedDict):
    """One recorded mount: a host source path and whether it's read-only."""

    source: str
    readonly: bool


# The whole state file: project dir (resolved path string) -> its mounts.
State = dict[str, list[Mount]]


def _load() -> State:
    try:
        with _PATH.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(data: State) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_name(_PATH.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(_PATH)


# Keys and sources are stored (and compared) as resolved path *strings*, so the
# JSON stays human-readable and round-trips through json.load as plain str.
def _key(path: StrPath) -> str:
    return str(Path(path).resolve())


def get_mounts(directory: StrPath) -> list[Mount]:
    """Return the recorded mounts for a directory as [{source, readonly}]."""
    return _load().get(_key(directory), [])


def set_mount(directory: StrPath, source: StrPath, readonly: bool) -> None:
    """Record a mount for a directory, or update its mode if already present."""
    key = _key(directory)
    source = _key(source)
    data = _load()
    mounts = data.get(key, [])
    for m in mounts:
        if m["source"] == source:
            m["readonly"] = readonly
            break
    else:
        mounts.append({"source": source, "readonly": readonly})
    data[key] = mounts
    _save(data)


def remove_mount(directory: StrPath, source: StrPath) -> bool:
    """Drop a recorded mount for a directory. Return True if it was present."""
    key = _key(directory)
    source = _key(source)
    data = _load()
    mounts = data.get(key, [])
    kept = [m for m in mounts if m["source"] != source]
    if len(kept) == len(mounts):
        return False
    if kept:
        data[key] = kept
    else:
        data.pop(key, None)
    _save(data)
    return True
