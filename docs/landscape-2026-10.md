# Landscape survey, October 2026

Begun as a pass over **Gondolin** (<https://github.com/earendil-works/gondolin>)
on 2026-10-06, then widened to the user-facing tool that consumes it (**pi**)
and to two tools reached from there (**NVIDIA OpenShell**, and
**agentbox** + **webterm**). All of it from published docs, commit logs and
issue trackers; nothing was installed or run. The previous pass was
[September](landscape-2026-09.md), 2026-09-14.

**Deliberately unlinked** from [docs/README.md](README.md), for the same reason
as its predecessor: working material, not user documentation, kept out of
IDEAS.md so that file stays reasoning-about-aiab rather than
reasoning-about-everyone-else.

It started as a single-subject pass prompted by a security page, and earned its
keep twice over. Gondolin's blocked-range list named a range ours does not
block. And following it to its consumers turned up the thing September got
wrong: an interactive approval loop for denied egress, shipped, by NVIDIA.

Two things about the shape of this file, since it grew past the usual pass.
Everything up to and including [Re-checked and still
true](#re-checked-and-still-true) is survey — what other tools do, and what
checking our code against them turned up, on 2026-10-06. The four sections
after it are **analysis dated 2026-10-07**, worked out against the code but
not against anyone else's tool: whether aiab could sit on Gondolin, how fast
`sbx` is converging, what a VM port would cost subsystem by subsystem, and
whether the git guard wants FUSE. They are reasoning-about-aiab, which
normally belongs in IDEAS.md — they are parked here because they are
*conclusions this survey produced*, and splitting them from their evidence
would make both halves harder to use. The open questions are where they go to
IDEAS.md.

- [What the survey changed](#what-the-survey-changed)
- [Where Gondolin sits](#where-gondolin-sits)
- [The egress models, compared properly](#the-egress-models-compared-properly)
- [Secrets: the fourth implementation](#secrets-the-fourth-implementation)
- [SSH egress: a design IDEAS.md does not have](#ssh-egress-a-design-ideasmd-does-not-have)
- [Filesystem, images, and the cost of a microVM](#filesystem-images-and-the-cost-of-a-microvm)
- [pi: the consumer, and its silence about all of this](#pi-the-consumer-and-its-silence-about-all-of-this)
- [OpenShell: the approval loop we thought was ours](#openshell-the-approval-loop-we-thought-was-ours)
- [agentbox and webterm](#agentbox-and-webterm)
- [On-the-fly mount mutation, across five tools](#on-the-fly-mount-mutation-across-five-tools)
- [Addendum: a sweep for movement (2026-10-06)](#addendum-a-sweep-for-movement-2026-10-06)
- [Re-checked and still true](#re-checked-and-still-true)
- [Could aiab be built on top of this?](#could-aiab-be-built-on-top-of-this)
- [Docker Sandboxes: the convergence tally](#docker-sandboxes-the-convergence-tally)
- [What a VM port would cost, by subsystem](#what-a-vm-port-would-cost-by-subsystem)
- [Shadow versus FUSE, and two gaps in the git guard](#shadow-versus-fuse-and-two-gaps-in-the-git-guard)
- [Open questions this leaves for IDEAS.md](#open-questions-this-leaves-for-ideasmd)
- [What was not verified](#what-was-not-verified)

## What the survey changed

**One gap found: CGNAT space is not blocked.** Gondolin's `isIpAllowed` blocks
internal ranges by default (`blockInternalRanges: true`) and its published list
includes `100.64/10` alongside the ranges you would expect. `is_external_address()`
in `aiab/netproxy.py` delegates to Python's `ipaddress` predicates, and
`100.64.0.0/10` is in none of them — not `is_private`, not `is_reserved`, not
`is_link_local`. Checked on the Python aiab targets (3.14.4):

```
ALLOWED  100.64.0.1             priv=False glob=False
ALLOWED  100.100.100.100        priv=False glob=False
ALLOWED  ::ffff:100.64.0.1      priv=False glob=False
```

This is the September hole again, one range over. The proxy dials out from the
host, so an allowed name resolving into `100.64/10` reaches whatever the host
can reach there — and that is not hypothetical space: Tailscale allocates
tailnet addresses from `100.64.0.0/10` and puts MagicDNS on `100.100.100.100`,
and ISPs CGNAT subscriber equipment into the same block. The tailnet case is
strictly worse than the LAN case a52da9b closed, because a tailnet spans
machines a LAN does not.

`tests/test_netproxy.py` covers loopback, RFC1918, link-local, ULA,
IPv4-mapped, unspecified, multicast and unparseable — `100.64/10` is absent
there too, so nothing was asserting this either way.

Worth noting what is *not* broken while in the neighbourhood: `::127.0.0.1`
(IPv4-compatible) and `64:ff9b::/96` (well-known NAT64) are both refused —
but by `is_reserved` covering `::/8`, not by anything NAT64-aware. A
network-specific NAT64 prefix (RFC 6052) in global space would not be caught.
That needs a NAT64 gateway on the host to matter, so it is a note, not a gap.

**Two documentation findings**, neither a stale field claim:

- `docs/commands.md` says ssh "and raw-socket protocols are simply cut off" in
  restricted mode. True by default and for the right reason — nothing in the
  container is proxy-aware for those protocols — but it reads as though the
  proxy refuses them, and it does not. `evaluate()` is matched on the host
  only; the port is parsed, logged and connected to but never policed. So
  `CONNECT github.com:22` succeeds today wherever `github.com` is allowed, and
  anything that can speak CONNECT reaches any port on an allowed host. The
  security consequence is already covered two bullets later ("hostname
  filtering is policy, not adversarial containment"), so this is a precision
  problem rather than a missing caveat — but it is precisely the hole Gondolin
  closes by denying CONNECT outright (`connect-not-allowed`) and by gating
  ports explicitly (`--ssh-allow-host PATTERN[:PORT]`, e.g.
  `"ssh.github.com:443"`).
- The plain-HTTP path rewrites the request to origin-form but passes the
  client's `Host:` header through unchanged, so the absolute-URI host that was
  policed and the `Host:` the upstream sees need not agree. The connection
  still goes to the allowed name's address, so this only matters on an edge
  that routes by `Host:` — a shared CDN — where a plain-HTTP request to an
  allowed name could be served another site's content. Low severity, subsumed
  by the stated threat model, and cheap to close by rewriting `Host:` from the
  URI.

## Where Gondolin sits

Apache-2.0, TypeScript, v0.13.0 as of 2026-10-04, and self-described as
experimental. QEMU microVM by default with an experimental libkrun backend; a
guest of conventional Linux userspace plus four small daemons (`sandboxd` for
exec, `sandboxfs` for FUSE-over-virtio-serial, `sandboxssh`, `sandboxingress`),
and a host Node process that is the sole policy enforcement point for all I/O.

On the axis that organised the September pass — **where the configuration
lives** — Gondolin takes a position none of the previous three did:

| | Config location | Committed? | Audience |
| --- | --- | --- | --- |
| Workshop | `.workshop/<name>.yaml` at project root | Yes | Projects that adopt it |
| Docker Sandboxes | `sbxenv.yaml` beside the project | No, but filesystem-local | One developer, per project |
| aiab | `~/.local/share/aiab/*.json`, keyed by directory | Nothing in or near the repo | Repos that know nothing about you |
| **Gondolin** | **nowhere — CLI flags, or the embedding program** | **n/a** | **A platform embedding agent execution** |

The CLI reference is explicit: no config file, no per-project config. State in
`~/.cache/gondolin/` is sessions, images and checkpoints — never policy. Every
invocation restates the whole policy (`--allow-host`, `--host-secret`,
`--dns`, `--mount-hostfs`, `--ssh-allow-host`), or you drop to the SDK and
write it: `createHttpHooks({ allowedHosts, secrets })` handed to
`VM.create()`, with `isRequestAllowed(request)` and
`isIpAllowed({ hostname, ip, family, port, protocol })` as the extension
points.

That predicts the rest of it, exactly as the axis did in September. Gondolin is
not competing to be the thing you run on your checkout each morning — the
ergonomics forbid it, and policy-as-JavaScript is a strange thing to retype at
a prompt. It is a **primitive for whoever is building the agent product**,
where the policy is the vendor's and the repo is the end user's. Its own
sibling project (`pi-chat`) consumes it that way, mounting the project at
`/workspace` through an extension rather than a flag.

So Gondolin is not really in the aiab/Workshop/Docker triple. It is the layer
those tools would sit on top of if they were written in TypeScript and did not
already have a container runtime. The overlap worth mining is the *policy
engine*, not the product.

## The egress models, compared properly

Gondolin does not run a proxy the guest is configured to use. The guest gets a
`virtio-net` NIC; the host receives frames and **implements its own userspace
network stack**, sniffs the first bytes of each TCP flow to classify it as
HTTP, TLS, SSH or unknown, and drops what it cannot classify. HTTP is parsed
and replayed through the host's `fetch`; TLS is MITM'd to recover the request
and then replayed the same way. UDP is DNS only, in three modes — `synthetic`
(default; the host answers, there is no upstream resolver, so no DNS
tunnelling), `trusted` (forward validated queries to named resolvers), `open`
(forward anything, and the docs say plainly that this enables tunnelling).

Set against aiab: the container's NIC is *removed* in restricted mode
(`mask_profile_devices` adds a container-local `none` device over each
profile-inherited NIC), and the only path out is an LXD proxy device
forwarding `127.0.0.1:3128` to an abstract unix socket on the host, with
`HTTP_PROXY`/`HTTPS_PROXY` pointed at it.

The difference that matters is not strength, it is **what happens to a tool
that does not honour the proxy**:

- In aiab it has no route and fails. That is fail-closed and it is what
  `concepts.md` claims as an advantage over the process sandboxes, correctly.
  It is also a usability cost the docs do not mention: the set of things that
  work in restricted mode is the set of things that implement proxy support.
- In Gondolin it works and is still policed, because mediation is at the
  frame level and the guest has no say in it.

Gondolin pays three prices for that, all of them documented, and all of them
reasons aiab should not copy the approach wholesale:

1. **It owns a TCP/IP stack.** Issue #138, "TCP sequence number can overflow
   uint32", fixed by `fix(network-stack): wrap TCP sequence numbers at 2^32`;
   open issue #155 proposes aligning connection close and segment responses
   with RFC 9293. aiab uses the host kernel's stack and has no such class of
   bug.
2. **No HTTP/2, no HTTP/3, no QUIC, no WebRTC, no general UDP.** The
   limitations page is unambiguous: mediation "currently focuses on HTTP/1.x
   over plain TCP and HTTPS via TLS interception". aiab's CONNECT tunnel
   carries whatever the endpoints agree on, h2 included.
3. **MITM everywhere, so a local CA private key on disk**, generated under
   `~/.cache/gondolin/ssl`, with the docs warning to treat it as sensitive
   because it can sign certs the guest trusts.

And the MITM is load-bearing in a way that produces the most instructive bugs
in the tracker — the mediation layer failing *silently*. Issue #150: "Many VFS
mounts silently drop the MITM CA (kernel cmdline truncated)", fixed by
fetching the bind list over RPC instead of the kernel command line. Issue
#148: "Guest clock never synced; stale clock breaks MITM TLS". Issue #147 and
the later `fix: TLS MITM on Bun host runtimes (#152)`: the interception
silently not working on a host runtime with an unimplemented `SNICallback`.

That class of failure is worth checking for in aiab, and the answer came out
well — see [Re-checked and still true](#re-checked-and-still-true).

Two smaller points of contrast. Gondolin re-evaluates policy "before each
outbound request" and again "when a new upstream connection is opened", via a
guarded `lookup()` in its undici dispatcher, and revalidates every redirect
target because it follows redirects itself (`redirect: "manual"`). aiab needs
neither: it never follows a redirect, and `connect_external()` vets the
resolved address and then connects to that same `sockaddr` rather than
re-resolving, which is the September fix. Conversely, an established aiab
CONNECT tunnel is never re-policed, so `aiab net deny` takes effect for the
next request and not for a tunnel already up. Minor, and inherent to tunnels.

## Secrets: the fourth implementation

September called three independent credential-brokering implementations the
strongest signal in the survey. This is the fourth, and it is the most
completely specified of them.

The guest gets placeholders as environment variables — by default
`<random-marker>.<normalized_secret_name>`, or fully random
`GONDOLIN_SECRET_<random>` under `secretPlaceholderMode: "unique"`, or custom
via `makePlaceholderFunc({ prefix, length, suffix, alphabet })`. Each secret
declares its own host allowlist:

```typescript
secrets: {
  SECRET_NAME: {
    hosts: ["api.example.com", "*.api.example.com"],
    value: process.env.SECRET_NAME!,
  }
}
```

The host's `onRequest` hook substitutes the real value into outbound headers
**only** when the destination matches that secret's `hosts` — which is
independent of the global `allowedHosts`, so egress policy and substitution
policy are separate decisions. A placeholder appearing in a request to a host
outside its own allowlist gets the request blocked. The CLI equivalent is
`--host-secret NAME@HOST[,HOST...][=VALUE]`, reading from the host environment
when the value is omitted.

Three details are worth lifting out, because they are the ones a
reimplementation would get wrong:

- **`Authorization: Basic` is decoded, substituted, and re-encoded**, as is
  `Proxy-Authorization: Basic`. This is the non-obvious detail that decides
  whether the mechanism works for git at all: git's HTTPS transport sends the
  credential as `Authorization: Basic base64(user:token)`, so a substituter
  that only scans raw header values never fires.
- **Query strings are opt-in** (`replaceSecretsInQuery: true`), and the advice
  is to leave it off. Bodies, URL paths and responses are never substituted.
- **Hook ordering is a leak hazard they had to warn about**: a user-supplied
  `onRequest` may run *after* substitution, so "do not log request
  headers/URLs from hooks unless willing to potentially expose real values".

The honest reading of all this for aiab is that it confirms September's
conclusion rather than unsettling it. Substitution requires seeing the headers,
seeing the headers requires terminating TLS, and Gondolin's own limitation
("Mediated ≠ Safe Networking") spells out what you still do not get: "If you
allow `api.example.com`, malicious guest code can still send _any_ data it can
read to that host. Servers that can echo headers back (eg: httpbin) can be used
to exfiltrate secrets." Its SSH and mapped-TCP paths get no substitution at
all, for the same reason.

So the price of keeping a credential out of the box is MITM of the host it is
for. September's split — worth it for git, not obviously worth it for the LLM
endpoint — survives contact with a full implementation, and now has a spec to
copy if the git half is ever built.

## SSH egress: a design IDEAS.md does not have

The most directly useful thing in this survey. aiab has no git-remote
credential story (IDEAS.md's "Let a container push to one fixed repo" entry),
and that entry's rejected options include "forwarding the agent" as too broad,
with the two live candidates being a per-directory scoped PAT or having the
host do the push behind a `git` wrapper or `git-remote-aiab` helper.

Gondolin ships a third shape, and it is explicitly aimed at this use case —
the docs say SSH egress is "primarily intended for workflows like **git over
SSH**":

- The host **intercepts the guest's outbound SSH flow and terminates it in an
  in-process SSH server**, presenting an ephemeral host key to the guest.
- For each guest `exec` request, the host opens its *own* upstream SSH
  connection using the **host's ssh-agent** (`agent: process.env.SSH_AUTH_SOCK`)
  or a configured private key. The guest never handles key material.
- Upstream host keys are verified host-side against the user's real
  `known_hosts`.
- An **`execPolicy` hook** can allow or deny each request before it is
  proxied — the docs offer restricting which git repositories are reachable as
  the worked example.
- **Only non-interactive `exec` channels**: interactive shells denied,
  subsystems including `sftp` denied.
- Upstream connections are capped and time-bounded
  (`maxUpstreamConnectionsPerTcpSession: 4`, `maxUpstreamConnectionsTotal: 64`)
  explicitly "to avoid guest-triggerable host DoS".

That is the *narrow* version of the thing IDEAS.md rejects wholesale. Agent
forwarding is too broad because the socket inside the box can sign anything —
Workshop declines to auto-connect `system:ssh-agent` for exactly this reason,
noted in September. Here the socket never enters the guest and the host decides
per-exec what may be run, which removes the objection rather than accepting it.
No PAT to mint, nothing to expire, no `insteadOf` rewrite, works with any
forge, and the user's existing agent and `known_hosts` do the work.

Transposing it onto aiab is not free, and there are two versions:

- **The cheap one, no new dependency.** The policy is already port-blind, so
  `CONNECT github.com:22` works today; what is missing is a key the container
  may use without holding. aiab could expose a *filtering* ssh-agent socket as
  a second proxy device — the agent protocol is small enough to parse in the
  stdlib — forwarding `REQUEST_IDENTITIES` and `SIGN_REQUEST` and nothing
  else, optionally with a parked confirmation reusing the `aiab monitor`
  machinery. Key material stays on the host. What it cannot do is bind a
  signature to a repository, so it is narrower than agent forwarding but wider
  than `execPolicy`.
- **The faithful one.** Terminate SSH in the proxy as Gondolin does, with a
  per-exec policy. That means an SSH server implementation, i.e. `paramiko` or
  `asyncssh`, and aiab is stdlib-only apart from `textual` for `aiab monitor`.
  A real cost to weigh, not an incidental one.

By the Scope test both are unambiguous box work: the agent cannot give itself a
credential path the masked NIC took away.

## Filesystem, images, and the cost of a microVM

The filesystem model is more principled than aiab's bind mounts and strictly
less useful for aiab's job. Guest `sandboxfs` (FUSE) turns operations into RPC
over virtio-serial; host `FsRpcService` validates and dispatches to a
`VirtualProvider`, composable from `MemoryProvider`, `RealFSProvider`,
`ReadonlyProvider` and custom wrappers, mounted at `/data` by default. The
validation is explicit about the things path handling gets wrong — "File names
are single components (no `/` or NUL)", paths normalised to absolute POSIX,
payloads capped at `MAX_RPC_DATA = 60 KiB` — and they still shipped
`fix(vfs): serialize fs-rpc requests to close path validation race` for the
TOCTOU in issue #143.

But the default guest has *no* host filesystem, and the safe-envelope advice is
to keep it that way: `MemoryProvider` for scratch, `ReadonlyProvider(RealFSProvider(...))`
where a host directory is genuinely needed, never the home directory or `/`.
aiab's entire premise is the live working tree, mounted read-write, which is
why the git guard exists and why Gondolin has nothing resembling it — it does
not have the problem.

Two more costs of the microVM, both from the limitations page, both cutting
aiab's way harder than Docker Sandboxes did in September:

- **Alpine only, and extra packages mean building a new image.** The guest
  image is deliberately minimal and the image builder supports only Alpine.
  Against `/setup-container`, `apt install` and Docker-in-container on a real
  Ubuntu, this is a different category of tool. `concepts.md`'s "a whole
  operating system" bullet is reinforced, not threatened.
- **Acceleration.** The QEMU page: KVM on Linux, "Requires access to
  `/dev/kvm`"; HVF on macOS; and TCG when neither is available, which is
  "much slower and is not the recommended mode for regular use". Nested
  virtualisation is not addressed anywhere in the docs. So September's
  conclusion — that the microVM trade cuts aiab's way more often than the
  hypervisor argument suggests — holds, with a nuance: Gondolin *degrades*
  where Docker Sandboxes refuses to run at all, and it runs on macOS, where
  aiab does not run.

On supply chain Gondolin is ahead in a small, copyable way: custom builds emit
a `manifest.json` carrying SHA-256 checksums, verifiable with
`gondolin build --verify`, and default assets come from GitHub over HTTPS.
aiab's templates are LXD images plus apt, with the agent installers trusted
transitively.

## pi: the consumer, and its silence about all of this

Gondolin is a component, so the UX question belongs to whatever ships on top.
That is **pi** (<https://github.com/earendil-works/pi>), the same org's
flagship and by a wide margin — 112.9k stars to Gondolin's 2.2k — a TypeScript
"minimal, extensible agent harness that you can make your own": unified LLM
API, agent loop, TUI with differential rendering, extensions, skills, prompt
templates, themes, an RPC control surface and a TypeScript SDK. A second
consumer, `pi-chat`, bridges Discord and Telegram to a sandboxed session, each
channel getting its own Gondolin micro-VM with a persistent workspace.

Interaction design is most of pi's pitch. None of it reaches the sandbox.

`packages/coding-agent/docs/containerization.md` presents isolation as a flat
menu of four — plain Docker, Docker Sandboxes, OpenShell, Gondolin — and
Gondolin's entry is a single line, `pi -e ~/.pi/agent/extensions/gondolin`,
against an extension installed by hand. In that whole document there is **no
network policy syntax for any of the four**, which is remarkable given that
programmable egress is the entire reason Gondolin exists. No interactive
approval. No description of what the user sees when something is blocked.
Mounts static per invocation. Where plain Docker's persistence is spelled out
(a named `pi-agent-home` volume keeping "container-local settings, credentials,
and sessions between runs"), Gondolin's is not stated at all.

What sharpens this: **pi has no permission system.** The README says it "runs
with the permissions of the user and process that launched it", with no
approval gates, and points at containerization as the answer. `concepts.md`
frames aiab's sandbox as the thing that makes it *safe to disable* permission
prompts. For pi there is nothing to disable — the sandbox is the only control
that exists, and it is opt-in, manually installed, one of four, and documented
without a policy language.

The consequence is visible in the wild. A Hacker News thread
(<https://news.ycombinator.com/item?id=47144838>) asks whether anyone has
managed to run pi fully sandboxed. The answers: one person tinkering with
Gondolin, one running agentbox plus webterm, and one who **had pi write them a
sandbox** — hooks on read/write/edit/bash with "temporary and permanent
override prompts". Someone hand-rolled parked decisions because nothing they
could install shipped one. That is about as direct a validation of the
`aiab monitor` design as a survey is going to produce.

The thread's main friction point validates the other half of `concepts.md`.
Restricting an agent to just the working directory fails because "pretty much
every linux application needs to be able to read from `/usr` and `/etc`", so
people end up with bwrap plus ACLs and a growing list of extra directories for
git and rust. That is the subtract-from-the-host shape losing in practice,
which is the argument `concepts.md` makes from first principles.

## OpenShell: the approval loop we thought was ours

Reached from pi's menu, and the most consequential find in this pass:
**NVIDIA OpenShell** (<https://github.com/NVIDIA/OpenShell>,
docs at <https://docs.nvidia.com/openshell/>). September concluded that parked
network decisions had "no prior art found, and this is the novel one". That
conclusion does not survive.

The architecture is a **gateway** (control plane: sandbox lifecycle, providers,
policies, credential store) and a **sandbox** (data plane). Policy is YAML,
passed at creation — `openshell sandbox create --name my-sandbox --policy
./policy.yaml` — and "injected from the gateway at sandbox creation and cannot
be modified by the agent at runtime". Filesystem access is read-only and
read-write path sets enforced by **Landlock LSM** in the kernel.

The egress policy is more expressive than aiab's on two axes at once:

```
network_policies:
  [rule_name]:
    binary: [path]
    endpoints: [host:port:access:protocol:mode]
```

— for example `api.github.com:443:read-only:rest:enforce`. So a rule names a
**binary**, and the proxy "matches every outbound connection to the binary that
opened it"; the endpoint carries port, an access preset, a protocol, and an
enforcement mode. Against that, aiab's policy is host-only and, as this pass
found, port-blind. `read-only` on a `rest` endpoint implies method-level
filtering, and `enforce` implies there is a non-enforcing mode to iterate in.

### The Policy Advisor, which is the part that matters

When OpenShell blocks a connection it **drafts a rule for it**, in every
sandbox, each draft allowing one binary to reach one host and port. The agent
inside can also ask: a **`policy.local` HTTP API**, reachable from inside the
sandbox and needing no network rules, lets it "read the current policy and
recent denials, submit proposals, and wait for decisions", with a `/wait`
endpoint that confirms when the sandbox has loaded a new rule. Outside, the
developer reviews a queue:

```
openshell rule get my-agent --status pending
openshell rule approve my-agent --chunk-id <chunk-id>
openshell rule reject my-agent --chunk-id <chunk-id> --reason "<explanation>"
```

A **prover** gates the review by comparing what the sandbox can reach with and
without the proposed rule, reporting four finding types: link-local reach,
uninspectable traffic bypass, credential expansion, capability expansion. An
opt-in auto mode approves without a human only when the prover delta is empty
and the destination is unflagged (private IPs, wildcard hosts, high ports,
database ports). Approved network rules **hot-reload into the running
sandbox** — and more than that, `openshell policy update` network changes "take
effect immediately. The sandbox loads new rules and closes previous
connections."

Set against aiab honestly:

- **aiab parks; OpenShell fails and retries.** The advisor docs are explicit:
  "The blocked request stays denied, so retry it after OpenShell approves the
  draft and loads the new rule." aiab holds the connection open in
  `_await_decision` and releases it on the answer, so the agent never sees a
  failure. That difference is real and still aiab's, and it is the better
  experience for the agent.
- **But the approval loop as a concept is not novel, and OpenShell's is
  richer.** Per-binary granularity, a drafted rule rather than "go write one
  yourself", a reviewed queue with reasons, a prover that reasons about
  reachability deltas, a documented auto-approve policy, and an audit trail.
  aiab has a notification with action buttons and a `monitor` tab.
- **OpenShell closes existing connections on a policy change; aiab does not.**
  Noted earlier in this pass as a limitation inherent to tunnels. It is not
  inherent — someone else does it.
- **`policy.local` breaks a premise in IDEAS.md.** The Scope section argues
  parked decisions are unimpeachable box work partly because "no agent can ask
  you to widen a boundary it cannot see". OpenShell deliberately hands the
  agent exactly that channel — see your denials, propose a rule, explain why,
  wait for the answer. The box-work *conclusion* survives untouched (only the
  thing imposing a boundary can lift it) but that particular supporting clause
  is now false in a shipped tool, and it is a capability aiab could add: today
  the agent gets a 403 whose text addresses the human, and has no way to
  attach a justification.
- **Where aiab is still ahead on this axis:** aiab's decision reaches the
  human where they already are — a desktop notification with buttons, focus-
  aware, next to a live monitor — rather than requiring them to notice a
  pending queue and run two CLI commands.

And the loop is not smooth in practice, which is the other half of the story.
Issue #2752: "approved + reloaded draft chunks still reject matching traffic
and keep re-drafting the same rule". Issue #2841 asks to "preflight the exact
effective candidate before proposal review". Issue #4035 asks for "a command
to check a policy file before creating a sandbox". Three requests in a row for
*knowing whether the policy you wrote will work before you find out the hard
way* — the friction of policy iteration showing up as feature requests.

Two structural differences worth recording. Credentials live in a gateway
credential store and are sent "only to" the provider host, which is a
per-destination scoping like Gondolin's per-secret allowlist and unlike an
env-var handoff. And OpenShell is gateway-oriented with explicit transfers —
pi's doc shows `openshell sandbox upload`/`download` moving a working folder in
and out — so there is no live host working tree, which is presumably why the
filesystem half of the policy can afford to be creation-time only.

## agentbox and webterm

The low-ceremony end of the field, and the pairing the HN thread actually
recommends.

**agentbox** — note the name collides with at least six unrelated projects, so
a future pass should be specific. The one in question is
<https://github.com/rcarmo/agentbox>: "Contain your coding agents
(literally)." Docker, a Debian Trixie dev environment, project mounted with
`-v $(pwd):/workspace`, agents preinstalled (Copilot CLI, Codex, pi, with
opencode, Gemini CLI and others behind Makefile targets), services off unless
switched on by env var (`ENABLE_DOCKER`, `ENABLE_SSH`, `ENABLE_RDP`), a `:gui`
tag with XFCE/XRDP/VS Code, and `docker-compose.override.yml` for overrides.
**No egress filtering at all** — the README has nothing on it and "network
isolation options" sit on the roadmap. So: aiab's filesystem story without its
network story, in a Dockerfile.

**webterm** (<https://github.com/rcarmo/webterm>) is the interesting half, and
it is not a sandbox. Go, terminal sessions over HTTP/WebSocket, a Ghostty
WebAssembly rendering engine, and a **dashboard of live tiles — screenshots of
every session, refreshed over SSE, with CPU sparklines, Docker watch mode, and
mobile/touch support with a virtual keyboard**.

That is both of the gaps September recorded, in one small Go binary:
a **fleet view** across sessions where `aiab monitor` is per-directory by
design, and **getting off the desktop**, where `notify.py` goes silent over
ssh for want of a session bus. It does it without an app, an account or a
protocol — just render the terminals to a web page. Worth knowing before any
mobile-notification idea gets designed: the cheap version of "see all my
agents from my phone" is a web page showing the terminals.

## On-the-fly mount mutation, across five tools

Worth stating plainly now that there are five data points — and worth stating
*carefully*, because the first version of this table was already out of date
when it was written. See [the addendum](#addendum-a-sweep-for-movement-2026-10-06)
for how Docker's row changed.

| | Changing the mount set | Changing network policy |
| --- | --- | --- |
| aiab | **in place on a running container** (`add_device` reconciles `readonly` via `lxc config device set`) | per-request policy re-read; established tunnels unaffected |
| Workshop | committed YAML, relaunch | no egress policy exists |
| Docker Sandboxes | **dynamic, as of v0.45.0/v0.46.0** — mounts persist across restarts and `sbx umount` works on a stopped sandbox. September's "changing the mount set destroys the sandbox" is stale. | pre-authorise, or dashboard |
| Gondolin | creation-time `vfs` config; no runtime API documented | per-request hooks (sync allow, async `onRequest`) |
| OpenShell | **creation-time only** — "filesystem, Landlock, and process settings take effect only when a sandbox starts, so changing them requires a new sandbox" | **immediate, and closes existing connections** |

So aiab is *not* alone here, and the claim this section was written to make does
not stand. Docker closed the gap between September's pass and this one, which
is the second time in two passes that `sbx` has shipped the thing an aiab
survey had just finished calling a differentiator. The narrower true statement:
aiab and Docker Sandboxes can both change mounts without rebuilding, and the
two tools with no live host working tree (OpenShell uploads; Gondolin defaults
to memory providers) cannot — which suggests the capability tracks *having a
live tree to mutate*, not any cleverness about devices.

On the network side the table still runs the other way, and OpenShell remains
ahead of us on the narrow point of tearing down connections an updated policy
no longer permits.

## Addendum: a sweep for movement (2026-10-06)

A deliberately quick pass over the previously-surveyed tools, looking only for
what changed. It turned up one new aiab finding, one stale claim of our own
(above), and a reminder that `sbx` moves faster than these surveys do.

### sandbox-runtime 0.0.77 and 0.0.78

Two releases since the 0.0.76 that prompted September's proxy fix, and 0.0.77
is dense with hardening. Three items are worth holding against aiab:

- **"A `/` write root (Linux) or read root (macOS) no longer switches off the
  denies beneath it."** A broad grant silently disabling narrower denies —
  the same bug shape as an allow for `x.com` swallowing a deny for
  `api.x.com`. aiab is clean here by construction: `evaluate()` decides on the
  longest matching rule across both lists, so specificity wins in either
  direction, and `tests/test_netproxy.py` covers it.
- **"Default write paths obey a read deny, and interior path spellings are
  canonicalised."** Checked: aiab resolves work-directory and mount paths
  through `Path.resolve()` (`cli.py:102`, `state.py:165`, and the batch paths
  at `cli.py:1244`/`1290`), so a symlinked or non-canonical spelling cannot
  make the recorded path and the guarded path diverge.
- **"violations: sanitize forged attribution keys."** The sandboxed process
  forging entries in the supervisor's own audit output. **This one we do not
  handle** — see below.

### The log-injection finding

`_log(f"DENY {method} {host}:{port}")` interpolates two attacker-controlled
strings. `method` is `request_line.split(maxsplit=2)[0]`, so any non-whitespace
bytes; `host` on the ALLOW/DENY path is whatever the request line carried
(`_SAFE_HOST_RE.fullmatch` gates only the *parking* path, not logging). Those
lines go two places: the per-container file under `~/.local/share/aiab/proxy/`,
and `aiab monitor`'s `RichLog` pane, which tails it (`monitor_tui.py:687`).

Measured rather than assumed, because Rich does strip some control characters:

```
input :  'DENY GET\x1b[31m\x1b[2Jevil\x07 host:443'
stored:  'DENY GET\x1b[31m\x1b[2Jevil host:443'
```

Rich's `Text` removes `BEL` but **keeps `ESC`**, so ANSI sequences survive into
the rendered line. Textual's `RichLog` defaults to `markup=False` (checked on
textual 2.1.2), so Rich *markup* like `[red]` is inert — but escape sequences
are a different channel and are not. For the plain log file the hazard is
unambiguous: `cat` or `tail` it and the escapes execute in the user's terminal.
For the TUI pane, corruption is likely but was not demonstrated headlessly.

Severity is low — display corruption, not an escape — but it is the exact class
sandbox-runtime just closed, it is cheap to fix, and the surface it corrupts is
the pane the user makes allow/deny decisions in.

### Docker Sandboxes: v0.38.0 → v0.46.0

Eight releases in two months, and the one that matters is the mount change
already folded into the table above. Also worth recording:

- **v0.45.0: "network access now fails closed with accurate errors" when policy
  evaluation fails or a request is malformed**, and **reverse-DNS lookups
  restricted to destination IPs already authorised by policy**. The second is a
  neat generalisation of the same family as our loopback problem — an
  unconstrained lookup as a side channel around a policy decision.
- **v0.45.0: experimental outbound UDP now follows sandbox network policy.** So
  Docker is growing a UDP story; aiab has no UDP at all by construction.
- **v0.45.0 fixed a credential-revocation bug** that "left running proxies
  authorized until sandbox recreation", and **v0.43.0 strengthened an OAuth
  gate to stop third-party kits inheriting built-in agent trust**. Both are
  consequences of credential injection — costs of the feature September
  weighed, now visible as shipped bugs.
- **v0.46.0 breaking change:** secret commands execute from a fresh temporary
  directory, so `./credential-helper`-style relative paths no longer resolve
  from the project directory. A path-resolution hole closed.
- Still no live denial prompt anywhere in the release notes.

### Canonical Workshop: no movement found

Nothing located between the 2026-05-27 announcement (v0.9.0) and today. The
September findings stand unchanged, including the two pages behind
readthedocs authentication. Absence of search results is weak evidence, but it
is the same weak evidence in both passes.

### The category around us has consolidated into hosted platforms

Not competitors, but worth naming so the shape of the field is recorded.
E2B (Firecracker microVMs per sandbox, own kernel and segmented network
namespace), Modal (gVisor with syscall interception), Northflank, Blaxel,
Firecrawl, plus sandbox features from Cloudflare, Vercel and Ramp, and
**LangSmith Sandboxes** in private preview on hardware-virtualised microVMs.
All of them are *remote code execution for agent-generated code* — no live host
working tree, no host credentials to defend, nothing on the user's machine.
They occupy the opposite corner from aiab on the axis that has organised three
passes now: configuration and execution both live with the vendor.

Also noted, as reading rather than as findings: two arXiv papers that bear on
the Scope argument — "Harness Engineering: Anatomy, Architecture, and Evolution
of Coding Agents — A Source-Code Study of Eleven Systems" (2609.00006) and
"Steerability via constraints: a substrate for scalable oversight of coding
agents" (2607.02389). Neither was read.

## Re-checked and still true

Prompted by specific Gondolin failures, and all the better for having been
asked rather than assumed.

- **aiab fails closed if the boundary cannot be established.** This was the
  question Gondolin #150 and #148 raised — their mediation silently stopped
  working. In `_apply_network_policy` (`aiab/cli.py`) the order is
  `mask_profile_devices()` *then* `ensure_proxy()` then `add_proxy_device()`,
  and `lxd.run()` passes `check=True` and re-raises, so a masking failure
  aborts the run rather than continuing with a live NIC, and a proxy failure
  leaves a container with no network rather than an unfiltered one. Both
  orderings are the safe ones.
- **The proxy cannot leak a credential into its own log.** `_log()` is only
  ever called with `METHOD host:port` — never the path, never the query
  string — so Gondolin's "do not log request headers/URLs from hooks" hazard
  has no analogue here. Over CONNECT aiab cannot see headers at all, and for
  plain HTTP it declines to log what it does see.
- **`connect_external()` still vets-then-connects.** No re-resolution between
  check and connect, which is the same property Gondolin gets from its guarded
  `lookup()`.
- **No DNS path at all in restricted mode.** The NIC is masked, so there is
  nothing for a DNS tunnel to ride; this is structurally stronger than
  Gondolin's `synthetic` default, which still answers queries.
- **`docs/commands.md`'s honesty about CONNECT holds up.** "Hostname filtering
  is policy, not adversarial containment: anything the agent can reach over an
  allowed CONNECT, it can tunnel arbitrary data through." Gondolin arrived at
  the same framing independently, with a sharper example — an allowed host that
  echoes headers back is an exfiltration endpoint.
- **The pending-file name cannot escape the pending directory.**
  `_SAFE_HOST_RE.fullmatch(host)` after lowercasing and stripping a trailing
  dot. Note the regex `[a-z0-9.-]+` does admit `.` and `..`; both then fail on
  `write_text` with `IsADirectoryError` and fall through to `DENY`, so the
  outcome is right but rests on the write failing rather than on the check.
  Rejecting those two explicitly would cost one line and make the guarantee
  local — Gondolin's `FsRpcService` makes exactly this check explicit.

One thing that is *not* capped, by contrast: `ProxyServer` is a
`ThreadingUnixStreamServer` with `daemon_threads = True` and no limit on
concurrent handlers, and a parked request holds its thread for up to
`_ASK_TIMEOUT` (60s) while writing a file named after the host into the pending
directory. Nothing bounds how many distinct unknown hosts an agent may ask for
at once. Gondolin puts DoS out of scope in its threat model and still capped its
SSH upstreams "to avoid guest-triggerable host DoS"; the reason to care here is
not host CPU but that the flood lands on the *human* — `aiab monitor` is the
thing being spammed, and prompt fatigue is a path to a bad decision being
clicked through. Low severity, and the fix is a cap plus a ceiling on pending
files.

## Could aiab be built on top of this?

Asked because Gondolin is the first tool in three passes that looks like a
*layer* rather than a rival, and the tempting reading is that it could replace
`lxd.py` and `netproxy.py`. Fair for one of those, not the other, and the
blocker is in neither.

**`netproxy.py` (566 lines) genuinely dissolves.** `is_external_address()`
becomes `blockInternalRanges` — which, per the top of this survey, is better
than ours. The CONNECT relay becomes frame-level mediation, and with it
`mask_profile_devices()` and `profile_nic_names()` disappear entirely, since
there is no NAT to mask. `evaluate()` survives as *our* logic transcribed into
`isRequestAllowed`, and parking survives on async `onRequest`. The mechanism
goes; the policy semantics move languages.

**`lxd.py` (881 lines) does not.** Lifecycle maps over — `create`, `start`,
`stop`, `exec`, `apply_limits` all have equivalents, and `init_pid`, the
project machinery and the `lxc query` caching are LXD bookkeeping that would
simply go. But the mount and overlay half (`add_device`,
`set_device_source`, `add_config_overlay`, `add_config_dir`) has no clean
home: Gondolin's VFS is documented at `VM.create()` time with no runtime API,
which is the gap tabulated above. And `mount_wayland` is lost outright.

**The blocker is in neither module.** Gondolin's guest is Alpine-only,
deliberately minimal, and extra packages mean building a new image. aiab's
premise is a real Ubuntu where `/setup-container` runs `apt install`, Docker
works inside, and `aiab base` tracks a release — that is `provision.py`,
`release.py`, `agents.py` and the base machinery, none of which has anywhere
to live. The swap takes out the two most mechanical modules and the product
with them.

The language mismatch is a wall rather than a detail. The hooks are SDK-only:
the CLI has no config file, no per-project state, and no way to express a
policy callback. So aiab could not shell out to `gondolin` and keep its own
brain — it would have to *be* the Node program. Against a tool that is
stdlib-only but for `textual`, with no `[build-system]` on purpose and run
from a checkout, that is not a port.

The number worth keeping: `lxd.py` + `netproxy.py` is 1,447 of 8,394 lines,
about 17%. `cli.py` (2,328), `monitor_tui.py` (1,254), `state.py` (796),
`agents.py` (419) and the attention/focus/notify/netwatch group (978) are the
bulk, and nothing in Gondolin's scope touches any of it — per-directory state
keyed by host path, three agents' homes and credentials, `_guard_git_repo`,
the monitor TUI, focus-aware suppression, lifecycle reaping, tmux. Even in the
best case Gondolin replaces the bottom sixth.

Which is its own answer, and the one worth carrying to IDEAS.md: **aiab is
mostly not a sandbox. It is a per-directory agent manager that happens to own
one.** That is the Scope section's anxiety arriving from a new direction.

So the counterfactual needs restating. The world where aiab-on-Gondolin works
is three changes deep — TypeScript, a disposable Alpine microVM instead of a
persistent Ubuntu machine, no Wayland, no apt — and at that point it is a
different tool wearing the name. The *slightly* different world is narrower and
more interesting: one where Gondolin's policy engine were a library independent
of its VM. Then `netproxy.py` is duplication, you keep LXD, and you consume an
egress policy someone else maintains full-time.

That is the separation Gondolin did not make, and could not: it owns the VM
precisely so it can own the frames. Which cuts against September's question 4
in a useful way — Gondolin is evidence the confinement layer is separable as a
*project*, and evidence against it being separable as a *library you bolt onto
someone else's container runtime*.

## Docker Sandboxes: the convergence tally

Stated plainly because three passes now point the same way: `sbx` is the only
tool surveyed that is converging on aiab's *answers* while having started from
the opposite premise, and it is doing it by attrition — a paid product whose
users keep hitting the same walls — rather than by reasoning.

Matching as of September: wraps the agent from outside, needs nothing in the
repo, per-workspace sandbox reused across runs, filesystem as an allowlist,
deny-by-default egress through a host-side proxy with wildcard domain rules,
seven agents, port forwarding, a real OS with sudo and Docker inside.

Added since: **dynamic mounts** (v0.45.0/v0.46.0, closing the differentiator
this very pass had just finished naming), reverse-DNS lookups restricted to
already-authorised IPs, failing closed when policy evaluation fails, and
outbound UDP under policy — which is further than aiab goes, having no UDP at
all.

Three things cannot converge, and they are structural rather than roadmap:

- **KVM on bare metal**, documented as not working inside a VM. aiab runs
  wherever LXD runs. That follows from choosing a microVM, and reversing it
  would mean adding a container mode.
- **Config beside the project** (`sbxenv.yaml`), which forecloses an
  `aiab mount --for DIR` equivalent: once config is a file next to each
  checkout, addressing another project's config is a path you have to know.
  This is the axis that has organised three passes.
- **The git guard is a chosen position, not a gap.** They document
  agent-modifiable git hooks in direct-mount mode as a limitation and answer it
  with clone mode — solving it by giving up the live working tree. The thing
  aiab protects, they decided not to have.

One thing is a release away. **Parked decisions are the last clear
differentiator in the egress space, and `sbx` already has most of the
machinery** — a dashboard with live monitoring and allowlisting. Three passes
with no live prompt says it is not a priority; OpenShell shipping an approval
loop says the idea is now in circulation. Treat the gap as borrowed time, not
a moat.

And the cadence matters: eight minor versions in two months against one survey
pass.

**The reframing this suggests for IDEAS.md.** The Scope section argues the
harness half is "where upstream moves fastest" while the sandbox half "still
has no upstream competitor", and that asymmetry is load-bearing for cutting the
first and keeping the second. Three passes say **both halves are contested,
just by different people**: agents compete on the harness half, sandbox tools
on the sandbox half, and `sbx` closes distance faster than these surveys run.
The remaining defensible combination is narrow and mostly structural — LXD
including inside a VM, nothing in or near the repo plus `--for DIR`, the git
guard on a live tree, GPL and no vendor account — with parked decisions as the
one contingent item. That is an argument from *combination*, not from absence,
and it is not the argument the Scope section currently makes.

## What a VM port would cost, by subsystem

Worked out because the microVM tools keep coming up, and answered against the
code rather than in the abstract. The three subsystems land in completely
different places.

**Notifications: nearly free, and by design.** Three of four pieces never
cross the boundary. `notify.py` is host-side `notify-send`, action buttons
included. `focus.py` is host-side tmux, reading `#{client_flags}`. Parking and
`netwatch` are two host processes sharing a host directory. Only
`attention.py` crosses, and it crosses via *files*: the agent writes
`STATE_MOUNT/attention/<session>` into the directory's state dir, shared at
`cli.py:735`. LXD VMs share host directories over virtiofs or 9p, so that
survives as-is — with two caveats to check rather than assume, that ownership
and idmap behave differently through virtiofs than through a container
bind-mount, and that LXD issue #13677 reports mount flags not being passed to
`lxd-agent` for *hotplugged* VM directory shares. Worth noting
`attention.py`'s own comment gets validated structurally: it keeps the host's
D-Bus session bus outside the container deliberately, and in a VM you could
not hand it over if you wanted to.

**netproxy: the policy is portable, the plumbing is not.** Unchanged:
`evaluate()`, `is_external_address()`, `connect_external()`,
`_await_decision`, `watcher_attached`, the per-request policy re-read, the
logging, the 403 bodies — all of it works on a parsed request and the host's
own network stack. Three things break:

1. **The delivery device is unavailable, not degraded.** Today it is
   `add_proxy_device(listen="tcp:127.0.0.1:3128", connect="unix:<abstract>",
   bind="instance")`. LXD implements that by setting up a host listener and
   then entering the container's network namespace to connect to loopback —
   exactly what it documents it cannot do for a VM, which runs its own kernel.
   Proxy devices in VMs are **NAT mode only**: DNAT rules, which cannot target
   a unix socket and presuppose the guest has a working NIC. aiab masks the
   NIC.
2. **The abstract socket goes, and so does its rationale.** It is abstract
   because snap-confined LXD cannot dial filesystem paths under `$HOME`;
   abstract sockets are per-netns kernel objects and do not exist across a VM
   boundary. Both the hack and the reason for it evaporate.
3. **`SO_PEERCRED` dies.** `verify_request` accepts uid 0 or our own uid, and
   its docstring is explicit that this peer-credential check is what stands in
   for socket file permissions. No shared kernel, no peer uid.

Two replacements, with different security character. **vsock plus a small
guest-side relay** (HTTP_PROXY TCP → `AF_VSOCK`) keeps "no NIC at all", keeps
fail-closed by construction, and authenticates by CID — at the cost of a guest
component aiab does not have. **A host-only bridge** with the proxy on the
bridge address and nftables denying the rest needs no guest component, but the
guarantee changes in kind, from "there is no network device" to "the firewall
rules are correct" — weaker in character even where equivalent in effect, and
effectively code-on-incus's model.

**Clipboard: this one actually breaks.** It is not a module. Clipboard is
`mount_wayland` (`lxd.py:799`) bind-mounting the host's Wayland socket at the
identical path inside, plus `wl-clipboard` in the guest (`agents.py:217-294`)
because Claude shells out to `wl-copy`/`wl-paste` for image paste-in and
copy-out. A unix socket is a kernel object rather than file content, so no
amount of virtiofs makes it connectable from a guest kernel. The options are a
Wayland proxy on both sides — the `wayland-proxy-virtwl`/sommelier family,
which exists precisely because VMs cannot do this — or a replacement.

The replacement is the interesting answer, because it is narrower *and*
better. What is used is clipboard get and set, not a compositor. A small
host-side service doing get/set over vsock with `wl-copy`/`wl-paste` shims in
the guest is less code than Wayland forwarding, and it replaces handing over
the entire compositor socket to obtain clipboard access — over-broad by the
same reasoning Workshop uses for declining to auto-connect
`system:ssh-agent`. So the VM move would force a narrowing that is arguably
correct on a container too.

**The rule underneath.** Everything host-side-only ports for free. Everything
crossing via *files* ports cheaply, over virtiofs. Everything crossing via
*kernel objects* — unix sockets, netns entry, `SO_PEERCRED` — breaks. All
three breakages are one fact in different clothes: a VM has its own kernel.
That is also the one-sentence reason Gondolin ships four daemons inside its
guest and aiab ships none.

**And the two destinations are not the same size.** An **LXD VM** keeps
`lxc exec` (via lxd-agent), disk devices, config, limits and snapshots, so
most of `lxd.py`'s lifecycle half survives and the damage concentrates in the
three subsystems above. A **Firecracker/Gondolin-style microVM** means owning
exec, filesystem sharing and networking yourself — a guest agent of your own,
which is the rewrite the layering section describes.

The fourth casualty is the git guard, which needs its own section.

## Shadow versus FUSE, and two gaps in the git guard

First what the guard *is*, because it is easy to describe wrongly and the
wrong description changes the conclusion. `_guard_git_repo` (`cli.py`) does not
mount the real files read-only. It copies the repo's `.git/hooks` into a
**host-side sidecar** under `state.git_guard_dir()` and overlays that at
`{container_path}/.git/hooks` **writable**; `.git/config` gets the same sidecar
treatment with `readonly=True`. `_reseed_tree`/`_reseed_file` refresh the
sidecars in place at setup — in place rather than by rename, deliberately, so
a sidecar already bind-mounted into a reused running container reflects the new
contents.

So hooks the agent plants land in a host directory outside the repo, never fire
on the host, and are wiped at the next setup. **That is Gondolin's
`ShadowProvider(writeMode: "tmpfs")` implemented with bind-mounts** — already,
today, for one of the two guarded paths. Which reframes the question of
whether aiab should "go full FUSE": it is further along that road than the
mechanism suggests.

Two gaps, both current behaviour rather than speculation:

1. **`.git/config` is read-only where it could be a shadow.** This is the wart
   359e4b7 had to document, and the thing IDEAS.md's PAT plan routes around —
   the `insteadOf` rewrite is forced into the container's *global* git config
   because `.git/config` is read-only. A writable sidecar would let the
   container write its own config with the host's copy untouched, and the
   semantics are already understood from hooks. A constraint becomes a
   feature.
2. **Repos whose `.git` is a gitfile are unguarded.** The docstring says so: a
   no-op "when its `.git` is a gitfile rather than a directory (e.g. a linked
   worktree or submodule checkout mounted directly) — there the `.git/hooks`
   and `.git/config` paths don't live where we'd mount them". The skip is
   correct given the mechanism, but the hole is real: a submodule checkout
   mounted read-write gets no guard at all today.

**What full FUSE would buy.** Predicates instead of enumerated paths, which is
the general form of gap 2 — a provider could follow the gitfile wherever it
points, where a bind-mount must resolve and enumerate each case. Per-operation
policy: allow-with-logging, deny `chmod` while allowing writes, merged views.
And mount count stops scaling with guarded paths.

**What it costs, with Gondolin as the evidence.** Its last release window
shipped five VFS correctness fixes — a TOCTOU in `RealFSProvider` path
validation (#143, closed by serialising fs-rpc), truncate coherence for
`MemoryProvider` handles, serialising concurrent guest file operations,
shadowed `mkdir` under backend-only parents, and forwarding guest `chmod` to
providers. Write the layer, own its bugs; the same pattern as their userspace
TCP stack. Two objections are specific to aiab:

- **Performance runs into the premise.** Gondolin can afford FUSE because its
  guest has no host filesystem by default and the advice is `MemoryProvider`
  for scratch, with RPC payloads capped at `MAX_RPC_DATA = 60 KiB`. aiab's
  premise is a live working tree under active development — `git status` on a
  large repo, builds, ripgrep, `node_modules` — which is the syscall-storm
  workload FUSE is worst at.
- **It is an enforcement downgrade.** A read-only bind-mount is enforced by
  the kernel. A userspace predicate is a weaker primitive with a
  check-then-use window, which is literally what #143 was.

Plus a FUSE filesystem in the hot path of every file operation, in a tool that
is stdlib-only apart from `textual`.

**And the sequencing matters.** FUSE is not an alternative to solving the VM
transport problem; it is built on top of it. A guest FUSE client has to talk to
a host service — `sandboxfs` + virtio-serial + `FsRpcService`. Once there is a
guest agent for the filesystem *and* a vsock relay for the proxy, Gondolin's
architecture has been rebuilt in Python. So full FUSE earns its cost if and
when a VM move makes host-mediated filesystem access mandatory rather than
optional — not as a way to improve the guard on a container, where extending
the shadow pattern already in the code is far cheaper.

## Open questions this leaves for IDEAS.md

1. **Block `100.64.0.0/10` in `is_external_address()`, and decide the rule.**
   The narrow fix adds the one range. The question worth settling is whether
   the predicate should stop enumerating CPython's notions of private and
   invert to `ip.is_global` instead, which answers "does this leave the
   machine" directly and absorbs future IANA special-purpose allocations
   without an edit. There is a trap in that, already measured: `::127.0.0.1`
   and `64:ff9b::7f00:1` both report `is_global=True` and are currently refused
   only because `is_reserved` covers `::/8`, so a straight swap to `is_global`
   would *open* two cases the present code closes. The defensible version is
   `is_global` **and** the existing predicates, not instead of them. Either
   way the new range belongs in `tests/test_netproxy.py` next to the others.
2. **Decide whether the egress policy should know about ports.** Today it does
   not, so an allowed host is allowed on every port. Gondolin gates ports
   explicitly and denies CONNECT outright. Doing nothing is defensible under
   the stated threat model — but if the ssh-egress idea below lands, "allowed
   on 22" becomes a decision worth making deliberately rather than inheriting.
3. **Add Gondolin's SSH-egress shape to the git-remote credential entry** as a
   third option beside the scoped PAT and the host-does-the-push helper. The
   entry currently rejects "forwarding the agent" as too broad, which is right
   about the broad version and wrong about this one: with the socket on the
   host and an `execPolicy` equivalent deciding each request, the breadth
   objection goes away. Two implementations to weigh — a stdlib filtering
   ssh-agent socket over the existing port-blind CONNECT, or a real SSH server
   in the proxy at the cost of aiab's first non-`textual` dependency.
4. **If git credential injection is ever built, the `Authorization: Basic`
   decode/substitute/re-encode step is the detail that makes or breaks it**,
   and the per-secret host allowlist (separate from the egress allowlist) is
   the right shape. Both now have a reference implementation to copy rather
   than a design to invent.
5. **Cap the parking machinery** — concurrent handler threads and pending
   files. Framed as protecting the human from a prompt flood, not as a DoS
   fix.
6. **Two one-line hardenings** worth doing or consciously declining: rewrite
   the `Host:` header from the absolute URI on the plain-HTTP path, and reject
   `.`/`..` in `_SAFE_HOST_RE` rather than relying on `write_text` to fail.
7. **The Scope section's "no upstream competitor" line is still the one to
   rewrite**, carried forward from September unresolved. Gondolin does not
   change the conclusion — it is not a competitor for aiab's audience, it is a
   primitive for people building agent products — but it is a fourth tool
   confining an agent from outside, and it does two things aiab does not
   (credential substitution, SSH egress with host-held keys). The remaining
   niche stated in September survives intact.
8. **Gondolin is the first tool surveyed that could plausibly be a dependency
   rather than a rival**, which is a question the Scope test has no slot for.
   Not actionable — it is TypeScript and aiab is Python-on-LXD — but the policy
   engine being separable from the product is a structural idea worth noticing,
   and it is the mirror of September's question 4 about aiab's confinement layer
   belonging in Workshop.
9. **Rewrite the Scope section's parked-decision argument.** It currently rests
   partly on "no agent can ask you to widen a boundary it cannot see", and
   OpenShell's `policy.local` gives the agent exactly that. The conclusion
   (parked decisions are box work) is untouched — only the thing that imposed
   the boundary can lift it — but the supporting clause has to go, and
   September's "no prior art found, and this is the novel one" needs replacing
   with the narrower true claim: aiab holds the request open where everyone
   else fails it and asks you to retry.
10. **Let the agent attach a reason to a denial.** Today it gets a 403 whose
    text addresses the human (`run 'aiab net allow' on the host`), and the
    pending file carries only a timestamp. OpenShell's `policy.local` lets the
    agent read its recent denials, propose a rule, say why, and wait. The cheap
    version here is one line of context in the pending file surfacing in
    `aiab monitor` alongside the host — the agent already knows what it was
    trying to do, and the human deciding currently does not. Weigh against the
    obvious objection: the justification is written by the thing being
    confined, so it informs the human without being evidence.
11. **Close established tunnels when policy narrows.** `aiab net deny` takes
    effect for the next request and not for a tunnel already up. This pass
    first recorded that as inherent to tunnels; OpenShell does it ("the sandbox
    loads new rules and closes previous connections"), so it is a choice, not a
    constraint. The proxy owns both sockets and could drop them.
12. **Should the egress policy know about binaries, not just hosts?**
    OpenShell pairs each rule with the binary that opened the connection, so
    `curl` and the agent can have different reach. aiab cannot see this — the
    container's processes are behind a unix socket and `SO_PEERCRED` gives a
    pid, which would need resolving against the container's namespace, and
    the agent can run anything anyway. Probably a no; worth recording as
    considered, because it is the most expressive egress policy the three
    passes have seen.
13. **Sanitise `method` and `host` before logging them.** Strip or escape
    non-printable bytes in `_log()`, or validate `method` against the HTTP
    token grammar and reuse `_SAFE_HOST_RE` on the logging path as well as the
    parking path. Low severity, two lines, and it protects the pane the user
    decides in. sandbox-runtime shipped the equivalent in 0.0.77.
14. **Make `.git/config` a writable sidecar, like `.git/hooks` already is.**
    The mechanism exists and the semantics are proven; what changes is one
    `readonly=True`. It removes the wart 359e4b7 documents and unblocks the
    `insteadOf` rewrite the PAT plan currently pushes into the container's
    global git config. Decide deliberately what is lost: config the agent
    writes stops being visible on the host, and is discarded at the next
    reseed.
15. **Guard repos whose `.git` is a gitfile.** Linked worktrees and submodule
    checkouts mounted read-write get no guard today, by a deliberate skip. The
    fix within the current mechanism is to resolve the gitfile and overlay at
    the real location; the general fix is a predicate, which is the FUSE
    argument.
16. **Rewrite the Scope section's competitive claim around *combination*, not
    absence.** Supersedes the September carry-over in item 7. The sandbox half
    now has four or five competitors and `sbx` is converging on our answers at
    eight releases to our one survey; the harness half is contested too. What
    is left is a narrow structural combination, and parked decisions are the
    one contingent item in it.
17. **If a VM port ever becomes live, the budget is three breakages and one
    rewrite.** The proxy delivery device, the abstract socket and
    `SO_PEERCRED` all go; the git guard needs a different mechanism entirely,
    because sidecar overlays of a single file are a container trick. Decide
    the destination first — an LXD VM keeps most of `lxd.py`, a bare microVM
    keeps none of it.
18. **A fleet view and an off-desktop view are the same feature, and webterm
    shows the cheap version.** September recorded both gaps separately
    (`aiab monitor` is per-directory; `notify-send` dies over ssh). A dashboard
    of live terminal tiles over HTTP answers both without an app, an account or
    a push protocol. No position taken on whether aiab should grow one — but
    if it ever does, this is the shape, and it is smaller than the mobile-app
    designs September was looking at.

## What was not verified

- **Gondolin's release notes carry no changelog bodies.** The releases page
  lists versions and artifacts only, so "mine the changelog" was done from
  commit subjects and the issue tracker instead. The repository has a
  `CHANGELOG` (there is a `docs(changelog): add missing unreleased entries`
  commit) which was not read; a second pass should start there.
- **The CGNAT finding is a unit-level observation, not an end-to-end
  demonstration.** `is_external_address("100.64.0.1")` returns True, and
  nothing in the test suite says otherwise — but the dev container has no DNS
  egress, so an allowed name actually resolving into `100.64/10` and reaching a
  tailnet peer was not exercised. Same limitation September recorded for the
  original fix.
- **The plain-HTTP `Host:` mismatch was reasoned, not demonstrated.** That a
  CDN edge would serve another site's content for an allowed name's address
  follows from how `Host:`-routed edges work; it was not tested against a real
  one.
- **Nested virtualisation.** Gondolin's docs never mention it. KVM-when-
  available with a TCG fallback implies it degrades rather than refuses inside
  a VM, which is better than Docker Sandboxes — but that is inference from the
  acceleration page, not a documented statement.
- **No interactive approval path is shipped — but one is implementable, which
  is a weaker result than "no prior art".** Checked in `docs/sdk-network.md`
  after the fact, because the hook signatures decide it. The allow-checks are
  synchronous and so cannot hold a request open:
  `isRequestAllowed: (req: Request) => boolean`,
  `isIpAllowed: ({ ip }) => boolean`, and `execPolicy: (req) => { allow: boolean }`
  for SSH. But `onRequest` is async —
  `async (req: Request) => Request | Response | undefined` — and returning a
  `Response` short-circuits the request. That is precisely aiab's parking
  semantics: await a human, then proceed or refuse. So an embedder can build
  parking on Gondolin, on the mediated HTTP/TLS path only; the SSH and
  mapped-TCP paths are pre-authorisation with a sync hook, and Gondolin has no
  CONNECT to park. So "no human in its loop by construction" was wrong; the
  correct claim is narrower — no human in its loop by default, with the
  extension point to add one on one of its three egress paths. For what ships
  rather than what is possible, see
  [OpenShell](#openshell-the-approval-loop-we-thought-was-ours): aiab is not
  alone in having built a human into the denial path, only in holding the
  request open rather than failing it.
- **Read at reference level only**, not exercised or deeply checked:
  `--disable-websockets`, the ingress gateway (`--listen`, `sandboxingress`),
  snapshot/resume, the libkrun backend, and `openTcpStream()`'s loopback-only
  restriction on guest-side backchannels.
- **Gondolin's runtime VFS is undocumented, not documented as absent.**
  `docs/vfs.md` describes provider configuration at `VM.create()` and no
  runtime API, and a `vm.fs` interface is referenced in a commit subject
  (`fix(examples): use vm.fs in pi example and stop forwarding host env`)
  without appearing on the page read. So the mount-mutation row in the table
  above is "not documented" for Gondolin, where it is a quoted "requires a new
  sandbox" for OpenShell. Worth settling against the source before the
  comparison is leaned on.
- **OpenShell's policy schema was not read.** The management page points at a
  "Policy Schema Reference" that was not fetched, so the
  `host:port:access:protocol:mode` endpoint shape and the one worked example
  (`api.github.com:443:read-only:rest:enforce`) are from a management page,
  not the schema. The `access` presets, the protocol values and the
  enforcement modes were never enumerated — in particular, that `enforce`
  implies a non-enforcing mode is inference from the word.
- **OpenShell's credential handling has two incompatible accounts in the
  sources** and this pass did not resolve them: one secondary description says
  the gateway "injects your API keys as environment variables", while the
  quickstart says OpenShell "stores its value in the credential store and
  sends it only to `openrouter.ai`". The second implies proxy-side injection
  and fits the `credential_endpoint_mismatch` response code; the first is a
  much weaker guarantee. Settle this before citing OpenShell either way on
  credentials.
- **Whether OpenShell's gateway is local or remote was not established.** The
  quickstart does not say. That `openshell sandbox upload`/`download` move a
  working folder rather than bind-mounting it comes from *pi's*
  containerization doc, not from OpenShell's own, and the inference that this
  is why its filesystem policy can be creation-time only is mine.
- **pi's Gondolin extension source was not read**, only pi's containerization
  doc. "Mounts static per invocation" is that doc's framing; whether the
  extension itself can remount is unknown.
- **The Hacker News thread was read as a summary**, not comment by comment, so
  the three approaches attributed to it are the substance rather than a
  faithful census.
- **agentbox and webterm were read at README level only.** And "agentbox" names
  at least seven unrelated projects — the one here is `rcarmo/agentbox`,
  identified because that author also wrote webterm and the HN reply paired
  them. A different agentbox may have been meant.
- **Which sandbox-runtime version shipped the loopback fix is now in doubt.**
  September recorded "refuse allow-listed hostnames that resolve to loopback,
  link-local or configured private ranges" as 0.0.76 (2026-09-10); the release
  listing read today attributes that same sentence to 0.0.77, and rendered the
  dates in 2024, which cannot be right given 0.0.76's known date. The
  `CHANGELOG.md` fetch 404'd. The *content* of the fix is not in question and
  a52da9b stands either way, but do not cite a version number from this pass
  without re-checking.
- **Docker's release notes were read as a summary of v0.38.0 onward**, not
  release by release, so "eight releases in two months" is counted from version
  numbers rather than from a list, and items are attributed to the version the
  summary named.
- **The TUI half of the log-injection finding was not demonstrated.** That Rich
  keeps `ESC` in `Text.plain` and that `RichLog` defaults to `markup=False`
  were both measured here; that an escape sequence then corrupts a running
  Textual pane is inference. The log-file half needs no demonstration.
- **"No movement found" for Workshop is absence of search results**, not a
  checked changelog. Its repository releases were not fetched this pass.
- **The hosted-platform list is from search summaries only.** No vendor
  documentation was read for E2B, Modal, Northflank, Blaxel, Firecrawl or
  LangSmith Sandboxes; they are recorded as the shape of the field, and no
  claim here should be relied on.
- **The VM-port analysis is a desk exercise.** No LXD VM was launched and
  nothing was measured. The LXD facts behind it — proxy devices in VMs being
  NAT-mode only, host directory shares going over 9p/virtiofs, `lxc exec`
  depending on lxd-agent — come from LXD documentation and forum text read on
  2026-10-07, not from running any of it. Issue #13677 was read as a title.
- **"A clipboard get/set service would be enough" is a design claim, not a
  verified one.** What `wl-copy`/`wl-paste` actually need from the compositor
  for Claude's image paste path (MIME negotiation, offers, multiple types) was
  not checked.
- **The FUSE performance argument is qualitative.** Nothing was benchmarked,
  here or against Gondolin; it rests on the shape of the workload and on
  Gondolin's own 60 KiB RPC cap and memory-provider-first advice.
- **Gondolin's five VFS fixes are commit subjects and issue titles**, not
  diffs. The TOCTOU characterisation of #143 follows from its title and the
  fix that closed it.
- **The 17% figure is `wc -l`**, which counts licence headers and this
  project's unusually heavy comments. It measures bulk, not effort or
  difficulty.
- **Nothing was run, and nothing was installed.** Every claim about Gondolin,
  pi, OpenShell, agentbox and webterm comes from published documentation,
  commit logs, issue titles and README text as of 2026-10-06. Star counts are
  from that date.
