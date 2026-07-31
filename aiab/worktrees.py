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
# aiab.worktrees - where a run's git worktree lives, and which ones exist.
#
# `aiab run --worktree[-branch]` puts the agent's checkout under
# <repo>/.git/aiab-worktrees/: inside the mounted repo, so it needs no extra
# bind-mount, and out of the way of ordinary directory listings.
#
# `aiab run` creates them from inside the container (where the repo is mounted
# at /work/<name>) and `aiab monitor` lists them from the host (where the same
# files live under the real repo), so the layout belongs to neither and lives
# here.

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

# Relative to the repo root, in both views of it.
DIR_NAME = ".git/aiab-worktrees"


def path_for(repo: str, branch: str | None) -> str:
    """Where a run's worktree lives: named for its branch, or for the instant.

    A branch name is the useful label — it is how you tell parallel sessions
    apart and how you find the result afterwards — so it names the directory
    too. Branch names may contain '/', which just nests the path; git's own
    D/F rule (no 'foo' alongside 'foo/bar') is what stops that colliding.

    Without a branch there is nothing to name it after, so fall back to the
    clock. time_ns() rather than time(): two --worktree runs starting in the
    same second would otherwise collide.
    """
    leaf = branch if branch else str(time.time_ns())
    return f"{repo}/{DIR_NAME}/{leaf}"


def _scan(root: Path, base: Path) -> Iterator[str]:
    """Yield worktree labels under base, relative to root.

    A linked worktree is a directory holding a `.git` *file* (the gitfile
    pointing at its admin dir), which is what identifies one here. Recursion
    stops at each worktree rather than descending into it: branch names may
    contain '/' so the path has to be walked, but a checkout can itself contain
    submodules, whose gitfiles would otherwise be reported as worktrees.
    """
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_dir():
            continue
        if (entry / ".git").is_file():
            yield str(entry.relative_to(root))
        else:
            yield from _scan(root, entry)


def existing(work_dir: Path) -> list[str]:
    """The branches of this directory's aiab worktrees, read from the host.

    The label is the worktree's path relative to the worktrees dir, which *is*
    the branch, because that is how path_for names them.

    Detached worktrees are skipped: path_for names those after the clock, so
    there is no branch to resume and offering one would mean creating a branch
    called 1738000000000000000. They are exactly the all-digit names, which is a
    coupling to path_for above rather than a guess about git.

    Deliberately no git: a worktree's `.git` file records its admin dir as an
    absolute path from when it was created, which for a container-made worktree
    is a container path that does not resolve here. Only the file's presence is
    needed, so that mismatch never comes up — and this works whether or not the
    host has git installed.
    """
    root = work_dir / DIR_NAME
    return [label for label in _scan(root, root) if not label.isdigit()]
