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

- **Port forwarding** — `aiab run --publish 8000` via an LXD proxy device, so
  a dev server the agent starts is reachable from the host browser. Right now
  there's no documented way to see a web app the agent is running.
- **Worktree follow-through** — `--worktree-keep` leaves a detached worktree
  buried in `.git/aiab-worktrees/<timestamp>`, but there's no command to list
  those worktrees, diff them, or pull the result out into a branch. Something
  like `aiab worktrees list`/`adopt` would close the loop, especially when
  running parallel sessions.

## Monitor pane (rename `aiab net watch` → `aiab monitor`)

The watch pane becomes a general session control panel, not just a network
decision console. The network log + pending-host buttons stay; a toggleable
mounts view is added alongside.

### Mounts view

- Toggled via a clickable `[Mounts]` button in the header (+ `m` hotkey);
  replaces the log area while active.
- Each mount row: path label + `[ro]`/`[rw]` toggle + `[×]` remove button,
  all mouse-clickable.
- `[+ Add]` button at the bottom focuses a path `Input` widget.
- Path input uses textual's `Suggester` (inline ghost completion, accept with
  Tab/→) backed by a `PathSuggester` that walks the filesystem.
- Default mode for new mounts: `ro`. Click the toggle to switch to `rw`.
- Toggling `ro`↔`rw` on an existing mount takes effect live (remove + re-add
  the LXD device, same as `aiab mount --ro`/`--rw`).
- Removing a mount takes effect live (`state.remove_mount` +
  `container.remove_dir_device`).

### Architecture changes

- The TUI constructor takes `container_name` (in addition to `work_dir`) and
  lazily constructs an `Lxd()` + `Container` handle when a mount operation is
  requested.
- Module rename: `netwatch_tui` → `monitor_tui` (or similar). CLI entry point
  becomes `aiab monitor`; `aiab net watch` can stay as an alias or be dropped.
- The shared plumbing in `aiab.netwatch` (pending scan, log tails, decision
  recording) stays; the mount logic lives in `aiab.state` already.

## Smaller items

- More agents in the registry (gemini-cli, codex, aider) — the dataclass
  design makes each one a single entry.
- A `--name`/session-suffix option so two agents of the same kind can run
  concurrently in one directory with separate containers.
- Resource limits (`limits.cpu`/`limits.memory`) for runaway agent processes.
- **`aiab doctor`** — check LXD init state and idmap support; first-run
  failures there are probably the worst onboarding experience.
