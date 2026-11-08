// GPL-3.0-or-later. Native V2 guard, independent of hook ordering.
const originalCommands = Symbol.for("opencode.host-sandbox.original-commands")
export default {
  id: "no-nested-sandbox",
  async setup(ctx) {
    await ctx.tool.hook("execute.before", event => {
      if (event.tool !== "shell") return
      const raw = globalThis[originalCommands]?.get(event.id)?.command
      const command = raw ?? event.input?.command ?? ""
      if (typeof command !== "string" || command.includes("nested-sandbox-ok")) return
      const bwrap = (command.match(/\bbwrap\b/g) || []).length
      const filters = (command.match(/apply-seccomp/g) || []).length
      const listeners = (command.match(/socat TCP-LISTEN/g) || []).length
      // Inspect the saved original when the sandbox hook ran first. Without
      // one, a complete user-provided wrapper is itself a nested invocation.
      if (bwrap && filters && listeners) {
        globalThis[originalCommands]?.get(event.id)?.cancel?.()
        throw new Error("no-nested-sandbox: refusing a hand-written sandbox wrapper. Send a plain shell command; use nested-sandbox-ok only for an intentional explicit exception.")
      }
    })
  },
}
