# Installation

- [Requirements](#requirements)
- [Install](#install)
- [Shell completion](#shell-completion)

## Requirements

- LXD, installed and initialised (`lxd init`), with your user able to run `lxc`.
- Python 3, with [Click](https://click.palletsprojects.com/) and PyYAML
  (on Debian/Ubuntu: `apt install python3-click python3-yaml`).
- Network access from containers (to install the agents and reach their APIs).
- [textual](https://textual.textualize.io/) ≥ 0.32, for `aiab monitor`
  (`pip install textual` — the `python3-textual` in the Ubuntu archive is a
  0.1.x relic that predates the modern API). Only `aiab monitor` imports it,
  so the other subcommands still work without it, but the interactive
  allow/deny prompts that restricted mode relies on do not.

## Install

`aiab` is a Python package with a thin launcher in `bin/aiab` that finds the
repo from its own real path. Symlink the launcher onto your `PATH`:

```sh
git clone https://github.com/mwhudson/agent-in-a-box ~/src/agent-in-a-box
ln -s ~/src/agent-in-a-box/bin/aiab ~/.local/bin/aiab
```

(The symlink works because the launcher resolves its real location to find the
`aiab` package next to it.)

## Shell completion

Optionally enable shell completion for subcommands, agent names, and
directories:

```sh
# bash — add to ~/.bashrc:
source ~/src/agent-in-a-box/completions/aiab.bash

# zsh — put the completion on your fpath, e.g.:
ln -s ~/src/agent-in-a-box/completions/aiab.zsh \
      ~/.zsh/completions/_aiab        # a dir on your $fpath, before compinit
```
