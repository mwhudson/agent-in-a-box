# IDEAS

## Scope

aiab's core job is to run an otherwise ordinary coding agent behind an external
security and lifecycle boundary: filesystem visibility, network egress, host
repository protection, container lifecycle, and the persistent state needed to
make that boundary usable. The agent's own prompts, models, commands, and
configuration systems should remain upstream-owned rather than becoming an
aiab configuration manager.

The boundary is not limited to isolation-only features. Worktrees, multiplexing,
instruction overlays, and per-directory environment settings can be useful
parity features, but they should be kept visibly separate from the sandbox
itself. The test for a proposed feature is: *does aiab need to provide this
because the agent is running in a box, or is it merely an opinionated way to
configure the agent?*

- **Worktrees** — assessed as harness, and since *removed* (404ed35 and the
  docs follow-up): they solved a real problem (two agents, one checkout) with
  nothing to do with isolation, and one every agent now ships a solution for.
  Concurrency is the agents' own business (commands.md says so under `aiab
  run`); kept here as the worked example of the test cutting something real.
- **tmux multiplexing.** Seven helpers in `cli.py` (`_tmux_group`,
  `_tmux_session_name`, `_tmux_sessions`, `_tmux_group_member`,
  `_tmux_window_commands`, `_tmux_joined_nothing`, `_reexec_under_tmux`),
  plus `_monitor_pane`. Split this one carefully: tmux existing *at all* is box
  work, because the monitor pane is how a network decision gets a live surface.
  The session group with a window per agent, shared across terminals and
  switched with `C-b w`, is not — that's multi-agent UX anyone would want bare.
- **The Agents pane**, plus `_resolve_shared_tree` and
  `AIAB_CONCURRENT_DECISION`: an explicit control plane for parallel agents.
  Built on a branch, never landed, branch since deleted — it duplicated
  something Claude Code is actively building, and was rooted in
  `--worktree-branch` besides.
- **`aiab profile`.** Profiles are a narrow execution-variant layer rather than
  a general configuration manager. They coordinate agent-scoped environment,
  additional network domains, and optional credential/session isolation. The
  profile may point Claude at OpenRouter, for example, because the endpoint,
  network allowlist, and credential namespace must agree. The agent's broader
  configuration remains upstream-owned. `--isolated` is the identity boundary:
  it gives the variant its own credential store and session container.
- **`aiab opencode config`** — a per-directory opencode settings editor whose
  entire justification is opencode's config > auth.json > env precedence. Agent
  configuration management, with no confinement content at all.
- **The shipped instruction overlays and `/repo-role`.** Of
  `agent-config/claude/CLAUDE.md`, one section ("Look in `/work`") is about the
  container; "Verify before you change", "When you're unsure", "Scope" and
  "Committing" are personal working preferences, and `/repo-role` is a per-repo
  latitude taxonomy. Same for the opencode and Copilot overlays. So a sandboxing
  tool versions one person's agent-behaviour opinions, and anyone installing it
  for the container gets them too. There *is* a parity argument for something
  being there — the container home is fresh, where on the host `~/.claude` would
  already exist — but the parity-honest version is to bring the user's own
  config in, not to ship an opinionated one from this repo. The two have drifted
  into the same mechanism.
- **`aiab env`** — half and half. "The container doesn't inherit your shell" is
  box work; "record variables per directory" is a config manager.

Passing the test despite looking like extras, and worth naming so a later cut
doesn't sweep them up: the persisted per-agent home (otherwise you authenticate
every run), `/setup-container` and the `/aiab` state dir (on the host your
machine already has the toolchain — its *form* reaches into the agent's surface,
but its reason is the box), port forwarding (a dev server inside the container
is otherwise unreachable), Wayland passthrough, the monitor's
Network/Domains/Mounts/Limits tabs, `upgrade-templates`/`gc`/`list`/`lxc`, and
not reaping a container out from under a `/background` session (box work by
definition — nothing the agent can defend itself against).

So the line isn't "isolation only" — that parity work is legitimately aiab's.
It's that **aiab's job is what the agent can't do for itself because aiab put it
in a box.** Everything else is dotfiles, and dotfiles are better mounted in than
maintained here. It matters more than tidiness: the harness half is exactly
where upstream moves fastest (worktrees already, agent view and agent teams for
multiplexing, and every agent ships its own config system), while the sandbox
half still has no upstream competitor — no agent confines itself from the
outside, and of the ~100 tools in `awesome-agent-orchestrators` only a handful
combine worktrees with containers at all.

One consequence worth acting on rather than just recording:

- The instruction overlay should change shape — and the shape is worked out
  (2026-08-17). The property worth keeping — versioned files, bind-mounted into
  every session's home, editable from either side, tracked by git — is the
  *mechanism*, and the mechanism is box work: it exists because the container
  home is fresh and a symlink into a checkout would dangle in-container
  (configuration.md explains why). The *content* is dotfiles. So the content
  moves to a personal dotfiles repo, and aiab keeps the mechanism plus a
  convention for finding the user's content:
  - *A stow-style user overlay dir.* `~/.config/aiab/agent-config/<agent>/`,
    laid out as a mirror of the container home
    (`claude/.claude/CLAUDE.md`, `claude/.claude/commands/repo-role.md`,
    `opencode/.config/opencode/AGENTS.md`, ...). At run time, walk the agent's
    dir and overlay each entry onto `$HOME/<relative path>` — the machinery
    already exists (`Container.add_config_overlay` is idempotent;
    `_apply_session_mounts` already loops `cfg.overlays`), and the layout
    itself expresses the destinations, so no per-agent path table, no new
    verb, no new state file. The dir can be a symlink into a dotfiles
    checkout: it resolves on the *host* before the path reaches LXD (resolve
    it explicitly when scanning), so the dangling-symlink problem doesn't
    apply. Order user overlays after the shipped ones, so the user's files win
    a collision.
  - *Rejected:* an `aiab overlay add HOST CONTAINER` verb (a new command plus
    a seventh state file to do what a convention dir does for free), and
    adding `.claude/CLAUDE.md` to `shared_paths` and hand-managing the shared
    home (loses the git checkout, and a symlink inside the shared home dangles
    in-container for exactly the reason above).
  - *What aiab keeps shipping:* `/setup-container` for all three agents (its
    reason is the box, per the list above) and the sandbox-orientation text —
    roughly the "Look in `/work`" paragraph, eventually the generated
    per-session file from "Tell the agent it's in a sandbox".
  - *The wrinkle: who owns `~/.claude/CLAUDE.md`.* Claude reads one global
    file, and after the split both the user and aiab have something to say.
    In preference order: (1) mount aiab's part at `/work/CLAUDE.md` — Claude
    Code collects CLAUDE.md from directories above the cwd, and the session
    cwd is `/work/<basename>`, so it reaches every session without touching
    the repo (the file is a mount, so "nothing in the repo" holds) or the
    user's global file; verify the parent-dir walk actually fires there, and
    expect it to be claude-only (opencode/copilot parent-walk behaviour
    unverified). (2) Concatenate per run — user file + aiab's section —
    into the dirstate and mount that; the same generation step "Tell the
    agent it's in a sandbox" wants anyway, at the cost that the mounted file
    is no longer the user's editable source. (3) A "read `/aiab/sandbox.md`"
    line in the user's own file — works, but couples the dotfiles to aiab,
    which is the dependency direction being removed.
  - *Migration order:* land the user-dir scan alongside the repo overlays;
    move the content out and symlink; only then trim `agent-config/` to
    `/setup-container` plus the sandbox note and rewrite configuration.md's
    overlay sections around the user dir. Steps one and two land without step
    three, so nothing breaks mid-move. One consequence to accept out loud:
    fresh installs stop getting opinionated instructions — which is the point
    — so configuration.md then needs a worked example of the convention dir,
    it being the documented way to get *any* global instructions in.

## Lifecycle gaps

- **`aiab refresh`** — after `upgrade-templates`, existing session containers
  stay stale forever (`docs/commands.md` just notes this). A command that
  recreates a session container from the updated template would be cheap to
  build, since recorded mounts are already replayed automatically on recreation
  and /setup-container restores the dev environment from its persisted script.
- **Snapshot/reset** — `lxc snapshot` before a risky run, `aiab reset` to roll
  the session container back. Useful when an agent has installed packages or
  mutated container state you want to keep most of the time.

## Security And Usability

- **Git guard: follow `core.hooksPath`** — `_guard_git_repo` (`cli.py:158`)
  hardcodes `.git/hooks`, but that's only where git looks by default. Husky
  (and anything else setting `core.hooksPath`) relocates the hook dir into the
  worktree, typically `.husky/_`. The agent can't *set* the key — `.git/config`
  is shadowed read-only — but in a repo where it's *already* set, the container
  reads that config, so the effective hook dir is a path in the mounted
  worktree that the guard doesn't shadow and the agent can write. Host `git
  commit` then runs it. That's the same trigger the guard already exists to
  block, reached by a path it doesn't follow, so closing it is finishing the
  stated guarantee rather than widening it. Bounded, too: a `core.hooksPath`
  pointing *outside* the worktree (`~/.githooks`) isn't mounted, so the agent
  can't write it — only in-worktree targets need anything.
  Two things make this less clear-cut than the `.git/hooks` case, and worth
  settling before building it:
  - *A planted husky hook is visible.* `.husky/` is tracked, so it shows up in
    `git status`/`git diff`, unlike `.git/hooks` which git never reports. The
    exposure is only the window where you commit without reading every changed
    file — real, but much weaker than the invisible case.
  - *Shadowing it would break legitimate work.* `.git/hooks` isn't source
    controlled, so a read-write sidecar costs nothing; `.husky/` is part of the
    project, and "add a pre-commit hook" is an ordinary task whose result is
    supposed to land in the tree. Shadowing would silently discard it. So the
    fix may be to warn rather than shadow.
  Explicitly *not* doing the rest of the "protect config files" list that
  code-on-incus ships (`.vscode`, `.claude/settings.json`, …). Those either
  fire only when you run something that obviously executes project code — you
  own that, and you reviewed the diff first — or defend against an agent
  weakening its own sandbox, which is a threat aiab doesn't have because the
  confinement is external and an agent editing its own settings gains nothing.
  What's left is host-config-dependent (it matters only if you run agents or
  open editors on the host), so it belongs in an opt-in list, if anywhere, not
  in the default guard.
- **Git guard: the `sudo umount` bypass (known, accepted)** — the shadows are
  bind mounts layered over the real `.git`, which lives inside the mounted work
  dir, so container root can `umount /work/<proj>/.git/hooks` and get the
  host's real hooks dir back, read-write. Recorded here because it's worth not
  rediscovering, and because the obvious fixes all turn out worse than the
  hole:
  - *Unprivileged processes already can't do it.* `unshare -Urm` then `umount`
    fails with `EINVAL` — mounts inherited through a user namespace are locked
    together and can't be detached individually. So this needs deliberate use
    of the container's passwordless sudo; nothing an agent stumbles into.
  - *Layering doesn't help.* Constructing `.git` from separately-mounted pieces
    (objects/refs/HEAD/index in, hooks/config sidecar-only) sounds like it
    removes what's underneath, but it doesn't: the work dir mount's source is
    the host repo, so the real `.git` is inherently in the tree. Umounting the
    pieces reveals an empty dir, umounting *that* reveals the real `.git`. It
    adds steps, not a ceiling. It only works if the mount source genuinely
    lacks `.git`, which LXD disk devices can't express (no exclusions) — you'd
    need a host-side prepared view (overlayfs / bind farm), and that wants host
    root, costing the "needs no privilege" property that makes `aiab run`
    cheap.
  - *Dropping sudo in the container would close it* — that's the only real
    lever, since the kernel already handles the unprivileged case — but
    passwordless sudo is far too useful (`/setup-container` installs
    toolchains with it) to trade away for a threat outside the stated model.
    Decided against.
  - *`chattr +i` on the host paths* (what code-on-incus does) is the one thing
    that would defend even against container root: the immutable flag can't be
    cleared or bypassed from the container, since that check lands in the
    initial user namespace. But it needs host root on every run, it makes the
    paths immutable *for the host user too* while a session is open (so
    host-side `git config`/`git remote add` fail), on a directory it only
    blocks create/delete/rename rather than edits to existing files, and a
    crash leaves flags set that need root to clear. Not worth it here.
  So: accepted, and `concepts.md` now says so plainly rather than claiming it
  would take a kernel escape. Worth revisiting only if aiab ever grows a mode
  aimed at deliberately hostile code rather than a wandering agent — that mode
  would want the no-sudo container, and the guard question answers itself.
- **A FUSE view of the work dir, for per-operation policy** — mount the
  project directory through a host-side FUSE daemon instead of binding it in
  directly, so every filesystem operation passes a decision point. It works
  mechanically: a disk device's source can be a FUSE mountpoint as easily as a
  real dir (`lxd.py:543`), `fusermount3` needs no root, and because the
  container's uids are mapped with `raw.idmap` (`provision.py:168`) there's no
  shifting to do, so idmapped-mounts-over-FUSE never comes up. The daemon has
  to run on the *host* — inside the container it's bypassable and, worse,
  umountable — supervised per session container the way the netproxy already
  is. Two wrinkles: `allow_other` is mandatory, so a one-time
  `user_allow_other` in the host's `/etc/fuse.conf`, not for the agent's sake
  but because LXD binds the source as root and container root (`sudo apt` from
  `/setup-container`) maps outside the daemon's owner uid; and LXD binds at
  device-add time, so a daemon that dies mid-session leaves the container
  seeing ESTALE until the device is re-added. Worth splitting by what the
  policy actually is, because only one tier needs any of this:
  - *Path allow/deny* — "hide `.env`", "`docs/` read-only" — doesn't need
    FUSE. Stack more disk devices: mount an empty file, or a read-only copy,
    over the path to be hidden. That's the git guard's existing trick
    generalized, at native speed, and it covers the common case today.
  - *Write isolation, or a review gate* where the agent's writes land in an
    upper layer to be accepted or discarded afterwards, is overlayfs's job,
    and the entry above already concluded that wants host root. FUSE would do
    it unprivileged, but for tracked files git is the backstop, which is what
    `concepts.md` already says.
  - *Per-operation decisions and audit* — "what did this agent read?", or park
    a write outside the worktree and ask — is the tier with no cheaper
    substitute, and the real argument for building it: it's the netproxy's
    architecture on a second axis. One `evaluate()`, denials tailed by `aiab
    monitor`, and the same structured audit log wanted for the network below.
  It would also close the `sudo umount` hole above, for exactly the reason
  that entry says layering can't: the mount source genuinely lacks the real
  `.git`, so umounting a shadow reveals whatever the view synthesizes rather
  than the host's hooks dir. Against it, mostly performance, shaped badly for
  this workload. Bulk I/O can be near-native (kernel passthrough on 6.9+, or
  writeback caching), but every uncached lookup/getattr is a round trip, and
  agent sessions are metadata storms — `git status` on a big repo, ripgrep
  over a tree, `npm ci`, a compiler stat'ing headers. Expect something like
  2–5× on tree walks and worse on many small files. Generous
  `entry_timeout`/`attr_timeout` buys most of that back but trades directly
  against enforcement: a cached dentry means a policy change doesn't bite
  until it's invalidated. Separately, inotify doesn't propagate host to
  container, so file watchers, `git fsmonitor` and dev servers get flaky. And
  the policy has to be path-based — `fuse_req_ctx` reports a pid in the
  container's pid namespace, so translating it to rule on *who* is asking is
  racy. So if built: passthrough plus an audit log first, no denials, opt-in
  per directory (`aiab mount --fuse`, or a profile) so the cost lands only
  where it's worth paying. Measure `git status` and a real build against the
  direct mount before adding any policy on top; if the tax turns out too high,
  the audit log is still the part nothing else provides.
- **Port forwarding** — `aiab run --publish 8000` via an LXD proxy device and
  persisted to state. Port detection and interactive forwarding already work in
  the monitor's Ports tab, but there's no CLI flag and forwarding isn't
  persisted across sessions.
- **Agent-created worktrees vs host-side `git worktree prune`** — the one
  fact that outlives the removed worktree feature (the rest of the old
  "worktree follow-through" entry died with it). Agents now make their own
  worktrees inside the container — Claude Code puts sessions under
  `.claude/worktrees/` in the repo — and those record their admin `gitdir` as
  a container path (`/work/<basename>/...`), which doesn't resolve on the
  host. So a host-side `git worktree prune`, or the one `git gc` runs for
  you, decides the worktree is stale and drops its registration. Verified
  against aiab's own worktrees before the removal, and the mechanism (an
  absolute container path in git's admin files) is identical: the directory
  and any branch survive, but git forgets the directory is a worktree, and
  re-entering it is awkward (`worktree add --force` might adopt it but could
  equally clobber uncommitted work; re-registering by hand means writing
  git's admin files yourself). Nothing to build — just worth knowing when a
  container-made worktree quietly vanishes from `git worktree list` on the
  host.
- **Bypass a split-tunnel VPN for container egress** — when the host is on a
  non-full-tunnel VPN, the host proxy's outbound `create_connection`
  (`aiab/netproxy.py`) follows the host routing table, so the container can
  reach VPN-routed resources via the tunnel. When those resources also have a
  non-VPN path, you may want the container's traffic to take the physical link
  instead. The lever is the proxy process's routing, not the container (the
  container has no independent egress) and not DNS (routing, not name→IP,
  decides the path). Two near-equivalent framings:
  - *Source-based policy routing.* One-time host setup (needs root, but only
    once): a lean routing table holding just the physical default route, plus
    `ip rule add from <physical-ip> lookup <table>`. Then the proxy binds its
    outbound sockets to `<physical-ip>` — `create_connection` at
    `netproxy.py:300` already takes `source_address=`, and binding a local IP
    is unprivileged, so the recurring half lives in aiab with no privilege.
  - *Always egress via the default gateway.* Same lean table, but don't be
    selective — route all proxy egress out the physical default route. Since
    that's the same gateway main uses for non-VPN destinations, they behave
    identically; only the VPN's more-specific routes get bypassed.
  Either way aiab grows a per-dir setting (proxy source IP / egress interface)
  that feeds `source_address`; the only root/one-time bits are the table, the
  rule, and a stable physical IP. Cheaper alternative if the VPN resources are
  tunnel-*only* (no other path): just deny them by CIDR in the proxy, since
  route-around and block are then the same outcome — but `evaluate()`
  (`netproxy.py:98`) matches hostnames only, so CIDR/IP deny would be new.
- **IDE integration** — aiab's value (confined agent process, network lockdown,
  git guard, "safe to disable permission prompts") is about *where the agent
  runs*, not the terminal front-end, so it carries over to IDE users. Three
  paths, in increasing effort and isolation:
  - *Integrated terminal (works today, zero changes)* — run `aiab run claude`
    in the IDE's terminal. Edits land through the mounted dir; every guarantee
    holds. You lose the deep extension UX (inline diff approval, "open this
    file", selection-as-context) since that needs a host↔container channel.
  - *Host extension driving a containerized agent (best fit for the thesis)* —
    the Claude/Copilot IDE extension normally launches the agent on the host and
    talks over a discovered socket/lockfile; to keep the sandbox it'd launch the
    agent via `lxc exec` inside the container and bridge that socket across the
    boundary. Stays true to "nothing in the repo, wrap from the outside," but
    needs a bridge built and leaves the host IDE side (extensions, language
    servers) unconfined.
  - *Editor backend in the container (cleanest, but devcontainer-shaped)* —
    Remote-SSH / JetBrains Gateway into the aiab container so editor + extension
    are confined too. Composes with the per-dir container we already build, but
    pulls toward the devcontainer world concepts.md positions against, and since
    LXD isn't Docker you'd need sshd + reachability and Remote-SSH (not "attach
    to running container").
- **Let a container push to one fixed repo, with a per-directory PAT** —
  there's no git-remote credential story today: the agent can commit locally
  but nothing lets it push. The tempting designs are all too broad (mounting
  `~/.ssh`, forwarding the agent, a general-purpose token in `aiab env`) or
  too heavy (a host-side broker minting short-lived tokens on demand, as
  Claude Code on the web does).
  The heavy version isn't needed, and the reason is worth writing down: a
  token scoped to *one repo* is nearly free, because the agent already has
  read-write access to that same repo's working tree. The worst it can do with
  the token is push bad code to a repo it can already write bad code into. The
  real deltas are narrow — it can push without you reviewing first, and
  depending on permissions touch other branches or rewrite history — and both
  are bounded by one repo. On-demand minting only pays for itself when the
  credential would otherwise be broad, which is exactly what scoping removes.
  So: a GitHub fine-grained PAT scoped to one repo, stored per directory on
  the host (same shape as the existing per-dir secrets, 0600), with the
  directory's push target recorded alongside it. Details that decide the
  implementation:
  - *Don't inject it as an env var.* `aiab env` is the wrong vehicle —
    environment is visible to every process in the container and trivially
    exfiltrated. Mount it read-only and point `credential.helper store
    --file=` at it, so it's reachable by git rather than ambient.
  - *The URL rewrite is the bit that bites.* Remotes are usually ssh
    (`git@github.com:…`) and a PAT only authenticates over HTTPS, so the
    container needs `url."https://github.com/".insteadOf = "git@github.com:"`.
    That has to live in the container's *global* git config, which aiab
    provisions — it can't go in `.git/config`, which the git guard mounts
    read-only. Good side effect: the rewrite exists only inside the container,
    so the host's remote is untouched.
  - *Egress.* `restrict` mode needs `github.com` allowed for the directory.
    It's a CONNECT tunnel, so the proxy authorizes but can't inspect.
  - *Expiry.* Fine-grained PATs expire, so this needs re-setting periodically.
    That's the one thing a GitHub App broker would fix, at a cost that isn't
    worth paying here. Org-owned repos may also require an owner to approve
    the token before it works at all.
  The alternative worth keeping in mind is **having the host do the push** —
  the container asks over a socket, the host runs `git push` in the mounted
  work dir (the commits are already there; nothing to transfer). No token
  anywhere, works with ssh remotes and any forge, nothing to expire. With a
  touch-required hardware key it also gets an unforgeable confirmation step
  for free: the touch *is* the approval, with no prompt to build. It costs a
  transport shim so the agent's `git push` doesn't just fail — either a `git`
  wrapper intercepting one verb, or a proper `git-remote-aiab` helper (git's
  documented extension point, which would make fetch work too, but means
  implementing the remote-helper protocol). Prefer this if the PAT route is
  blocked by org policy, or if unreviewed pushes turn out to be the thing
  worth preventing.
  Not doing:
  [sandbox-claude](https://github.com/pvillega/sandbox-claude)'s per-container
  ed25519 deploy keys registered via `gh`. Key material inside the boundary,
  needs admin on the repo, GitHub-only, and needs a manual "add this pubkey"
  fallback anyway — a scoped PAT is less machinery for a better result.
  Noted because deploy keys are the more obvious design and it'd be easy to
  reach for them first.
- **A structured audit log of network decisions** — the proxy already sees and
  rules on every request and logs denials to stderr, which `aiab run`
  redirects to a per-container file under `PROXY_DIR`
  (`netproxy.py`), and `aiab monitor` tails. That's live-only and
  unstructured: once the session is gone there's no answering "what did this
  agent try to reach, and when?". Writing one JSON line per decision
  (timestamp, host, allow/deny, which rule matched, parked-then-approved or
  not) into the directory's state dir would make it durable and greppable,
  and give the monitor something better to render than log text. Cheap,
  because the decision point is already a single place (`evaluate()`).
  Prior art: code-on-incus ships a JSONL audit log and a `coi audit` stream.
- **Tell the agent it's in a sandbox** — nothing currently explains the
  confinement to the agent. The shipped `agent-config/claude/CLAUDE.md` /
  `agent-config/opencode/AGENTS.md` overlays are static and say nothing about
  the network policy, so a denied request surfaces as an opaque 403 and the
  agent either flails or invents a reason. A generated per-session file —
  network mode and the current allow/deny lists, what's mounted and
  read-write vs read-only,
  that `.git/config` is deliberately read-only, and above all *that the user
  can allow a domain with `aiab net allow` if asked* — turns "mysterious
  failure" into "ask for what I need". The overlay machinery (`agents.py`
  `overlays`) already puts files in the container home, but these overlays are
  versioned repo files; this one is generated per run, so it wants either a
  second generated-overlay path or to be written into the dirstate dir and
  referenced. Keep it short and factual: it competes for context with the
  actual instructions. Prior art: code-on-incus auto-injects a
  `SANDBOX_CONTEXT.md` into each tool's native context system.

## Deferred / Small

- **Let the agent's own sandbox work inside the container** — agents now ship
  their own isolation (Claude Code's `/sandbox`, bubblewrap-based on Linux),
  and it composes with ours as a second layer for anyone who wants it. The
  docs note unprivileged containers need a nested-sandbox setting before it'll
  work, so this is probably one setting in the templates plus a line in the
  docs, not a feature.
- More agents in the registry (gemini-cli, codex, aider) — the dataclass
  design makes each one a single entry.
- A `--name`/session-suffix option for running two agents of the same kind in
  one directory with separate containers. Mostly delivered by isolated
  profiles, which already fork both halves (`profiles.container_prefix` and
  `profiles.home_key`); what's left is only the ad-hoc case, where you want a
  second container without naming a profile for it first.
- **`aiab doctor`** — check LXD init state and idmap support; first-run
  failures there are probably the worst onboarding experience.

## Upstream facts worth not rediscovering

Found while working out where each agent keeps its state, and true of the
agents rather than of aiab — so they outlive whatever prompted them.

- **Claude Code supports a custom launcher** at `~/.local/bin/claude` since
  v2.1.207: auto-update leaves it alone and installs under `versions/`. That's
  the fallback if the apt package (`agents._claude_install`) ever stops being
  viable. Downside: it then keeps every version on disk forever, which is what
  the apt switch was getting away from.
- **`COPILOT_HOME` relocates copilot's whole config directory.** There is no
  documented equivalent for claude (`CLAUDE_CONFIG_DIR` is not in the settings
  docs) or for opencode (no documented XDG support) — which is why
  `Agent.shared_paths` layers mounts over one home instead of just pointing
  each agent at a different one.
