# Installation

- [Requirements](#requirements)
- [Install](#install)
- [Shell completion](#shell-completion)

## Requirements

- LXD, installed and initialised (`lxd init`), with your user able to run `lxc`.
- Python 3, with [Click](https://click.palletsprojects.com/)
  (on Debian/Ubuntu: `apt install python3-click`).
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

Completion for subcommands, options, agent names and directories comes from
Click, which generates it from `aiab`'s own command definitions. Enable it by
adding one line to your shell's startup file:

```sh
# bash — add to ~/.bashrc:
eval "$(_AIAB_COMPLETE=bash_source aiab)"

# zsh — add to ~/.zshrc:
eval "$(_AIAB_COMPLETE=zsh_source aiab)"

# fish — add to ~/.config/fish/completions/aiab.fish:
_AIAB_COMPLETE=fish_source aiab | source
```

That runs `aiab` once per shell start. To avoid it, write the script out once
and source the file instead — just remember to regenerate it after a `git pull`
that adds or renames a subcommand:

```sh
_AIAB_COMPLETE=bash_source aiab > ~/.local/share/aiab-completion.bash
# then in ~/.bashrc:
source ~/.local/share/aiab-completion.bash
```

The completion runs `aiab` to answer each request, so it needs `aiab` on your
`PATH` (the symlink above).
