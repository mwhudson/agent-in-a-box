# agent-in-a-box

Run coding agents (Claude Code, opencode, GitHub Copilot CLI) inside disposable
[LXD](https://canonical.com/lxd) containers, with the current directory mounted
in. Each project directory gets its own container, so an agent running with
permission prompts disabled can only touch the directories you've mounted —
not the rest of your machine.

Everything is driven by a single command, `aiab`, with a subcommand per task:

```
aiab run <agent>            # run an agent in a container for the current dir
aiab remove <agent>         # delete that container
aiab mount DIR ...          # mount extra directories into a dir's containers
aiab unmount DIR ...        # remove those mounts
aiab upgrade-templates      # apt upgrade + reinstall agents in the templates
aiab list                   # list the containers
aiab lxc ...                # run lxc against the 'aiab' project
```

`<agent>` is one of `claude`, `claude-or` (Claude via OpenRouter), `opencode`,
or `copilot`.

## How it works

The first time you run `aiab run claude` it creates a **base container** from
`ubuntu:24.04`, installs the agent into it, then stops it as a template.
Subsequent runs clone a lightweight **per-directory session container** from that
base — its name is derived from the directory path
(`claude-<basename>-<hash>`), so re-running in the same directory reuses the
same container.

Your working directory is mounted into the container under `/work/<basename>`,
and the agent is launched there. The container's user is mapped to your host
UID/GID (via `raw.idmap`), so files the agent creates in mounted directories are
owned by you on the host.

Authentication is persisted on the host (under
`~/.local/share/aiab/<agent>/home`) and mounted into the container, so you only
log in once.

All containers these tools create live in a dedicated LXD project named
`aiab` (created automatically on first use), so they stay grouped together
and out of your `default` project. The project is created with
`features.profiles=false` and `features.images=false`, so it shares the default
project's profiles (network/storage) and image cache — containers work out of
the box, they're just namespaced separately. List them with `aiab list` (or
the raw `aiab lxc list`).

## Versioned Claude config (CLAUDE.md + slash commands)

The `claude/` directory in this repo is the source of truth for the global
config you want available in every Claude session:

```
claude/
  CLAUDE.md        -> mounted at ~/.claude/CLAUDE.md  (global instructions)
  commands/        -> mounted at ~/.claude/commands/  (custom /slash commands)
```

`aiab run claude` bind-mounts these into the session container's `~/.claude` as
LXD devices, sourced from this repo's own location (found via the launcher's
real path, so it works no matter which project directory you're running in).
Because it's a bind mount, the files are the *same* on the host and in the
container — edit them here and commit, or edit them from inside a session;
either way the change is reflected in both and tracked by git.

Why not just symlink them into the config dir? The config dir is mounted into
the container, so a symlink there would have to resolve to a path that exists
*inside* the container — but this repo is only mounted (at `/work/<basename>`)
when it happens to be the working directory, so the link would dangle in every
other session. Bind-mounting sidesteps that entirely.

Notes:

- Your **credentials** are *not* versioned — they stay in the per-agent
  config dir (`~/.local/share/aiab/claude/home/.claude/`); only `CLAUDE.md` and
  `commands/` are overlaid from the repo.
- This applies to the default (Claude API) `claude` agent only. The `claude-or`
  / OpenRouter agent does not get the overlay.
- Missing entries are skipped, so it's fine to delete `claude/CLAUDE.md` or
  leave `claude/commands/` empty.

## Versioned opencode config (AGENTS.md + commands)

The `opencode/` directory plays the same role for `aiab run opencode`,
bind-mounted into the container's `~/.config/opencode`:

```
opencode/
  AGENTS.md       -> mounted at ~/.config/opencode/AGENTS.md       (global instructions)
  commands/       -> mounted at ~/.config/opencode/commands/       (custom commands)
```

`AGENTS.md` is opencode's equivalent of `CLAUDE.md` (auto-loaded as global
instructions). As with the Claude overlay, credentials are *not* versioned (they
stay in `~/.local/share/aiab/opencode/home/.local/share/opencode/auth.json`),
and missing entries are skipped.

`opencode.json` is *not* versioned in this repo. On first run, `aiab run
opencode` writes a permissive config —

```json
{ "$schema": "https://opencode.ai/config.json", "permission": "allow" }
```

— to `~/.local/share/aiab/opencode/home/.config/opencode/opencode.json`, which
is inside the bind-mounted home so it needs no separate overlay. The
`"permission": "allow"` setting lets opencode run without permission prompts
(opencode has no Claude-style `--dangerously-skip-permissions` flag; this is the
equivalent), safe for the same reason — the container can only see the
directories you've mounted into it. It's only written when absent, so you can
edit it (e.g. to add MCP servers) and your changes persist.

## Requirements

- LXD, installed and initialised (`lxd init`), with your user able to run `lxc`.
- Python 3.
- Network access from containers (to install the agents and reach their APIs).

## Install

`aiab` is a Python package with a thin launcher in `bin/aiab` that finds the
repo from its own real path. Symlink the launcher onto your `PATH`:

```sh
git clone https://github.com/mwhudson/agent-in-a-box ~/src/agent-in-a-box
ln -s ~/src/agent-in-a-box/bin/aiab ~/.local/bin/aiab
```

(The symlink works because the launcher resolves its real location to find the
`aiab` package next to it.)

Optionally enable shell completion for subcommands, agent names, and
directories:

```sh
# bash — add to ~/.bashrc:
source ~/src/agent-in-a-box/completions/aiab.bash

# zsh — put the completion on your fpath, e.g.:
ln -s ~/src/agent-in-a-box/completions/aiab.zsh \
      ~/.zsh/completions/_aiab        # a dir on your $fpath, before compinit
```

### Migrating from the old `lxd-*` scripts

Earlier versions shipped separate `lxd-claude` / `lxd-opencode` / … scripts that
used an `lxd-ai` LXD project, `~/.local/share/lxd-<agent>/` config dirs, and
`<agent>-<hash>-<basename>` container names. The first `aiab` command you run
migrates that layout automatically — it renames the project to `aiab`, moves the
config dirs under `~/.local/share/aiab/<agent>/`, and reorders container names to
`<agent>-<basename>-<hash>`. It only fires once (when the old `lxd-ai` project
exists and the new `aiab` one doesn't); after that it's a no-op. Your
credentials are preserved, so you don't have to re-authenticate.

## Usage

Run `aiab run`, `aiab remove`, etc. from inside the project directory you want
the agent to work in (or use `--for DIR` on the commands that accept it).

### aiab run

```
aiab run <agent> [--also DIR]... [--also-rw DIR]... [--shell] [-- AGENT_ARGS...]
```

- `<agent>` — `claude`, `claude-or`, `opencode`, or `copilot`.
- `--also DIR` — also mount `DIR` **read-only** into the container (repeatable).
- `--also-rw DIR` — also mount `DIR` read-write (repeatable).
- `--shell` — open an interactive shell in the container instead of the agent.
- Anything after `--` is passed straight through to the agent.

`--also` / `--also-rw` mounts are remembered for the directory (see [`aiab
mount`](#aiab-mount--aiab-unmount) below), so they're re-applied on later runs
and for other agents in the same directory. Mounts recorded for the directory
are re-applied on every run regardless.

The base container is created automatically on first use. Authenticate inside
the container on first run; credentials are stored under
`~/.local/share/aiab/<agent>/home` and reused afterwards.

`claude-or` runs Claude against [OpenRouter](https://openrouter.ai) instead of
the Claude API, using a separate base container and config dir. On first use it
prompts for your OpenRouter API key and model and writes them to
`~/.local/share/aiab/claude-or/home/.claude/settings.json`.

### aiab remove

```
aiab remove <agent> [--for DIR]
```

Deletes the session container for the directory (current directory, or `--for
DIR`). The base/template container is left intact, so the next run clones a
fresh one quickly.

### aiab mount / aiab unmount

```
aiab mount   [--for DIR] [--ro | --rw] DIR [DIR ...]
aiab unmount [--for DIR] DIR [DIR ...]
```

`mount` records each `DIR` as an **extra mount for the project directory** and
adds it to every agent container (`claude`, `claude-or`, `opencode`,
`copilot`) that already exists for it. Because the set is recorded (in
`~/.local/share/aiab/mounts.json`, keyed by the directory), it also reaches
containers that don't exist yet: a different agent started for the same
directory, or a container deleted and recreated, gets the same mounts
automatically — `aiab run` replays them every time it brings a container up.
Run-time `--also` / `--also-rw` mounts are recorded the same way.

Running containers pick the mounts up immediately; stopped ones apply them the
next time they start. It's fine to `mount` before any container exists — the
mounts are just recorded for later.

Mounts are **read-only by default** — handy for reference code you want the
agent to read but not change. Re-running on an already-recorded directory just
reconciles its mode, so `aiab mount --rw DIR` flips an existing read-only mount
to read-write (and `--ro DIR` flips it back).

`unmount` drops each `DIR` from the directory's record and removes it from any
existing containers, so it isn't replayed on the next run.

- By default both target the current directory; use `--for DIR` to target a
  different project directory.
- `--ro` / `--rw` — read-only (the default) or read-write (`mount` only).

### aiab upgrade-templates

```
aiab upgrade-templates [AGENT ...]
```

Updates template containers in place. With no arguments, updates all template
containers that currently exist. Pass one or more agent names to update only
those.

Each update starts the template container, runs `apt-get update` and
`dist-upgrade`, re-runs the agent installer (which fetches the latest version),
then stops the container again. Session containers cloned afterwards include the
updates; existing session containers are not affected.

### aiab list

```
aiab list [--for DIR]
```

Lists the `aiab` session containers, and for each its working-directory source
mount and any extra mounts (added via `--also`/`--also-rw` or `aiab mount`):

```
claude-myproj-ab12cd  [RUNNING]
  source: /home/me/myproj -> /work/myproj
  mount:  /home/me/ref    -> /work/ref (ro)
opencode-myproj-ef34gh  [STOPPED]
  source: /home/me/myproj -> /work/myproj
```

The bare base/template containers are omitted. With `--for DIR`, shows only the
containers for that project directory. For the raw LXD view, use `aiab lxc
list`.

### aiab lxc

```
aiab lxc <args...>
```

Runs `lxc --project aiab <args...>` — a convenience for poking at the containers
directly, e.g. `aiab lxc list` or `aiab lxc exec claude-myproj-abc123 -- bash`.

## License

Copyright (C) 2026 Canonical Ltd.

This project is free software, licensed under the GNU General Public License
version 3 (or, at your option, any later version). See the [LICENSE](LICENSE)
file for the full text.
