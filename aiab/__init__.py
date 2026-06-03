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
# This package holds the engine (lxd), the agent registry (agents), the
# one-time migration from the old lxd-* tools (migrate), and the command-line
# front end (cli). The `bin/aiab` launcher just imports cli.main().
#
# Only plain constants live here so that submodules can import them without
# triggering a circular import through this __init__.

# LXD project that all aiab containers live in. Keeps them grouped and out of
# the user's 'default' project.
PROJECT = "aiab"

# Conventions shared by every agent container. The working directory (and any
# extra mounts) land under WORK_PREFIX; the container runs as CONTAINER_USER
# with CONTAINER_HOME as its home, mapped to the host user via raw.idmap so
# files created in mounts are owned by the host user.
CONTAINER_USER = 1000
CONTAINER_HOME = "/home/ubuntu"
WORK_PREFIX = "/work"
