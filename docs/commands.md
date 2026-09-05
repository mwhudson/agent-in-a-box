# Command reference

Run `aiab run`, `aiab remove`, etc. from inside the project directory you want
the agent to work in (or use `--for DIR` on the commands that accept it).

- [aiab run](#aiab-run)
- [aiab remove](#aiab-remove)
- [aiab mount / aiab unmount](#aiab-mount--aiab-unmount)
- [aiab net](#aiab-net)
- [aiab base](#aiab-base)
- [aiab limits](#aiab-limits)
- [aiab env](#aiab-env)
- [aiab profile](#aiab-profile)
- [aiab opencode](#aiab-opencode)
- [aiab monitor](#aiab-monitor)
- [aiab upgrade-templates](#aiab-upgrade-templates)
- [aiab list](#aiab-list)
- [aiab gc](#aiab-gc)
- [aiab lxc](#aiab-lxc)

## aiab run

```
aiab run <agent> [--for DIR] [--add-mount DIR]... [--add-mount-rw DIR]... [--base RELEASE] [--profile NAME] [--no-git-guard] [--shell] [--no-tmux] [-- AGENT_ARGS...]
```

- `<agent>` — `claude`, `opencode`, or `copilot`.
- `--for DIR` — run the agent for `DIR` instead of the current directory; `DIR`
  is the container's working directory, mounted at `/work/<basename>`.
- `--add-mount DIR` — mount `DIR` **read-only** into the container and record it for this directory (repeatable).
- `--add-mount-rw DIR` — mount `DIR` read-write and record it (repeatable).
- `--base RELEASE` — build/use Ubuntu `RELEASE` (e.g. `22.04` or `jammy`) and
  record it for this directory.
- `--profile NAME` — apply a named profile for this run (see
  [`aiab profile`](#aiab-profile)).
- `--no-git-guard` — don't shadow the repo's `.git/hooks` and `.git/config`
  (see [the git guard](concepts.md#protecting-the-host-repo-the-git-guard)).
- `--shell` — open an interactive shell in the container instead of the agent.
- `--no-tmux` — don't wrap the session in tmux with an `aiab monitor` control
  pane (see [Launching it](#launching-it) under `aiab monitor`).
- Anything after `--` is passed straight through to the agent.

`--add-mount` / `--add-mount-rw` mounts are remembered for the directory (see [`aiab
mount`](#aiab-mount--aiab-unmount) below), so they're re-applied on later runs
and for other agents in the same directory. Mounts recorded for the directory
are re-applied on every run regardless.

The base container is created automatically on first use. Authenticate inside
the container on first run; credentials are stored under
`~/.local/share/aiab/<agent>/home` and reused afterwards.

You can run several agents for one directory at once — they share the session
container, its network policy and its filtering proxy, and the container stays
up until the last one exits. They also share the *one working tree*, so keeping
concurrent agents out of each other's way is the agents' own business: most can
put a session in its own git worktree, and aiab leaves that to them.

To run Claude against [OpenRouter](https://openrouter.ai) instead of the Claude
API, use the built-in `openrouter` profile:

```
aiab run --profile openrouter claude
```

On first use it prompts for your OpenRouter API key and writes it to
`~/.local/share/aiab/claude@openrouter/home/.claude/settings.json`. See
[`aiab profile`](#aiab-profile).

## aiab remove

```
aiab remove <agent> [--for DIR] [--profile NAME]
```

Deletes the session container for the directory (current directory, or `--for
DIR`). The base/template container is left intact, so the next run clones a
fresh one quickly. An isolated profile runs in its own container, so pass
`--profile NAME` to remove that one.

## aiab mount / aiab unmount

```
aiab mount   [--for DIR] [--ro | --rw] DIR [DIR ...]
aiab unmount [--for DIR] DIR [DIR ...]
```

`mount` records each `DIR` as an **extra mount for the project directory** and
adds it to every agent container (`claude`, `opencode`, `copilot`) that
already exists for it. Because the set is recorded (in
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

## aiab net

```
aiab net status   [--for DIR]
aiab net restrict [--for DIR]
aiab net open     [--for DIR]
aiab net allow    [--for DIR | --global] [--agent AGENT] [--duration TIME] DOMAIN...
aiab net deny     [--for DIR | --global] [--agent AGENT] DOMAIN...
```

The interactive console for steering the proxy live is a separate command,
[`aiab monitor`](#aiab-monitor).

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

`--global` records the rule on a single allow/deny list shared by **every**
directory, instead of the current one — handy for the domains you find
yourself allowing in project after project. A directory's own rules take
precedence over the global ones (on an equal-length match the local rule
wins; a longer global rule still beats a shorter local one), and `aiab net
status` prints the global list under its own heading. `--global` can't be
combined with `--for`.

`--agent AGENT` scopes a rule to one agent — handy when only one agent needs a
domain, e.g. an MCP server you've installed for `opencode`:

```
aiab net allow --global --agent opencode mcp.example.com
```

The agent axis is orthogonal to the directory axis, so they combine freely:
`--agent` on its own scopes to the current directory for that agent, add
`--global` to cover every directory, or `--for DIR` for another one. An agent
sees the all-agents rules plus its own; other agents never see agent-scoped
rules. The built-in defaults are themselves the all-agents/per-agent layer of
this system: the apt baseline applies to every agent, and each agent's own
API/auth/telemetry domains (shown by `aiab net status`) are its per-agent
defaults — they are always allowed and can't be denied.

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

## aiab base

```
aiab base [--for DIR]                 # show the directory's base release
aiab base [--for DIR] RELEASE         # set it (e.g. 22.04, jammy or devel)
aiab base [--for DIR] default         # clear back to the default (26.04)
```

By default a directory's containers are built on Ubuntu 26.04. `aiab base
RELEASE` overrides that for one project directory; `RELEASE` is a version
(`22.04`), a codename (`jammy`), or `devel` for the release currently in
development, and `default` clears the override. Like `aiab net` and `aiab
mount`, the choice is persisted per directory (keyed by the resolved path) and
only edits recorded state — no LXD connection needed.

Codenames and `devel` are resolved from the host's `distro-info-data`
(`/usr/share/distro-info/ubuntu.csv`), which every Ubuntu and Debian system
has, so new releases work without an aiab update; a small built-in table
covers hosts that lack it. With no argument the command lists the releases
still in standard support (plus the devel one) from that same data — a
suggestion, not a limit: any release with an `ubuntu-daily:` image can be
used, including ones past EOL. `devel` is resolved when you set it, so what's
recorded is a fixed version (`aiab base devel` today records `26.10`) rather
than an alias that would quietly mean the next release in six months.

Templates are built from the `ubuntu-daily:` remote, whose images are rebuilt
continuously — so a new template has fewer updates to install, and the
in-development release works as a base before it is published to the release
(`ubuntu:`) remote.

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

Every container records the release it was built on (`user.aiab_base`), which
is what that check compares against — not the name, since the default
template's bare name means "the default" and so stops meaning what it did
whenever the default moves. A template whose record disagrees with the release
now being asked for is deleted and rebuilt, and any session cloned from it goes
with it. Containers created before the record existed are dated from their
name: an alternate-base one says its release outright, and a bare-named one
must be from 24.04, the default before 26.04. So the first `aiab run` after
upgrading to a 26.04-default aiab rebuilds each agent's default template —
once, and only for the agents you actually use.

## aiab limits

```
aiab limits [--for DIR]                            # show the directory's limits
aiab limits [--for DIR] [--cpu N] [--memory SIZE]  # set one or both
aiab limits [--for DIR] --reset                    # restore the defaults
```

Shows or sets the CPU and memory limits applied to a directory's session
containers. The defaults are **4 vCPUs** and **8GiB**. `--cpu N` and
`--memory SIZE` (e.g. `8GiB`, `512MiB`) update the limits individually;
`--reset` restores both. Like the other state commands the choice is persisted
per directory and only edits recorded state — no LXD connection needed.

Limits are (re-)applied on every `aiab run`, and LXD applies CPU/memory changes
to a running container immediately, so adjusting them takes effect on the next
run (and a running session picks the new values up the next time it starts).

## aiab env

```
aiab env list  [--for DIR]
aiab env set   [--for DIR] [--agent AGENT] NAME VALUE
aiab env unset [--for DIR] [--agent AGENT] NAME
```

Records environment variables to inject into the agent process when it runs in
a directory. By default a variable applies to **every agent** run there;
`--agent AGENT` scopes it to a single agent, and an agent-specific value
overrides the directory-wide one. Like `aiab mount`/`aiab net`, the records are
persisted per directory (in `~/.local/share/aiab/env.json`) and only edit
recorded state. They take effect the next time an agent starts.

`HOME`, `PATH`, and the network-proxy and Wayland variables are managed by aiab
and stay authoritative — `set` refuses `HOME`/`PATH`, and the rest can't be
overridden by a recorded value.

This is the building block for per-directory credentials. Agents that read their
key straight from the environment can be pointed at a directory-specific key
this way — for example, a different OpenRouter key for `claude` in one
directory:

```
aiab env set --agent claude ANTHROPIC_AUTH_TOKEN sk-or-...
```

opencode is the exception: its stored login (`opencode auth login`) takes
precedence over an environment variable, so a per-directory opencode key goes
through [`aiab opencode config`](#aiab-opencode) instead, which writes a config
file (config outranks the login) and injects the pointer to it via this
mechanism.

## aiab profile

```
aiab profile list
aiab profile show NAME
aiab profile add NAME [--agent AGENT]... [--isolated] [--env NAME=VALUE]... [--allow DOMAIN]... [--description TEXT]
aiab profile remove NAME
```

A **profile** is a named agent execution variant applied with `aiab run
--profile NAME`. It coordinates a small set of runtime concerns that otherwise
need to change together: agent environment, network domains, and optionally the
agent's credential and session identity. It is not a general-purpose manager
for the agent's prompts, config files, mounts, or resource limits.
Those remain the responsibility of the agent or of the corresponding aiab
command.

Unlike everything under [`aiab env`](#aiab-env) or [`aiab
net`](#aiab-net), a profile isn't recorded against a project directory — it is
chosen per run, so the same directory can be used with and without one.

A profile carries:

- `--env NAME=VALUE` — environment variables injected into the agent. A
  variable recorded for the *directory* wins over the profile's, so precedence
  is dir > profile. The `--env` option accepts the environment variables that
  the upstream agent uses for provider, model, or other runtime selection; it
  is not intended to model the agent's complete configuration surface.
- `--allow DOMAIN` — domains always reachable while the directory is in
  restricted mode. These join the agent's own API domains rather than the
  recorded allow list, so a profile that points an agent at a different
  endpoint works without a separate `aiab net allow`.
- `--agent AGENT` — restrict the profile to one or more agents (repeatable).
  Running it against an agent it doesn't list is an error, not a silent no-op.
  Omit it and the profile applies to any agent.
- `--isolated` — give the profile its own **credential store** and **session
  container**, instead of sharing the agent's.

### When to use --isolated

`--isolated` is the difference between a profile that changes an agent's
*settings* and one that forks its *identity*.

Use it when the profile changes who the agent authenticates as — the built-in
`openrouter` profile sets it, so an OpenRouter token never lands in the same
`~/.claude` as a Claude login. Credentials live in
`~/.local/share/aiab/<agent>@<profile>/home`, and sessions run in a container
named `<agent>-<profile>-<dir>`.

Leave it off when the profile only layers settings — a profile that tightens
the network, say. It then reuses the agent's normal credential store and
container, so entering it doesn't mean authenticating again.

Either way the **template container is shared**: a profile changes how an agent
is executed, never how it's *installed*, so there's nothing to build twice.
Agent configuration that is not represented by the profile remains in the
agent's own config files or in commands such as `aiab env` and `aiab opencode
config`.

### The built-in openrouter profile

`openrouter` ships with aiab and points Claude Code at
[OpenRouter](https://openrouter.ai):

```
aiab run --profile openrouter claude
```

It is scoped to `claude`, isolated, allows `openrouter.ai`, and sets the
endpoint plus a `/model` picker entry. On first use it prompts for your API key
and stores it in the profile's own credential store.

It sets `ANTHROPIC_CUSTOM_MODEL_OPTION` rather than `ANTHROPIC_MODEL`
deliberately: an environment variable outranks Claude Code's `model` setting,
but `/model` saves a switch *into* that setting — so setting `ANTHROPIC_MODEL`
would make an in-session model switch silently revert on the next launch. The
custom-model-option variable leaves the setting free and adds an entry to the
`/model` picker, which otherwise lists only Anthropic model names this endpoint
won't accept.

Built-in profiles can't be edited or removed. To vary one, record your own
profile under a different name.

## aiab opencode

```
aiab opencode config [--for DIR]                # show the overlay
aiab opencode config [--for DIR] PATH VALUE     # set a key
aiab opencode config [--for DIR] --unset PATH   # remove a key
```

Manages a per-directory opencode config overlay — for settings you want for
opencode in **one directory only**, most usefully a directory-specific provider
key or model. opencode resolves a provider's key **config > stored login
(`opencode auth login`) > environment variable**, so unlike [`aiab
env`](#aiab-env) a config file *can* override the shared login.

`aiab opencode config` writes a small `opencode.json` into the directory's
[state dir](concepts.md#per-directory-state-and-the-setup-script-aiab) (mounted
at `/aiab`, so it survives container recreation and never lands in your repo)
and points opencode at it by injecting `OPENCODE_CONFIG` for the opencode agent
(via [`aiab env`](#aiab-env)). opencode merges that file *above* its global
config, so the rest of your opencode setup — and the shared login in every other
directory — is untouched.

`PATH` is a dotted key path; `VALUE` is read as JSON when it parses (so
`true`/`false`/numbers work) and as a string otherwise:

```
aiab opencode config provider.openrouter.options.apiKey sk-or-...
aiab opencode config model anthropic/claude-sonnet-4-6
```

With no `PATH` it prints the overlay. `--unset PATH` removes a key, and removing
the last key drops the overlay file and the `OPENCODE_CONFIG` pointer. Changes
take effect the next time opencode starts. In a restricted directory, remember
to [`aiab net allow`](#aiab-net) a new provider's API domain.

## aiab monitor

```
aiab monitor [--for DIR]
```

`aiab monitor` is the session control panel: a single pane with five tabs,
selected from the **Network** / **Domains** / **Mounts** / **Ports** /
**Limits** buttons in the header (or the `1`–`5` keys; `m`, `p`, and `l` also
jump to Mounts, Ports, and Limits).

### Network tab

The network tab turns the deny-then-rerun loop into a live conversation. It
tails the proxy logs for the directory's containers, and — while the monitor
is running — the proxy **holds** requests for domains in neither list instead
of refusing them: the console rings the terminal bell and prompts for a
decision.

The proxy logs scroll in the middle and each undecided host gets a row of
**Allow / 15m / Deny / Skip** buttons you can click — the mouse works inside
tmux too. The keyboard does the same job: `a`/`t`/`d`/`s` answer for the
oldest prompt, `q` quits.

Allow is permanent, 15m lapses after 15 minutes, Deny records a refusal (so
it won't ask again), Skip leaves the request to time out. The parked request
waits up to 60 seconds for the verdict and then proceeds or fails, so the
agent's `npm install` usually just works once you answer — no retry needed.
Without a monitor session attached the proxy keeps the old fail-fast
behaviour.

Sixty seconds is easy to miss in a pane you aren't looking at, so each parked
host is *also* raised as a desktop notification, carrying the same **Allow**
/ **15m** / **Deny** buttons — clicking one there decides the host without
going back to the terminal, and ignoring it is Skip. The notification is
withdrawn once the host is decided in the pane or the request gives up
waiting. If a click lands after that (the notification outlives the proxy's
wait) the rule is still recorded, so the agent's next attempt goes straight
through.

This needs `notify-send` on your `PATH` — `apt install libnotify-bin`.
Without it, or without a notification daemon to talk to, nothing is raised and
the pane and the bell are the whole UI, exactly as before.

### Waiting agents

The monitor also notices when the *agent* is waiting on you — its turn ended,
or it stopped to ask something — and says so on the desktop once the wait has
gone on for 15 seconds. Answering the agent (or the session ending) withdraws
the notification. The log line in the Network tab records it too.

This is Claude only, and it works by hooks: `aiab run` writes a Claude Code
managed-settings drop-in into the session container which records "waiting
since" into the directory's state dir (mounted at `/aiab`), and the monitor —
which is on the host — reads it from there. The host's session bus is
deliberately *not* passed into the container; one file crossing the boundary
is the whole channel.

Managed settings are a separate source from `~/.claude/settings.json`, and
hooks from different sources are concatenated rather than overridden, so these
hooks neither displace your own nor can be displaced by them. `aiab` claims
one drop-in file, `50-aiab-attention.json`, not
`/etc/claude-code/managed-settings.json` itself.

The 15 seconds is aiab's own, not Claude Code's idle threshold — the hooks
only record *when* the wait started, and the monitor decides when that has
gone on long enough. Note that "you noticed" means "you sent a prompt": if you
are reading the output for 20 seconds before replying, you get a notification
anyway.

### Domains tab

The domains tab is where you revisit decisions already made. It lists every
domain currently allowed or denied for the directory, each as a row with
**Allow / 15m / Deny / ×** buttons: click **Allow** on a denied row to flip it
(or **Deny** on an allowed one), and **×** drops the rule entirely so the host
gets parked and re-prompted next time. A domain input at the bottom allows a
new domain up front, before the agent ever reaches for it. These are the same
records the Network-tab prompts write, so a parked request waiting on a domain
is released the moment you allow it here.

### Mounts tab

The mounts tab lists the directory's extra mounts (the ones `aiab mount`
records). Each row has the path, a read-only/read-write toggle, and a remove
button; a path input at the bottom (with inline filesystem completion — accept
the ghost suggestion with Tab or →) adds a new one, read-only by default.
Edits go through the same persistent record as `aiab mount`/`aiab unmount`, so
they apply to every agent and survive container recreation, and they take
effect **live** on the running session container (no restart needed). It is
the point-and-click face of `aiab mount`/`aiab unmount`.

### Ports tab

The ports tab lists the TCP ports the session container is listening on (above
a low threshold, and excluding aiab's own proxy port) and offers to forward each
to the host — handy when the agent starts a dev server inside the container and
you want to hit it from your browser. Each port is a row you can forward or
drop.

### Limits tab

The limits tab shows the directory's CPU and memory limits, each editable inline
with a **Set** button. Changes are saved to the same record as
[`aiab limits`](#aiab-limits) and take effect on the next `aiab run` for the
directory.

### Launching it

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

The session it creates is named `aiab-<container>`, so a second `aiab run` for
the same directory finds the first instead of starting an unrelated session. It
joins as a tmux **session group**: one shared window list with a window per
agent, but each terminal keeps its own current window. So both terminals can
see every agent running for the directory and switch between them (`C-b w`)
without moving the other terminal's view. When your agent exits, only your
session goes away — the other terminals and their agents carry on.

## aiab upgrade-templates

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

## aiab list

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

## aiab gc

```
aiab gc
```

Removes every session container whose source directory no longer exists
(stopping it first if necessary), and prunes the recorded mounts, network
policies, base/limits/env records, and [state
dirs](concepts.md#per-directory-state-and-the-setup-script-aiab) of deleted
directories along with it. The base/template containers are never touched.

## aiab lxc

```
aiab lxc <args...>
```

Runs `lxc --project aiab <args...>` — a convenience for poking at the containers
directly, e.g. `aiab lxc list` or `aiab lxc exec claude-myproj-abc123 -- bash`.
