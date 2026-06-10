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
# aiab.state - persistent per-directory records (mounts, network policy).
#
# The extra directories a user mounts into a project directory's containers
# (via `aiab mount` or `aiab run --add-mount`) are recorded here, keyed by the
# project directory's real path, so they:
#   (a) reach every agent started for that directory, not just the ones that
#       happened to exist when the mount was added, and
#   (b) survive deleting and recreating a directory's container.
# `aiab run` replays the recorded mounts each time it brings a container up.
#
# Mount state is a single JSON file:
#   { "<dir real path>": [ {"source": "<host path>", "readonly": bool}, ... ] }
#
# A directory's network policy (managed by `aiab net`) lives in a second JSON
# file with the same keying:
#   { "<dir real path>": { "mode": "open" | "restricted",
#                          "allow": [ {"domain": str, "expires": float|null} ] } }
# The filtering proxy (aiab.netproxy) re-reads it on every request, so
# `aiab net allow`/`deny` take effect immediately in running sessions.

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TypedDict

from . import StrPath

_STATE_DIR = Path.home() / ".local" / "share" / "aiab"
_PATH = _STATE_DIR / "mounts.json"
_NET_PATH = _STATE_DIR / "network.json"


class Mount(TypedDict):
    """One recorded mount: a host source path and whether it's read-only."""

    source: str
    readonly: bool


# The whole state file: project dir (resolved path string) -> its mounts.
State = dict[str, list[Mount]]


def _load_file(path: Path) -> dict:
    try:
        with path.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def _load() -> State:
    return _load_file(_PATH)


def _save(data: State) -> None:
    _save_file(_PATH, data)


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


# -- network policy --

MODE_OPEN = "open"
MODE_RESTRICTED = "restricted"


class Allow(TypedDict):
    """One allowed domain: the name and an optional expiry (unix time)."""

    domain: str
    expires: float | None


class NetworkPolicy(TypedDict):
    """A directory's network policy: its mode and extra allowed domains."""

    mode: str
    allow: list[Allow]


# The whole network state file: project dir -> its policy.
NetState = dict[str, NetworkPolicy]


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower().lstrip("*.").rstrip(".")


def _unexpired(allows: list[Allow]) -> list[Allow]:
    now = time.time()
    return [a for a in allows if a["expires"] is None or a["expires"] > now]


def get_network(directory: StrPath) -> NetworkPolicy:
    """Return the network policy for a directory (default: open, no allows).

    Expired allow entries are filtered from the returned policy; they are only
    actually pruned from the file by the mutating functions below.
    """
    data: NetState = _load_file(_NET_PATH)
    policy = data.get(_key(directory))
    if policy is None:
        return {"mode": MODE_OPEN, "allow": []}
    policy["allow"] = _unexpired(policy["allow"])
    return policy


def _save_network_policy(key: str, policy: NetworkPolicy) -> None:
    data: NetState = _load_file(_NET_PATH)
    policy["allow"] = _unexpired(policy["allow"])
    if policy["mode"] == MODE_OPEN and not policy["allow"]:
        data.pop(key, None)  # back to the default; keep the file tidy
    else:
        data[key] = policy
    _save_file(_NET_PATH, data)


def set_network_mode(directory: StrPath, mode: str) -> None:
    """Set a directory's network mode (MODE_OPEN or MODE_RESTRICTED)."""
    policy = get_network(directory)
    policy["mode"] = mode
    _save_network_policy(_key(directory), policy)


def add_network_allow(directory: StrPath, domain: str, expires: float | None) -> None:
    """Allow a domain (and its subdomains) for a directory.

    If the domain is already allowed, its expiry is replaced — so re-allowing
    with expires=None makes a previously temporary grant permanent.
    """
    domain = _normalize_domain(domain)
    policy = get_network(directory)
    for a in policy["allow"]:
        if a["domain"] == domain:
            a["expires"] = expires
            break
    else:
        policy["allow"].append({"domain": domain, "expires": expires})
    _save_network_policy(_key(directory), policy)


def remove_network_allow(directory: StrPath, domain: str) -> bool:
    """Drop an allowed domain. Return True if it was present (and unexpired)."""
    domain = _normalize_domain(domain)
    policy = get_network(directory)
    kept = [a for a in policy["allow"] if a["domain"] != domain]
    if len(kept) == len(policy["allow"]):
        return False
    policy["allow"] = kept
    _save_network_policy(_key(directory), policy)
    return True
