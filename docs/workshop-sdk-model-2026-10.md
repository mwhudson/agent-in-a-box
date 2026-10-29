# Workshop's SDK model, and the answer it already has to aiab's home problem

Third of three notes from the 2026-10-26/29 pass on the container home. The
[audit](home-audit-2026-10.md) argued the home mixes state with
configuration; the [harness matrix](harness-state-matrix-2026-10.md) worked out
where each harness keeps what and how a credential can be injected. This one
looks at how **Canonical Workshop** solves the same problem, because it turns
out to have shipped the conclusion those two notes were converging on.

Relevant because Workshop is aiab's closest neighbour: LXD system containers,
one per project directory, Ubuntu, same employer. The
[September survey](landscape-2026-09.md#canonical-workshop) covered it and left
an open question — "is Workshop's interface system the shape aiab's surface is
groping toward?" — which §5 here answers.

Sources are public docs and the SDK repos, read 2026-10-29; nothing installed
or run. Workshop 0.9.8 / SDKcraft 0.9.4.

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

An SDK declares `plugs`, each naming an `interface` type. Eight exist:

| Interface | What it grants | aiab's equivalent |
|---|---|---|
| `mount` | a directory, mounted at `workshop-target` | `aiab mount`, and the home-dir machinery |
| `secret` | a credential from the **host keyring** | **nothing** |
| `desktop` | the host Wayland socket | `Agent.wayland` |
| `tunnel` | TCP/UDP/Unix socket forwarding | port forwarding |
| `ssh` | host SSH agent proxied over a Unix socket | nothing (deliberately) |
| `gpu` | GPU pass-through | nothing |
| `camera` | host video capture devices | nothing |
| `custom-device` | host devices by subsystem | nothing |

The September survey's read — that this is "a far more principled version of
what aiab does ad-hoc across `mount`, `env`, `limits`, Wayland passthrough and
port forwarding" — holds up. What it missed, because the `secret` interface
only shipped in 0.9.8, is that the list includes the one interface aiab's
credential problem actually needs.

## 3. The two interfaces that matter here

### `mount` — and it is **per-workshop**

`workshop-target` names the path inside the container. The backing store is
"a dedicated volume created by Workshop at runtime **for each SDK in every
workshop**", placed under `$XDG_DATA_HOME` on the host.

So Workshop's answer to "what is shared between containers" is, by default,
**nothing.** The claude-code SDK's description says credentials are "persisted
between workshop *updates*" — across rebuilds of the same workshop, not across
workshops. Sharing is possible but explicit and manual, at whole-directory
granularity:

```
workshop remount <workshop-name>/codex:codex-config ~/.codex
```

Note the addressing: `<workshop>/<sdk>:<plug>`. The unit of sharing is a
*named, typed plug the SDK declared* — not a path list that the surrounding
tool reverse-engineered from watching the agent.

### `secret` — identity comes from the host, not from a mount

Shipped in Workshop 0.9.8 with SDKcraft 0.9.4. The shape:

- An SDK declares a `secret` **plug** to say it needs a credential. Regular
  SDKs may only declare plugs; **slots may only be declared by the system
  SDK**, because the slot carries the host keyring lookup details.
- The container fetches it at runtime via `workshopctl get-secret <sdk>.<plug>`.
  Delivery is that command, systemd credential delivery, or an ad-hoc env var
  scoped to a single `workshop exec`.
- **It never auto-connects.** "Secrets must always be attached by an explicit
  user decision" — both the code-level AutoConnect veto and the base
  declaration deny it. (Same reasoning Workshop uses to refuse auto-connecting
  `system:ssh-agent`, which both surveys already noted.)
- Connections are invalidated when a plug or slot definition changes, so a
  stale grant can't silently persist across an SDK upgrade.

**This is the split the audit argued for, shipped:** per-container volume for
config and state, host-side keyring for identity, and identity reaches the
container through a broker call rather than a shared file. The thing that is
shared between containers is *the host's keyring* — shared by being on the
host, which is the one place it doesn't need a mount to be reachable from.

## 4. The claude-code SDK, concretely

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

Three things in there are worth aiab's attention.

**The wrapper is the credential wiring, and it uses `apiKeyHelper` for the
reason I'd independently concluded it should.** Per the wrapper:
`CLAUDE_CODE_OAUTH_TOKEN` from the environment wins; otherwise it calls
`workshopctl get-secret claude-code.oauth-token` and exports the result. But
for the API key it sets

```json
"apiKeyHelper": "workshopctl get-secret claude-code.api-key"
```

— explicitly so the key is fetched on demand and **never lands in a subprocess
environment**. That is the harness matrix's §6 recommendation, already in
production, from the same employer, on the same stack. It also only installs
the helper when `ANTHROPIC_API_KEY` is unset and the secret is actually
readable, so a missing grant degrades rather than breaking the session.

**They solved `.claude.json` by writing one field, not by sharing the file.**
When a token is present the wrapper ensures `~/.claude.json` has
`"hasCompletedOnboarding": true`, to skip interactive login. This is directly
useful: aiab shares *all* of `.claude.json` and documents the cost (the
per-project map and counters come along for the ride, see `agents.py`), when
what a fresh container actually needs from that file is one boolean. Worth
testing whether aiab can drop `.claude.json` from `shared_paths` and seed that
flag instead.

**`workshop-prompt.md` is a shipped instruction overlay.** An SDK can
contribute prompt text to the environment. IDEAS.md has two entries in this
area — the instruction-overlay extraction and "tell the agent it's in a
sandbox" — and this is prior art for both, with the SDK rather than the user's
dotfiles as the author.

The codex SDK is the same shape, minus the secrets: one `codex-config` mount
at `/home/workshop/.codex`, and authentication via `OPENAI_API_KEY` in the
environment or interactive `codex login`. So the `secret` interface is adopted
per-SDK, and claude-code's is the most developed use of it so far.

## 5. Answering September's open question

> Is Workshop's interface system the shape aiab's surface is groping toward?

**Yes, and more specifically than the question assumed.** It isn't only that
`mount`/`env`/`limits`/Wayland/port-forwarding would be tidier as plugs and
slots. It's that the taxonomy contains a `secret` interface, and *that* is the
missing piece in the problem this pass started from:

- The audit's finding — the home mixes lifetimes, and mounts can't split a
  file — is a consequence of having only one interface kind (a mount) for
  every kind of thing.
- The matrix's finding — config dir and credential are coupled, so you must
  inject — is what Workshop's two interfaces separate by construction.

aiab's `shared_paths` / `private_paths` pair is, in this light, an attempt to
express *interface kind* in the only vocabulary available: paths. Hence the
five lifetimes collapsing to one bit, and hence the `EBUSY`/nesting/stub
workarounds, which are all mount-mechanics problems that a `secret` interface
simply doesn't have.

Two caveats before this becomes "adopt Workshop's model":

- **Workshop's default is less sharing than aiab's, not more.** Per-workshop
  mounts mean a fresh login per workshop unless a secret is connected or a
  mount is remounted by hand. aiab's premise — repos that know nothing about
  you, log in once per agent — wants identity shared by default. So the
  *mechanism* transfers; the default doesn't.
- **A keyring is a host dependency aiab doesn't have today.** Workshop's slot
  carries "host keyring lookup details"; aiab's credential store is a
  directory of files. `apiKeyHelper` pointed at an aiab-side command needs no
  keyring, so the broker shape is adoptable without the storage backend.

## 6. What to take from this

In rough order of cost:

1. **Seed `hasCompletedOnboarding` instead of sharing `.claude.json`** (§4).
   Small, testable, removes the one compromise `agents.py` explicitly
   documents.
2. **`apiKeyHelper` pointed at an aiab command**, rather than a bind-mounted
   `.credentials.json`. The matrix already recommended this; this note
   upgrades it from "the docs permit it" to "the neighbouring Canonical tool
   does exactly this in production".
3. **Treat `secret` as a resource kind aiab lacks**, and let that inform the
   `aiab env` / git-push-PAT entries in IDEAS.md — both of which are really
   requests for a secret interface, and both of which IDEAS.md currently
   evaluates as env-var problems.
4. **Longer term, the interface taxonomy as the Scope test's missing
   vocabulary.** September's question 3 noted the Scope test "would cut
   differently if there were a principled interface model to defer to." There
   is one, it has eight entries, and aiab implements four of them ad hoc.

## 7. Corrections to the earlier surveys

- The [October survey](landscape-2026-10.md) records "Canonical Workshop: no
  movement found", and caveats that this is absence of search results rather
  than evidence of none. There *was* movement: Workshop 0.9.8 added the
  `secret` interface and SDKcraft 0.9.4 added secret plugs, and the agent SDK
  repos (`claude-code-sdk`, `codex-sdk`, `agy-sdk`, plus `tailscale-sdk`,
  `openjdk-sdk`) are public. Worth a targeted pass next time, by repo rather
  than by release announcement.
- The September survey lists six built-in slots; the current reference lists
  eight interface types (adding `secret` and `mount` as first-class entries).

## Sources

- [Workshop: SDK internals](https://ubuntu.com/workshop/docs/reference/sdks/)
- [Workshop 0.9.8 release notes](https://ubuntu.com/workshop/docs/release-notes/v0.9.8/)
- [Workshop definition reference](https://ubuntu.com/workshop/docs/reference/definition-files/workshop-definition/)
- [canonical/claude-code-sdk](https://github.com/canonical/claude-code-sdk) —
  [`sdkcraft.yaml`](https://raw.githubusercontent.com/canonical/claude-code-sdk/main/sdkcraft.yaml),
  [`wrapper/claude`](https://raw.githubusercontent.com/canonical/claude-code-sdk/main/wrapper/claude)
- [canonical/codex-sdk](https://github.com/canonical/codex-sdk) ·
  [canonical/agy-sdk](https://github.com/canonical/agy-sdk) ·
  [canonical/tailscale-sdk](https://github.com/canonical/tailscale-sdk)
- [Doc: Document the secret interface (workshop#1086)](https://github.com/canonical/workshop/pull/1086) ·
  [Merge workshop secrets implementation (workshop#1095)](https://github.com/canonical/workshop/pull/1095)
- [canonical/use-workshop-skill](https://github.com/canonical/use-workshop-skill)
- [Introducing Workshop](https://canonical.com/blog/introducing-workshop-sandboxed-development-environments)
