---
name: landscape-survey
description: Survey tools that overlap agent-in-a-box — a named competitor, a feature area, or the whole field — and write the result up as a dated, parked survey in docs/. Use when asked to compare aiab against another project, to check what someone else does about some part of aiab's job, or to refresh the picture of the field.
---

Survey the tools that overlap aiab and write up what the comparison changes.

The user's request is: `$ARGUMENTS` — a named project, a feature area ("what
does anyone else do about notifications?"), or nothing, meaning a full refresh.

The point of this is **not** a feature matrix. Past passes paid for themselves
in three ways, in descending order of value: a security hole found in someone
else's changelog and fixed here, claims in our own docs that had gone stale,
and a short list of open questions handed to IDEAS.md. Aim at those.

## Read aiab from the code, not from memory or from the docs

Every claim about what aiab does gets checked against the source. The docs are
one of the things being audited, so they are not the authority here.

- Egress allowlist, parked decisions, the loopback check — `aiab/netproxy.py`
  (`is_external_address`, `connect_external`).
- Git guard — `_guard_git_repo` in `aiab/cli.py`.
- Mounts, overlays, devices on a live container — `aiab/lxd.py`
  (`add_device`, `add_config_overlay`).
- Attention, focus-aware suppression, desktop notifications —
  `aiab/attention.py`, `aiab/focus.py`, `aiab/notify.py`, `aiab/netwatch.py`.
- Per-directory state and profiles — `aiab/state.py`, `aiab/profiles.py`.

## The lens

IDEAS.md's Scope test: *does aiab need to provide this because the agent is
running in a box, or is it merely an opinionated way to configure the agent?*
Sort what you find by that, and say so when a feature splits across it rather
than forcing a verdict — the notification work is the worked example, and the
honest answer there was "keep the half that only works from out here".

The axis that turned out to predict nearly everything else about a tool is
**where its configuration lives** — committed in the repo, beside the repo, or
on the host keyed by directory. Reach for that before reaching for isolation
mechanism.

## Mandatory moves

1. **Mine the changelogs for security fixes, not just features.** The
   September pass found `sandbox-runtime` 0.0.76 refusing allow-listed names
   that resolve to loopback, recognised the same hole in our proxy, and that
   became a52da9b. Anyone solving the same problem will hit the same bugs, and
   their release notes are free review of our code. Do this part first.
2. **Audit our own claims.** Grep `docs/concepts.md`, `README.md` and
   IDEAS.md for assertions about the field ("no upstream competitor", "nothing
   else does X") and report each as confirmed-and-rechecked or stale, with
   what it should say instead. Name the file. Re-checked-and-still-true is a
   result worth writing down.
3. **Date everything, and record what you could not verify.** The survey ends
   with a "What was not verified" section giving the reason — paywalled docs,
   absence of evidence rather than a documented no, a test the dev container's
   lack of DNS egress made impossible. A survey whose limits are legible can
   be acted on; one that reads as uniformly confident cannot.
4. **Hand conclusions to IDEAS.md, don't apply them.** Close with "Open
   questions this leaves for IDEAS.md". Do not edit IDEAS.md, and do not fix
   the stale doc claims, unless the user asks — those are separate commits
   with separate judgement calls in them.

## Where to start looking

Not a checklist to work through; the field as of the last pass, so you spend
your time on what has changed and on what nobody has named yet.

- **The near neighbours** — Canonical Workshop (LXD, same stack, same
  employer), Docker Sandboxes (`sbx`), lincubate, code-on-incus.
- **Credential brokering** — nono, Buildkite Cleanroom, Claude Code on the
  web. Three independent implementations was the strongest signal in the last
  survey.
- **Adjacent layers** — `sandbox-runtime`, fence (command-level policy),
  litterbox (Wayland), Claude Code's own "Choose a sandbox environment" page,
  `awesome-agent-orchestrators`.
- **Attention and remote approval** — Omnara, Agent Orchestrator, Pushary,
  Forge Remote, claude-tmux-focus, and the agents' own notification hooks.

Read issue trackers, not only docs. The best material in the last pass was in
unanswered issues: Docker's SSE buffering bug, its missing post-creation
mounts, its `source:target:ro` request closed as duplicate.

## Output

A new file, `docs/landscape-<YYYY>-<MM>.md`, **deliberately unlinked** from
`docs/README.md` — working material, not user documentation. Say so in the
header, with the date of this pass and of the previous one. Follow
`docs/landscape-2026-09.md` for shape.

Never overwrite or rewrite an earlier survey: a new pass is a new file, and the
old one stays as the dated record of what was true then. Appending a new axis
to the current pass's file is fine while that pass is still the current one.

A narrow request ("just compare us to Workshop") still gets the audit and the
open-questions sections; it just gets one subject instead of a field. If the
whole result is one section, append it to the current pass's file rather than
starting a new one for it.
