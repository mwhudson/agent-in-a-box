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
# as data (the OpenRouter key prompt, opencode's permissive config). The cli
# and migration modules iterate over this single table, so adding an agent
# means adding one entry here.

import getpass
import json
import os
import sys

from . import CONTAINER_HOME

# Repo root (the directory containing this package), used to locate the
# versioned config overlays that ship alongside the code.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

DEFAULT_OR_MODEL = "anthropic/claude-sonnet-4-6"


def _claude_install():
    return [
        ("Installing claude ...",
         ["runuser", "-u", "ubuntu", "--",
          "bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"]),
    ]


def _ensure_openrouter_config(config_host_dir):
    """Write ~/.claude/settings.json with OpenRouter config if not present."""
    settings_path = os.path.join(config_host_dir, ".claude", "settings.json")
    if os.path.exists(settings_path):
        return

    print("OpenRouter config not found — setting up now.", file=sys.stderr)
    print(file=sys.stderr)

    api_key = getpass.getpass("OpenRouter API key (sk-or-...): ").strip()
    if not api_key:
        sys.exit("Error: API key is required")

    model = input(f"Model [{DEFAULT_OR_MODEL}]: ").strip() or DEFAULT_OR_MODEL

    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": "https://openrouter.ai/api",
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_MODEL": model,
        }
    }
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print(f"Wrote OpenRouter config to {settings_path}", file=sys.stderr)
    print(file=sys.stderr)


def _ensure_opencode_permissive_config(config_host_dir):
    """Write a permissive opencode.json into the container home if absent.

    Lets opencode run without permission prompts (it has no Claude-style
    --dangerously-skip-permissions flag); safe because the container can only
    see the directories mounted into it. Written straight into the mounted
    home (no overlay needed) and only when absent, so hand-edits survive.
    """
    config = os.path.join(config_host_dir, ".config", "opencode", "opencode.json")
    if os.path.exists(config):
        return
    os.makedirs(os.path.dirname(config), exist_ok=True)
    with open(config, "w") as f:
        json.dump({
            "$schema": "https://opencode.ai/config.json",
            "permission": "allow",
        }, f, indent=2)
        f.write("\n")
    print(f"Created permissive opencode config: {config}", file=sys.stderr)


def _overlays(*pairs):
    """Build (host_path, container_path) overlay pairs rooted at REPO_ROOT."""
    return [(os.path.join(REPO_ROOT, src), dst) for src, dst in pairs]


# The registry. Keys are agent names; they double as the base/template
# container name and the per-directory container prefix.
#
# Fields:
#   command           binary to run inside the container
#   install_cmds      (description, cmd) pairs run when building the template
#   upgrade_cmds      (description, cmd) pairs run by `aiab upgrade-templates`
#                     (defaults to install_cmds when omitted)
#   skip_permissions  prepend --dangerously-skip-permissions to the agent
#   wayland           bind-mount the host Wayland socket (clipboard support)
#   overlays          versioned config bind-mounted onto the container home
#   prepare           optional hook(config_host_dir) run before launch
AGENTS = {
    "claude": {
        "command": f"{CONTAINER_HOME}/.local/bin/claude",
        "install_cmds": _claude_install(),
        "skip_permissions": True,
        # Versioned Claude config (CLAUDE.md + slash commands) from this repo.
        "overlays": _overlays(
            ("claude/CLAUDE.md", f"{CONTAINER_HOME}/.claude/CLAUDE.md"),
            ("claude/commands", f"{CONTAINER_HOME}/.claude/commands"),
        ),
    },
    "claude-or": {
        # Claude pointed at OpenRouter instead of the Claude API. Same binary,
        # separate template/config so credentials don't mix. No repo overlay.
        "command": f"{CONTAINER_HOME}/.local/bin/claude",
        "install_cmds": _claude_install(),
        "skip_permissions": True,
        "prepare": _ensure_openrouter_config,
    },
    "opencode": {
        "command": f"{CONTAINER_HOME}/.opencode/bin/opencode",
        "install_cmds": [
            ("Installing opencode ...",
             ["runuser", "-u", "ubuntu", "--",
              "bash", "-c", "curl -fsSL https://opencode.ai/install | bash"]),
            ("Installing wl-clipboard ...",
             ["apt-get", "install", "-y", "-q", "wl-clipboard"]),
        ],
        # Re-running the installer is enough to upgrade; no need to reinstall
        # wl-clipboard, which apt dist-upgrade already covers.
        "upgrade_cmds": [
            ("Updating opencode ...",
             ["runuser", "-u", "ubuntu", "--",
              "bash", "-c", "curl -fsSL https://opencode.ai/install | bash"]),
        ],
        "wayland": True,
        "prepare": _ensure_opencode_permissive_config,
        "overlays": _overlays(
            ("opencode/AGENTS.md", f"{CONTAINER_HOME}/.config/opencode/AGENTS.md"),
            ("opencode/commands", f"{CONTAINER_HOME}/.config/opencode/commands"),
        ),
    },
    "copilot": {
        "command": "copilot",
        "install_cmds": [
            ("Installing Node.js 22 ...",
             ["bash", "-c",
              "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -"
              " && apt-get install -y -q nodejs"]),
            ("Installing copilot ...",
             ["npm", "install", "-g", "@github/copilot"]),
        ],
        # Node is already in the template; just refresh the copilot package.
        "upgrade_cmds": [
            ("Updating copilot ...",
             ["npm", "install", "-g", "@github/copilot"]),
        ],
    },
}

AGENT_NAMES = tuple(AGENTS)


def get(agent):
    """Return the registry entry for an agent, with defaults filled in."""
    cfg = AGENTS[agent]
    return {
        "command": cfg["command"],
        "install_cmds": cfg["install_cmds"],
        "upgrade_cmds": cfg.get("upgrade_cmds", cfg["install_cmds"]),
        "skip_permissions": cfg.get("skip_permissions", False),
        "wayland": cfg.get("wayland", False),
        "overlays": cfg.get("overlays", []),
        "prepare": cfg.get("prepare"),
    }
