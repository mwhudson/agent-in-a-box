# agent-in-a-box

Run coding agents (Claude Code, opencode, GitHub Copilot CLI) inside disposable
[LXD](https://canonical.com/lxd) containers, with the current directory mounted
in. Each project directory gets its own container, so an agent running with
permission prompts disabled can only touch the directories you've mounted —
not the rest of your machine. Network access is locked down too: by default
the agent can only reach its own API plus domains you've explicitly allowed,
managed per directory with [`aiab net`](#aiab-net).

Everything is driven by a single command, `aiab`, with a subcommand per task:

```
aiab run <agent>            # run an agent in a container for the current dir
aiab remove <agent>         # delete that container
aiab mount DIR ...          # mount extra directories into a dir's containers
aiab unmount DIR ...        # remove those mounts
aiab net ...                # restrict a dir's containers' network access
aiab base ...               # pick the Ubuntu release a dir's containers use
aiab monitor                # interactive network + mounts control panel
aiab upgrade-templates      # apt upgrade + reinstall agents in the templates
aiab list                   # list the containers
aiab gc                     # remove containers whose directory is gone
aiab lxc ...                # run lxc against the 'aiab' project
```

`<agent>` is one of `claude`, `claude-or` (Claude via OpenRouter), `opencode`,
or `copilot`.

## How it works

The first time you run `aiab run claude` it creates a **base container** from
`ubuntu:24.04` (or whatever release the directory is set to — see
[`aiab base`](#aiab-base)), installs the agent into it, then stops it as a
template. Subsequent runs clone a lightweight **per-directory session container**
from that base — its name is derived from the directory path
(`claude-<basename>-<hash>`), so re-running in the same directory reuses the
same container.

Your working directory is mounted into the container under `/work/<basename>`,
and the agent is launched there. The container's user is mapped to your host
UID/GID (via `raw.idmap`), so files the agent creates in mounted directories are
owned by you on the host.

When the last session using a container exits, the container is stopped about
five minutes later (by a small detached helper) rather than immediately — so
exiting never waits on the stop, and starting another session shortly after
reuses the still-running container. Starting a new session cancels the
pending stop.

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

## Per-directory state and the setup script (`/aiab`)

Each project directory also gets a persistent state directory on the host
(`~/.local/share/aiab/dirstate/<basename>-<hash>/`), mounted read-write at
`/aiab` inside every session container for that directory — the same dir for
every agent. It holds per-directory state the *agent* maintains that should
survive container recreation; today that's the container setup script.

The `/setup-container` slash command (shipped from this repo for both Claude
and opencode, see below) maintains `/aiab/setup.sh`: when the script doesn't
exist it works out the toolchain and dependency installs from the project's
own docs and writes them there; when it does — notably in a freshly recreated
container — it shows the saved script and offers to run it. Either way it only
runs the script after you confirm in the session, so recreating a container's
dev environment is `/setup-container` plus a "yes". Since the file lives on
the host you can also inspect or edit it from outside the container (each
state dir's `.source` file records the project directory it belongs to).

[`aiab gc`](#aiab-gc) removes a directory's state dir (setup script included)
along with its other records once the directory itself is gone.

## Protecting the host repo (the git guard)

The working directory is mounted **read-write**, so an off-the-rails agent can
write bad code into your tree — that's inherent to letting it do the job, and
git is your backstop. But `.git` is special: git hooks (`.git/hooks/*`) and
several `.git/config` keys (`core.hooksPath`, `core.pager`, `core.fsmonitor`,
`[alias]`, filter `clean`/`smudge`, …) are *code that runs when host git
touches the repo* — fired by commands as innocuous as `git status` or `git
diff`, **outside the container**. Left unguarded, an agent could drop such a
payload into the mounted `.git` and have it execute on your machine the next
time you run git there.

So by default `aiab run`, in a git repository, gives the container its **own
copies** of `.git/hooks` and `.git/config`, seeded from the real ones and
bind-mounted over them:

- `.git/hooks` is shadowed **read-write**, so hooks the agent (or its tooling)
  installs still work *inside* the container — they just live in the sidecar
  and never reach the host's hooks dir.
- `.git/config` is shadowed **read-only**: the container reads your real config
  (so your aliases, hooks path, etc. still apply in-session) but can't change
  what host git sees.

The host's real `.git/hooks` and `.git/config` are shadowed and left untouched.
The copies live under the directory's state dir and are reseeded on every run,
so they're disposable. Like the rest of the sandbox this is a guard against an
agent wandering, not against a deliberate exploit — defeating it would take a
kernel container escape.

Notes:

- This narrows the `.git`-based vectors only. An agent can still write a
  `Makefile`, `package.json` `postinstall`, `.envrc`, etc. that runs when *you*
  invoke the corresponding tool — but those need you to actively run something,
  unlike git hooks which fire from everyday read-only-feeling commands.
- Because `.git/config` is read-only in the container, in-session commands that
  rewrite it (`git config --local …`, `git remote add …`) fail. Local work
  (add/commit/diff/log/branch/checkout) is unaffected. Use `--no-git-guard` if
  you need the agent to edit repo config.
- It only kicks in when the working directory's `.git` is a real directory;
  a directly-mounted linked worktree or submodule checkout (where `.git` is a
  gitfile) is skipped.
- The same guard is applied to any **read-write** directory you mount in
  (`aiab mount --rw`, `--add-mount-rw`) that is itself a git repo, so the agent
  can't plant host-firing hooks there either. Read-only mounts can't be written,
  so they're left alone.

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
- Python 3, with [Click](https://click.palletsprojects.com/) and PyYAML
  (on Debian/Ubuntu: `apt install python3-click python3-yaml`).
- Network access from containers (to install the agents and reach their APIs).
- Optionally, [textual](https://textual.textualize.io/) ≥ 0.32 for the
  clickable `aiab monitor` UI (`pip install textual` — the
  `python3-textual` in the Ubuntu archive is a 0.1.x relic that predates the
  modern API). Without it the monitor falls back to a plain keystroke network
  console (and the domains and mounts tabs are unavailable).

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
aiab run <agent> [--for DIR] [--add-mount DIR]... [--add-mount-rw DIR]... [--no-git-guard] [--shell] [-- AGENT_ARGS...]
```

- `<agent>` — `claude`, `claude-or`, `opencode`, or `copilot`.
- `--for DIR` — run the agent for `DIR` instead of the current directory; `DIR`
  is the container's working directory, mounted at `/work/<basename>`.
- `--add-mount DIR` — mount `DIR` **read-only** into the container and record it for this directory (repeatable).
- `--add-mount-rw DIR` — mount `DIR` read-write and record it (repeatable).
- `--no-git-guard` — don't shadow the repo's `.git/hooks` and `.git/config`
  (see [the git guard](#protecting-the-host-repo-the-git-guard)).
- `--shell` — open an interactive shell in the container instead of the agent.
- Anything after `--` is passed straight through to the agent.

`--add-mount` / `--add-mount-rw` mounts are remembered for the directory (see [`aiab
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
Run-time `--add-mount` / `--add-mount-rw` mounts are recorded the same way.

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

### aiab net

```
aiab net status   [--for DIR]
aiab net restrict [--for DIR]
aiab net open     [--for DIR]
aiab net allow    [--for DIR] [--duration TIME] DOMAIN...
aiab net deny     [--for DIR] DOMAIN...
```

The interactive console for steering the proxy live is a separate command,
[`aiab monitor`](#aiab-monitor--the-interactive-control-plane).

By default a directory's network policy is **restricted**; `aiab net open`
records an **open** (unrestricted) policy for directories where you want the
old free-for-all back, and `aiab net restrict` switches one back again. The
policy is persisted per project directory (like `aiab mount`'s record, so it
applies to every agent and survives container recreation). When an agent
starts in a restricted directory:

- the container's NIC (inherited from the default profile) is masked, so it
  has **no direct network access at all**;
- a small filtering HTTP(S) proxy is started on the host and exposed inside
  the container (at `127.0.0.1:3128`, via an LXD proxy device), and the agent
  is launched with `HTTP_PROXY`/`HTTPS_PROXY` pointing at it. The proxy
  listens on an *abstract* unix socket — snap-confined LXD can't dial
  filesystem socket paths under your home directory — with a peer-credential
  check (root and your uid only) standing in for socket file permissions;
- the proxy only admits requests to the agent's own API domains (Claude needs
  `anthropic.com`/`claude.ai`, copilot needs `github.com`/`githubcopilot.com`,
  and so on — see `aiab net status` for the full per-agent list) plus the
  directory's recorded allowlist, and refuses its denylist. Everything else
  gets a 403 naming the host, and is logged to
  `~/.local/share/aiab/proxy/<container>.log`.

`allow` adds domains to the allowlist, `deny` to the denylist (subdomains
included in both: allowing `github.com` also allows `api.github.com`). The
two are kept disjoint — allowing a domain drops its deny record and vice
versa — and when rules overlap, the most specific one wins, so you can allow
`api.x.com` inside a denied `x.com`. The proxy re-reads the policy on
**every request**, so changes take effect immediately in running sessions —
when the agent hits a wall mid-task, run `aiab net allow some.domain` from
another terminal and it can carry on. `--duration 10m` (also `90s`, `2h`,
`1d`; bare numbers are minutes) makes a grant that lapses on its own;
re-allowing a domain replaces its expiry.

Mode changes (`restrict`/`open`) only take *full* effect the next time an
agent starts, because the NIC masking and proxy environment are applied at
launch. `aiab net open` does loosen a running restricted session immediately
(the proxy starts passing everything), but direct, un-proxied network access
only returns on the next run.

Caveats:

- Only **proxy-aware** traffic works in restricted mode. HTTPS/HTTP clients
  that honour the proxy environment (the agents themselves, curl, git's
  https transport, pip, npm, apt) are fine; ssh (so git-over-ssh) and
  raw-socket protocols are simply cut off.
- Hostname filtering is policy, not adversarial containment: anything the
  agent can reach over an allowed CONNECT, it can tunnel arbitrary data
  through. The threat model is "keep the agent from wandering", same as the
  filesystem sandbox.
- Template provisioning and `aiab upgrade-templates` are unaffected — they
  need apt and the agent installers, and don't run agent-authored code.

### aiab base

```
aiab base [--for DIR]                 # show the directory's base release
aiab base [--for DIR] RELEASE         # set it (e.g. 22.04 or jammy)
aiab base [--for DIR] default         # clear back to the default (24.04)
```

By default a directory's containers are built on Ubuntu 24.04. `aiab base
RELEASE` overrides that for one project directory; `RELEASE` is a version
(`22.04`) or a codename (`jammy`), and `default` clears the override. Like
`aiab net` and `aiab mount`, the choice is persisted per directory (keyed by
the resolved path) and only edits recorded state — no LXD connection needed.

Each agent gets its own template **per release**. The default release keeps the
plain template name (`claude`); other releases get a separate template
(`claude-base-2204`), built lazily the first time you run an agent for a
directory set to that release. `aiab upgrade-templates` refreshes every
template that exists, whatever its release.

Changing a directory's base takes effect on the next `aiab run` there: if the
directory already has a session container built from a different release, it is
discarded and re-cloned from the right template. Your work isn't in the
container — the working directory is a host bind mount — so the rebuild is just
the clone cost.

### aiab monitor — the interactive control plane

```
aiab monitor [--for DIR] [--plain]
```

`aiab monitor` is the session control panel: a single pane with three tabs,
selected from the **Network** / **Domains** / **Mounts** buttons in the header
(or the `1`/`2`/`3` keys).

#### Network tab

The network tab turns the deny-then-rerun loop into a live conversation. It
tails the proxy logs for the directory's containers, and — while the monitor
is running — the proxy **holds** requests for domains in neither list instead
of refusing them: the console rings the terminal bell and prompts for a
decision.

With [textual](https://textual.textualize.io/) installed it is a small UI:
the proxy logs scroll in the middle and each undecided host gets a row of
**Allow / 15m / Deny / Skip** buttons you can click — the mouse works inside
tmux too. The keyboard does the same job: `a`/`t`/`d`/`s` answer for the
oldest prompt, `q` quits. Without textual (or with `--plain`) you get the
line-based prompt with the same keys:

```
==> registry.npmjs.org ? [a/t/d/s]
```

Allow is permanent, 15m lapses after 15 minutes, Deny records a refusal (so
it won't ask again), Skip leaves the request to time out. The parked request
waits up to 60 seconds for the verdict and then proceeds or fails, so the
agent's `npm install` usually just works once you answer — no retry needed.
Without a monitor session attached the proxy keeps the old fail-fast
behaviour.

#### Domains tab

The domains tab is where you revisit decisions already made. It lists every
domain currently allowed or denied for the directory, each as a row with
**Allow / 15m / Deny / ×** buttons: click **Allow** on a denied row to flip it
(or **Deny** on an allowed one), and **×** drops the rule entirely so the host
gets parked and re-prompted next time. A domain input at the bottom allows a
new domain up front, before the agent ever reaches for it. These are the same
records the Network-tab prompts write, so a parked request waiting on a domain
is released the moment you allow it here.

#### Mounts tab

The mounts tab lists the directory's extra mounts (the ones `aiab mount`
records). Each row has the path, a read-only/read-write toggle, and a remove
button; a path input at the bottom (with inline filesystem completion — accept
the ghost suggestion with Tab or →) adds a new one, read-only by default.
Edits go through the same persistent record as `aiab mount`/`aiab unmount`, so
they apply to every agent and survive container recreation, and they take
effect **live** on the running session container (no restart needed). It is
the point-and-click face of `aiab mount`/`aiab unmount`.

#### Launching it

You rarely need to start it by hand: when a directory is restricted and
`tmux` is installed, `aiab run` automatically wraps the session — the agent
in the main pane, `aiab monitor` in a small pane below (inside an existing
tmux session it just splits the current window). The tmux sessions aiab
creates get the tmux `mouse` option switched on, so a click lands on the
monitor pane's buttons even while the agent pane has focus (and clicking a
pane focuses it); in your own tmux sessions aiab leaves the option alone,
so there you may need to focus the monitor pane first. Pass `--no-tmux` to
run bare, and run `aiab monitor` standalone in any terminal if you prefer
your own layout.

### aiab upgrade-templates

```
aiab upgrade-templates [AGENT ...]
```

Updates template containers in place. With no arguments, updates all template
containers that currently exist — including the per-release templates an agent
picks up via [`aiab base`](#aiab-base). Pass one or more agent names to update
only those (still across every release each has a template for).

Each update starts the template container, runs `apt-get update` and
`dist-upgrade`, re-runs the agent installer (which fetches the latest version),
then stops the container again. Session containers cloned afterwards include the
updates; existing session containers are not affected.

### aiab list

```
aiab list [--for DIR]
```

Lists the `aiab` session containers, and for each its working-directory source
mount, any extra mounts (added via `--add-mount`/`--add-mount-rw` or `aiab
mount`), and its network state (see [`aiab net`](#aiab-net)):

```
claude-myproj-ab12cd  [RUNNING]
  source: /home/me/myproj -> /work/myproj
  mount:  /home/me/ref    -> /work/ref (ro)
  network: restricted (2 allowed domains)
opencode-myproj-ef34gh  [STOPPED]
  source: /home/me/myproj -> /work/myproj
  network: open
```

The network line shows the directory's *recorded* policy; if the container
hasn't picked a mode change up yet (that happens when an agent next starts),
it's marked `applies on next run`.

The bare base/template containers are omitted. With `--for DIR`, shows only the
containers for that project directory. For the raw LXD view, use `aiab lxc
list`.

### aiab gc

```
aiab gc
```

Removes every session container whose source directory no longer exists
(stopping it first if necessary), and prunes the recorded mounts, network
policies, and [state dirs](#per-directory-state-and-the-setup-script-aiab) of
deleted directories along with it. The base/template containers are never
touched.

### aiab lxc

```
aiab lxc <args...>
```

Runs `lxc --project aiab <args...>` — a convenience for poking at the containers
directly, e.g. `aiab lxc list` or `aiab lxc exec claude-myproj-abc123 -- bash`.

## Development

The code is type-hinted and kept clean with [black](https://black.readthedocs.io/)
(formatting), [flake8](https://flake8.pycqa.org/) (linting), and
[mypy](https://mypy-lang.org/) (type checking). Tests use
[pytest](https://docs.pytest.org/). All four are apt packages, so no virtualenv
is needed:

```sh
sudo apt install python3-mypy black flake8 python3-pytest
```

A `Makefile` wraps them (configuration lives in `pyproject.toml` and `.flake8`):

```sh
make check         # what CI runs: format-check + lint + typecheck + test
make test          # run the test suite with pytest
make format        # reformat in place with black
make lint          # flake8
make typecheck     # mypy
```

## License

Copyright (C) 2026 Canonical Ltd.

This project is free software, licensed under the GNU General Public License
version 3 (or, at your option, any later version). See the [LICENSE](LICENSE)
file for the full text.
