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


# Claude Code from Anthropic's signed apt repository rather than the native
# installer (`claude.ai/install.sh`). The installer hardcodes
# ~/.local/bin/claude and ~/.local/share/claude — there is no install-dir
# knob — which puts ~800MB of binaries *inside the agent's home*, the one
# directory aiab shares between every container for an agent. Installing from
# apt puts it in the template image instead, where copilot's already lives, so
# the shared home holds only config and credentials.
#
# 'latest' rather than 'stable': the native installer auto-updated, and this
# keeps that. Package installs don't auto-update, so a version only moves when
# `aiab upgrade-templates` rebuilds the template.
#
# downloads.claude.ai needs no net rule of its own — claude's api_domains
# already allow claude.ai and its subdomains.
_CLAUDE_APT_KEY = "https://downloads.claude.ai/keys/claude-code.asc"
_CLAUDE_APT_KEYRING = "/etc/apt/keyrings/claude-code.asc"
_CLAUDE_APT_LINE = (
    f"deb [signed-by={_CLAUDE_APT_KEYRING}] "
    "https://downloads.claude.ai/claude-code/apt/latest latest main"
)


def _claude_install() -> list[Step]:
    return [
        (
            "Installing claude ...",
            [
                "bash",
                "-c",
                "set -e; "
                "install -d -m 0755 /etc/apt/keyrings; "
                f"curl -fsSL {_CLAUDE_APT_KEY} -o {_CLAUDE_APT_KEYRING}; "
                f"echo '{_CLAUDE_APT_LINE}'"
                " > /etc/apt/sources.list.d/claude-code.list; "
                "apt-get update -q; "
                "apt-get install -y -q claude-code",
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
    # Paths under the container home, relative to it, that stay *shared*
    # between every container for this agent: credentials and user-level
    # config, the things you want to set up once. Everything else in the home
    # is per-directory (see aiab.state.session_home_dir), because the rest of
    # what an agent keeps there — transcripts, daemon state, session locks —
    # describes one machine and is wrong in another container.
    #
    # An allowlist rather than a list of things to keep apart: agents grow new
    # state directories faster than we would notice them, and the failure mode
    # here should be "not shared" rather than "silently shared".
    #
    # A trailing '/' marks a directory, anything else a file. Both ends of a
    # bind mount have to exist, so a path that isn't in the shared home yet is
    # created empty before mounting.
    shared_paths: list[str] = field(default_factory=list)
    # Argv appended to `command` that lists the agent's currently-active
    # sessions as a JSON array on stdout, each an object with a `kind` field.
    # A `kind == "background"` entry is a session that outlives the foreground
    # process — Claude Code's /background hands off to a daemon like this — so
    # its presence means stopping the container would kill live work. Must not
    # need a TTY. None if the agent has no such concept. See
    # aiab.lifecycle.has_live_background_session.
    background_ls: list[str] | None = None

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
        # Found on PATH, not an absolute path: the apt package installs
        # system-wide, but a shared home built before this switch still has
        # ~/.local/bin/claude, which PATH finds first. That shadowing is
        # deliberate for now — session containers cloned before the switch
        # have no apt package, and would break if pointed at one. It ends by
        # itself once the home stops being shared per-agent.
        command="claude",
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
        # .claude.json carries the OAuth session, so it has to be shared —
        # which also shares its per-project map and assorted counters. Checked
        # and accepted: the entries that would matter (allowedTools,
        # mcpServers) are unused, and permission state is moot in a container
        # the agent already runs unconfined in. Everything else under .claude
        # (projects/, daemon*, tasks/, sessions/, jobs/, file-history/,
        # history.jsonl) is per-directory.
        shared_paths=[
            ".claude.json",
            ".claude/.credentials.json",
            ".claude/settings.json",
            ".claude/plugins/",
        ],
        # `/background` hands the session to a daemon that keeps running after
        # the foreground claude exits; `claude agents --json` lists the still-
        # active ones (empty array when none), no TTY required.
        background_ls=["agents", "--json"],
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
        # .config/opencode is opencode's global config layer, and doubles as an
        # npm package holding plugin dependencies — shared, since `aiab
        # opencode config` already provides the per-directory layer above it
        # (OPENCODE_CONFIG). .opencode holds the binary, installed into the
        # home by opencode's installer. The session and message data —
        # opencode.db, snapshot/, storage/, log/ — stays per-directory; it is
        # one database for every project otherwise.
        shared_paths=[
            ".config/opencode/",
            ".local/share/opencode/auth.json",
            ".local/share/opencode/mcp-auth.json",
            ".opencode/",
        ],
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
        # Copilot keeps everything under one directory, so the split is inside
        # it: config.json holds the authentication state, the other three are
        # user configuration. session-state/, session-store.db,
        # command-history-state.json, logs/ and ide/ are per-directory — and
        # session-state/ is where the inuse.<pid>.lock files live, which only
        # mean anything in the container that wrote them.
        shared_paths=[
            ".copilot/config.json",
            ".copilot/mcp-config.json",
            ".copilot/lsp-config.json",
            ".copilot/settings.json",
        ],
    ),
}

AGENT_NAMES: tuple[str, ...] = tuple(AGENTS)


def get(agent: str) -> Agent:
    """Return the Agent for a name. Raises KeyError if unknown."""
    return AGENTS[agent]
