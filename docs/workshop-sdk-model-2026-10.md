# Workshop's SDK model, and the answer it already has to aiab's home problem

Third of three notes from the 2026-10-26/29 pass on the container home. The
[audit](home-audit-2026-10.md) argued the home mixes state with
configuration; the [harness matrix](harness-state-matrix-2026-10.md) worked out
where each harness keeps what and how a credential can be injected. This one
looks at how **Canonical Workshop** solves the same problem, because it turns
out to have shipped the conclusion those two notes were converging on — and,
on a closer look, to have declined to solve the part aiab cares most about.

Relevant because Workshop is aiab's closest neighbour: LXD system containers,
one per project directory, Ubuntu, same employer. The
[September survey](landscape-2026-09.md#canonical-workshop) covered it and left
an open question — "is Workshop's interface system the shape aiab's surface is
groping toward?" — which §6 answers.

**Revised 2026-11-01 against the Workshop source** at `/work/workshop`, v0.9.9
(`f8fdf0d5`). The first version of this note was written from published docs
and the SDK repos; source reading confirmed the architecture, sharpened four
details, and overturned the security framing. Corrections are marked
**[revised]**. Still nothing installed or run.

**Deliberately unlinked** from [docs/README.md](README.md), like the surveys.

## 1. What an "SDK" is in Workshop

Not a library. **An SDK is a packaged component that installs and configures
one part of a development environment**, built with `sdkcraft` (a craft-family
tool, so `sdkcraft.yaml` reads like `snapcraft.yaml`: `parts`, `plugs`,
`platforms`, `adopt-info`). A workshop definition lists the SDKs it wants:

```yaml
sdks:
  - name: claude-code
    channel: latest/stable
```

The agent harnesses are themselves SDKs — [`canonical/claude-code-sdk`](https://github.com/canonical/claude-code-sdk),
[`canonical/codex-sdk`](https://github.com/canonical/codex-sdk),
[`canonical/agy-sdk`](https://github.com/canonical/agy-sdk) (Google
Antigravity). So aiab's `agents.py` registry entry and a Workshop agent SDK
are the same artefact, with one difference that is the whole point of this
note: **aiab's entry lists paths, Workshop's declares resource *kinds*.**

## 2. The interface taxonomy

An SDK declares `plugs`, each naming an `interface` type. Eight exist —
confirmed against `docs/explanation/interfaces/` and `tests/main/interface-*`:

| Interface | What it grants | aiab's equivalent |
|---|---|---|
| `mount` | a directory at `workshop-target`, optionally `read-only` | `aiab mount`, and the home-dir machinery |
| `secret` | a credential from the host keyring, brokered per request | **nothing** |
| `desktop` | the host Wayland socket | `Agent.wayland` |
| `tunnel` | TCP/UDP/Unix socket forwarding | port forwarding |
| `ssh` | host SSH agent proxied over a Unix socket | nothing (deliberately) |
| `gpu` | GPU pass-through | nothing |
| `camera` | host video capture devices | nothing |
| `custom-device` | host devices by subsystem | nothing |

The September survey's read — "a far more principled version of what aiab does
ad-hoc across `mount`, `env`, `limits`, Wayland passthrough and port
forwarding" — holds up. What it missed, because `secret` only shipped in
0.9.8, is that the list contains the one interface aiab's credential problem
needs.

## 3. `mount` — and it is per-workshop

`workshop-target` names the path inside the container. The host-side backing
store is created by Workshop, and **[revised]** the layout is exact
(`internal/workshop/workshop_dirs.go`, `SdkMountHostSource`):

```
$XDG_DATA_HOME/workshop/id/<project>/<workshop>/mount/<sdk>/<plug>
```

falling back to `~/.local/share` when `XDG_DATA_HOME` is unset. Four levels of
keying — project, workshop, SDK, plug — so this is strictly finer-grained than
aiab's one shared home per agent. Workshop's answer to "what is shared between
containers" is, by default, **nothing**; the claude-code SDK's "persisted
between workshop *updates*" means across rebuilds of one workshop.

Sharing is possible, as a user action that redirects a declared plug's source:

```
workshop remount ws/mount-sdk:cache ~/.local/cache/
```

Attempted atomically; otherwise it requires the workshop to have been stopped,
and a remounted plug can be reset to its default source. Note the addressing,
`<workshop>/<sdk>:<plug>`: the unit of sharing is **a named, typed plug the SDK
declared**, with the host source a separate, user-owned decision. aiab's
`shared_paths` conflates those two — it is simultaneously the declaration and
the policy, baked into the registry.

## 4. `secret` — identity brokered from the host keyring

The division of labour is the elegant part. The SDK author knows a credential
is *needed*; only the user knows *where it lives*. So:

**The plug** (in `sdkcraft.yaml`, by the SDK author) carries no attributes at
all — it names a need, not a value or a location. Only regular SDKs may
declare one; the system SDK may not.

```yaml
plugs:
  api-key:
    interface: secret
```

**The slot** **[revised]** — I had this as "declared by the system SDK", which
undersold it. The slot is written by **the user, in `workshop.yaml`**, attached
to the `system` SDK, and carries freedesktop Secret Service lookup details:

```yaml
sdks:
  - name: system
    slots:
      demo-key:
        interface: secret
        attributes:
          service: secret-demo
          account: test
  - name: secret-demo
```

`attributes` are the name/value pairs the keyring item was stored with, and a
lookup succeeds only when **exactly one** item carries all of them. An optional
`collection` selects a keyring collection by label (default: `default`).
Workshop validates the slot's shape at launch but doesn't touch the keyring
until a process asks.

**Connection is never automatic, and [revised] more strongly than I wrote.**
I said auto-connection was denied in two places; the sharper fact is that
*listing the pair in the workshop definition's `connections` doesn't connect it
either*. Only `workshop connect dev/secret-demo:api-key dev/system:demo-key`
at runtime does. **A secret grant therefore cannot be committed to a repo** —
which, for a tool whose whole pitch is a committed, shareable YAML, is a
deliberate and sharp choice. It shows as `manual` in `workshop connections`,
and connecting "reads nothing from the keyring and puts nothing in the
workshop; it only permits later requests."

A `workshop refresh` preserves the connection only while `attributes` and
`collection` are unchanged; changing either disconnects with a warning, because
the new lookup could match a different item.

### Delivery: two modes, and one of them is a file

This was the open question I most wanted answered — whether a secret can back a
*file*, since that decides whether the design transfers to opencode and copilot
or only to claude. **[revised] It can, but in a unit-shaped way:**

- **Command**: `workshopctl get-secret <SDK>.<PLUG>` → the value on **stdout**.
  Typically called from a wrapper the SDK ships.
- **Service**: every workshop runs a secret socket, whose path runtime hooks
  receive in `SDK_SYSTEMD_SECRET_SOCKET`. A unit declares
  `LoadCredential=secret-demo.api-key:${SDK_SYSTEMD_SECRET_SOCKET}` and systemd
  places the value in the unit's credentials directory, i.e. a file under
  `/run/credentials/<UNIT>`. Looked up on every start of the unit, and only
  then.

Every request is a **fresh keyring lookup**; Workshop "doesn't persist the
value" and handles it only transiently. So rotation works by itself, and a
locked keyring fails the request — Workshop never unlocks or prompts.

For aiab the asymmetry from the harness matrix therefore stands: claude's
`apiKeyHelper` consumes a *command*, so it fits mode one exactly, while
opencode's `auth.json` and copilot's `config.json` want a file that no unit
owns — a wrapper would have to capture stdout and write it, putting the
credential at rest in the container, which is the thing the design avoids.

## 5. The honest security limit — and why it matters for aiab

**[revised] This overturns the framing of the first version of this note.**
Workshop's own docs are unusually candid, and they say the secret interface
does *not* defend against the agent.

From `docs/explanation/interfaces/secret-interface.rst`, "What a connection
exposes": a connection exposes the secret "to the entire workshop, not to one
SDK or one process." While connected, **any** process in the workshop —
"including commands that don't belong to the SDK and units installed by other
SDKs" — can request the value by naming `{SDK}.{PLUG}`. A systemd credential
file is readable by the unit's user and root, and because ordinary workshop
commands share the `workshop` user, other processes as that user can read a
running service's credential file. Once a process has the value, "Workshop has
no further control over it", and disconnecting "can't withdraw a value that a
process has already received."

And `docs/security.md` § Coding agents says it outright: avoid connecting the
secret interface "to a workshop where an autonomous agent runs, or disconnect
them before the agent starts", because it "lets any process in the workshop
request the credential from your host keyring while it's connected." The same
section warns that remounting an agent's config directory from your home
"exposes its credentials and settings to everything in the workshop" — i.e.
the one route to aiab-style shared identity is the route Workshop warns against.

So **Workshop's threat model is not aiab's.** The secret interface is built for
a cooperative developer environment: it keeps credentials out of committed
YAML, off disk, and behind an explicit grant. It is not an agent-containment
mechanism, and Workshop doesn't claim it is.

What that means for the matrix's recommendation: adopting `apiKeyHelper`-style
brokering in aiab buys real things —

- the long-lived credential never enters the container (only a current value
  does, transiently);
- it isn't at rest on a container filesystem;
- it isn't in the environment of every subprocess;
- revocation and rotation are host-side and take effect on the next request;
- requests are visible on the host.

— and does **not** buy "the agent cannot obtain the credential". An agent in
the container can run the helper exactly as the wrapper does. That is a
narrower benefit than the first version of this note implied, and it is still
worth having; it just shouldn't be sold as confinement.

### Request records are not an audit trail

Each request creates a `Retrieve secret "dev/secret-demo:api-key"` change,
`Done` or `Error`, with the cause in its task log and **never** the value. But
the docs are explicit that these are "troubleshooting history, not a durable
audit trail, because Workshop prunes ready changes." Worth noting next to
IDEAS.md's structured-network-audit-log entry: the same distinction applies
there, and Workshop has already decided which side of it to be on.

## 6. Answering September's open question

> Is Workshop's interface system the shape aiab's surface is groping toward?

**Yes for the mechanism, no for the defaults, and the question was aimed one
level too low.** It isn't only that `mount`/`env`/`limits`/Wayland/port
forwarding would be tidier as plugs and slots. It's that:

- the audit's finding — the home mixes lifetimes, and mounts can't split a
  file — is a consequence of having only *one* interface kind, a mount, for
  every kind of thing;
- the matrix's finding — config dir and credential are coupled, so you must
  inject — is what Workshop's two interfaces separate by construction.

aiab's `shared_paths`/`private_paths` pair is an attempt to express *interface
kind* in the only vocabulary available: paths. Hence five lifetimes collapsing
to one bit, and hence the `EBUSY`, nesting-order and root-owned-stub
workarounds — all mount-mechanics problems that a `secret` interface doesn't
have.

Three caveats against reading this as "adopt Workshop's model":

- **Its default is less sharing than aiab wants.** Per-workshop mounts mean a
  login per workshop unless a secret is connected or a plug remounted by hand.
  aiab's premise — repos that know nothing about you, log in once per agent —
  wants identity shared by default. The mechanism transfers; the default
  doesn't.
- **A keyring is a host dependency aiab doesn't have.** Workshop requires a
  freedesktop Secret Service on the user's D-Bus session bus. aiab's store is a
  directory of files. `apiKeyHelper` pointed at an aiab command needs no
  keyring, so the broker shape is adoptable without the backend.
- **The security property is weaker than aiab's posture needs** (§5). Workshop
  tells you to disconnect secrets before running an autonomous agent; aiab's
  whole premise is that the agent runs unattended.

## 7. Workshop's docs validate aiab's two differentiators, in its own words

A side-effect of reading `docs/security.md`, and better evidence than either
survey had. Both surveys inferred these from absence; Workshop now states them.

- **The git hazard, named and not fixed.** "Git runs hooks from `.git/hooks`,
  and settings in `.git/config` can name commands for Git to run, so anything
  an agent writes there runs on your host the next time you use Git in the
  project." The mitigation offered is advice — "use a separate clone of the
  repository rather than your working copy" — not a mechanism. That is exactly
  what aiab's git guard does mechanically.
- **Egress is open, confirmed.** "Workshop doesn't filter outgoing traffic, so
  an agent can reach any host that the workshop's network can reach and send
  project data there." The September survey deduced this from the absence of
  any allowlist in the reference; it's now explicit.
- **A worktree convergence.** "A Git worktree is a separate project with a
  mount of its own, but its `.git` file points to the main repository outside
  that mount, so Git commands fail inside the workshop; run them on the host."
  aiab hit the same gitfile-vs-directory distinction and handles it in
  `_guard_git_repo`, which skips when `.git` is not a directory.

Also worth noting: `sudo` without a password for the `workshop` user, and SDK
hooks running as `root`.

## 8. What to take from this

In rough order of cost:

1. **Seed `hasCompletedOnboarding` instead of sharing `.claude.json`** (§9).
   Small, testable, removes the one compromise `agents.py` explicitly
   documents.
2. **`apiKeyHelper` pointed at an aiab command**, rather than a bind-mounted
   `.credentials.json` — with §5's benefit statement, not a confinement claim.
   Claude only; opencode and copilot want a file, and materialising one in the
   container forfeits most of the benefit.
3. **Treat `secret` as a resource kind aiab lacks**, and let that reframe the
   `aiab env` and git-push-PAT entries in IDEAS.md — both are really requests
   for a secret interface, and both are currently evaluated as env-var
   problems. Workshop's "the grant cannot be committed" rule is worth stealing
   whatever the storage backend.
4. **Longer term, the interface taxonomy as the Scope test's missing
   vocabulary.** September's question 3 noted the Scope test "would cut
   differently if there were a principled interface model to defer to." There
   is one, it has eight entries, and aiab implements four of them ad hoc.

## 9. The claude-code SDK, concretely

`sdkcraft.yaml` declares exactly three plugs:

```yaml
plugs:
  claude-config:
    interface: mount
    workshop-target: /home/workshop/.claude
  oauth-token:
    interface: secret
  api-key:
    interface: secret
```

Plus `parts` that download the `claude` binary from
`downloads.claude.ai/claude-code-releases`, **verify it against the sha256 in
that version's `manifest.json`** and refuse to install on mismatch; a
`wrapper/claude`; and a `workshop-prompt.md`.

**The wrapper is the credential wiring.** `CLAUDE_CODE_OAUTH_TOKEN` from the
environment wins; otherwise it calls
`workshopctl get-secret claude-code.oauth-token` and exports the result. For
the API key it instead sets

```json
"apiKeyHelper": "workshopctl get-secret claude-code.api-key"
```

so the key is fetched on demand and never lands in a subprocess environment —
the harness matrix's recommendation, in production, same employer, same stack.
It installs the helper only when `ANTHROPIC_API_KEY` is unset and the secret
actually reads, so a missing grant degrades rather than breaking the session.

**They solved `.claude.json` by writing one field, not sharing the file.** With
a token present the wrapper ensures `~/.claude.json` has
`"hasCompletedOnboarding": true`, to skip interactive login. Directly
applicable: aiab shares *all* of `.claude.json` and documents the cost (the
per-project map and counters ride along, see `agents.py`), when one boolean is
what a fresh container needs.

**`workshop-prompt.md` is a shipped instruction overlay**, authored by the SDK
rather than the user's dotfiles — prior art for both of IDEAS.md's entries in
that area.

The codex SDK is the same shape minus the secrets: one `codex-config` mount at
`/home/workshop/.codex`, authentication via `OPENAI_API_KEY` or interactive
`codex login`. So `secret` is adopted per-SDK, and claude-code's is the most
developed use of it so far.

## 10. Corrections to the earlier surveys

- The [October survey](landscape-2026-10.md) records "Canonical Workshop: no
  movement found", caveating that this is absence of search results rather than
  evidence of none. There *was* movement: 0.9.8 added the `secret` interface,
  SDKcraft 0.9.4 added secret plugs, 0.9.9 is current, and the SDK repos
  (`claude-code-sdk`, `codex-sdk`, `agy-sdk`, `tailscale-sdk`, `openjdk-sdk`)
  are public. Next pass should go by repo, not by release announcement.
- The September survey lists six built-in slots; the current reference has
  eight interface types, adding `secret` and listing `mount` as first-class.

## Sources

Primary, read from `/work/workshop` at v0.9.9 (`f8fdf0d5`):
`docs/explanation/interfaces/secret-interface.rst`,
`docs/explanation/interfaces/mount-interface.rst`,
`docs/security.md`,
`internal/workshop/workshop_dirs.go`,
`internal/interfaces/builtin/{secret,mount}.go`,
`internal/secrets/provider/secretservice/`,
`internal/overlord/hookstate/ctlcmd/getsecret.go`,
`tests/main/interface-*`.

Published:

- [Workshop: SDK internals](https://ubuntu.com/workshop/docs/reference/sdks/) ·
  [0.9.8 release notes](https://ubuntu.com/workshop/docs/release-notes/v0.9.8/)
- [canonical/claude-code-sdk](https://github.com/canonical/claude-code-sdk) —
  [`sdkcraft.yaml`](https://raw.githubusercontent.com/canonical/claude-code-sdk/main/sdkcraft.yaml),
  [`wrapper/claude`](https://raw.githubusercontent.com/canonical/claude-code-sdk/main/wrapper/claude)
- [canonical/codex-sdk](https://github.com/canonical/codex-sdk) ·
  [canonical/agy-sdk](https://github.com/canonical/agy-sdk)
- [Document the secret interface (workshop#1086)](https://github.com/canonical/workshop/pull/1086) ·
  [Merge workshop secrets implementation (workshop#1095)](https://github.com/canonical/workshop/pull/1095)
