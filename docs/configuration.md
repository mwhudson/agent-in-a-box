# Configuration

Two kinds of configuration sit alongside the sandbox:

- **Versioned config overlays** — global instructions and custom commands,
  shipped from this repo and bind-mounted into every session for an agent.
- **Per-directory configuration** — environment variables and opencode settings
  scoped to a single project directory, for things like a directory-specific
  API key.

In both cases your **credentials are kept separate** from versioned config: they
live in the per-agent config dir (`~/.local/share/aiab/<agent>/home/...`) and are
never part of the repo.

- [Versioned Claude config (CLAUDE.md + slash commands)](#versioned-claude-config-claudemd--slash-commands)
- [Versioned opencode config (AGENTS.md + commands)](#versioned-opencode-config-agentsmd--commands)
- [Versioned Copilot config (copilot-instructions.md + a custom agent)](#versioned-copilot-config-copilot-instructionsmd--a-custom-agent)
- [Per-directory configuration](#per-directory-configuration)

## Versioned Claude config (CLAUDE.md + slash commands)

The `agent-config/claude/` directory in this repo is the source of truth for the global
config you want available in every Claude session:

```
agent-config/claude/
  CLAUDE.md        -> mounted at ~/.claude/CLAUDE.md  (global instructions)
  commands/        -> mounted at ~/.claude/commands/  (custom /slash commands)
```

`aiab run claude` bind-mounts these into the session container's `~/.claude` as
LXD devices, sourced from this repo's own location (found via the launcher's
real path, so it works no matter which project directory you're running in).
Because it's a bind mount, the files are the *same* on the host and in the
container — edit them here and commit, or edit them from inside a session;
either way the change is reflected in both and tracked by git.

Why not just symlink them into the config dir? The config dir is mounted into
the container, so a symlink there would have to resolve to a path that exists
*inside* the container — but this repo is only mounted (at `/work/<basename>`)
when it happens to be the working directory, so the link would dangle in every
other session. Bind-mounting sidesteps that entirely.

Notes:

- Your **credentials** are *not* versioned — they stay in the per-agent
  config dir (`~/.local/share/aiab/claude/home/.claude/`); only `CLAUDE.md` and
  `commands/` are overlaid from the repo.
- This applies to the default (Claude API) `claude` agent only. The `claude-or`
  / OpenRouter agent does not get the overlay.
- Missing entries are skipped, so it's fine to delete `agent-config/claude/CLAUDE.md` or
  leave `agent-config/claude/commands/` empty.

## Versioned opencode config (AGENTS.md + commands)

The `agent-config/opencode/` directory plays the same role for `aiab run opencode`,
bind-mounted into the container's `~/.config/opencode`:

```
agent-config/opencode/
  AGENTS.md       -> mounted at ~/.config/opencode/AGENTS.md       (global instructions)
  commands/       -> mounted at ~/.config/opencode/commands/       (custom commands)
```

`AGENTS.md` is opencode's equivalent of `CLAUDE.md` (auto-loaded as global
instructions). As with the Claude overlay, credentials are *not* versioned (they
stay in `~/.local/share/aiab/opencode/home/.local/share/opencode/auth.json`),
and missing entries are skipped.

`opencode.json` is *not* versioned in this repo. On first run, `aiab run
opencode` writes a permissive config —

```json
{ "$schema": "https://opencode.ai/config.json", "permission": "allow" }
```

— to `~/.local/share/aiab/opencode/home/.config/opencode/opencode.json`, which
is inside the bind-mounted home so it needs no separate overlay. The
`"permission": "allow"` setting lets opencode run without permission prompts
(opencode has no Claude-style `--dangerously-skip-permissions` flag; this is the
equivalent), safe for the same reason — the container can only see the
directories you've mounted into it. It's only written when absent, so you can
edit it (e.g. to add MCP servers) and your changes persist.

To override opencode config in **one directory only** (a different key or model
there), use [`aiab opencode config`](commands.md#aiab-opencode) — see
[Per-directory configuration](#per-directory-configuration) below.

## Versioned Copilot config (copilot-instructions.md + a custom agent)

The `agent-config/copilot/` directory plays the same role for `aiab run copilot`,
bind-mounted into the container's `~/.copilot`:

```
agent-config/copilot/
  copilot-instructions.md  -> mounted at ~/.copilot/copilot-instructions.md  (global instructions)
  agents/                   -> mounted at ~/.copilot/agents/                  (custom agents)
```

`copilot-instructions.md` is Copilot CLI's equivalent of `CLAUDE.md`/`AGENTS.md`
(auto-loaded as global instructions). Credentials are not versioned (they stay
in `~/.local/share/aiab/copilot/home/...`), and missing entries are skipped.

Copilot CLI has no Claude/opencode-style mechanism where dropping a file in a
commands directory creates a same-named `/slash-command` — there's no
filename-to-command mapping, and custom agent files don't take arguments the
way `$ARGUMENTS` does in the Claude/opencode commands. The closest equivalent
it does have is a **custom agent**: a `*.agent.md` file in `~/.copilot/agents/`,
selected interactively with `/agent` or via `copilot --agent <name>`. So
`agent-config/copilot/agents/setup-container.agent.md` carries the same instructions as
the Claude/opencode `/setup-container` command, but you reach it by picking
"setup-container" from `/agent` rather than typing `/setup-container`.
`repo-role` isn't ported at all — it exists to set per-repo latitude in
Claude Code's own persistent memory system, which Copilot CLI has no
equivalent of.

## Per-directory configuration

Beyond the repo-wide overlays, two commands record configuration scoped to a
single project directory (kept in `~/.local/share/aiab/`, keyed by the resolved
path, and applied on the next `aiab run` there):

- [`aiab env`](commands.md#aiab-env) injects environment variables into the
  agent process — directory-wide, or scoped to one agent with `--agent`. This is
  the way to give an agent a directory-specific value of anything it reads from
  the environment, including credentials. For example, a different OpenRouter key
  for `claude-or` in one directory:

  ```
  aiab env set --agent claude-or ANTHROPIC_AUTH_TOKEN sk-or-...
  ```

- [`aiab opencode config`](commands.md#aiab-opencode) sets keys in a
  per-directory opencode config overlay (e.g. a provider key or model). opencode
  resolves a key **config > stored login > environment variable**, so — unlike a
  plain env var, which loses to the shared `opencode auth login` — a config file
  overrides the login for that directory while every other directory keeps the
  shared one:

  ```
  aiab opencode config provider.openrouter.options.apiKey sk-or-...
  ```

Neither writes anything into your repo: the records live in aiab's state, and
the opencode overlay file lives in the directory's
[state dir](concepts.md#per-directory-state-and-the-setup-script-aiab).
