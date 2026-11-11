// aiab's attention plugin: telling the host when opencode is waiting on you.
//
// The container half of the channel aiab.attention describes. Write the file
// named by $AIAB_ATTENTION when the agent starts waiting on you, remove it
// when you answer; `aiab monitor`, which is on the host, reads it out of the
// directory's state dir (mounted at /aiab) and raises the notification. What
// counts as long enough to be worth saying is not decided here — the file's
// mtime is *since when*, and how long is host-side policy.
//
// aiab mounts this file into ~/.config/opencode/plugins/, opencode's global
// plugin directory, and sets AIAB_ATTENTION for the session, naming the file
// after the agent's home key. Without that variable the plugin does nothing,
// which is what an opencode run outside aiab gets.
//
// One file, both opencode majors. opencode 2 rewrote the plugin API, and a v1
// plugin does not run there at all — but a plugin may default-export an object
// carrying both entrypoints, and each major picks out the one it knows:
// opencode 1 calls server() and uses the hooks it returns, opencode 2 reads id
// and calls setup(). That keeps one template serving both while a container is
// upgraded (see `aiab upgrade-templates`), and it is why the two adapters
// below sit either side of a shared core rather than being written twice.
//
// The docs spell the v2 half `...Plugin.define({...})`, but define() is the
// identity function — it exists to type-check the definition in TypeScript —
// so a plain object literal is exactly what both loaders see, and this file
// stays dependency-free. Note that opencode 1 *detects* the dual shape from
// the `id` alone and then insists on server(): dropping the v1 half here would
// make it throw rather than quietly ignore us.
//
// Nothing here can be allowed to throw. opencode 1 calls the event hook
// without awaiting it (packages/opencode/src/plugin/index.ts), so a rejection
// would surface in the user's session as an unhandled one; under opencode 2 we
// own the event loop, and a throw inside it would end our subscription and
// leave the host silent for the rest of the session.

import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { dirname } from "node:path"

const FILE = process.env.AIAB_ATTENTION

// The reasons the monitor shows, kept in step with aiab.attention's
// _WAITING_FOR_* — it displays what is written here.
const WAITING_FOR_PROMPT = "Waiting for your next prompt"
const WAITING_FOR_ANSWER = "Waiting for a response"

// ---------------------------------------------------------------------------
// what the two majors share: the file, and who is waiting on whom
// ---------------------------------------------------------------------------

// A wait already recorded for the same reason is left alone: its mtime is
// *since when*, and the host reads a change as a new question, so repeating
// ourselves would restart its countdown to a notification and lose the fact
// that you had already looked. opencode does repeat itself — a permission is
// updated more than once for one prompt.
const record = (reason) => {
  try {
    if (readFileSync(FILE, "utf8").trim() === reason) return
  } catch {}
  try {
    mkdirSync(dirname(FILE), { recursive: true })
    writeFileSync(FILE, reason + "\n")
  } catch {}
}

const clear = () => {
  try {
    rmSync(FILE, { force: true })
  } catch {}
}

// Whether a session is one you are talking to, cached by id. A subagent's
// session goes idle every time its task finishes, and that is the middle of
// the turn for the session you are watching, so only a session with no parent
// is a wait on you. `get` is the caller's, because the two majors ask
// differently; a lookup that fails is not a wait, and one that answers without
// a session is treated as a root, as the v1 plugin always has.
const rootSessions = (get) => {
  const rooted = new Map()
  return async (id) => {
    if (!rooted.has(id)) {
      try {
        const session = await get(id)
        rooted.set(id, !session?.parentID)
      } catch {
        return false
      }
    }
    return rooted.get(id)
  }
}

// ---------------------------------------------------------------------------
// opencode 1
// ---------------------------------------------------------------------------

async function server({ client, directory }) {
  if (!FILE) return {}

  const isRoot = rootSessions((id) =>
    client.session.get({ path: { id }, query: { directory } }).then((r) => r.data),
  )

  return {
    // You answered: a message of yours has arrived.
    "chat.message": async () => clear(),

    event: async ({ event }) => {
      switch (event.type) {
        // The turn ended, so it is waiting for a prompt.
        case "session.idle":
          if (await isRoot(event.properties.sessionID)) record(WAITING_FOR_PROMPT)
          break
        // It stopped mid-turn to ask for something. Whose session raised it
        // doesn't matter: the answer has to come from you either way.
        case "permission.updated":
          record(WAITING_FOR_ANSWER)
          break
        case "permission.replied":
          clear()
          break
      }
    },

    // The session is over: opencode disposes plugins as it shuts down.
    dispose: async () => clear(),
  }
}

// ---------------------------------------------------------------------------
// opencode 2
// ---------------------------------------------------------------------------
//
// The same three moments, renamed: hooks are registered on the domain that
// owns them rather than returned by name, events arrive from a stream we
// subscribe to and abort ourselves, permission.updated is now
// permission.asked, and an event's payload is under `data` rather than
// `properties`. chat.message becomes the session's prompt hook, which runs as
// your prompt is admitted — early, but clearing a file is cheap and idempotent,
// which is what that hook asks of us.
//
// Every session in this container maps to the one AIAB_ATTENTION file, so
// events are taken as they come: a wait is a wait on whoever is at this
// terminal, the same assumption the v1 half makes. That holds because aiab
// gives each directory its own container and its own state dir — an opencode 2
// server serving several locations at once would need `event.location` taken
// into account here.

async function setup(ctx) {
  if (!FILE) return

  const isRoot = rootSessions((id) => ctx.session.get({ sessionID: id }))

  // You answered: a prompt of yours is being admitted.
  await ctx.session.hook("prompt", () => clear())

  const stream = new AbortController()
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({
        types: ["session.idle", "permission.asked", "permission.replied"],
        signal: stream.signal,
      })) {
        try {
          switch (event.type) {
            // The turn ended, so it is waiting for a prompt.
            case "session.idle":
              if (await isRoot(event.data.sessionID)) record(WAITING_FOR_PROMPT)
              break
            // It stopped mid-turn to ask for something.
            case "permission.asked":
              record(WAITING_FOR_ANSWER)
              break
            case "permission.replied":
              clear()
              break
          }
        } catch {}
      }
    } catch {
      // Aborting the stream ends the loop by throwing; so would a server that
      // went away. Either way there is nothing left to report.
    }
  })()

  // The session is over: opencode 2 runs this as it unloads the plugin.
  return () => {
    stream.abort()
    clear()
  }
}

export default { id: "aiab.attention", setup, server }
