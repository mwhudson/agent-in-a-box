# Landscape survey, September 2026

A snapshot of the tools that overlap aiab, taken 2026-09-14 (the previous pass
informed IDEAS.md around 2026-08-17). **Deliberately unlinked** from
[docs/README.md](README.md): this is working material, not user documentation,
and it is parked here rather than in IDEAS.md to keep that file's
reasoning-about-aiab separate from reasoning-about-everyone-else.

Everything below is dated. The space moves fast enough that a claim about a
competitor is worth re-checking before it is acted on, not least the two the
survey found already stale in our own docs.

- [What the survey changed](#what-the-survey-changed)
- [The triple: aiab, Workshop, Docker Sandboxes](#the-triple-aiab-workshop-docker-sandboxes)
- [Canonical Workshop](#canonical-workshop)
- [Docker Sandboxes](#docker-sandboxes)
- [Everything else worth naming](#everything-else-worth-naming)
- [Notifications and attention](#notifications-and-attention)
- [Open questions this leaves for IDEAS.md](#open-questions-this-leaves-for-ideasmd)
- [What was not verified](#what-was-not-verified)

## What the survey changed

**One bug, found and fixed.** `sandbox-runtime` 0.0.76 (2026-09-10) shipped
"refuse allow-listed hostnames that resolve to loopback, link-local or
configured private ranges". aiab had the same hole: the proxy decided on the
request's *name* and then handed that name to `socket.create_connection()`,
which resolves it again on the host. Since the proxy dials out from the host
and the container's NIC is masked, an allowed name resolving inward was a
tunnel to the host's loopback services or the user's LAN — and allowing
`example.com` allows every `*.example.com`, whose DNS an attacker may control.
Fixed in a52da9b: `is_external_address()` vets the resolved address and
`connect_external()` connects to the address that was vetted, rather than
re-resolving after the check.

**Two claims in our own docs went stale.**

- `concepts.md` says the sandbox half has no upstream competitor. That is now
  wrong twice: Docker Sandboxes went generally available 2026-01-30, and
  Canonical's own Workshop shipped 2026-05-27. The IDEAS.md Scope section
  leans on the same claim, where it is load-bearing for the argument that the
  harness half should be cut and the sandbox half kept.
- Still *accurate*, and worth knowing it was re-checked: the claim that
  `sandbox-runtime` "denies writes and network by default but allows reads
  everywhere". Its README still documents reads as allowed everywhere with
  `denyRead` as an opt-in carve-out, writes as allow-only, network as
  allow-only. The "listing the secrets is the wrong shape for the problem"
  line stands.

## The triple: aiab, Workshop, Docker Sandboxes

Three tools that put a coding agent in a container, distinguished less by
their isolation than by **where their configuration lives** — which turns out
to predict nearly everything else about them.

| | Config location | Committed? | Audience |
| --- | --- | --- | --- |
| Workshop | `.workshop/<name>.yaml` or `workshop.yaml` at project root | Yes, version-controlled and shared with contributors | Projects that **adopt** it |
| Docker Sandboxes | `sbxenv.yaml` in a wrapper directory *beside* the project | No, but filesystem-local | One developer, per project |
| aiab | `~/.local/share/aiab/*.json`, keyed by directory | Nothing in or near the repo | Repos that know nothing about you |

Workshop's YAML *is* the deliverable: reproducible on any machine, reviewed in
a pull request. Docker asks you to restructure your checkout layout so the
config sits outside the mounted directory. aiab centralises state on the host,
which is what makes `aiab mount --for DIR` possible at all — once config is a
file beside each project, addressing another project's config stops being a
flag and becomes a path you have to know.

The README's "long tail that ships nothing" is precisely the gap Workshop
leaves, since Workshop requires the project to have adopted Workshop.

What each is actually isolating differs too:

- **Workshop** isolates the *environment* from the host. Reproducibility
  first, confinement as a property of unprivileged containers.
- **aiab** isolates the *agent* from everything. Confinement first.
- **Docker Sandboxes** isolates the *VM* from the host, and credentials from
  the agent.

And the premises diverge at the root. Workshop's "agent-ready" means *an agent
can work here* — opencode is an SDK you install **inside** the environment.
aiab and Docker wrap the agent **from outside**, on the premise that the agent
may not cooperate. Workshop assumes a developer who wants a good environment
and happens to use an agent; aiab assumes the agent is what you are defending
against. Neither is a gap in the other; they are different bets.

## Canonical Workshop

<https://github.com/canonical/workshop>, announced 2026-05-27. LXD 6.8+,
installed as a snap. aiab's exact stack — LXD system containers, tied to a
project directory, Ubuntu — from the same employer.

- **Interfaces, not ad-hoc grants.** A snapd-inspired plug/slot system governs
  host resource access. Built-in slots include `system:camera`,
  `system:desktop`, `system:gpu`, `system:mount`, `system:ssh-agent`, plus
  `custom-device` and `tunnel`. `gpu` and `mount` auto-connect; `ssh-agent`
  deliberately does not, for security reasons.
- **Definition keys**: `name`, `base` (ubuntu@20.04 through ubuntu@26.04),
  `sdks`, `connections` (explicit plug-to-slot wiring on top of auto-connect),
  and `actions` (named shell scripts run via `workshop run`).
- **Commands**: `workshop init`, `launch`, `shell`, `run`, `refresh`.
- **No egress filtering.** The only network interface is `tunnel`, which
  *forwards* an address or Unix socket. There is no allowlist, no proxy, no
  domain policy anywhere in the public reference. The default posture is not
  documented, which is itself the answer: an LXD container with ordinary
  networking.
- **No git guard**, which follows from the premise — a committed
  `workshop.yaml` implies a repo you already trust.

So the two things aiab does that nothing else in the triple does — the
deny-by-default egress allowlist with parked decisions, and the git guard —
are exactly what Workshop lacks. And Workshop's interface system is a far more
principled version of what aiab does ad-hoc across `mount`, `env`, `limits`,
Wayland passthrough and port forwarding.

## Docker Sandboxes

`sbx`, generally available 2026-01-30, v0.38.0 as of 2026-08-06. Free for
commercial use; org governance is paid. No Docker Desktop or Docker Engine
required.

### Where it overlaps aiab almost exactly

Wraps the agent from outside; needs nothing in the repo; per-workspace sandbox
reused across runs and persisting until removed; filesystem as an allowlist
(only the working directory, the agent config directory, and a staged kit are
mounted); deny-by-default egress through a host-side proxy with wildcard
domain rules (`sbx policy allow network` ≈ `aiab net allow`); seven agents
(Claude Code, Codex, Copilot, Gemini, opencode, Kiro, Docker Agent); port
forwarding via `sbx ports`; a real OS inside with sudo and Docker.

### Where it differs structurally

- **MicroVM, not a system container** — own kernel, and UDP/ICMP blocked at
  the network layer. Stronger on paper, but it requires **KVM on bare metal**
  and is documented as not working inside a VM. aiab runs wherever LXD runs.
  That trade cuts aiab's way more often than the hypervisor argument suggests.
- **Linux is shipping**, contrary to the January blog post's "coming soon":
  the releases repo carries `DockerSandboxes-linux-amd64-ubuntu2604.deb` and
  Rocky 8 RPMs. (Arm's own install guide describes Linux as aarch64-only, but
  that is an Arm-authored page describing Arm; the release artifacts are
  better evidence.)

### Credential injection, and what it costs

The headline feature, and the one worth understanding properly before copying
it. Sandboxes start with `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` set inside
the VM to the sentinel `proxy-managed`. The agent sends the placeholder; the
host proxy substitutes the real key. Docker's docs: "the token is never stored
in plaintext inside the sandbox".

This requires **TLS interception**, and Docker does it: a leaf certificate
minted per intercepted CONNECT, signed by an ephemeral
`CN=Docker Sandboxes Proxy CA`, with the CA path injected into the child
environment pointing at a PEM holding both the ephemeral CA and the system
roots. Interception is **selective** — applied to hosts where a credential is
injected. (Issue #480: `api2.cursor.sh` is intercepted because Cursor uses
proxy-managed OAuth, while "other agents like OpenCode remain unaffected
because their provider hosts aren't intercepted".)

Three consequences:

1. **Those connections are not end-to-end encrypted**, and Docker says so.
   Prompts and completions pass in cleartext through a vendor-authored process
   on the host. Local, not remote — but a different trust posture from a
   CONNECT tunnel, and the price of the credential guarantee rather than a
   bonus on top of it.
2. **MITM on streaming APIs is where it breaks.** Issue #480: the proxy
   buffers the entire response body even when the response is
   `text/event-stream` with `Transfer-Encoding: chunked`, ignoring the
   origin's do-not-buffer signalling. Cursor's agent becomes unusable — six
   frames a second apart arrive as one chunk after five seconds. No maintainer
   response. LLM API traffic is all streaming SSE, so this is the load-bearing
   failure mode of the design, not an edge case.
3. **Tools that ignore the system CA store break** — Java and Node are the
   named offenders. Claude Code is Node, so this leans on
   `NODE_EXTRA_CA_CERTS`.

### Mounts, versus `aiab mount`

Two mechanisms. Ad-hoc positional arguments (`sbx run claude . ~/other:ro`,
read-only via a `:ro` suffix), and a declarative per-project `sbxenv.yaml`
that is considerably broader than `mounts.json` — workspace plus extra
mounts, env vars, port mappings, CPU/memory/GPU/USB/display, credentials from
vaults, MCP server registrations, kits, and host-side lifecycle commands, with
a user-level `~/.sbxenv.yaml` for defaults and strict schema validation.

Four differences from aiab:

- **No `--for` equivalent.** No way to configure another project's sandbox
  from where you are standing; you `cd` there or you find that project's
  `sbxenv.yaml`. This follows directly from config living beside each project.
- **Changing the mount set destroys the sandbox** ("if the required mount set
  changes, the previous sandbox is removed and a fresh one is created"). aiab
  applies to running containers immediately, and `Container.add_device()`
  (`aiab/lxd.py`) reconciles the readonly flag in place via `lxc config device
  set`, so `aiab mount --rw DIR` flips an existing mount without recreating
  anything. On Docker, adding a reference checkout costs whatever the agent
  installed in that VM. Issue #198 asks for post-creation mounts and concedes
  "rm + re-create is maybe good enough".
- **Cross-agent replay.** aiab records mounts against the *directory*, so all
  three agent containers pick them up. `sbxenv.yaml` names a single `agent:`.
- **Neither controls the mount target.** aiab uses `/work/<basename>` with
  collision suffixing; Docker mounts at the host's absolute path. Issue #198
  asks Docker for explicit `source:target:ro` syntax; marked duplicate, no
  maintainer response.

### Where aiab is still ahead

- **The git guard.** Docker's security page lists as a *documented limitation*
  that in direct mount mode the agent can modify git hooks, CI configs and
  build scripts on the host before review. That is what `_guard_git_repo`
  closes. Their alternative is clone mode — host repo read-only at
  `/run/sandbox/source`, agent works in a private clone — which avoids the
  problem by giving up the live working tree.
- **Parked network decisions.** The proxy holds the request open while `aiab
  monitor` asks. Docker has a dashboard with live monitoring and allowlisting,
  so this is the same territory; whether they park the in-flight request or
  fail it and let you retry was not established.
- Per-directory agent homes with credentials separated per agent, Wayland
  passthrough, no vendor account, GPL-3.

### Where Docker is ahead

Credential injection (above); an **MCP gateway** on the host brokering
registered MCP servers and enforcing policy before tool execution, where
aiab's MCP servers run inside the container unconstrained; `sbx policy log`,
which is the structured network audit log IDEAS.md still has as an idea; kits
as shareable registry-pushed environment definitions; and SSH/editor
integration for VS Code and Cursor.

## Everything else worth naming

- **Claude Code now documents the choice.** A "Choose a sandbox environment"
  page compares six approaches and states plainly that the built-in Bash
  sandbox leaves MCP servers and hooks running unconstrained on the host. This
  is now the canonical framing `concepts.md` is arguing against.
- **lincubate** (<https://github.com/popey/lincubate>) — the nearest neighbour
  after Workshop. LXD, one container per project directory, project
  bind-mounted with UID mapping, seven agents, `lb.toml` config hierarchy.
  Credential sharing is off by default but is env-var forwarding when on, and
  there is no real network-restriction story.
- **code-on-incus** grew "active defense": kernel-level monitoring for reverse
  shells, C2, exfiltration and DNS tunnelling, auto-pausing on HIGH and
  killing the container on CRITICAL. Three nftables modes
  (restricted/allowlist/open) and a one-flag `hardened` profile.
- **Credential brokering has converged from three directions** — nono (phantom
  credentials, real secret injected by a trusted proxy outside the sandbox),
  Buildkite's Cleanroom (host-side gateway injects git and registry
  credentials on the upstream leg, scoped by a `cleanroom.yaml` policy), and
  Claude Code on the web (a proxy holds the GitHub token outside and issues
  scoped credentials inside). Three independent implementations is the
  strongest signal in this survey.
- **fence** does command-level deny policies (`git push`, `rm -rf /`) —
  adjacent to the git guard, at a different layer. **litterbox** does GUI
  sandboxing via Wayland forwarding, which is parity with what aiab already
  has, not a gap.
- A claim worth engaging rather than accepting: one widely-cited survey argues
  the market "has shifted decisively toward OS primitives over containers,
  with every healthy new entrant choosing container-free architectures since
  late 2025". It surveys mostly macOS-leaning local CLI wrappers; Docker
  Sandboxes, Cleanroom, code-on-incus and Workshop all went the other way in
  2026. But `concepts.md` argues against process sandboxes on the merits and
  has no answer to "the ecosystem has moved on", which is the sentence that
  will get asked about.

## Notifications and attention

A different axis from isolation, and the one where aiab turns out to have the
most that is actually unlike anything else: telling you, on the host, that
something in the box wants you. Surveyed because the recent `aiab.attention` /
`aiab.notify` / `aiab.focus` work looked like obvious parity catch-up and on
inspection mostly is not.

**The agent-waiting half is well-trodden.** Both agents ship it. Claude Code
has `preferredNotifChannel` (`auto`, `terminal_bell`, `iterm2`,
`iterm2_with_bell`, `kitty`, `ghostty`, `notifications_disabled`) plus the
`Notification` hook, which fires with type `permission_prompt` when it needs
approval and `idle_prompt` when it is done and waiting; desktop notifications
are native only in Ghostty, Kitty and iTerm2. opencode emits `session.idle`,
permission, question and error events, and has at least five community
notification plugins. So aiab reimplements something that exists — but once,
from outside, for three agents, instead of three plugins with three different
ideas of what an event is. The managed-settings drop-in is also a better
citizen than most of those plugins, which write into the user's own settings;
hook lists concatenate across sources, so aiab's can neither displace the
user's nor be displaced by them.

**Focus-aware suppression: no prior art found.** The nearest neighbour is
`claude-tmux-focus`, which on a `Notification` event switches to the tmux
window, focuses the pane, highlights it and notifies — the *inverse* of
`aiab.focus`, using tmux to pull you toward the agent rather than to decide
whether to interrupt you, and it notifies unconditionally either way.

`focus.py`'s reasoning for why neither agent can answer this checks out
independently: Claude's hook events stop at `UserPromptSubmit`, opencode's at
`chat.message`, and Claude's own focus tracking never leaves its TUI. Claude
Code issue #72067 is worth knowing about here — their tmux focus-events
integration is actively awkward (focus-out forces a TUI re-render, users
disable focus-events to stop it, Claude then nags them to re-enable it).
Reading `#{client_flags}` from tmux on the host sidesteps that entirely, and
is only available to aiab *because* it is the thing wrapping the terminal.

**Parked network decisions: no prior art found, and this is the novel one.**
Docker Sandboxes is the only other tool here with a deny-by-default egress
allowlist, and its model is strictly pre-authorisation: three presets (Open,
Balanced, Locked Down), `sbx policy allow network` to add rules, `sbx policy
check network <domain>` to verify a domain *before* running, `sbx policy log`
to audit afterwards. Nothing in its policy documentation describes a live
prompt when a request is refused — it is blocked, logged, and you go add a
rule and retry. aiab parks the request, notifies with action buttons, and
releases it on the answer.

By IDEAS.md's own Scope test this is unimpeachable box work: no agent can ask
you to widen a boundary it cannot see, and the boundary exists only because
aiab imposed it.

**Where everyone else is ahead: getting off the desktop.** Omnara,
AgentsRoom, Pushary, Forge Remote and Onepilot all let an agent's prompt be
answered from a phone; Agent Orchestrator pairs a desktop daemon with a mobile
app over LAN or Tailscale and shows a "Needs you" column for blocked sessions.
aiab is desktop-only through `notify-send` and, as `notify.py` says, goes
silent over ssh with no session bus — which is exactly the situation where the
notification would be worth most. Several of those tools also offer a fleet
view across repos, where `aiab monitor` is per-directory by design.

## Open questions this leaves for IDEAS.md

1. **Rewrite the Scope section's "no upstream competitor" line.** The
   conclusion probably survives — the remaining niche is x86 Linux under LXD
   including inside VMs, GPL, no config in or near the repo, plus the egress
   allowlist and the git guard — but the argument has to change, because the
   field is no longer empty.
2. **Credential injection: adopt for git, not for the LLM.** IDEAS.md's
   per-directory PAT entry rejects a broker as "too heavy", meaning on-demand
   minting. Cleanroom's variant is much cheaper — static credentials injected
   by a proxy aiab already runs per session. But that entry's other note, that
   "it's a CONNECT tunnel, so the proxy authorizes but can't inspect", turns
   out to be the property being given up rather than an obstacle to route
   around. Injection looks worth it for git and registry traffic
   (non-streaming, and the PAT is exactly the credential that should not sit
   in the container) and not obviously worth it for LLM traffic, where it
   means MITM-ing every prompt and inheriting the SSE problem Docker has not
   solved. For a tool whose pitch is confining the agent on the user's behalf,
   reading the user's prompts is a stranger thing to do than it is for a
   platform vendor.
3. **Is Workshop's interface system the shape aiab's surface is groping
   toward?** `mount`, `env`, `limits`, Wayland, port forwarding are an ad-hoc
   set of host-resource grants; Workshop generalised exactly that into plugs
   and slots. The Scope test ("does aiab need to provide this because the
   agent is in a box?") would cut differently if there were a principled
   interface model to defer to.
4. **Does aiab's confinement layer belong in Workshop?** The egress proxy and
   the git guard are the two pieces nobody else in the triple has, and
   Workshop is the one that would benefit most.
5. Smaller: the structured network audit log already in IDEAS.md now has a
   shipped reference implementation to compare against (`sbx policy log`), and
   an MCP gateway is a gap aiab has not considered at all.
6. **The notification work splits awkwardly across the Scope test.** The
   parked-host notification is unambiguous box work. The agent-waiting
   notification is harness work by the strict test — every agent ships one —
   *except* that its focus-awareness is only answerable from the host, where
   aiab already is. So it is a parity feature whose best version is
   box-shaped, and the Scope section's binary has no slot for that today. It
   is a better worked example than the ones currently listed, because the
   honest answer is neither "cut it" nor "keep it" but "keep the half that
   only works from out here".
7. **Notifications die exactly when they are worth most.** `notify-send` needs
   a session bus, so an ssh session gets nothing. Everyone doing remote
   approval does it from a phone. Nothing here argues aiab should grow a
   mobile app, but the gap is real and currently unacknowledged.

## What was not verified

- Workshop's AI-agent how-to and interfaces explanation pages are behind
  Canonical's readthedocs authentication. The interface list and the absence
  of egress filtering come from the public reference and search results, not
  from reading those two pages. Workshop may have agent-specific controls not
  visible from outside.
- Whether Docker parks an in-flight request pending a decision, or fails it.
  Its policy documentation describes only pre-authorisation (presets, `sbx
  policy allow network`, `sbx policy check network`) and after-the-fact
  auditing (`sbx policy log`), and never mentions a live prompt — but that is
  absence of evidence, not a documented "no".
- Whether `sbxenv.yaml`'s single `agent:` key can be overridden per run to get
  aiab's cross-agent mount replay.
- The proxy fix was exercised against names resolving inward (`localhost`,
  `ip6-localhost`, both refused) but not against a live public resolution:
  the dev container has no DNS egress. Public addresses are covered by
  literal-based unit tests instead.
