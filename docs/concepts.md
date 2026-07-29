# Concepts

How `aiab` works under the hood and the safety model it gives you.

- [Why not a devcontainer, or a process sandbox?](#why-not-a-devcontainer-or-a-process-sandbox)
- [How it works](#how-it-works)
- [Per-directory state and the setup script (`/aiab`)](#per-directory-state-and-the-setup-script-aiab)
- [Protecting the host repo (the git guard)](#protecting-the-host-repo-the-git-guard)

## Why not a devcontainer, or a process sandbox?

The point of `aiab` is that it needs **nothing in the repo**. Run `aiab run`
in any checkout and it works — no `.devcontainer/`, no Dockerfile, no
committed config of any kind. That's the whole reason it exists: I work across
a lot of repositories, and most of them don't carry any agent or sandbox setup
(yet). If a project *does* ship a devcontainer (or similar), you'd just use
that; `aiab` is for the long tail that ships nothing.

It also wraps the agent from the *outside* — the agent process itself runs
inside the container, so it can only see the directories you've mounted and
can only reach the domains you've allowed, whether or not it cooperates. That's
what makes it safe to disable permission prompts, and it's why the same sandbox
applies equally to Claude, opencode, and Copilot CLI.

Devcontainers aren't really the live comparison any more, though. Agents have
started shipping their own isolation — Claude Code has a sandboxed Bash tool
(Seatbelt on macOS, bubblewrap on Linux) and an experimental
`@anthropic-ai/sandbox-runtime` that wraps the whole process, and third-party
wrappers around bubblewrap do the same for several agents at once. Those are
lighter than a container and worth using: if you run one agent, on a machine
you already trust, and don't need anything installed, they're less machinery
than this. The differences that stay:

- **What the agent can see.** `aiab` mounts the directories you name and
  nothing else, so the visible filesystem is an allowlist. A process sandbox
  starts from your whole host filesystem and subtracts from it —
  `sandbox-runtime` today denies writes and network by default but allows
  *reads* everywhere, so `~/.ssh`, your other checkouts and anything else on
  disk stay readable until you enumerate them. Listing the secrets is the
  wrong shape for the problem; forgetting one is silent.
- **Whose credentials are at stake.** Each agent gets its own home under
  `~/.local/share/aiab/<agent>/home`, so what's reachable in a session is a
  login kept for that purpose. Wrapping the host's binary means the host's
  config and credentials instead, writable — including the agent's own
  settings file, which is somewhere hooks can live.
- **How the network is enforced.** Both filter egress through a proxy, but
  here the container's NIC is masked, so a tool that ignores `HTTP_PROXY` has
  no route at all rather than a way around the filter.
- **A whole operating system.** You can `apt install`, run Docker, and keep a
  real dev environment that
  [`/setup-container`](#per-directory-state-and-the-setup-script-aiab)
  rebuilds from scratch.
- **One mechanism for every agent**, configured the same way, rather than a
  per-agent feature that only exists for the agents that ship one.

They also compose: nothing stops you running an agent's own sandbox *inside*
an `aiab` container if you want a second layer.

## How it works

The first time you run `aiab run claude` it creates a **base container** from
`ubuntu-daily:24.04` (or whatever release the directory is set to — see
[`aiab base`](commands.md#aiab-base)), installs the agent into it, then stops it as a
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

The `/setup-container` slash command (shipped from this repo for Claude and
opencode; shipped as a custom agent for Copilot, see
[Configuration](configuration.md)) maintains
`/aiab/setup.sh`: when the script doesn't exist it works out the toolchain and
dependency installs from the project's own docs and writes them there; when
it does — notably in a freshly recreated
container — it shows the saved script and offers to run it. Either way it only
runs the script after you confirm in the session, so recreating a container's
dev environment is `/setup-container` plus a "yes". Since the file lives on
the host you can also inspect or edit it from outside the container (each
state dir's `.source` file records the project directory it belongs to).

[`aiab gc`](commands.md#aiab-gc) removes a directory's state dir (setup script
included) along with its other records once the directory itself is gone.

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
agent wandering, not against a deliberate exploit. The shadows are bind mounts
over the real `.git` — which is inside the mounted work dir — so `sudo umount
.git/hooks` in the container exposes the host's real hooks dir, read-write. No
kernel bug needed; it just takes deliberately reaching for it. What the kernel
*does* rule out is the unprivileged version: a process that isn't root in the
container can't detach the shadow, because mounts inherited through a user
namespace are locked together and `umount` fails with `EINVAL`.

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
