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

# Ubuntu's /etc/profile resets PATH, and the stock ~/.profile that would add
# ~/.local/bin back is shadowed by the host dir mounted over the container
# home — so login shells would miss the agent binaries without this snippet
# (profile.d is sourced after the reset). Agent processes don't read profiles;
# they get the equivalent PATH from the env `aiab run` passes (see aiab.cli).
_PROFILE_D_PATH = "/etc/profile.d/aiab.sh"
_PROFILE_D_SNIPPET = """\
# Written by aiab: agents (and tools they install) live in ~/.local/bin.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) PATH="$HOME/.local/bin:$PATH" ;;
esac
"""

# sudo strips the environment by default; this sudoers.d snippet preserves
# proxy variables so that `sudo apt install ...` works inside containers whose
# network is routed through the filtering proxy.
_SUDOERS_D_PATH = "/etc/sudoers.d/aiab-proxy-env"
_SUDOERS_D_SNIPPET = """\
# Written by aiab: preserve proxy env vars through sudo so that apt and
# other network-using commands work in restricted-network containers.
Defaults env_keep += "http_proxy https_proxy no_proxy HTTP_PROXY HTTPS_PROXY NO_PROXY"
"""


def _add_local_bin_to_path(container: Container) -> None:
    """Write the /etc/profile.d snippet that puts ~/.local/bin on PATH."""
    container.exec(
        ["tee", _PROFILE_D_PATH],
        input=_PROFILE_D_SNIPPET.encode(),
        stdout=subprocess.DEVNULL,
    )


def _configure_sudo_proxy_env(container: Container) -> None:
    """Write a sudoers.d snippet that preserves proxy env vars through sudo."""
    container.exec(
        ["tee", _SUDOERS_D_PATH],
        input=_SUDOERS_D_SNIPPET.encode(),
        stdout=subprocess.DEVNULL,
    )
    # sudoers files must be mode 0440 and owned by root.
    container.exec(["chmod", "0440", _SUDOERS_D_PATH])


# NOTE: the profile.d / sudoers fixups above are written into the template at
# build time (_create) and refreshed on `aiab upgrade-templates`
# (update_template); session containers inherit them by being cloned from the
# template, so `aiab run` does no per-session tweak step. The trade-off: a new
# fixup only reaches sessions after the template is rebuilt or upgraded.


def provision_base(
    container: Container,
    *,
    image: str,
    base: str,
    config_host_dir: StrPath,
    config_container_path: str,
    config_device_name: str,
    install_cmds: list[Step],
    container_user: int = 0,
) -> None:
    """Create a base template container, install the agent, then stop it.

    ``base`` is the canonical release ``image`` was picked for; it is recorded
    on the container as user.aiab_base so that a later change to the default
    base is noticed rather than silently reusing a template built on the old
    one (see aiab.release and the rebuild check in aiab.cli).

    Provisioning runs under a temporary '<name>-provisioning' container name
    and is only renamed to the final name on success.  This means that a
    half-built container left behind by a previous interrupted run is never
    mistaken for a valid template — if '<name>' does not exist, provisioning
    was never completed successfully.
    """
    tmp = Container(container.lxd, container.name + "-provisioning")
    if tmp.exists():
        print(
            f"Removing incomplete previous provisioning attempt '{tmp.name}' ...",
            file=sys.stderr,
        )
        tmp.delete()

    try:
        _create(
            tmp,
            image,
            base,
            config_host_dir,
            config_container_path,
            config_device_name,
            install_cmds,
            container_user,
        )
        print(
            f"Stopping base container '{tmp.name}' (template) ...",
            file=sys.stderr,
        )
        tmp.stop()
    except Exception:
        if tmp.exists():
            print(
                f"Provisioning failed; removing '{tmp.name}' ...",
                file=sys.stderr,
            )
            tmp.delete()
        raise

    tmp.rename(container.name)
    print(f"Base container '{container.name}' is ready.", file=sys.stderr)


def _create(
    container: Container,
    image: str,
    base: str,
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
        f"Creating container '{container.name}' from {image} ...",
        file=sys.stderr,
    )
    container.create(image)

    # Map the host user's UID/GID to the container user so that files created
    # inside mounted directories appear owned by the host user.
    container.set_config(
        "raw.idmap", f"uid {uid} {container_user}\ngid {gid} {container_user}"
    )

    # Record the release this was built from, so it survives a change to the
    # default base. Set before the install so it is already there when the
    # container is renamed to its final name; sessions cloned from here
    # inherit it, and aiab.cli overwrites it with the directory's own.
    container.set_config("user.aiab_base", base)

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

    _add_local_bin_to_path(container)
    _configure_sudo_proxy_env(container)

    for description, cmd in install_cmds:
        print(description, file=sys.stderr)
        container.exec(cmd, stdout=subprocess.DEVNULL)


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

    # Templates built before the snippet existed pick it up on upgrade.
    _add_local_bin_to_path(container)
    _configure_sudo_proxy_env(container)

    for description, cmd in update_cmds:
        print(description, file=sys.stderr)
        container.exec(cmd, stdout=subprocess.DEVNULL)

    print(f"Stopping '{container.name}' (template) ...", file=sys.stderr)
    container.stop()
    print(f"'{container.name}' updated.\n", file=sys.stderr)
    return True
