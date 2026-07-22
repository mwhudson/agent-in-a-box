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
# aiab - run coding agents in disposable per-directory LXD containers.
#
# This package holds the engine (lxd), the agent registry (agents), and the
# command-line front end (cli). The `bin/aiab` launcher just imports
# cli.main().
#
# Only plain constants (and a shared type alias) live here so that submodules
# can import them without triggering a circular import through this __init__.

import os

# Anything accepted where a filesystem path is expected: a str or a Path.
StrPath = str | os.PathLike[str]

# LXD project that all aiab containers live in. Keeps them grouped and out of
# the user's 'default' project.
PROJECT: str = "aiab"

# Conventions shared by every agent container. The working directory (and any
# extra mounts) land under WORK_PREFIX; the container runs as CONTAINER_USER
# with CONTAINER_HOME as its home, mapped to the host user via raw.idmap so
# files created in mounts are owned by the host user.
CONTAINER_USER: int = 1000
CONTAINER_HOME: str = "/home/ubuntu"
WORK_PREFIX: str = "/work"

# Each project directory's persistent state dir (see aiab.state.dir_state_dir)
# is mounted read-write here in that directory's session containers, so state
# the agent maintains — notably the /setup-container setup script at
# STATE_MOUNT/setup.sh — survives container recreation.
STATE_MOUNT: str = "/aiab"
