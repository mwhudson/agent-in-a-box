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
#                          "deny": [ str, ... ],
#                          "agents": { "<agent>": {"allow": [...],
#                                                  "deny": [...]} } } }
# The "allow"/"deny" lists apply to every agent; the optional "agents" overlay
# adds rules for one agent only (orthogonal to directory — see below).
# The filtering proxy (aiab.netproxy) re-reads it on every request, so
# `aiab net allow`/`deny` take effect immediately in running sessions. The
# deny list records domains the user has explicitly refused, so the proxy can
# fail them fast instead of re-asking an attached `aiab monitor` session.
#
# A directory's allow/deny lists can be supplemented by a *global* allow/deny
# list that applies to every directory. It lives in the same file under the
# reserved key "*" (which can never collide with a resolved, absolute path),
# carries only allow/deny (its mode is unused), and is managed with
# `aiab net allow/deny --global`. A directory's own rules take precedence over
# the global ones (see aiab.netproxy.evaluate).
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
# A directory's injected environment variables (managed by `aiab env`) live in
# a fifth JSON file, keyed by directory and then by scope — the "*" bucket
# applies to every agent run there, an agent-named bucket only to that agent:
#   { "<dir real path>": { "*": {"FOO": "bar"},
#                          "<agent>": {"OPENCODE_CONFIG": "..."} } }
# `aiab run` merges the "*" bucket then the running agent's bucket (agent wins)
# into the agent process environment (see aiab.cli._session_env).
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
from typing import NotRequired
from typing import TypedDict

from . import StrPath, release
from .lxd import dir_slug

_STATE_DIR = Path.home() / ".local" / "share" / "aiab"
_PATH = _STATE_DIR / "mounts.json"
_NET_PATH = _STATE_DIR / "network.json"
_BASE_PATH = _STATE_DIR / "base.json"
_LIMITS_PATH = _STATE_DIR / "limits.json"
_ENV_PATH = _STATE_DIR / "env.json"
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


class AgentRules(TypedDict):
    """One agent's scoped allow/deny lists within a policy."""

    allow: list[Allow]
    deny: list[str]


class NetworkPolicy(TypedDict):
    """A directory's network policy.

    ``mode`` plus the all-agents ``allow``/``deny`` lists, and an optional
    ``agents`` overlay keyed by agent name. The agent the proxy is serving
    gets its overlay's rules on top of the all-agents ones (see
    network_for_agent); other agents never see them. The agent axis is
    orthogonal to the directory axis — each has an "all" default: the
    all-agents lists here, and the global policy (GLOBAL_KEY) across dirs.
    """

    mode: str
    allow: list[Allow]
    deny: list[str]
    agents: NotRequired[dict[str, AgentRules]]


# The whole network state file: project dir -> its policy. The reserved key
# below holds the global allow/deny list shared by every directory.
NetState = dict[str, NetworkPolicy]

# Key for the global policy in the network file. A real key is always a
# resolved, absolute path, so "*" can never collide with one.
GLOBAL_KEY = "*"


def _policy_key(directory: StrPath | None, global_: bool) -> str:
    """Resolve the network-file key: the global one, or a directory's path."""
    if global_:
        return GLOBAL_KEY
    assert directory is not None, "directory is required unless global_ is set"
    return _key(directory)


def _normalize_domain(domain: str) -> str:
    return domain.strip().lower().lstrip("*.").rstrip(".")


def _unexpired(allows: list[Allow]) -> list[Allow]:
    now = time.time()
    return [a for a in allows if a["expires"] is None or a["expires"] > now]


def _blank_policy() -> NetworkPolicy:
    return {"mode": DEFAULT_MODE, "allow": [], "deny": [], "agents": {}}


def _normalized(raw: NetworkPolicy | None) -> NetworkPolicy:
    """Return a policy with every field present and expired allows dropped.

    Tolerates records written by older versions (no ``deny``, no ``agents``).
    """
    if raw is None:
        return _blank_policy()
    raw.setdefault("deny", [])
    raw.setdefault("agents", {})
    raw["allow"] = _unexpired(raw["allow"])
    for bucket in raw["agents"].values():
        bucket.setdefault("deny", [])
        bucket["allow"] = _unexpired(bucket.get("allow", []))
    return raw


def _scope(policy: NetworkPolicy, agent: str | None) -> AgentRules | NetworkPolicy:
    """Return the mapping whose ``allow``/``deny`` an agent scope writes to.

    agent=None targets the all-agents lists (the policy itself); a name targets
    that agent's overlay bucket, created empty if missing.
    """
    if agent is None:
        return policy
    return policy.setdefault("agents", {}).setdefault(agent, {"allow": [], "deny": []})


def _is_default(policy: NetworkPolicy) -> bool:
    return policy["mode"] == DEFAULT_MODE and not (
        policy["allow"] or policy["deny"] or policy.get("agents")
    )


def _compact(policy: NetworkPolicy) -> None:
    """Drop empty agent overlays (and the ``agents`` key when none remain) so
    the file stays tidy and round-trips to the old shape when unused."""
    overlays = policy.get("agents", {})
    for name in list(overlays):
        if not overlays[name]["allow"] and not overlays[name]["deny"]:
            del overlays[name]
    if not overlays:
        policy.pop("agents", None)


@contextmanager
def _open_policy(key: str) -> Iterator[NetworkPolicy]:
    """Lock the network file, yield the normalized policy for ``key``, then
    save it back compacted — or drop the record if it's back to the default."""
    with _locked_path(_NET_PATH):
        data: NetState = _load_file(_NET_PATH)
        policy = _normalized(data.get(key))
        yield policy
        _compact(policy)
        if _is_default(policy):
            data.pop(key, None)  # back to the default; keep the file tidy
        else:
            data[key] = policy
        _save_file_unlocked(_NET_PATH, data)


def get_network(directory: StrPath) -> NetworkPolicy:
    """Return the network policy for a directory (default: DEFAULT_MODE, empty).

    Expired allow entries are filtered from the returned policy; they are only
    actually pruned from the file by the mutating functions below.
    """
    return _normalized(_load_file(_NET_PATH).get(_key(directory)))


def get_global_network() -> NetworkPolicy:
    """Return the global policy shared by every directory.

    Only the allow/deny lists (all-agents and per-agent) are meaningful; the
    mode field is unused (global policy never restricts a directory by itself).
    """
    return _normalized(_load_file(_NET_PATH).get(GLOBAL_KEY))


def _flatten(policy: NetworkPolicy, agent: str) -> NetworkPolicy:
    """Collapse a policy for one agent: the all-agents rules plus that agent's
    overlay, carrying the mode through. Used to feed the proxy."""
    bucket = policy.get("agents", {}).get(agent, {"allow": [], "deny": []})
    return {
        "mode": policy["mode"],
        "allow": policy["allow"] + bucket["allow"],
        "deny": policy["deny"] + bucket["deny"],
    }


def network_for_agent(directory: StrPath, agent: str) -> NetworkPolicy:
    """A directory's policy flattened for one agent (see _flatten)."""
    return _flatten(get_network(directory), agent)


def global_for_agent(agent: str) -> NetworkPolicy:
    """The global policy flattened for one agent (see _flatten)."""
    return _flatten(get_global_network(), agent)


def set_network_mode(directory: StrPath, mode: str) -> None:
    """Set a directory's network mode (MODE_OPEN or MODE_RESTRICTED)."""
    with _open_policy(_key(directory)) as policy:
        policy["mode"] = mode


def add_network_allow(
    directory: StrPath | None,
    domain: str,
    expires: float | None,
    *,
    global_: bool = False,
    agent: str | None = None,
) -> None:
    """Allow a domain (and its subdomains) in one scope.

    If the domain is already allowed in the same scope its expiry is replaced
    — so re-allowing with expires=None makes a temporary grant permanent. Any
    deny record for the same domain in that scope is dropped, so allow/deny
    stay disjoint. global_=True targets the shared global list (``directory``
    ignored); ``agent`` restricts the rule to one agent (default: all agents).
    """
    domain = _normalize_domain(domain)
    with _open_policy(_policy_key(directory, global_)) as policy:
        scope = _scope(policy, agent)
        scope["deny"] = [d for d in scope["deny"] if d != domain]
        for a in scope["allow"]:
            if a["domain"] == domain:
                a["expires"] = expires
                break
        else:
            scope["allow"].append({"domain": domain, "expires": expires})


def remove_network_allow(
    directory: StrPath | None,
    domain: str,
    *,
    global_: bool = False,
    agent: str | None = None,
) -> bool:
    """Drop an allowed domain from a scope. Return True if it was present."""
    domain = _normalize_domain(domain)
    with _open_policy(_policy_key(directory, global_)) as policy:
        scope = _scope(policy, agent)
        kept = [a for a in scope["allow"] if a["domain"] != domain]
        present = len(kept) != len(scope["allow"])
        scope["allow"] = kept
    return present


def add_network_deny(
    directory: StrPath | None,
    domain: str,
    *,
    global_: bool = False,
    agent: str | None = None,
) -> None:
    """Deny a domain (and its subdomains) in one scope.

    A denied domain is refused by the proxy without asking an attached
    `aiab monitor` session. Any allow record for the same domain in that scope
    is dropped, so allow/deny stay disjoint. global_=True targets the shared
    global list (``directory`` ignored); ``agent`` restricts the rule to one
    agent (default: all agents).
    """
    domain = _normalize_domain(domain)
    with _open_policy(_policy_key(directory, global_)) as policy:
        scope = _scope(policy, agent)
        scope["allow"] = [a for a in scope["allow"] if a["domain"] != domain]
        if domain not in scope["deny"]:
            scope["deny"].append(domain)


def remove_network_deny(
    directory: StrPath | None,
    domain: str,
    *,
    global_: bool = False,
    agent: str | None = None,
) -> bool:
    """Drop a denied domain from a scope. Return True if it was present."""
    domain = _normalize_domain(domain)
    with _open_policy(_policy_key(directory, global_)) as policy:
        scope = _scope(policy, agent)
        kept = [d for d in scope["deny"] if d != domain]
        present = len(kept) != len(scope["deny"])
        scope["deny"] = kept
    return present


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


# -- injected environment variables --

# Bucket name for variables that apply to every agent run in a directory, as
# opposed to a single agent's bucket (keyed by agent name).
ENV_ALL_AGENTS = "*"

# The whole env file: dir -> bucket ("*" or agent name) -> {name: value}.
EnvState = dict[str, dict[str, dict[str, str]]]


def get_env(directory: StrPath, agent: str) -> dict[str, str]:
    """Return the env vars to inject for an agent in a directory.

    Merges the directory-wide ("*") bucket with the agent's own bucket, the
    agent-specific values winning on conflict. Empty if nothing is recorded.
    """
    data: EnvState = _load_file(_ENV_PATH)
    dir_env = data.get(_key(directory), {})
    return {**dir_env.get(ENV_ALL_AGENTS, {}), **dir_env.get(agent, {})}


def set_env(directory: StrPath, agent: str, name: str, value: str) -> None:
    """Record an env var for a directory, scoped to one agent or all ("*")."""
    key = _key(directory)
    with _locked_path(_ENV_PATH):
        data: EnvState = _load_file(_ENV_PATH)
        dir_env = data.setdefault(key, {})
        dir_env.setdefault(agent, {})[name] = value
        _save_file_unlocked(_ENV_PATH, data)


def unset_env(directory: StrPath, agent: str, name: str) -> bool:
    """Drop a recorded env var. Return True if it was present.

    Emptied buckets (and then the directory entry) are removed to keep the file
    tidy.
    """
    key = _key(directory)
    with _locked_path(_ENV_PATH):
        data: EnvState = _load_file(_ENV_PATH)
        bucket = data.get(key, {}).get(agent, {})
        if name not in bucket:
            return False
        del bucket[name]
        if not bucket:
            data[key].pop(agent, None)
        if not data[key]:
            data.pop(key, None)
        _save_file_unlocked(_ENV_PATH, data)
    return True


def list_env(directory: StrPath) -> dict[str, dict[str, str]]:
    """Return the recorded env buckets for a directory, unmerged.

    Maps each bucket name ("*" or an agent name) to its {name: value} dict;
    empty when nothing is recorded.
    """
    data: EnvState = _load_file(_ENV_PATH)
    return data.get(_key(directory), {})


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


def prune_stale() -> (
    tuple[list[str], list[str], list[str], list[str], list[str], list[str]]
):
    """Remove records for directories that no longer exist from all state.

    Covers the mount, network, base, limits, and env JSON files and the
    per-directory state dirs (a state dir whose recorded .source path is gone
    is deleted, setup script and all; ones without a readable .source are left
    alone). Returns (pruned_mount_dirs, pruned_network_dirs, pruned_base_dirs,
    pruned_state_dirs, pruned_limit_dirs, pruned_env_dirs) as lists of
    project-directory path strings.
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
            if key == GLOBAL_KEY:
                continue  # the global policy belongs to no directory
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

    pruned_env: list[str] = []
    with _locked_path(_ENV_PATH):
        env_data = _load_file(_ENV_PATH)
        for key in list(env_data):
            if not Path(key).is_dir():
                del env_data[key]
                pruned_env.append(key)
        if pruned_env:
            _save_file_unlocked(_ENV_PATH, env_data)

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

    return (
        pruned_mounts,
        pruned_net,
        pruned_base,
        pruned_state,
        pruned_limits,
        pruned_env,
    )
