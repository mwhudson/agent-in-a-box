# Landscape survey, October 2026

A pass over **Gondolin** (<https://github.com/earendil-works/gondolin>), taken
2026-10-06 from its published docs, commit log and issue tracker. The previous
pass was [September](landscape-2026-09.md), 2026-09-14.

**Deliberately unlinked** from [docs/README.md](README.md), for the same reason
as its predecessor: working material, not user documentation, kept out of
IDEAS.md so that file stays reasoning-about-aiab rather than
reasoning-about-everyone-else.

A single-subject pass, prompted by the security page rather than by a sweep of
the field. It earned its keep anyway: Gondolin's blocked-range list named a
range ours does not block.

- [What the survey changed](#what-the-survey-changed)
- [Where Gondolin sits](#where-gondolin-sits)
- [The egress models, compared properly](#the-egress-models-compared-properly)
- [Secrets: the fourth implementation](#secrets-the-fourth-implementation)
- [SSH egress: a design IDEAS.md does not have](#ssh-egress-a-design-ideasmd-does-not-have)
- [Filesystem, images, and the cost of a microVM](#filesystem-images-and-the-cost-of-a-microvm)
- [Re-checked and still true](#re-checked-and-still-true)
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
- **No interactive approval path was found**, and that is absence of evidence
  rather than a documented no. Everything in the CLI and SDK is
  pre-authorisation (`--allow-host`, `isRequestAllowed`, `execPolicy`), with no
  sign of a request being held open while someone is asked. Consistent with
  being a library — there is no human in its loop by construction — so parked
  network decisions remain without prior art after a fourth tool.
- **Read at reference level only**, not exercised or deeply checked:
  `--disable-websockets`, the ingress gateway (`--listen`, `sandboxingress`),
  snapshot/resume, the libkrun backend, and `openTcpStream()`'s loopback-only
  restriction on guest-side backchannels.
- **Nothing was run.** Gondolin was not installed; every claim about it comes
  from its published documentation, commit log and issue titles as of
  2026-10-06.
