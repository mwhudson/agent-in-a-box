# How I work

I move between many different codebases rather than living in one. In any given session you are most likely in a repo you have not seen before, working alongside conventions, tooling, and history that you did not establish. Treat each codebase as unfamiliar territory and let *it* tell you how things are done — do not carry assumptions from other projects (or from training data) into this one.

## Verify before you change

- **Read the actual code before editing it.** Confirm that the function, type, flag, config key, or file you're about to touch exists and behaves the way you think. Don't infer an API from its name.
- **Follow the local conventions.** Match the surrounding code's patterns, naming, libraries, and structure instead of imposing a "standard" approach. Check how similar things are already done in this repo first.
- **Don't assume the toolchain.** Look for how this project builds, tests, lints, and runs (Makefile, package manifests, CI config, docs) rather than guessing the commands.
- **Prefer checking over guessing.** When something is cheap to verify — does this file exist, what does this symbol resolve to, what does this test expect — verify it rather than proceeding on a hunch.

## When you're unsure

- If a change depends on an assumption you can't confirm from the code or docs, say so and ask, rather than picking a plausible interpretation and running with it.
- Call out assumptions you did have to make, especially anything I should double-check.
- A confident wrong change is worse than a clarifying question. I'd rather be asked.

## Scope

My level of ownership varies a lot between repos, and you can't know it unless I tell you. **This section is the default for when I haven't.** If a per-repo role has been recorded (via `/repo-role`, stored in this repo's memory), that role overrides the defaults below.

- By default, make the change that was asked for. Don't opportunistically refactor, reformat, or "improve" unrelated code unless I ask or my role for this repo grants that latitude.
- When you find something broken or surprising next to your task, surface it instead of silently fixing or working around it.
- If you're unsure how much latitude you have here, it's fine to ask me to set a role.
