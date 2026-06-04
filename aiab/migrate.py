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
# aiab.migrate - one-time, implicit migration from the old lxd-* tools.
#
# The previous generation of scripts (lxd-claude, lxd-opencode, ...) put their
# containers in an LXD project named 'lxd-ai', persisted config under
# ~/.local/share/lxd-<agent>/, and named session containers
# <agent>-<hash>-<basename>. aiab uses the project 'aiab',
# ~/.local/share/aiab/<agent>/, and <agent>-<basename>-<hash>.
#
# maybe_migrate() runs at startup and moves everything across the first time it
# sees the old layout. It is keyed solely off the project pair, so once 'aiab'
# exists it does nothing.

import re
import shutil
import sys
from pathlib import Path

from . import PROJECT
from .agents import AGENT_NAMES
from .lxd import Lxd, agent_home_dir

OLD_PROJECT = "lxd-ai"

# <hash> is the 6-hex md5 prefix produced by container_name_for_dir.
_HASH_RE = re.compile(r"[0-9a-f]{6}")


def maybe_migrate():
    """Migrate from the old lxd-* layout if (and only if) it's present.

    Trigger: the old 'lxd-ai' project exists and the new 'aiab' project does
    not. Anything else (already migrated, or a fresh install) is a no-op.
    """
    if Lxd(PROJECT).project_exists():
        return
    if not Lxd(OLD_PROJECT).project_exists():
        return
    _migrate()


def _migrate():
    print(f"Migrating from the old '{OLD_PROJECT}' layout to '{PROJECT}' ...",
          file=sys.stderr)
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


def _move_config_dirs():
    base = Path.home() / ".local" / "share"
    new_root = base / PROJECT
    for agent in AGENT_NAMES:
        old = base / f"lxd-{agent}"
        new = new_root / agent
        if old.is_dir() and not new.exists():
            new_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            print(f"  Moved config {old} -> {new}", file=sys.stderr)


def _new_container_name(name):
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
        rest = name[len(prefix):]
        h, sep, basename = rest.partition("-")
        if not sep or not _HASH_RE.fullmatch(h):
            return None  # not our hash-prefixed scheme; leave alone
        return f"{agent}-{basename}-{h}"
    return None


def _move_instances(new):
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
            print(f"  Repointed {new_name}:{agent}config -> {source}",
                  file=sys.stderr)


def _agent_for(name):
    """Return the agent a container belongs to, by longest-prefix match."""
    for agent in sorted(AGENT_NAMES, key=len, reverse=True):
        if name == agent or name.startswith(agent + "-"):
            return agent
    return None
