# IDEAS

## Lifecycle gaps

- **`aiab refresh`** — after `upgrade-templates`, existing session containers
  stay stale forever (the README just notes this). A command that recreates a
  session container from the updated template would be cheap to build, since
  recorded mounts are already replayed automatically on recreation and
  /setup-container restores the dev environment from its persisted script.
- **Snapshot/reset** — `lxc snapshot` before a risky run, `aiab reset` to roll
  the session container back. Useful when an agent has installed packages or
  mutated container state you want to keep most of the time.

## Project usability

- **Git guard: follow `core.hooksPath`** — `_guard_git_repo` (`cli.py:419`)
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
- **Port forwarding** — `aiab run --publish 8000` via an LXD proxy device and
  persisted to state. Port detection and interactive forwarding already work in
  the monitor's Ports tab, but there's no CLI flag and forwarding isn't
  persisted across sessions.
- **Worktree follow-through** — `--worktree-keep` leaves a detached worktree
  buried in `.git/aiab-worktrees/<timestamp>`, but there's no command to list
  those worktrees, diff them, or pull the result out into a branch. Something
  like `aiab worktrees list`/`adopt` would close the loop, especially when
  running parallel sessions.
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
    `netproxy.py:266` already takes `source_address=`, and binding a local IP
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
  (`netproxy.py:96`) matches hostnames only, so CIDR/IP deny would be new.
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
  agent either flails or invents a reason. A generated per-session file — network mode and
  the current allow/deny lists, what's mounted and read-write vs read-only,
  that `.git/config` is deliberately read-only, and above all *that the user
  can allow a domain with `aiab net allow` if asked* — turns "mysterious
  failure" into "ask for what I need". The overlay machinery (`agents.py`
  `overlays`) already puts files in the container home, but these overlays are
  versioned repo files; this one is generated per run, so it wants either a
  second generated-overlay path or to be written into the dirstate dir and
  referenced. Keep it short and factual: it competes for context with the
  actual instructions. Prior art: code-on-incus auto-injects a
  `SANDBOX_CONTEXT.md` into each tool's native context system.
- **Named profiles — i.e. generalize `GLOBAL_KEY`** — the two-layer version of
  this already exists for network policy: `GLOBAL_KEY = "*"` (`state.py:250`)
  is a reserved key in the same structure directories use, `_policy_key()`
  chooses between it and a directory's path, and `_flatten()` merges them with
  the directory's own rules winning (`state.py:47`). A profile is that
  mechanism with an arbitrary name rather than one hardcoded one: storage
  shape, merge, and precedence are already written. Precedence extends
  naturally to **dir > profile > global**, and `--global` becomes sugar for a
  reserved profile every directory implicitly uses — so every command that
  takes `--global` grows `--profile NAME` alongside it.
  The cost isn't the naming, it's that *only* network is layered: `get_base`,
  `get_limits` and `get_env` are keyed by directory with no global tier and no
  merge, so profiling them means building layering that doesn't exist yet.
  Mounts should stay out — they're absolute host paths belonging to one
  directory, with nothing to reuse.
  What justifies it is **a hardened profile**, not the obvious per-language
  one. Its value is exactly that it spans setting *types* no single command
  covers — restricted net with only the agent's API domain, tight limits, git
  guard forced on, and the no-sudo container from the `sudo umount` entry
  above. That's a mode you want to enter atomically and be certain of, and
  it's error-prone to assemble by hand; it's also the natural home for the
  "deliberately hostile code" mode that entry says would settle the guard
  question. The per-language case (a `rust` profile allowing crates.io, …) is
  weaker and probably doesn't pay for itself: `--global` already covers the
  domains every repo needs, and if profiles would differ by two domains each,
  a couple of `aiab net allow` calls per repo is less machinery than a profile
  system.
  One decision to make deliberately rather than discover: whether applying a
  profile *copies* its settings into the directory's state or *references* it.
  Copying is predictable but forfeits the "fix the allowlist once, every repo
  using it follows" benefit that motivates profiles at all. Referencing keeps
  it — but since the proxy re-reads policy per request, editing a profile
  changes a running session's network policy immediately, which is welcome
  when widening and a trap when narrowing. Referencing matches how global
  already behaves, so probably that, eyes open. Auto-selection (`Cargo.toml`
  present → apply `rust`) is the magic that makes behaviour hard to reason
  about; leave it out of a first cut, it's easy to add once profiles exist.

## Smaller items

- **Let the agent's own sandbox work inside the container** — agents now ship
  their own isolation (Claude Code's `/sandbox`, bubblewrap-based on Linux),
  and it composes with ours as a second layer for anyone who wants it. The
  docs note unprivileged containers need a nested-sandbox setting before it'll
  work, so this is probably one setting in the templates plus a line in the
  docs, not a feature.
- More agents in the registry (gemini-cli, codex, aider) — the dataclass
  design makes each one a single entry.
- A `--name`/session-suffix option so two agents of the same kind can run
  concurrently in one directory with separate containers.
- **`aiab doctor`** — check LXD init state and idmap support; first-run
  failures there are probably the worst onboarding experience.
