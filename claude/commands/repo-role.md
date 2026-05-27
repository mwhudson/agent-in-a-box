---
description: Declare my ownership/influence level for this repo (persists per-repo) to calibrate how much latitude you take
argument-hint: "[owner|maintainer|contributor|read-only] [optional free-text nuance]"
---

The user is telling you their relationship to the **current repository** so you can calibrate how much latitude to take when making changes. This **overrides the conservative default** in `~/.claude/CLAUDE.md` (which assumes minimal-footprint, ask-before-refactoring). Persist it so future sessions in this repo already know it.

The user's input is: `$ARGUMENTS`

## If the input is empty

Don't set anything. Instead, read this repo's stored role (see "Where to store it" below) and report the current setting if one exists, then list the available presets so the user can pick one. Stop there.

## Interpret the input

The input starts with a preset level, optionally followed by free-text nuance that refines or overrides it (e.g. `maintainer but the CI config is off-limits`). Map the preset to this latitude:

- **owner** — Full latitude. Refactor, restructure, rename, and fix adjacent issues you spot as you go. You may establish or change conventions. Still explain non-trivial changes, but you don't need to ask permission for improvements that serve the project.
- **maintainer** — Broad latitude within the *established* conventions. Refactor and clean up freely in service of the task, but follow the patterns already here rather than introducing your own. Flag large restructurings or API changes before doing them.
- **contributor** — Minimal-footprint changes. Do what was asked and little else. Match local style exactly. No opportunistic refactors, reformatting, or unrelated cleanup. Surface adjacent problems rather than fixing them.
- **read-only** — The user is exploring or reviewing. Don't edit files without asking first. Favor explanation and analysis.

If the input is free-text only with no clear preset, infer the closest level from their description and say which one you assumed. Always let the free-text nuance take precedence over the preset's defaults where they conflict.

## Where to store it

Write to **this project's memory directory** — the directory containing the `MEMORY.md` referenced in your context (e.g. `.../projects/<this-repo>/memory/`). This is keyed per-repo and lives outside the repository, so nothing is committed to a project the user may not own.

1. Create or overwrite `repo-role.md` in that directory with this frontmatter and body:

   ```markdown
   ---
   name: repo-role
   description: The user's ownership/influence level for this repo and the latitude it grants
   metadata:
     type: feedback
   ---

   The user is a **<level>** of this repo.

   **Why:** <one line restating their nuance, if any>
   **How to apply:** <the concrete latitude from the matching preset, adjusted for their nuance>
   ```

2. Add or update the one-line pointer in `MEMORY.md`: `- [Repo role](repo-role.md) — <level>: <short latitude hook>`. If a pointer already exists, update it in place rather than duplicating.

## Confirm

Acknowledge in one or two lines: the level you recorded, the key behavioral change it implies for this repo, and that it will persist for future sessions here. Then apply it for the rest of this session.
