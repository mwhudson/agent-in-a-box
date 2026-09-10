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
// Nothing here can be allowed to throw: opencode calls the event hook without
// awaiting it (packages/opencode/src/plugin/index.ts), so a rejection would
// surface in the user's session as an unhandled one.

import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { dirname } from "node:path"

const FILE = process.env.AIAB_ATTENTION

// The reasons the monitor shows, kept in step with aiab.attention's
// _WAITING_FOR_* — it displays what is written here.
const WAITING_FOR_PROMPT = "Waiting for your next prompt"
const WAITING_FOR_ANSWER = "Waiting for a response"

export const AiabAttention = async ({ client, directory }) => {
  if (!FILE) return {}

  // A wait already recorded for the same reason is left alone: its mtime is
  // *since when*, and the host reads a change as a new question, so repeating
  // ourselves would restart its countdown to a notification and lose the fact
  // that you had already looked. opencode does repeat itself — a permission
  // is updated more than once for one prompt.
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
  // the turn for the session you are watching, so only a session with no
  // parent is a wait on you.
  const rooted = new Map()
  const isRoot = async (id) => {
    if (!rooted.has(id)) {
      try {
        const session = await client.session.get({ path: { id }, query: { directory } })
        rooted.set(id, !session.data?.parentID)
      } catch {
        return false
      }
    }
    return rooted.get(id)
  }

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
