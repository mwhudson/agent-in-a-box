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
#                          "allow": [ {"domain": str, "expires": float|null} ],
#                          "deny": [ str, ... ] } }
# The filtering proxy (aiab.netproxy) re-reads it on every request, so
# `aiab net allow`/`deny` take effect immediately in running sessions. The
# deny list records domains the user has explicitly refused, so the proxy can
# fail them fast instead of re-asking an attached `aiab monitor` session.
#
# A directory's base Ubuntu release (managed by `aiab base`) lives in a third
# JSON file with the same keying:
#   { "<dir real path>": "22.04" }
# Directories with no record use the default release (see aiab.release).
#
# A directory's resource limits (managed by `aiab limits`) live in a fourth
# JSON file with the same keying:
#   { "<dir real path>": {"cpu": int, "memory": str} }
# Directories with no record use DEFAULT_LIMITS. `aiab run` applies the limits
# to the session container on every start (LXD applies CPU/memory changes
# immediately on a running container).
#
# Each directory also gets a persistent state *directory* (dirstate/<slug>/),
# mounted read-write at STATE_MOUNT inside its session containers, for state
# the agent itself maintains — notably the /setup-container setup script. A
# .source file inside records the project directory it belongs to.

from __future__ import annotations

import json
import shutil
import time
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_UN, flock
from pathlib import Path
from typing import Iterator
from typing import TypedDict

from . import StrPath, release
from .lxd import dir_slug

_STATE_DIR = Path.home() / ".local" / "share" / "aiab"
_PATH = _STATE_DIR / "mounts.json"
_NET_PATH = _STATE_DIR / "network.json"
_BASE_PATH = _STATE_DIR / "base.json"
_LIMITS_PATH = _STATE_DIR / "limits.json"
_DIRSTATE_DIR = _STATE_DIR / "dirstate"

# Records the owning project directory inside each dirstate dir, so
# prune_stale() can tell when the directory is gone.
_SOURCE_FILE = ".source"


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


@contextmanager
def _locked_path(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a") as lock:
        flock(lock, LOCK_EX)
        try:
            yield
        finally:
            flock(lock, LOCK_UN)


def _save_file_unlocked(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def _save_file(path: Path, data: dict) -> None:
    with _locked_path(path):
        _save_file_unlocked(path, data)


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
    with _locked_path(_PATH):
        data = _load()
        mounts = data.get(key, [])
        for m in mounts:
            if m["source"] == source:
                m["readonly"] = readonly
                break
        else:
            mounts.append({"source": source, "readonly": readonly})
        data[key] = mounts
        _save_file_unlocked(_PATH, data)


def remove_mount(directory: StrPath, source: StrPath) -> bool:
    """Drop a recorded mount for a directory. Return True if it was present."""
    key = _key(directory)
    source = _key(source)
    with _locked_path(_PATH):
        data = _load()
        mounts = data.get(key, [])
        kept = [m for m in mounts if m["source"] != source]
        if len(kept) == len(mounts):
            return False
        if kept:
            data[key] = kept
        else:
            data.pop(key, None)
        _save_file_unlocked(_PATH, data)
    return True


# -- network policy --

MODE_OPEN = "open"
MODE_RESTRICTED = "restricted"

# The mode for directories with no recorded policy. Restricted-by-default is
# an experiment (flip this back to MODE_OPEN to undo); use `aiab net open` to
# opt a directory out.
DEFAULT_MODE = MODE_RESTRICTED


class Allow(TypedDict):
    """One allowed domain: the name and an optional expiry (unix time)."""

    domain: str
    expires: float | None


class NetworkPolicy(TypedDict):
    """A directory's network policy: mode, allowed domains, denied domains."""

    mode: str
    allow: list[Allow]
    deny: list[str]


# The whole network state file: project dir -> its policy.
NetState = dict[str, NetworkPolicy]


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower().lstrip("*.").rstrip(".")


def _unexpired(allows: list[Allow]) -> list[Allow]:
    now = time.time()
    return [a for a in allows if a["expires"] is None or a["expires"] > now]


def get_network(directory: StrPath) -> NetworkPolicy:
    """Return the network policy for a directory (default: DEFAULT_MODE, empty).

    Expired allow entries are filtered from the returned policy; they are only
    actually pruned from the file by the mutating functions below.
    """
    data: NetState = _load_file(_NET_PATH)
    policy = data.get(_key(directory))
    if policy is None:
        return {"mode": DEFAULT_MODE, "allow": [], "deny": []}
    policy.setdefault("deny", [])  # records from before the deny list existed
    policy["allow"] = _unexpired(policy["allow"])
    return policy


def _save_network_policy(key: str, policy: NetworkPolicy) -> None:
    with _locked_path(_NET_PATH):
        data: NetState = _load_file(_NET_PATH)
        policy["allow"] = _unexpired(policy["allow"])
        if (
            policy["mode"] == DEFAULT_MODE
            and not policy["allow"]
            and not policy["deny"]
        ):
            data.pop(key, None)  # back to the default; keep the file tidy
        else:
            data[key] = policy
        _save_file_unlocked(_NET_PATH, data)


def set_network_mode(directory: StrPath, mode: str) -> None:
    """Set a directory's network mode (MODE_OPEN or MODE_RESTRICTED)."""
    key = _key(directory)
    with _locked_path(_NET_PATH):
        data: NetState = _load_file(_NET_PATH)
        policy = data.get(key)
        if policy is None:
            policy = {"mode": DEFAULT_MODE, "allow": [], "deny": []}
        policy.setdefault("deny", [])
        policy["allow"] = _unexpired(policy["allow"])
        policy["mode"] = mode
        if (
            policy["mode"] == DEFAULT_MODE
            and not policy["allow"]
            and not policy["deny"]
        ):
            data.pop(key, None)  # back to the default; keep the file tidy
        else:
            data[key] = policy
        _save_file_unlocked(_NET_PATH, data)


def add_network_allow(directory: StrPath, domain: str, expires: float | None) -> None:
    """Allow a domain (and its subdomains) for a directory.

    If the domain is already allowed, its expiry is replaced — so re-allowing
    with expires=None makes a previously temporary grant permanent. Any deny
    record for the same domain is dropped, so allow/deny stay disjoint.
    """
    domain = _normalize_domain(domain)
    key = _key(directory)
    with _locked_path(_NET_PATH):
        data: NetState = _load_file(_NET_PATH)
        policy = data.get(key)
        if policy is None:
            policy = {"mode": DEFAULT_MODE, "allow": [], "deny": []}
        policy.setdefault("deny", [])
        policy["allow"] = _unexpired(policy["allow"])
        policy["deny"] = [d for d in policy["deny"] if d != domain]
        for a in policy["allow"]:
            if a["domain"] == domain:
                a["expires"] = expires
                break
        else:
            policy["allow"].append({"domain": domain, "expires": expires})
        data[key] = policy
        _save_file_unlocked(_NET_PATH, data)


def remove_network_allow(directory: StrPath, domain: str) -> bool:
    """Drop an allowed domain. Return True if it was present (and unexpired)."""
    domain = _normalize_domain(domain)
    key = _key(directory)
    with _locked_path(_NET_PATH):
        data: NetState = _load_file(_NET_PATH)
        policy = data.get(key)
        if policy is None:
            policy = {"mode": DEFAULT_MODE, "allow": [], "deny": []}
        policy.setdefault("deny", [])
        policy["allow"] = _unexpired(policy["allow"])
        kept = [a for a in policy["allow"] if a["domain"] != domain]
        if len(kept) == len(policy["allow"]):
            return False
        policy["allow"] = kept
        if (
            policy["mode"] == DEFAULT_MODE
            and not policy["allow"]
            and not policy["deny"]
        ):
            data.pop(key, None)  # back to the default; keep the file tidy
        else:
            data[key] = policy
        _save_file_unlocked(_NET_PATH, data)
    return True


def add_network_deny(directory: StrPath, domain: str) -> None:
    """Deny a domain (and its subdomains) for a directory.

    A denied domain is refused by the proxy without asking an attached
    `aiab monitor` session. Any allow record for the same domain is
    dropped, so allow/deny stay disjoint.
    """
    domain = _normalize_domain(domain)
    key = _key(directory)
    with _locked_path(_NET_PATH):
        data: NetState = _load_file(_NET_PATH)
        policy = data.get(key)
        if policy is None:
            policy = {"mode": DEFAULT_MODE, "allow": [], "deny": []}
        policy.setdefault("deny", [])
        policy["allow"] = [
            a for a in _unexpired(policy["allow"]) if a["domain"] != domain
        ]
        if domain not in policy["deny"]:
            policy["deny"].append(domain)
        data[key] = policy
        _save_file_unlocked(_NET_PATH, data)


def remove_network_deny(directory: StrPath, domain: str) -> bool:
    """Drop a denied domain. Return True if it was present."""
    domain = _normalize_domain(domain)
    key = _key(directory)
    with _locked_path(_NET_PATH):
        data: NetState = _load_file(_NET_PATH)
        policy = data.get(key)
        if policy is None:
            policy = {"mode": DEFAULT_MODE, "allow": [], "deny": []}
        policy.setdefault("deny", [])
        policy["allow"] = _unexpired(policy["allow"])
        kept = [d for d in policy["deny"] if d != domain]
        if len(kept) == len(policy["deny"]):
            return False
        policy["deny"] = kept
        if (
            policy["mode"] == DEFAULT_MODE
            and not policy["allow"]
            and not policy["deny"]
        ):
            data.pop(key, None)  # back to the default; keep the file tidy
        else:
            data[key] = policy
        _save_file_unlocked(_NET_PATH, data)
    return True


# -- base release --
#
# The Ubuntu release a directory's template/session containers are built on,
# stored as a canonical version string keyed by resolved path:
#   { "<dir real path>": "22.04" }
# Directories with no record use release.DEFAULT_BASE; a directory set back to
# the default drops its entry to keep the file tidy.


def get_base(directory: StrPath) -> str:
    """Return the canonical base release for a directory (default if unset)."""
    data: dict[str, str] = _load_file(_BASE_PATH)
    return data.get(_key(directory), release.DEFAULT_BASE)


def set_base(directory: StrPath, base: str) -> None:
    """Record a directory's base release (canonical version string).

    Setting it back to the default removes the record. The caller is expected
    to have normalised ``base`` via release.normalize().
    """
    key = _key(directory)
    with _locked_path(_BASE_PATH):
        data: dict[str, str] = _load_file(_BASE_PATH)
        if base == release.DEFAULT_BASE:
            data.pop(key, None)
        else:
            data[key] = base
        _save_file_unlocked(_BASE_PATH, data)


# -- resource limits --


class ResourceLimits(TypedDict):
    """Per-directory resource limits applied to session containers."""

    cpu: int
    memory: str


DEFAULT_LIMITS: ResourceLimits = {"cpu": 4, "memory": "8GiB"}


def get_limits(directory: StrPath) -> ResourceLimits:
    """Return the resource limits for a directory (DEFAULT_LIMITS if unset)."""
    data: dict[str, ResourceLimits] = _load_file(_LIMITS_PATH)
    recorded = data.get(_key(directory))
    if recorded is None:
        return dict(DEFAULT_LIMITS)  # type: ignore[return-value]
    return {**DEFAULT_LIMITS, **recorded}  # type: ignore[return-value]


def set_limits(directory: StrPath, limits: ResourceLimits) -> None:
    """Record resource limits for a directory.

    If limits match DEFAULT_LIMITS exactly the record is dropped to keep the
    file tidy.
    """
    key = _key(directory)
    with _locked_path(_LIMITS_PATH):
        data: dict[str, ResourceLimits] = _load_file(_LIMITS_PATH)
        if limits == DEFAULT_LIMITS:
            data.pop(key, None)
        else:
            data[key] = limits
        _save_file_unlocked(_LIMITS_PATH, data)


# -- per-directory state dir --


def dir_state_dir(directory: StrPath) -> Path:
    """Return (creating it if needed) the persistent state dir for a directory.

    This is the host side of the STATE_MOUNT mount inside the directory's
    session containers; /setup-container keeps the container setup script
    there so it survives container recreation. The .source file written here
    maps the dir back to its project directory for prune_stale().
    """
    key = _key(directory)
    d = _DIRSTATE_DIR / dir_slug(key)
    with _locked_path(_DIRSTATE_DIR):
        d.mkdir(parents=True, exist_ok=True)
        (d / _SOURCE_FILE).write_text(key + "\n")
    return d


def git_guard_dir(directory: StrPath, source: StrPath | None = None) -> Path:
    """Return (creating it if needed) a host dir holding sidecar copies of a
    git repo's .git/hooks and .git/config (see aiab.cli's git guard).

    With source=None the dir guards the directory's own repo. Pass source to
    get a distinct per-mount subdir for a read-write mounted repo, keyed by the
    mount's path (via dir_slug) so two mounted repos don't collide with each
    other or with the directory's own guard.

    It lives inside the directory's dir_state_dir, so prune_stale() reclaims it
    together with the rest of that directory's state when the project directory
    is gone. The contents are reseeded from the real repo on every run, so they
    are disposable; nothing here needs to survive on its own.
    """
    d = dir_state_dir(directory) / "git-guard"
    if source is not None:
        d = d / dir_slug(source)
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_stale() -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """Remove records for directories that no longer exist from all state.

    Covers the mount, network, base, and limits JSON files and the
    per-directory state dirs (a state dir whose recorded .source path is gone
    is deleted, setup script and all; ones without a readable .source are left
    alone). Returns (pruned_mount_dirs, pruned_network_dirs, pruned_base_dirs,
    pruned_state_dirs, pruned_limit_dirs) as lists of project-directory path
    strings.
    """
    pruned_mounts: list[str] = []
    with _locked_path(_PATH):
        mounts_data = _load()
        for key in list(mounts_data):
            if not Path(key).is_dir():
                del mounts_data[key]
                pruned_mounts.append(key)
        if pruned_mounts:
            _save_file_unlocked(_PATH, mounts_data)

    pruned_net: list[str] = []
    with _locked_path(_NET_PATH):
        net_data = _load_file(_NET_PATH)
        for key in list(net_data):
            if not Path(key).is_dir():
                del net_data[key]
                pruned_net.append(key)
        if pruned_net:
            _save_file_unlocked(_NET_PATH, net_data)

    pruned_base: list[str] = []
    with _locked_path(_BASE_PATH):
        base_data = _load_file(_BASE_PATH)
        for key in list(base_data):
            if not Path(key).is_dir():
                del base_data[key]
                pruned_base.append(key)
        if pruned_base:
            _save_file_unlocked(_BASE_PATH, base_data)

    pruned_limits: list[str] = []
    with _locked_path(_LIMITS_PATH):
        limits_data = _load_file(_LIMITS_PATH)
        for key in list(limits_data):
            if not Path(key).is_dir():
                del limits_data[key]
                pruned_limits.append(key)
        if pruned_limits:
            _save_file_unlocked(_LIMITS_PATH, limits_data)

    pruned_state: list[str] = []
    with _locked_path(_DIRSTATE_DIR):
        if _DIRSTATE_DIR.is_dir():
            for d in _DIRSTATE_DIR.iterdir():
                try:
                    source = (d / _SOURCE_FILE).read_text().strip()
                except OSError:
                    continue
                if not Path(source).is_dir():
                    shutil.rmtree(d)
                    pruned_state.append(source)

    return pruned_mounts, pruned_net, pruned_base, pruned_state, pruned_limits
