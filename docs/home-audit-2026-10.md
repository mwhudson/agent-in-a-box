# The container home: what's shared, what should be, and what else we could do

An audit of the code that builds a container's home directory, written
2026-10-26 because that code is getting hard to follow. Grounded in the
running state under `~/.local/share/aiab` and in strings read out of the
installed `claude-code` 2.1.233 and the `opencode` binary in the shared home —
the env-var findings in §5 are from those binaries, not from docs, and none of
them has been run yet.

**Deliberately unlinked** from [docs/README.md](README.md), like the landscape
surveys: working material, not user documentation.

## 1. The framing

The question that prompted this was "what do we share between containers". The
better framing, which arrived mid-audit, is this:

> The home dir — and more acutely `~/.claude` and `~/.opencode` — contains both
> *state* and *configuration*.

That is fair, and it is the actual disease. Everything convoluted in
`aiab/cli.py` and `aiab/agents.py` is downstream of it. Two refinements,
though, because the two-category version understates the problem:

**It isn't two categories, it's about five**, and they have different
lifetimes:

| Kind | Example | Right lifetime |
|---|---|---|
| **Identity** | `.credentials.json`, `auth.json` | Per *user*, forever |
| **Preference** | `settings.json`, `.config/opencode/`, installed plugins | Per user, versioned if you're lucky |
| **Machine state** | `daemon.lock`, `session-state/*.lock`, `ide/` | Per *container*, worthless elsewhere |
| **Work history** | `projects/`, `history.jsonl`, `opencode.db`, `file-history/` | Per *directory* |
| **Cache / derived** | `.cache/`, `.npm/`, `shell-snapshots/`, `paste-cache/` | Disposable anywhere |

aiab's `shared_paths` collapses all five into one bit: shared or not. So
"machine state" and "work history" end up on the same side by accident of being
"not in the allowlist", which is right today but for the wrong reason — and
"cache" sits in whichever home it happens to land in, growing.

**The sharper problem is that the conflation is also *intra-file*.** It isn't
only that `~/.claude` mixes kinds — it's that single files do:

- `.claude.json` holds the OAuth session *and* a per-project map *and*
  assorted counters. aiab shares it and documents the trade-off
  (`agents.py`), which is honest, but it means a per-directory project map is
  shared across every directory.
- Copilot's `.copilot/config.json` is config that copilot *rewrites*, which is
  what made sharing it a mount point and broke startup with `EBUSY`
  ([#3](https://github.com/mwhudson/agent-in-a-box/issues/3)).

This is the load-bearing observation for the rest of the audit: **no
bind-mount scheme can split a single file.** Mounts work at path granularity;
the conflation is finer than that. So long as the mechanism is mounts, the
code has to keep absorbing the mismatch — which is exactly the shape the
convolution has taken.

## 2. What is actually shared today

Mechanism, from `cli.py:_apply_session_mounts`: the container's home is the
directory's own (`state.session_home_dir`, under the dirstate dir), and the
shared parts are bind-mounted back on top from `~/.local/share/aiab/<agent>/home`.
Nested mounts are re-added in outermost-first order each run, because LXD's
own ordering isn't ours to pick.

Four layers land on the home, in this order:

1. `dir_state_dir` → `/aiab` (per directory, all agents)
2. `shared_paths` from the agent's shared home (per agent, all directories)
3. `private_paths` from the directory's own home (carved back out of 2)
4. `cfg.overlays` — versioned config from this repo

| Agent | Shared | Carved back out |
|---|---|---|
| claude | `.claude.json`, `.claude/.credentials.json`, `.claude/settings.json`, `.claude/plugins/` | — |
| opencode | `.config/opencode/`, `.local/share/opencode/auth.json`, `.local/share/opencode/mcp-auth.json`, `.opencode/` | — |
| copilot | `.copilot/` (whole dir) | `session-state/`, `session-store.db`, `command-history-state.json`, `logs/`, `ide/` |

Three things the running state shows that the code doesn't:

**The shared homes are mostly dead weight.** `claude/home` is 2.5G and
`opencode/home` is 4.3G, but only ~22M of claude's is reachable by a session
container — `.claude/plugins/` (22M) plus three small files. The rest is
pre-split residue: `.cache` (1.3G), `.local` (914M), `go/` (185M), `.npm`
(21M), and stray `testbuild/`, `ic-grub-test/` trees from when the whole home
was the mount. Dated Jul 31 / Aug 2, which is
[f173066](https://github.com/mwhudson/agent-in-a-box/commit/f173066) — "give
each directory its own agent home". Nothing reclaims it; `gc` prunes
per-directory state, not this.

**opencode's shared home is still live and large**, and legitimately so:
`.opencode/` holds the binary the installer put there, so the 4.3G isn't all
residue the way claude's is. Worth separating the two cases before any cleanup.

**Mount points accumulate as unreadable stubs.** In
`dirstate/agent-in-a-box-38bef0/home/claude` the shared and overlay targets
exist as 0-byte, mode-`000`, root-owned files — `.claude.json`,
`.credentials.json`, `settings.json`, `CLAUDE.md` — and mode-`d---------`
directories `commands/`, `plugins/`. LXD created them as container root,
outside the idmap, so on the host they're unreadable junk. Harmless, but it
means a per-directory home can't be inspected or archived without tripping
over them, and `_home_mounts`' `touch()`/`mkdir()` pre-creation only covers
the shared side.

## 3. What we want to share

Mapping §1's taxonomy onto the three agents, independent of mechanism:

- **Identity — share, always.** This is the whole reason a shared home exists:
  log in once per agent. `.credentials.json`, `auth.json`, `mcp-auth.json`.
- **Preference — share.** `settings.json`, `.config/opencode/`, plugins. These
  are dotfiles, and IDEAS.md already concludes they'd be better *mounted in
  from the user's own dotfiles* than curated here.
- **Machine state — never share.** Locks and daemon state that describe one
  container. Already the default, and the allowlist's failure direction is
  correct.
- **Work history — per directory.** Already right, and the strongest argument
  for the current design: one `opencode.db` for every project was the bug the
  split fixed.
- **Cache — don't share, and ideally don't keep.** Currently unmanaged. 1.3G
  of `.cache` in a home nothing mounts is the symptom.

The one genuinely contested item is `.claude.json`. It is shared because the
OAuth session lives in it, and that drags along a per-project map that
*should* be per-directory. It's the clearest case of intra-file conflation
forcing a wrong answer.

## 4. Why the current mechanism is convoluted

Not a criticism of the code so much as a description of what it's absorbing:

- **Mounts can't split files** (§1), so `.claude.json` is shared whole.
- **A bind-mounted file can't be `rename()`d onto** — `EBUSY` — so config an
  agent rewrites must be shared at *directory* granularity, which is why
  `private_paths` exists at all, and why `agents.py` has to warn that the
  allowlist rationale stops applying inside such a directory.
- **Nesting order isn't ours**, so `_apply_session_mounts` force-re-adds inner
  mounts every run and `_drop_stale_home_overlays` exists to unwind mounts a
  previous aiab wanted.
- **Both ends must exist**, so `_home_mounts` pre-creates — and LXD creates
  the rest as root (§2).

Four distinct workarounds, each individually justified, all tracing back to
"the agent's home mixes lifetimes and we're separating it with mounts".

## 5. Other approaches

### (a) Status quo: allowlist + bind mounts

Keep it. Costs: the four workarounds above; `.claude.json` shared too
broadly; residue unreclaimed; a new agent needs its state directories
reverse-engineered.

Virtue, and it is a real one: **it works for all three agents uniformly and
depends on nothing upstream.** Every alternative below trades that away.

### (b) Relocate with environment variables

IDEAS.md records that `COPILOT_HOME` relocates copilot's whole config dir, and
that there's "no documented equivalent for claude (`CLAUDE_CONFIG_DIR` is not
in the settings docs) or for opencode (no documented XDG support)". **Both
halves of that are now out of date** — undocumented, but present in the
shipped binaries:

**claude** (`claude-code` 2.1.233):

- `CLAUDE_CONFIG_DIR` — 51 occurrences. `.claude.json` resolves against
  `CLAUDE_CONFIG_DIR || homedir()`, so it relocates the config dir *and* that
  file.
- `CLAUDE_SECURESTORAGE_CONFIG_DIR` — the credential store's directory,
  resolved independently of `CLAUDE_CONFIG_DIR`.

The second is the interesting one. The credential-store account name is built
from a hash of the config dir *unless* `CLAUDE_SECURESTORAGE_CONFIG_DIR` is
set — so a per-directory `CLAUDE_CONFIG_DIR` would silently get *separate
credentials* per directory, and pinning the securestorage dir is what
decouples identity from config. Which is precisely the split aiab wants:

```
CLAUDE_CONFIG_DIR=<per-directory>     # state + work history + preference
CLAUDE_SECURESTORAGE_CONFIG_DIR=<shared>   # identity
```

Two env vars in place of the entire mount stack — for claude.

**opencode**: `OPENCODE_CONFIG_DIR` overrides the config dir
(`config: OPENCODE_CONFIG_DIR ?? default`), and `XDG_DATA_HOME` relocates the
data dir, with `auth.json` at `$XDG_DATA_HOME/opencode/auth.json`. So config
and data *can* be split — but `auth.json` and the session data (`opencode.db`,
`storage/`) both live under the data dir, so one knob moves identity and work
history **together**. opencode cannot express the split that claude can.

Trade-offs:

- *For:* deletes most of §4. No mount points in the home, so no `EBUSY`, no
  nesting order, no root-owned stubs, no pre-creation. The split lands where
  the agent itself draws it instead of where we guess.
- *Against:* **undocumented and per-agent.** These are strings in a binary;
  they can move in any release, and we'd find out when a container stops
  seeing its login. It also replaces one uniform mechanism with three
  different ones, and opencode's doesn't fully work. Any agent added later
  needs its own knob discovered.
- *Where it bites hardest:* `.claude.json` moving into the per-directory dir
  means per-directory onboarding state — probably an improvement, but it needs
  checking that no part of the OAuth session lives there, since `agents.py`
  currently says it does.

### (c) Credentials via env var, or a host-side broker

The original question. Plain `aiab env` injection is already rejected in
IDEAS.md, for a good reason worth restating: **the environment is visible to
every process in the container and trivially exfiltrated.** That verdict
stands for a general-purpose token.

But claude ships a first-class protocol for exactly this, which IDEAS.md
doesn't record:

- `CLAUDE_CODE_HOST_AUTH_ENV_VAR` — names the env var holding the credential.
  Notably, claude **deletes that variable from the environment it hands to
  subprocesses**, so the secret doesn't propagate to everything the agent
  spawns. That is materially better than `aiab env`, and it's the part that
  makes the env-var route arguable at all.
- `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST` — declares the host owns the provider.
- `CLAUDE_CODE_HOST_CREDS_FILE` — a credentials *file* rather than a variable.
- `ANTHROPIC_UNIX_SOCKET` — a socket the host serves. This is the
  "host-does-it" design IDEAS.md reaches for in the git-push entry, already
  built, for auth.

There is also visible prior art inside the binary: a "byoc" container flow that
writes `.credentials.json` into a config dir, copies `.claude.json` alongside,
and sets `CLAUDE_CODE_OAUTH_TOKEN` + `CLAUDE_CONFIG_DIR` for the child. Someone
upstream containerises claude roughly the way aiab does.

Trade-offs:

- *For:* identity stops being a file in a shared home at all. The socket
  variant means no credential inside the boundary, and a natural place to put
  per-directory policy or an approval step later.
- *Against:* claude-only — nothing equivalent surfaced for opencode or
  copilot, so it cannot be *the* mechanism, only a per-agent upgrade. Env-var
  and creds-file variants still put a live token inside the container
  (narrower than `aiab env`, not absent). The socket variant means aiab
  running an auth service, and owning token refresh. All of it undocumented.

### (d) Symlink farm or overlayfs

Make the home real and point into shared stores.

Symlinks are the design IDEAS.md already rejected for the instruction overlay,
for a reason that applies here too: a symlink into a host path dangles inside
the container unless that path is mounted anyway, so it buys nothing. A
symlink *within* the container's view could work, but agents that
write-temp-then-rename would replace the symlink with a regular file, silently
unsharing it — the `EBUSY` problem inverted, and worse for being silent.

overlayfs (shared lower, per-directory upper) handles rename correctly and
fixes the file-granularity problem for *reads*. But writes go to the upper
layer, so a credential refresh would land per-directory and never reach the
other containers — the opposite of what identity needs. It would have to be
inverted per path, which is the current allowlist with more machinery.

Not recommended, but worth having written down so it isn't re-derived.

### (e) Copy in, copy out

Copy identity/preference into a fresh home at session start, copy changed
credentials back at exit.

- *For:* no mounts at all. Every §4 workaround disappears. Works identically
  for every agent, present and future.
- *Against:* the copy-back is a merge, and two concurrent sessions that both
  refresh a token race. Picking which files to copy back is the same
  allowlist problem, now with a write-conflict mode. A crashed container
  loses whatever it refreshed. Credentials briefly exist in two places.

The race is probably disqualifying given that concurrent sessions per
directory are explicitly supported, and
[404ed35](https://github.com/mwhudson/agent-in-a-box/commit/404ed35) left
concurrency to the agents.

## 6. Where this leaves it

The reframing is right, and it also says what a fix has to do: **stop
separating by path and start separating by the agent's own notion of config
dir vs credential store.** (b) is the only option that does that, and it's
the only one that deletes code rather than adding it.

What I'd suggest, in order, without doing any of it yet:

1. **Reclaim the residue** (§2) — independent of everything else, ~2.5G for
   claude, and it makes the next step legible. Needs care to distinguish
   opencode's live `.opencode/` binary from actual dead weight.
2. **Test (b) on claude only**, behind a per-agent capability, keeping the
   mount path for the other two. Claude is the agent where it fully works, and
   the one whose `.claude.json` compromise it fixes. If `CLAUDE_CONFIG_DIR`
   moves upstream, the mount path is still there.
3. **Leave (c) alone for now.** It's a real improvement to credential blast
   radius, but it's claude-only *and* undocumented *and* it's a second change
   to the same surface. Not at the same time as (b).
4. **Record the taxonomy** (§1) in `agents.py` next to `shared_paths`, even if
   the mechanism doesn't change. The allowlist's comment explains the failure
   direction well but not the five kinds, and "which kind is this" is the
   question someone adding an agent is actually asking.

## 7. Verify before building any of it

Every §5(b)/(c) claim is read from a binary and has not been run. Before
committing to a design:

- Does `CLAUDE_CONFIG_DIR` actually relocate everything under `~/.claude`, or
  only some of it? Are there paths still resolved against `$HOME`?
- With `CLAUDE_CONFIG_DIR` set per directory and
  `CLAUDE_SECURESTORAGE_CONFIG_DIR` pinned shared, does a login in one
  directory show up in another? (This is the whole premise.)
- Does any part of the OAuth session live in `.claude.json`, as `agents.py`
  currently asserts? If so, moving it per-directory breaks the shared login.
- On headless Linux, where does the credential store actually write — a file
  under the securestorage dir, or something else?
- Does `CLAUDE_SECURESTORAGE_CONFIG_DIR` survive `claude` refreshing a token,
  or does refresh rewrite via a path that ignores it?
- For opencode: does `OPENCODE_CONFIG_DIR` cover the whole config layer that
  `.config/opencode/` currently shares, including plugin dependencies?
