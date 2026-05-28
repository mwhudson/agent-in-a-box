# agent-in-a-box

Run coding agents (Claude Code, opencode, GitHub Copilot CLI) inside disposable
[LXD](https://canonical.com/lxd) containers, with the current directory mounted
in. Each project directory gets its own container, so an agent running with
permission prompts disabled can only touch the directories you've mounted —
not the rest of your machine.

## Tools

| Script | What it does |
| --- | --- |
| `lxd-claude` | Run [Claude Code](https://claude.ai/code) in a per-directory container. |
| `lxd-opencode` | Run [opencode](https://opencode.ai) in a per-directory container. |
| `lxd-claude-mount` | Mount extra host directories into an already-running `lxd-claude` session. |
| `lxd-opencode-mount` | Mount extra host directories into an already-running `lxd-opencode` session. |
| `lxd-copilot` | Run the [GitHub Copilot CLI](https://github.com/github/copilot-cli) in a shared container. |
| `lxd-ai-update` | Update template containers (apt upgrade + agent binary). |
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

All containers these tools create live in a dedicated LXD project named
`lxd-ai` (created automatically on first use), so they stay grouped together
and out of your `default` project. The project is created with
`features.profiles=false` and `features.images=false`, so it shares the default
project's profiles (network/storage) and image cache — containers work out of
the box, they're just namespaced separately. List them with
`lxc list --project lxd-ai`.

## Versioned Claude config (CLAUDE.md + slash commands)

The `claude/` directory in this repo is the source of truth for the global
config you want available in every Claude session:

```
claude/
  CLAUDE.md        -> mounted at ~/.claude/CLAUDE.md  (global instructions)
  commands/        -> mounted at ~/.claude/commands/  (custom /slash commands)
```

`lxd-claude` bind-mounts these into the session container's `~/.claude` as LXD
devices, sourced from this repo's own location (found via the script's real
path, so it works no matter which project directory you're running in). Because
it's a bind mount, the files are the *same* on the host and in the container —
edit them here and commit, or edit them from inside a session; either way the
change is reflected in both and tracked by git.

Why not just symlink them into the config dir? The config dir is mounted into
the container, so a symlink there would have to resolve to a path that exists
*inside* the container — but this repo is only mounted (at `/work/<basename>`)
when it happens to be the working directory, so the link would dangle in every
other session. Bind-mounting sidesteps that entirely.

Notes:

- Your **credentials** are *not* versioned — they stay in the per-machine
  config dir (`~/.local/share/lxd-claude/home/.claude/`); only `CLAUDE.md` and
  `commands/` are overlaid from the repo.
- This applies to the default (Claude API) container only. The `--or` /
  OpenRouter container does not get the overlay.
- Missing entries are skipped, so it's fine to delete `claude/CLAUDE.md` or
  leave `claude/commands/` empty.

## Versioned opencode config (opencode.json + AGENTS.md)

The `opencode/` directory plays the same role for `lxd-opencode`, bind-mounted
into the container's `~/.config/opencode`:

```
opencode/
  opencode.json   -> mounted at ~/.config/opencode/opencode.json  (global config)
  AGENTS.md       -> mounted at ~/.config/opencode/AGENTS.md       (global instructions)
  commands/       -> mounted at ~/.config/opencode/commands/       (custom commands)
```

`opencode/opencode.json` sets `"permission": "allow"`, so opencode runs without
permission prompts. opencode has no Claude-style `--dangerously-skip-permissions`
flag; this config is the equivalent, and it's safe for the same reason — the
container can only see the directories you've mounted into it. `AGENTS.md` is
opencode's equivalent of `CLAUDE.md` (auto-loaded as global instructions). As
with the Claude overlay, credentials are *not* versioned (they stay in
`~/.local/share/lxd-opencode/home/.local/share/opencode/auth.json`), and missing
entries are skipped.

## Requirements

- LXD, installed and initialised (`lxd init`), with your user able to run `lxc`.
- Python 3.
- Network access from containers (to install the agents and reach their APIs).

## Install

The scripts expect `lxd_ai.py` to sit next to them (they add their own directory
to `sys.path`). Symlink the entry points onto your `PATH`, e.g.:

```sh
git clone https://github.com/mwhudson/agent-in-a-box ~/src/agent-in-a-box
cd agent-in-a-box
ln -s $(pwd)/lxd-claude      ~/.local/bin/lxd-claude
ln -s $(pwd)/lxd-opencode    ~/.local/bin/lxd-opencode
ln -s $(pwd)/lxd-claude-mount  ~/.local/bin/lxd-claude-mount
ln -s $(pwd)/lxd-opencode-mount ~/.local/bin/lxd-opencode-mount
ln -s $(pwd)/lxd-copilot      ~/.local/bin/lxd-copilot
ln -s $(pwd)/lxd-ai-update   ~/.local/bin/lxd-ai-update
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

### lxd-opencode

```
lxd-opencode [--also DIR]... [--shell] [-- OPENCODE_ARGS...]
```

Works just like `lxd-claude` — per-directory session containers cloned from an
`opencode` base, your working directory mounted at `/work/<basename>`, files
owned by you on the host.

- `--also DIR` — also mount `DIR` into the container (repeatable).
- `--shell` — open an interactive shell in the container instead of opencode.
- Anything after `--` is passed straight through to `opencode`.

On first run, authenticate inside the container with `opencode auth login`;
credentials persist on the host under `~/.local/share/lxd-opencode/`.
Use `lxd-opencode-mount` to mount additional directories into a running session
(see below).

### lxd-claude-mount

Mount additional host directories into a session that's already running:

```
lxd-claude-mount [--or] [--for DIR] [--read-only] DIR [DIR ...]
```

- By default it targets the container for the current directory.
- `--for DIR` — target the container for a different project directory.
- `--or` — target an OpenRouter session container.
- `--read-only` — mount the directories read-only (the container cannot modify them).

### lxd-opencode-mount

Mount additional host directories into an opencode session that's already running:

```
lxd-opencode-mount [--for DIR] [--read-only] DIR [DIR ...]
```

- By default it targets the container for the current directory.
- `--for DIR` — target the container for a different project directory.
- `--read-only` — mount the directories read-only (the container cannot modify them).

### lxd-ai-update

Update template containers in place (apt upgrade + agent binary):

```
lxd-ai-update [AGENT...]
```

With no arguments, updates all template containers that currently exist.
Pass one or more agent names to update only those: `claude`, `claude-or`,
`opencode`, `copilot`.

Each update starts the template container, runs `apt-get update` and
`dist-upgrade`, re-runs the agent installer (all installers are idempotent
and will fetch the latest version), then stops the container again. Session
containers cloned afterwards will include the updates; existing session
containers are not affected.

### lxd-copilot

```
lxd-copilot [--also DIR]... [-- COPILOT_ARGS...]
```

Runs the GitHub Copilot CLI. Unlike `lxd-claude`, it uses a single shared
container named `copilot` (not one per directory). Config/auth is persisted
under `~/.local/share/lxd-copilot/config`.

## License

Copyright (C) 2026 Canonical Ltd.

This project is free software, licensed under the GNU General Public License
version 3 (or, at your option, any later version). See the [LICENSE](LICENSE)
file for the full text.
