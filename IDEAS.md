# TODO

## Lifecycle gaps

- **`aiab refresh`** — after `upgrade-templates`, existing session containers
  stay stale forever (the README just notes this). A command that recreates a
  session container from the updated template would be cheap to build, since
  recorded mounts are already replayed automatically on recreation.
- **Snapshot/reset** — `lxc snapshot` before a risky run, `aiab reset` to roll
  the session container back. Useful when an agent has installed packages or
  mutated container state you want to keep most of the time.

## Project usability

- **Per-directory setup commands** — when a session container is (re)created,
  the agent lands in a container with no project dependencies installed. A
  recorded setup script (like mounts are recorded — e.g. run `apt install ...`
  / `make deps` on first start) would make `remove` + recreate genuinely
  cheap. Pairs naturally with `refresh`.
- **Port forwarding** — `aiab run --publish 8000` via an LXD proxy device, so
  a dev server the agent starts is reachable from the host browser. Right now
  there's no documented way to see a web app the agent is running.
- **Worktree follow-through** — `--worktree-keep` leaves a detached worktree
  buried in `.git/aiab-worktrees/<timestamp>`, but there's no command to list
  those worktrees, diff them, or pull the result out into a branch. Something
  like `aiab worktrees list`/`adopt` would close the loop, especially when
  running parallel sessions.

## Smaller items

- More agents in the registry (gemini-cli, codex, aider) — the dataclass
  design makes each one a single entry.
- A `--name`/session-suffix option so two agents of the same kind can run
  concurrently in one directory with separate containers.
- Resource limits (`limits.cpu`/`limits.memory`) for runaway agent processes.
- **`aiab doctor`** — check LXD init state and idmap support; first-run
  failures there are probably the worst onboarding experience.

## Non-feature

- The repo has lint/format/type checks but no tests. The `state.py`
  record-keeping logic and container-name derivation are pure enough to
  unit-test cheaply, which would derisk the more invasive features above.
