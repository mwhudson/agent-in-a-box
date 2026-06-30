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

## Smaller items

- More agents in the registry (gemini-cli, codex, aider) — the dataclass
  design makes each one a single entry.
- A `--name`/session-suffix option so two agents of the same kind can run
  concurrently in one directory with separate containers.
- **`aiab doctor`** — check LXD init state and idmap support; first-run
  failures there are probably the worst onboarding experience.
