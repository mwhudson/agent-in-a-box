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

import json
import os

_PATH = os.path.expanduser("~/.local/share/aiab/mounts.json")


def _load():
    try:
        with open(_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(data):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, _PATH)


def get_mounts(directory):
    """Return the recorded mounts for a directory as [{source, readonly}]."""
    return _load().get(os.path.realpath(directory), [])


def set_mount(directory, source, readonly):
    """Record a mount for a directory, or update its mode if already present."""
    key = os.path.realpath(directory)
    source = os.path.realpath(source)
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


def remove_mount(directory, source):
    """Drop a recorded mount for a directory. Return True if it was present."""
    key = os.path.realpath(directory)
    source = os.path.realpath(source)
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
