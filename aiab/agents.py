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
# aiab.agents - the registry of known agents.
#
# Each agent is described entirely as data: the binary to run, how to install
# it, whether to skip permission prompts, which versioned config to overlay,
# and an optional one-time prepare() hook for anything that can't be expressed
# as data (opencode's permissive config). The cli and migration modules
# iterate over this single table, so adding an agent means adding one entry
# here.
#
# A *variant* of an agent -- same binary, different endpoint and
# credentials -- is not an entry here; that's a profile (see aiab.profiles).

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import CONTAINER_HOME
from .provision import Step

# Repo root (the directory containing this package), used to locate the
# versioned config overlays that ship alongside the code.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _claude_install() -> list[Step]:
    return [
        (
            "Installing claude ...",
            [
                "runuser",
                "-u",
                "ubuntu",
                "--",
                "bash",
                "-c",
                "curl -fsSL https://claude.ai/install.sh | bash",
            ],
        ),
    ]


def _ensure_opencode_permissive_config(config_host_dir: Path) -> None:
    """Write a permissive opencode.json into the container home if absent.

    Lets opencode run without permission prompts (it has no Claude-style
    --dangerously-skip-permissions flag); safe because the container can only
    see the directories mounted into it. Written straight into the mounted
    home (no overlay needed) and only when absent, so hand-edits survive.
    """
    config = Path(config_host_dir) / ".config" / "opencode" / "opencode.json"
    if config.exists():
        return
    config.parent.mkdir(parents=True, exist_ok=True)
    with config.open("w") as f:
        json.dump(
            {
                "$schema": "https://opencode.ai/config.json",
                "permission": "allow",
            },
            f,
            indent=2,
        )
        f.write("\n")
    print(f"Created permissive opencode config: {config}", file=sys.stderr)


def _overlays(*pairs: tuple[str, str]) -> list[tuple[Path, str]]:
    """Build (host_path, container_path) overlay pairs rooted at REPO_ROOT."""
    return [(REPO_ROOT / src, dst) for src, dst in pairs]


@dataclass
class Agent:
    """A known agent.

    The agent's name (its key in AGENTS) doubles as the base/template container
    name and the per-directory container prefix.
    """

    # Binary to run inside the container.
    command: str
    # Steps run when building the template container.
    install_cmds: list[Step]
    # Steps run by `aiab upgrade-templates`. Left empty, it defaults to
    # install_cmds (filled in by __post_init__).
    upgrade_cmds: list[Step] = field(default_factory=list)
    # Arguments to prepend to the agent command (e.g., permission-skipping flags).
    extra_args: list[str] = field(default_factory=list)
    # Bind-mount the host Wayland socket (clipboard support).
    wayland: bool = False
    # Versioned config (host_path, container_path) pairs bind-mounted onto the
    # container home.
    overlays: list[tuple[Path, str]] = field(default_factory=list)
    # Optional hook(config_host_dir) run before launch.
    prepare: Callable[[Path], None] | None = None
    # Domains (including subdomains) the agent needs to function — its API,
    # auth, and telemetry endpoints. Always allowed when the directory's
    # network mode is 'restricted' (see aiab.netproxy).
    api_domains: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.upgrade_cmds:
            self.upgrade_cmds = self.install_cmds


# Domains every agent needs regardless of which one runs — the Ubuntu apt
# mirrors used during container setup. Always allowed in restricted mode, like
# each agent's api_domains; they are the all-agents default of the net rules
# (see aiab.netproxy / aiab.state), not a hardcoded special case in the proxy.
BASELINE_DOMAINS: list[str] = [
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "ports.ubuntu.com",
]


# The registry. Keys are agent names.
AGENTS: dict[str, Agent] = {
    "claude": Agent(
        command=f"{CONTAINER_HOME}/.local/bin/claude",
        # wl-clipboard stays in the upgrade path (install_cmds doubles as
        # upgrade_cmds) so existing templates pick it up; apt-get install is a
        # no-op once it's present.
        install_cmds=_claude_install()
        + [
            (
                "Installing wl-clipboard ...",
                [
                    "apt-get",
                    "install",
                    "-y",
                    "-q",
                    "--no-install-recommends",
                    "wl-clipboard",
                ],
            ),
        ],
        extra_args=["--dangerously-skip-permissions"],
        # Claude shells out to wl-clipboard (wl-copy/wl-paste) for clipboard
        # access on Wayland — image paste in, copy out.
        wayland=True,
        # Versioned Claude config (CLAUDE.md + slash commands) from this repo.
        overlays=_overlays(
            ("agent-config/claude/CLAUDE.md", f"{CONTAINER_HOME}/.claude/CLAUDE.md"),
            ("agent-config/claude/commands", f"{CONTAINER_HOME}/.claude/commands"),
        ),
        # anthropic.com covers api./statsig./console.; claude.ai is used for
        # OAuth login; sentry.io for crash reporting.
        api_domains=["anthropic.com", "claude.ai", "claude.com", "sentry.io"],
    ),
    "opencode": Agent(
        command=f"{CONTAINER_HOME}/.opencode/bin/opencode",
        install_cmds=[
            (
                "Installing opencode ...",
                [
                    "runuser",
                    "-u",
                    "ubuntu",
                    "--",
                    "bash",
                    "-c",
                    "curl -fsSL https://opencode.ai/install | bash",
                ],
            ),
            (
                "Installing wl-clipboard ...",
                [
                    "apt-get",
                    "install",
                    "-y",
                    "-q",
                    "--no-install-recommends",
                    "wl-clipboard",
                ],
            ),
        ],
        # Re-running the installer is enough to upgrade; no need to reinstall
        # wl-clipboard, which apt dist-upgrade already covers.
        upgrade_cmds=[
            (
                "Updating opencode ...",
                [
                    "runuser",
                    "-u",
                    "ubuntu",
                    "--",
                    "bash",
                    "-c",
                    "curl -fsSL https://opencode.ai/install | bash",
                ],
            ),
        ],
        wayland=True,
        prepare=_ensure_opencode_permissive_config,
        # opencode.ai for auth/updates, models.dev for its model catalogue,
        # anthropic.com for the default provider. Using a different provider
        # in restricted mode needs an `aiab net allow` for its API domain.
        api_domains=["opencode.ai", "models.dev", "anthropic.com"],
        overlays=_overlays(
            (
                "agent-config/opencode/AGENTS.md",
                f"{CONTAINER_HOME}/.config/opencode/AGENTS.md",
            ),
            (
                "agent-config/opencode/commands",
                f"{CONTAINER_HOME}/.config/opencode/commands",
            ),
        ),
    ),
    "copilot": Agent(
        command="copilot",
        install_cmds=[
            (
                "Installing Node.js 22 ...",
                [
                    "bash",
                    "-c",
                    "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -"
                    " && apt-get install -y -q nodejs",
                ],
            ),
            (
                "Installing copilot ...",
                ["npm", "install", "-g", "@github/copilot"],
            ),
        ],
        # Node is already in the template; just refresh the copilot package.
        upgrade_cmds=[
            (
                "Updating copilot ...",
                ["npm", "install", "-g", "@github/copilot"],
            ),
        ],
        extra_args=["--yolo"],
        # github.com covers api. and the device-code login flow;
        # githubcopilot.com is the Copilot API; githubusercontent.com serves
        # auxiliary content.
        api_domains=["github.com", "githubcopilot.com", "githubusercontent.com"],
        # Versioned Copilot config (global instructions + a custom agent).
        # Copilot CLI has no Claude/opencode-style filename-triggered slash
        # commands, so setup-container ships as a custom agent instead
        # (invoked via `/agent` or `--agent setup-container`, not a slash
        # command of the same name).
        overlays=_overlays(
            (
                "agent-config/copilot/copilot-instructions.md",
                f"{CONTAINER_HOME}/.copilot/copilot-instructions.md",
            ),
            (
                "agent-config/copilot/agents",
                f"{CONTAINER_HOME}/.copilot/agents",
            ),
        ),
    ),
}

AGENT_NAMES: tuple[str, ...] = tuple(AGENTS)


def get(agent: str) -> Agent:
    """Return the Agent for a name. Raises KeyError if unknown."""
    return AGENTS[agent]
