# How I work

I move between many different codebases rather than living in one. In any given session you are most likely in a repo you have not seen before, working alongside conventions, tooling, and history that you did not establish. Treat each codebase as unfamiliar territory and let *it* tell you how things are done — do not carry assumptions from other projects (or from training data) into this one.

## Look in `/work` for related code

You're running in a container with this project mounted under `/work`. Any *other* directories under `/work` are sibling source trees I've deliberately mounted into this session because they may be relevant — a library this project depends on, a related service, or another repo worth cross-referencing. When something you need isn't in the current project (a definition, the implementation behind an API you call, behaviour you rely on), check whether it lives in another `/work/*` directory before treating it as external or unknowable. And if code that would help isn't mounted anywhere under `/work`, just ask me to mount it — I can add directories to your running session without restarting it, and they'll appear under `/work`. Read it freely for context; the scope rules below still apply, so don't change code outside the current project without asking.

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

My level of ownership varies a lot between repos, and you can't know it unless I tell you. **This section is the default for when I haven't.**

- By default, make the change that was asked for. Don't opportunistically refactor, reformat, or "improve" unrelated code unless I ask.
- When you find something broken or surprising next to your task, surface it instead of silently fixing or working around it.
- If you're unsure how much latitude you have here, it's fine to ask me to set a role.

## Committing

Commit freely at natural checkpoints — once a piece of work is coherent and the code is in a good state, go ahead and commit without waiting to be asked. A series of small, clearly-described commits is easier for me to review and reshape than one large pile of uncommitted changes. Don't worry about getting authorship or signatures right: these commits are made inside a container, and I'll redo them outside it (correcting the author info and GPG-signing where needed) before anything is pushed. Just focus on a clear message saying what changed and why, and leave pushing to me.

A quirk of that container worth knowing about up front: `.git/config` is normally **read-only** in here. The container gets its own copy of the repo's config bind-mounted over the real one, so everything in my config still applies while you work, but nothing in the session can change what git sees on my side. The practical effect is that commands which rewrite repo config — `git config --local …`, `git remote add …`, installing a credential helper, and some tools' first-run setup — fail with a permissions error. The same goes for any other writable repo mounted under `/work`. That's deliberate, not a broken checkout, so don't try to route around it by chmod-ing the file, unmounting anything, or rewriting `.git/config` by hand; tell me instead and I'll rerun without the guard if the task really needs it. Ordinary local work — add, commit, diff, log, branch, checkout — is unaffected.
