// GPL-3.0-or-later. Native V2 host-only shell sandbox port.
// Policy names/defaults follow opencode-sandbox 0.6.0 (MIT). Enforcement is delegated
// to its maintained @anthropic-ai/sandbox-runtime dependency, not reimplemented.
import { SandboxManager } from "@anthropic-ai/sandbox-runtime"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"

const denied = [".ssh", ".gnupg", ".aws/credentials", ".azure", ".config/gcloud", ".config/gh", ".kube", ".docker/config.json", ".npmrc", ".netrc", ".env"]
const domains = ["registry.npmjs.org", "*.npmjs.org", "registry.yarnpkg.com", "pypi.org", "*.pypi.org", "crates.io", "*.crates.io", "github.com", "*.github.com", "gitlab.com", "*.gitlab.com", "bitbucket.org", "*.bitbucket.org", "api.openai.com", "api.anthropic.com", "generativelanguage.googleapis.com", "*.googleapis.com"]
const unsafe = new Set(["/", "/home", "/usr", "/etc", "/var", "/opt", "/Library", "/System", "/private", "/Volumes", "/Users"])

async function json(file) {
  try { return JSON.parse(await fs.readFile(file, "utf8")) }
  catch (error) { if (error.code === "ENOENT") return undefined; throw error }
}

export default {
  id: "oc-box.host-sandbox",
  async setup(ctx) {
    // The existing explicit opt-out is kept for copied oc-box launches only.
    // The host migration never sets this variable or disables sandbox policy.
    if (process.env.OC_BOX_CONTAINER === "1" || ["1", "true"].includes(process.env.OPENCODE_DISABLE_SANDBOX)) return
    const base = path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config"), "opencode-sandbox")
    let user
    try {
      user = process.env.OPENCODE_SANDBOX_CONFIG
        ? JSON.parse(process.env.OPENCODE_SANDBOX_CONFIG)
        : await json(path.join(base, "projects", path.basename(ctx.location.directory) + ".json")) || await json(path.join(base, "config.json")) || {}
      if (!user || typeof user !== "object" || Array.isArray(user) || (user.mode && !["enforce", "permissive"].includes(user.mode))) throw Error("invalid policy")
    } catch {
      console.error("Host sandbox policy invalid; shell commands will be blocked until it is corrected")
      await ctx.tool.hook("execute.before", event => {
        if (event.tool === "shell") throw new Error("Host sandbox policy invalid; command blocked")
      })
      return
    }
    if (user.disabled === true) return
    const permissive = user.mode === "permissive"
    const roots = [...new Set([ctx.location.directory, ctx.location.project?.worktree, os.tmpdir()].filter(Boolean))].filter(value => !unsafe.has(path.resolve(value)))
    const policy = {
      ...user,
      filesystem: { denyRead: denied.map(value => path.join(os.homedir(), value)), allowRead: [], allowWrite: roots, denyWrite: [], ...user.filesystem },
      network: { allowedDomains: domains, deniedDomains: [], allowLocalBinding: false, ...user.network },
    }
    // Initialization is outside tool execution. If enforce setup fails, keep a
    // blocking hook active rather than an inactive plugin allowing raw shell.
    let available = false
    let warned = false
    try {
      await SandboxManager.initialize(policy)
      available = true
    } catch {
      console.error("Host sandbox unavailable; " + (permissive ? "explicit permissive policy allows unsandboxed shell" : "shell commands will be blocked"))
    }
    const commands = new Map()
    await ctx.tool.hook("execute.before", async event => {
      if (event.tool !== "shell") return
      if (typeof event.input?.command !== "string") throw new Error("Host sandbox: unsupported shell input; command blocked")
      if (!available) {
        if (!permissive) throw new Error("Host sandbox unavailable in enforce mode; command blocked")
        return
      }
      const command = event.input.command
      try {
        const wrapped = await SandboxManager.wrapWithSandbox(command, undefined, undefined, undefined, { commandId: event.id, commandText: command })
        commands.set(event.id, { command, input: event.input })
        event.input = { ...event.input, command: wrapped }
      } catch {
        if (!permissive) throw new Error("Host sandbox could not wrap command in enforce mode; command blocked")
        if (!warned) console.error("Host sandbox wrapping failed; explicit permissive policy allows unsandboxed shell")
        warned = true
      }
    })
    await ctx.tool.hook("execute.after", event => {
      const original = commands.get(event.id)
      if (!original) return
      // Do not call V1 history endpoints or write V2 database records. Restore
      // hook-visible input and clean temporary mounts for success AND errors.
      if (event.input && typeof event.input === "object") event.input.command = original.command
      commands.delete(event.id)
      SandboxManager.cleanupAfterCommand()
    })
    return async () => {
      commands.clear()
      await SandboxManager.reset()
    }
  },
}
