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

import os
import re
import shutil
import subprocess
import sys

from . import PROJECT
from . import lxd
from .agents import AGENT_NAMES

OLD_PROJECT = "lxd-ai"

# <hash> is the 6-hex md5 prefix produced by container_name_for_dir.
_HASH_RE = re.compile(r"[0-9a-f]{6}")


def maybe_migrate():
    """Migrate from the old lxd-* layout if (and only if) it's present.

    Trigger: the old 'lxd-ai' project exists and the new 'aiab' project does
    not. Anything else (already migrated, or a fresh install) is a no-op.
    """
    if lxd.project_exists(PROJECT):
        return
    if not lxd.project_exists(OLD_PROJECT):
        return
    _migrate()


def _migrate():
    print(f"Migrating from the old '{OLD_PROJECT}' layout to '{PROJECT}' ...",
          file=sys.stderr)
    # An LXD project can't be renamed while it holds instances, so create the
    # new project (sharing the default project's profiles/images) and move the
    # instances across, then drop the now-empty old project.
    lxd.use_project(PROJECT)
    _move_instances()
    _move_config_dirs()
    lxd.run(["lxc", "project", "delete", OLD_PROJECT])
    print(f"  Deleted empty project {OLD_PROJECT}", file=sys.stderr)
    print("Migration complete.\n", file=sys.stderr)


def _move_config_dirs():
    base = os.path.expanduser("~/.local/share")
    new_root = os.path.join(base, PROJECT)
    for agent in AGENT_NAMES:
        old = os.path.join(base, f"lxd-{agent}")
        new = os.path.join(new_root, agent)
        if os.path.isdir(old) and not os.path.exists(new):
            os.makedirs(new_root, exist_ok=True)
            shutil.move(old, new)
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


def _move_instances():
    """Move every instance from the old project into the new one.

    `lxc move <src> <dst> --target-project` both moves the instance across
    projects and (when the names differ) renames it, so a single command also
    applies the <agent>-<hash>-<basename> -> <agent>-<basename>-<hash> reorder.
    Instances are stopped first: cross-project moves need a stopped instance,
    and the next `aiab run` restarts the session container anyway.
    """
    result = subprocess.run(
        ["lxc", "--project", OLD_PROJECT, "list", "--format=csv",
         "--columns=n,s"],
        capture_output=True, text=True,
    )
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, _, state = line.partition(",")
        if state.strip().upper() == "RUNNING":
            lxd.run(["lxc", "--project", OLD_PROJECT, "stop", name])
        new = _new_container_name(name) or name
        lxd.run(["lxc", "--project", OLD_PROJECT, "move", name, new,
                 "--target-project", PROJECT])
        if new != name:
            print(f"  Moved {name} -> {PROJECT}:{new}", file=sys.stderr)
        else:
            print(f"  Moved {name} -> project {PROJECT}", file=sys.stderr)
