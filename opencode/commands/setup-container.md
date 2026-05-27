---
description: Scan a repo's docs and produce the commands to set up the current container as a dev environment
---

You are setting up a development environment for the project in the current working directory **inside the container you are already running in**. The container exists and the OS is already chosen — your job is to produce the commands that install the toolchain and dependencies on top of it so the project can be built, run, and tested here. Figure those commands out by reading the project's own documentation — not by guessing from generic conventions — and present them clearly. Do **not** build or run anything; the user will run the commands themselves.

## 1. Discover the documentation

Read whatever the project actually provides. Look for (and don't assume all exist):

- `README*`, `CONTRIBUTING*`, `DEVELOPING*`, `HACKING*`, `INSTALL*`
- A `docs/` directory — especially pages about "getting started", "development", "setup", "build", "contributing"
- Any existing containerization or CI setup, treated as a *source of setup steps* (not as something to reproduce as an image): `Dockerfile*`, `.devcontainer/`, `docker-compose*.y*ml`, `Containerfile`, and CI workflows under `.github/workflows/` (these often encode the canonical setup steps)
- Dependency / toolchain manifests that reveal the runtime and version: `package.json` + lockfiles, `pyproject.toml`/`requirements*.txt`/`uv.lock`/`poetry.lock`, `go.mod`, `Cargo.toml`, `Gemfile`, `pom.xml`/`build.gradle`, `.tool-versions`/`.nvmrc`/`.python-version`, `Makefile`, `Taskfile.y*ml`

Prefer instructions written in the docs over inferences from manifests. When the docs and the manifests disagree, trust the docs but note the discrepancy.

## 2. Determine the setup

Assume a generic Linux container with a normal base toolchain (a shell, a package manager, network access) but nothing project-specific installed yet. From what you read, work out:

- **Language runtime / toolchain** — what to install and the *specific version* the project targets (pin it; don't default to "latest"). Check what's already present before reinstalling.
- **System packages** — OS-level deps the docs mention (build tools, libs, headers), installed via the container's package manager.
- **Package manager** — how project dependencies are installed (e.g. `npm ci`, `uv sync`, `poetry install`, `go mod download`, `bundle install`).
- **Project dependency install** — the exact commands the docs prescribe.
- **Build / codegen steps** — anything required before the app runs.
- **Env vars, services, ports** — anything needed at runtime (databases, env files, exposed ports), with the caveat that external services may not be reachable from inside the container.
- **Verification** — the command the docs use to confirm a working setup (test suite, lint, a "hello world" run).

## 3. Present the result

Output, in this order:

1. **Summary** — one short paragraph: detected language/runtime + version and package manager, and where you found the authoritative instructions (cite the files).
2. **Setup commands** — a single copy-pasteable shell block, ordered, with a brief `# comment` before each logical group explaining what it does and which doc it came from. Pin versions. Assume the commands run from the repo root inside the current container.
3. **Run & verify** — the command(s) to build/start the app and to confirm the environment works.
4. **Gaps & assumptions** — bullet anything the docs didn't specify that you had to assume, and anything the user should double-check (secrets, external services, anything that may not be available inside the container).

Keep it faithful to the repository. If you genuinely can't find setup documentation, say so plainly and list what you'd need rather than fabricating commands.

$ARGUMENTS
