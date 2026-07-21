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
# aiab.migrate - one-time, implicit migrations between aiab layouts.
#
# The previous generation of scripts (lxd-claude, lxd-opencode, ...) put their
# containers in an LXD project named 'lxd-ai', persisted config under
# ~/.local/share/lxd-<agent>/, and named session containers
# <agent>-<hash>-<basename>. aiab uses the project 'aiab',
# ~/.local/share/aiab/<agent>/, and <agent>-<basename>-<hash>.
#
# maybe_migrate() moves everything across the first time it sees the old
# layout. It is keyed solely off the project pair, so once 'aiab' exists it
# does nothing.
#
# migrate_claude_or() is a later, separate migration: claude-or stopped being
# an agent and became the built-in 'openrouter' profile. It is called from
# `aiab run` and keyed off the old credential store still being present.

from __future__ import annotations

import contextlib
import json
import re
import shutil
import sys
from pathlib import Path

from . import PROJECT
from . import profiles
from .agents import AGENT_NAMES
from .lxd import Lxd, agent_home_dir

OLD_PROJECT: str = "lxd-ai"

# <hash> is the 6-hex md5 prefix produced by container_name_for_dir.
_HASH_RE: re.Pattern[str] = re.compile(r"[0-9a-f]{6}")


def maybe_migrate() -> None:
    """Migrate from the old lxd-* layout if (and only if) it's present.

    Trigger: the old 'lxd-ai' project exists and the new 'aiab' project does
    not. Anything else (already migrated, or a fresh install) is a no-op.
    """
    if Lxd(PROJECT).project_exists():
        return
    if not Lxd(OLD_PROJECT).project_exists():
        return
    _migrate()


def _migrate() -> None:
    print(
        f"Migrating from the old '{OLD_PROJECT}' layout to '{PROJECT}' ...",
        file=sys.stderr,
    )
    # An LXD project can't be renamed while it holds instances, so create the
    # new project (sharing the default project's profiles/images) and move the
    # instances across, then drop the now-empty old project.
    new = Lxd(PROJECT)
    new.ensure_project()
    # Move the host config dirs first, so the new source paths exist before we
    # repoint each container's config mount at them in _move_instances().
    _move_config_dirs()
    _move_instances(new)
    Lxd(OLD_PROJECT).delete_project()
    print(f"  Deleted empty project {OLD_PROJECT}", file=sys.stderr)
    print("Migration complete.\n", file=sys.stderr)


def _move_config_dirs() -> None:
    base = Path.home() / ".local" / "share"
    new_root = base / PROJECT
    for agent in AGENT_NAMES:
        old = base / f"lxd-{agent}"
        new = new_root / agent
        if old.is_dir() and not new.exists():
            new_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            print(f"  Moved config {old} -> {new}", file=sys.stderr)


def _new_container_name(name: str) -> str | None:
    """Reorder an old session-container name to the new scheme.

    <agent>-<hash>-<basename>  ->  <agent>-<basename>-<hash>

    Returns None for names that aren't old per-directory containers (e.g. the
    bare base/template containers, or anything not matching the scheme), which
    are left untouched.
    """
    # Match the longest agent prefix first so 'claude-or' wins over 'claude'.
    for agent in sorted(AGENT_NAMES, key=len, reverse=True):
        if name == agent:
            return None  # base/template container
        prefix = agent + "-"
        if not name.startswith(prefix):
            continue
        rest = name[len(prefix) :]
        h, sep, basename = rest.partition("-")
        if not sep or not _HASH_RE.fullmatch(h):
            return None  # not our hash-prefixed scheme; leave alone
        return f"{agent}-{basename}-{h}"
    return None


def _move_instances(new: Lxd) -> None:
    """Move every instance from the old project into the new one.

    `lxc move <src> <dst> --target-project` both moves the instance across
    projects and (when the names differ) renames it, so a single command also
    applies the <agent>-<hash>-<basename> -> <agent>-<basename>-<hash> reorder.
    Instances are stopped first: cross-project moves need a stopped instance,
    and the next `aiab run` restarts the session container anyway.

    Each container's config device bind-mounts the agent's host home dir;
    _move_config_dirs() relocated that dir, so the device's source is updated
    to the new path here (all containers share it, having been cloned from the
    same base).
    """
    old = Lxd(OLD_PROJECT)
    for name, status in old.instances().items():
        if status.strip().upper() == "RUNNING":
            old.run(["stop", name])
        new_name = _new_container_name(name) or name
        old.run(["move", name, new_name, "--target-project", PROJECT])
        if new_name != name:
            print(f"  Moved {name} -> {PROJECT}:{new_name}", file=sys.stderr)
        else:
            print(f"  Moved {name} -> project {PROJECT}", file=sys.stderr)

        agent = _agent_for(new_name)
        if agent:
            source = agent_home_dir(agent)
            new.container(new_name).set_device_source(f"{agent}config", source)
            print(
                f"  Repointed {new_name}:{agent}config -> {source}",
                file=sys.stderr,
            )


def _agent_for(name: str) -> str | None:
    """Return the agent a container belongs to, by longest-prefix match."""
    for agent in sorted(AGENT_NAMES, key=len, reverse=True):
        if name == agent or name.startswith(agent + "-"):
            return agent
    return None


# ---------------------------------------------------------------------------
# claude-or -> the 'openrouter' profile
# ---------------------------------------------------------------------------
#
# claude-or used to be an agent in its own right: the same claude binary with
# a different endpoint, its own credential store, and its own template. It is
# now the built-in 'openrouter' profile (see aiab.profiles), so the only thing
# worth carrying across is the credential store — session containers are
# disposable by design and the template is rebuilt from the shared 'claude'
# one.
#
# This runs from `aiab run` rather than at startup, and is keyed off the old
# store still being there, so it is a single stat() on every other run.

_OLD_OR_AGENT = "claude-or"
_NEW_OR_HOME = "claude@openrouter"

# Supplied by the profile's env now, so they're dropped from the migrated
# settings.json. ANTHROPIC_MODEL is not merely redundant: env outranks the
# `model` settings field but `/model` persists a switch into it, so leaving it
# set makes an in-session model switch silently revert on the next launch.
_PROFILE_MANAGED = ("ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL")


def migrate_claude_or(conn: Lxd) -> None:
    """Move the old claude-or credential store to the openrouter profile's.

    A no-op once done (or on an install that never used claude-or). Leftover
    claude-or containers are reported rather than deleted: they're cheap to
    recreate but they're the user's to remove.
    """
    old_home = agent_home_dir(_OLD_OR_AGENT)
    new_home = agent_home_dir(_NEW_OR_HOME)
    if not old_home.is_dir() or new_home.exists():
        return

    print(
        "claude-or is now the 'openrouter' profile; moving its credentials ...",
        file=sys.stderr,
    )
    new_home.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_home), str(new_home))
    print(f"  Moved {old_home} -> {new_home}", file=sys.stderr)
    # Only 'home' moved; drop the agent dir it sat in if nothing else is there.
    with contextlib.suppress(OSError):
        old_home.parent.rmdir()
    _rewrite_or_settings(new_home)
    _report_stale_or_containers(conn)
    print("Run it with: aiab run --profile openrouter claude\n", file=sys.stderr)


def _rewrite_or_settings(home: Path) -> None:
    """Strip the now profile-supplied vars from a migrated settings.json.

    Everything else in the file is left alone — it may hold settings the user
    added themselves. A model that wasn't the profile's default is called out,
    since dropping ANTHROPIC_MODEL does change which model is selected.
    """
    settings_path = home / ".claude" / "settings.json"
    try:
        with settings_path.open() as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return
    env = settings.get("env")
    if not isinstance(env, dict):
        return

    model = env.get("ANTHROPIC_MODEL")
    dropped = [k for k in _PROFILE_MANAGED if k in env]
    if not dropped:
        return
    for key in dropped:
        del env[key]
    with settings_path.open("w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"  Dropped {', '.join(dropped)} from {settings_path}", file=sys.stderr)
    print("    (the profile supplies them now)", file=sys.stderr)
    if model and model != profiles.DEFAULT_OR_MODEL:
        print(
            f"    Note: your model was {model}; the profile defaults to "
            f"{profiles.DEFAULT_OR_MODEL}. To keep yours, pick it with /model, "
            "or set it per directory with\n"
            f"      aiab env set --agent claude ANTHROPIC_CUSTOM_MODEL_OPTION {model}",
            file=sys.stderr,
        )


def _report_stale_or_containers(conn: Lxd) -> None:
    """Name any leftover claude-or containers so the user can clear them out."""
    try:
        stale = [
            n
            for n in conn.instances()
            if n == _OLD_OR_AGENT or n.startswith(_OLD_OR_AGENT + "-")
        ]
    except OSError:
        return
    if not stale:
        return
    print(
        f"  {len(stale)} old claude-or container(s) are now unused: "
        f"{', '.join(sorted(stale))}",
        file=sys.stderr,
    )
    print(
        "    Remove them with: lxc --project aiab delete --force <name>",
        file=sys.stderr,
    )
