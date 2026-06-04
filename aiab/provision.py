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
# aiab.provision - building and updating the per-agent template containers.
#
# These functions drive a Container (from aiab.lxd) through the one-time
# install of an agent and later in-place upgrades. Keeping them out of aiab.lxd
# leaves that module a generic LXD wrapper that knows nothing about agents;
# the per-agent install/upgrade commands come from aiab.agents via the cli.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import StrPath
from .lxd import Container

# An install/upgrade step: a description and the argv to run in the container.
Step = tuple[str, list[str]]


def provision_base(
    container: Container,
    *,
    config_host_dir: StrPath,
    config_container_path: str,
    config_device_name: str,
    install_cmds: list[Step],
    container_user: int = 0,
) -> None:
    """Create a base template container, install the agent, then stop it."""
    _create(
        container,
        config_host_dir,
        config_container_path,
        config_device_name,
        install_cmds,
        container_user,
    )
    print(
        f"Stopping base container '{container.name}' (template) ...",
        file=sys.stderr,
    )
    container.stop()


def _create(
    container: Container,
    config_host_dir: StrPath,
    config_container_path: str,
    config_device_name: str,
    install_cmds: list[Step],
    container_user: int,
) -> None:
    """Create and configure a fresh container, then install the agent.

    install_cmds is a list of (description, cmd) pairs where cmd is run via
    Container.exec.
    """
    uid = os.getuid()
    gid = os.getgid()

    print(
        f"Creating container '{container.name}' from ubuntu:24.04 ...",
        file=sys.stderr,
    )
    container.create("ubuntu:24.04")

    # Map the host user's UID/GID to the container user so that files created
    # inside mounted directories appear owned by the host user.
    container.set_config(
        "raw.idmap", f"uid {uid} {container_user}\ngid {gid} {container_user}"
    )

    # Mount a dedicated config directory for persistent authentication.
    # On first use the agent will prompt for credentials inside the container.
    Path(config_host_dir).mkdir(parents=True, exist_ok=True)
    container.add_config_dir(config_host_dir, config_container_path, config_device_name)
    print(f"Container config: {config_host_dir}", file=sys.stderr)

    print(f"Starting container '{container.name}' ...", file=sys.stderr)
    container.start()

    print("Waiting for cloud-init ...", file=sys.stderr)
    container.exec(["cloud-init", "status", "--wait"], stdout=subprocess.DEVNULL)

    print("Updating packages ...", file=sys.stderr)
    container.exec(["apt-get", "update", "-q"], stdout=subprocess.DEVNULL)
    container.exec(["apt-get", "dist-upgrade", "-y", "-q"], stdout=subprocess.DEVNULL)

    for description, cmd in install_cmds:
        print(description, file=sys.stderr)
        container.exec(cmd, stdout=subprocess.DEVNULL)

    print(f"Container '{container.name}' is ready.\n", file=sys.stderr)


def update_template(
    container: Container, *, update_cmds: list[Step], container_user: int = 0
) -> bool:
    """Update an existing template container and stop it again.

    Starts the container, runs apt-get update/dist-upgrade, then runs
    update_cmds (same format as install_cmds), then stops it. Returns False
    (and prints a note) if the container does not exist.
    """
    if not container.exists():
        print(
            f"Skipping '{container.name}': container does not exist.",
            file=sys.stderr,
        )
        return False

    if container.status() != "RUNNING":
        print(f"Starting '{container.name}' ...", file=sys.stderr)
        container.start()

    print("Updating packages ...", file=sys.stderr)
    container.exec(["apt-get", "update", "-q"], stdout=subprocess.DEVNULL)
    container.exec(["apt-get", "dist-upgrade", "-y", "-q"], stdout=subprocess.DEVNULL)

    for description, cmd in update_cmds:
        print(description, file=sys.stderr)
        container.exec(cmd, stdout=subprocess.DEVNULL)

    print(f"Stopping '{container.name}' (template) ...", file=sys.stderr)
    container.stop()
    print(f"'{container.name}' updated.\n", file=sys.stderr)
    return True
