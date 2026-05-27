# lxd-claude

Run coding agents (Claude Code, GitHub Copilot CLI) inside disposable
[LXD](https://canonical.com/lxd) containers, with the current directory mounted
in. Each project directory gets its own container, so an agent running
`--dangerously-skip-permissions` can only touch the directories you've mounted —
not the rest of your machine.

## Tools

| Script | What it does |
| --- | --- |
| `lxd-claude` | Run [Claude Code](https://claude.ai/code) in a per-directory container. |
| `lxd-claude-mount` | Mount extra host directories into an already-running `lxd-claude` session. |
| `lxd-copilot` | Run the [GitHub Copilot CLI](https://github.com/github/copilot-cli) in a shared container. |
| `lxd_ai.py` | Shared helper module imported by the scripts above. |

## How it works

The first time you run `lxd-claude` it creates a **base container** from
`ubuntu:24.04`, installs the agent into it, then stops it as a template.
Subsequent runs clone a lightweight **per-directory session container** from that
base — its name is derived from the directory path
(`claude-<hash>-<basename>`), so re-running in the same directory reuses the
same container.

Your working directory is mounted into the container under `/work/<basename>`,
and the agent is launched there. The container's user is mapped to your host
UID/GID (via `raw.idmap`), so files the agent creates in mounted directories are
owned by you on the host.

Authentication is persisted on the host (under `~/.local/share/lxd-claude/` and
similar) and mounted into the container, so you only log in once.

## Requirements

- LXD, installed and initialised (`lxd init`), with your user able to run `lxc`.
- Python 3.
- Network access from containers (to install the agents and reach their APIs).

## Install

The scripts expect `lxd_ai.py` to sit next to them (they add their own directory
to `sys.path`). Symlink the entry points onto your `PATH`, e.g.:

```sh
git clone <this-repo> ~/src/lxd-claude
ln -s ~/src/lxd-claude/lxd-claude      ~/.local/bin/lxd-claude
ln -s ~/src/lxd-claude/lxd-claude-mount ~/.local/bin/lxd-claude-mount
ln -s ~/src/lxd-claude/lxd-copilot     ~/.local/bin/lxd-copilot
```

(Symlinks work because each script resolves its real location to find
`lxd_ai.py`.)

## Usage

### lxd-claude

```
lxd-claude [--or] [--also DIR]... [--shell] [-- CLAUDE_ARGS...]
```

Run from inside the project directory you want the agent to work in.

- `--also DIR` — also mount `DIR` into the container (repeatable).
- `--shell` — open an interactive shell in the container instead of running Claude.
- `--or` — run Claude against [OpenRouter](https://openrouter.ai) instead of the
  Claude API. Uses a separate `claude-or` base container; on first use it prompts
  for your OpenRouter API key and model and writes them to
  `~/.local/share/lxd-claude-or/home/.claude/settings.json`.
- Anything after `--` is passed straight through to `claude`.

On first run inside a fresh base container, authenticate Claude as prompted;
credentials are stored on the host and reused afterwards.

### lxd-claude-mount

Mount additional host directories into a session that's already running:

```
lxd-claude-mount [--or] [--for DIR] DIR [DIR ...]
```

- By default it targets the container for the current directory.
- `--for DIR` — target the container for a different project directory.
- `--or` — target an OpenRouter session container.

### lxd-copilot

```
lxd-copilot [--also DIR]... [-- COPILOT_ARGS...]
```

Runs the GitHub Copilot CLI. Unlike `lxd-claude`, it uses a single shared
container named `copilot` (not one per directory). Config/auth is persisted
under `~/.local/share/lxd-copilot/config`.
