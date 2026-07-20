# agent-in-a-box

Run coding agents (Claude Code, opencode, GitHub Copilot CLI) inside disposable
[LXD](https://canonical.com/lxd) containers, with the current directory mounted
in. Each project directory gets its own container, so an agent running with
permission prompts disabled can only touch the directories you've mounted —
not the rest of your machine. Network access is locked down too: by default
the agent can only reach its own API plus domains you've explicitly allowed,
managed per directory with [`aiab net`](docs/commands.md#aiab-net).

It needs **nothing in the repo** — no `.devcontainer/`, no Dockerfile, no
committed config — and wraps the agent from the *outside*, so the same sandbox
applies whether or not the agent cooperates. See
[Concepts](docs/concepts.md#why-not-a-devcontainer-or-a-process-sandbox) for
the rationale.

Everything is driven by a single command, `aiab`, with a subcommand per task:

```
aiab run <agent>            # run an agent in a container for the current dir
aiab remove <agent>         # delete that container
aiab mount DIR ...          # mount extra directories into a dir's containers
aiab unmount DIR ...        # remove those mounts
aiab net ...                # restrict a dir's containers' network access
aiab base ...               # pick the Ubuntu release a dir's containers use
aiab limits ...             # set a dir's container CPU/memory limits
aiab env ...                # inject environment variables per directory
aiab opencode config ...    # per-directory opencode config (e.g. its key)
aiab monitor                # interactive network + mounts control panel
aiab upgrade-templates      # apt upgrade + reinstall agents in the templates
aiab list                   # list the containers
aiab gc                     # remove containers whose directory is gone
aiab lxc ...                # run lxc against the 'aiab' project
```

`<agent>` is one of `claude`, `claude-or` (Claude via OpenRouter), `opencode`,
or `copilot`. Full options for every subcommand are in the
[command reference](docs/commands.md).

## Quick start

Install [LXD](https://canonical.com/lxd) and `lxd init` it, then:

```sh
git clone https://github.com/mwhudson/agent-in-a-box ~/src/agent-in-a-box
ln -s ~/src/agent-in-a-box/bin/aiab ~/.local/bin/aiab

cd ~/some/project
aiab run claude            # builds the container on first use, then launches
```

Authenticate inside the container on first run; credentials are stored on the
host and reused afterwards. Full requirements, shell completion, and migration
notes are in [Installation](docs/install.md).

## Documentation

- **[Concepts](docs/concepts.md)** — how it works, the per-directory state dir,
  and the git guard.
- **[Configuration](docs/configuration.md)** — versioned config overlays and
  per-directory environment variables / opencode keys.
- **[Command reference](docs/commands.md)** — every subcommand and its options.
- **[Installation](docs/install.md)** — requirements, install, completion,
  migration.
- **[Development](docs/development.md)** — linting, type checking, tests.

## License

Copyright (C) 2026 Canonical Ltd.

This project is free software, licensed under the GNU General Public License
version 3 (or, at your option, any later version). See the [LICENSE](LICENSE)
file for the full text.
