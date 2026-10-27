# Where the harnesses keep their state, and how to point it elsewhere

Companion to [home-audit-2026-10.md](home-audit-2026-10.md), which argued that
the container home's problem is mixing *state* with *configuration*. This is
the reference table that argument needs: for each harness, which kinds of
information it keeps, where, what relocates them, and how a credential can be
supplied without an interactive login.

Written 2026-10-26/27. **Deliberately unlinked** from
[docs/README.md](README.md), like the landscape surveys.

Provenance matters here, so each claim is marked:

- **(doc)** — in the vendor's documentation, linked at the end.
- **(bin)** — read out of a binary on this machine: `claude-code` 2.1.233 at
  `/usr/bin/claude`, and the `opencode` in `~/.local/share/aiab/opencode/home/.opencode/bin`.
  Undocumented; may move in any release.
- **(obs)** — observed in the live state under `~/.local/share/aiab`.

Codex and Gemini CLI are from docs only; neither is installed and neither is
in aiab's registry. They're here because the registry is meant to grow
([IDEAS.md](../IDEAS.md) lists them), and because Codex turns out to have the
design the other three are missing.

## 1. The matrix

Rows are the five lifetimes from the audit. Paths are relative to `$HOME`
unless stated.

| | **claude** | **opencode** | **copilot** | **codex** | **gemini-cli** |
|---|---|---|---|---|---|
| **Identity** (per user, forever) | `.claude/.credentials.json`, mode 0600 (doc). Plus **`~/.config/anthropic/{configs,credentials}/`** for `ant auth login` / keyless Console profiles — *outside* the config dir (doc) | `.local/share/opencode/auth.json`, `mcp-auth.json` (obs); path is `$XDG_DATA_HOME/opencode/auth.json` (bin) | **Inside `.copilot/config.json`** as `copilotTokens` + `loggedInUsers` (obs) | `$CODEX_HOME/auth.json`, *or* the OS keyring — selectable (doc) | `.gemini/oauth_creds.json`, mode 0600 (doc) |
| **Preference** (per user) | `.claude/settings.json`, `.claude/plugins/`, `CLAUDE.md` (doc) | `.config/opencode/` — config layer, doubles as an npm package for plugin deps (obs) | `.copilot/settings.json` — note `config.json` says *"User settings belong in settings.json. This file is managed automatically."* (obs) | `$CODEX_HOME/config.toml` (doc) | `.gemini/settings.json` (doc) |
| **Machine state** (per container) | `.claude/daemon*`, `daemon.lock`, `sessions/`, `ide/`, `.oauth_refresh.lock` (obs) | — | `.copilot/session-state/` (`inuse.<pid>.lock`), `.copilot/ide/`, `session-store.db-wal`, `-shm` (obs) | — | — |
| **Work history** (per directory) | `.claude/projects/`, `history.jsonl`, `file-history/`, `tasks/`, `jobs/`, `session-env/`, `shell-snapshots/` (obs) | `.local/share/opencode/` — `opencode.db`, `storage/`, `snapshot/`, `log/` (obs) | `.copilot/session-store.db`, `command-history-state.json`, `logs/` (obs) | `$CODEX_HOME/sessions/`, `history.jsonl` (doc) | `.gemini/tmp/`, project `.gemini/` (doc) |
| **Cache / derived** (disposable) | `.claude/cache/`, `paste-cache/`, `telemetry/`, `statsig/`; `.cache/`, `.npm/` (obs) | `.cache/`, `.npm/` (obs) | `$XDG_CACHE_HOME/copilot` — **not** under `COPILOT_HOME` (doc) | `$CODEX_HOME/log/` (doc) | `.gemini/` misc (doc) |
| **Mixed-kind single file** | `.claude.json` — OAuth session *+* per-project map *+* counters (per `agents.py`) | — | `config.json` — token *+* `trustedFolders` *+* `firstLaunchAt`; and `permissions-config.json` keyed by `"locations": {"/work/...": ...}` (obs) | — | — |

The bottom row is the one that matters. **Two of five harnesses put identity,
preference and per-directory state in one file**, and in both cases it's a
file the harness rewrites — so it can't be bind-mounted (`EBUSY`) and it
can't be split by path. That is the whole reason aiab's home code looks the
way it does.

Codex is the counter-example and worth calling out: `cli_auth_credentials_store`
is an explicit `file | keyring | auto` choice, so identity is deliberately
separable from everything else. It's the only one of the five that treats
"where do credentials live" as a question the operator gets to answer.

## 2. Relocation knobs

What you can point somewhere else, and at what granularity.

| Harness | Knob | Moves | Leaves behind |
|---|---|---|---|
| claude | `CLAUDE_CONFIG_DIR` (doc) | `.claude/` **and** `.claude.json` **and** `.credentials.json` | `~/.config/anthropic/` profiles (doc), `.cache/` |
| claude | `CLAUDE_SECURESTORAGE_CONFIG_DIR` (bin) | the credential store only | everything else |
| opencode | `OPENCODE_CONFIG_DIR` (bin), `OPENCODE_CONFIG` (used by aiab today) | config layer | data dir |
| opencode | `XDG_DATA_HOME` (bin) | data dir — **`auth.json` and `opencode.db` together** | config |
| copilot | `COPILOT_HOME` (doc) | all of `.copilot/` | cache |
| copilot | `COPILOT_CACHE_HOME` (doc) | cache | the rest |
| codex | `CODEX_HOME` (doc) | everything: config, auth, sessions, logs, skills | — |
| gemini | — none documented | | |

**The decisive detail, now confirmed in the docs**: setting
`CLAUDE_CONFIG_DIR` *also* re-keys the credential store — "a session with a
different `CLAUDE_CONFIG_DIR` reads a different entry". It is documented
precisely as the *multi-account* mechanism. So the obvious aiab move — a
per-directory `CLAUDE_CONFIG_DIR` — gives each directory **its own login**,
which is the opposite of what we want.

That retracts the optimistic reading in the audit's §5(b). The clean
"per-directory config, shared credentials" split needs
`CLAUDE_SECURESTORAGE_CONFIG_DIR`, which is **(bin)** — undocumented, and not
something to build a load-bearing design on. The documented way to get a
shared identity across per-directory config dirs is to supply the credential
from outside, which is §3.

opencode has the same shape of problem for a different reason: one knob moves
`auth.json` and `opencode.db` together, so you cannot have per-directory
sessions *and* a shared login via `XDG_DATA_HOME` alone.

## 3. Supplying a credential without a login flow

Your instinct — "that would mean you could not use the harness's own login
flows, which might be a bit annoying" — is right that it's a real cost, but
the two aren't mutually exclusive. Every one of these harnesses expects to be
driven headless, and the intended pattern is: **run the interactive flow once
to mint a durable credential, then hand that credential in.** The login flow
is how you *get* the env var, not something the env var replaces.

| Harness | Mechanisms, in the harness's own precedence order |
|---|---|
| claude | 1 cloud-provider vars · 2 `ANTHROPIC_AUTH_TOKEN` · 3 `ANTHROPIC_API_KEY` · 4 **`apiKeyHelper`** (a script that returns a key) · 5 `CLAUDE_CODE_OAUTH_TOKEN` · 6 Anthropic profiles · 7 `/login` (doc) |
| claude (host-managed) | `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`, `CLAUDE_CODE_HOST_AUTH_ENV_VAR` (names the var), **`CLAUDE_CODE_HOST_CREDS_FILE`** (absolute path, ≤64KiB, owner-only-readable or ignored), `ANTHROPIC_UNIX_SOCKET`, plus `CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH` / `..._REFRESH_TIMEOUT_MS` so the *host* refreshes (bin, partly doc'd in the env-var reference) |
| opencode | `auth.json`, or provider API keys via env (`ANTHROPIC_API_KEY` etc.) |
| copilot | `COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN` (doc) |
| codex | `codex login --with-api-key`, env var at runtime, or `cli_auth_credentials_store` (doc) |
| gemini | `GEMINI_API_KEY` / `GOOGLE_API_KEY`, or `.gemini/.env` (doc) |

### The one-time-login route, concretely

`claude setup-token` opens the same browser flow as `/login`, prints a
**one-year** OAuth token, and *saves it nowhere* — you capture it and set
`CLAUDE_CODE_OAUTH_TOKEN`. It runs fine on a headless box: it prints a URL you
open in a browser anywhere. So the flow is preserved; it just happens once a
year on the host instead of once per container.

Two real costs, not just annoyance:

- **Narrower scope.** The token "can only make model requests" — no Remote
  Control, no claude.ai connectors (doc). The binary is blunter: "env-var and
  setup-token sessions default to `user:inference` only", where an interactive
  login can also hold `user:profile`, `user:office`, `user:ccr_inference`
  (bin). So an env-credential session is a *less capable* session, and that
  gap will widen as features attach to the richer scopes.
- **Renewal is now ours.** A year is long enough to forget and short enough to
  bite. `/login`'s own expiry warning doesn't fire for env credentials (doc).

### `apiKeyHelper` is the better shape than a bare env var

Worth separating from the env-var question, because it answers IDEAS.md's
objection ("environment is visible to every process in the container and
trivially exfiltrated") without giving up on headless credentials:
`apiKeyHelper` is a *script* claude runs to obtain a key, intended for
"dynamic or rotating credentials, such as short-lived tokens fetched from a
vault" (doc). The credential needn't be in the environment at all.

`CLAUDE_CODE_HOST_CREDS_FILE` is the same idea one notch further: a file, and
claude *requires* it to be owner-only readable or it ignores it. Combined with
`CLAUDE_CODE_HOST_AUTH_ENV_VAR` — where claude deletes the named variable from
the environment it passes to subprocesses (bin) — the blast radius is
genuinely smaller than `aiab env`, which was the thing IDEAS.md rejected.

And `ANTHROPIC_UNIX_SOCKET` is the host-side broker IDEAS.md reaches for in
the git-push entry, already built, for auth. aiab is a host process that
already owns a per-directory boundary; this is the hook for putting policy
there.

## 4. What the SDKs actually do

The short answer is that **they don't solve it differently — they just can't
use the interactive path, so they lean on exactly these hooks.** From the
Claude docs: "`apiKeyHelper`, `ANTHROPIC_API_KEY`, and `ANTHROPIC_AUTH_TOKEN`
apply to the CLI and the surfaces that wrap it, including the VS Code
extension, the Agent SDK, and GitHub Actions."

So there's no separate SDK credential system to borrow. What's informative is
the *shape* the embedding case pushed them into, which is the host-managed
protocol in §3: the embedding host owns the provider and the token, declares
so with `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST`, supplies the credential by
file or named variable, and takes responsibility for refresh. The child never
logs in and never holds a long-lived secret.

There is also first-party prior art for the containerised case: the binary
carries a "byoc" flow that writes `.credentials.json` into a config dir, copies
`.claude.json` alongside, and sets `CLAUDE_CODE_OAUTH_TOKEN` + `CLAUDE_CONFIG_DIR`
for the child (bin). That is close to what aiab does by hand, done by
upstream — i.e. **per-session config dir + injected credential** is the
pattern the vendor converged on, not bind-mounting pieces of a shared home.

It also runs a scanner that knows where *other* harnesses keep config —
`.codex/config.toml`, `.gemini/` — and distinguishes user-scope from
project-scope, flagging when "the user-scope read or write root has been
redirected" (bin). Someone upstream is solving the same problem across
harnesses, which is mild reassurance that the config-dir-redirection approach
is the expected one.

## 5. Two landmines this turned up

Neither is about the home-dir design; both are live and worth separating from
it.

**Copilot's `session-store.db` is split from its WAL.** `private_paths` lists
`.copilot/session-store.db`, but the shared `.copilot/` also contains
`session-store.db-wal` and `session-store.db-shm` (obs), which are *not*
listed. So a per-directory database would be paired with a shared
write-ahead log. No per-directory copilot home exists yet (obs — none under
`dirstate/*/home/copilot`), so this is latent rather than observed: the first
per-directory copilot session gets a freshly-`touch`ed empty db plus a WAL
belonging to a different database. Also unlisted:
`permissions-config.json`, which is keyed by directory path, so every
directory's tool approvals are visible in every container. This is exactly the
failure `agents.py` warns about — "new state an agent starts keeping there is
shared until someone lists it."

**`GH_TOKEN` would hijack copilot's login.** Copilot's precedence is
`COPILOT_GITHUB_TOKEN` > `GH_TOKEN` > `GITHUB_TOKEN`, and the docs are
explicit: "If you set `GH_TOKEN` for another tool, the CLI uses that token
instead of the OAuth token from `copilot login`." IDEAS.md's git-push entry
contemplates a per-directory PAT; injecting it as `GH_TOKEN` would silently
re-authenticate copilot as that PAT — a repo-scoped token where a Copilot
subscription is expected. If that idea proceeds, the PAT must reach git
without going through those names (which the IDEAS.md entry already prefers,
for a different reason — `credential.helper store --file=`).

## 6. What this changes

The audit's recommendation survives but inverts on one point. "Per-directory
`CLAUDE_CONFIG_DIR`" is still the right destination, but it **cannot** be
combined with a shared credentials *file* — the config dir is the credential
key by design. So the two halves are:

- **state/config split** → `CLAUDE_CONFIG_DIR` (doc, stable, and the whole
  point of the variable)
- **shared identity** → supplied from outside: `apiKeyHelper` or
  `CLAUDE_CODE_HOST_CREDS_FILE` reading one host-side credential, rather than
  a bind-mounted `.credentials.json`

Which means the env-var question and the home-dir question are the same
question, and §3 is the answer to both. The cost is the scope reduction in
§3, and that's the thing worth deciding on before any of it gets built.

Still true that nothing here has been run. The audit's §7 checklist stands,
minus the `CLAUDE_SECURESTORAGE_CONFIG_DIR` items, which the documented
multi-account behaviour has now answered the other way.

## Sources

- [Claude Code: Authentication](https://code.claude.com/docs/en/authentication)
- [Claude Code: Environment variables](https://code.claude.com/docs/en/env-vars)
- [Claude Code: Agent SDK reference (Python)](https://code.claude.com/docs/en/agent-sdk/python)
- [Claude Code CLI environment variables (community gist)](https://gist.github.com/jedisct1/9627644cda1c3929affe9b1ce8eaf714)
- [GitHub Copilot CLI configuration directory](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-config-dir-reference)
- [Authenticating GitHub Copilot CLI](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli)
- [Codex: Authentication](https://learn.chatgpt.com/docs/auth)
- [Codex: Advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [Codex CLI environment variables reference](https://codex.danielvaughan.com/2026/06/03/codex-cli-environment-variables-runtime-configuration-headless-ci-container-deployment/)
- [Gemini CLI configuration](https://geminicli.com/docs/reference/configuration/)
- [gemini-cli settings.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/settings.md)
