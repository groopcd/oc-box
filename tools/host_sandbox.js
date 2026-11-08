// GPL-3.0-or-later. Native V2 host-only shell sandbox port.
// Policy names/defaults follow opencode-sandbox 0.6.0 (MIT). Enforcement is delegated
// to its maintained @anthropic-ai/sandbox-runtime dependency, not reimplemented.
import { SandboxManager } from "@anthropic-ai/sandbox-runtime"
import fs from "node:fs/promises"
import os from "node:os"
import path from "node:path"
const originalCommands = Symbol.for("opencode.host-sandbox.original-commands")
const runtimeLease = Symbol.for("opencode.host-sandbox.runtime-lease")

const failure = code => Object.assign(new Error("Host sandbox lifecycle: " + code), { sandboxCode: code })
const codes = new Set(["lease-generation", "in-use-policy", "unowned-runtime", "runtime-owner-mismatch", "runtime-reset"])
const category = (error, fallback) => codes.has(error?.sandboxCode) ? error.sandboxCode : fallback

function reserve(policy, signature) {
  let lease = globalThis[runtimeLease]
  if (lease && (lease.version !== 2 || lease.manager !== SandboxManager)) throw failure("lease-generation")
  if (!lease) {
    if (SandboxManager.getConfig() !== undefined) throw failure("unowned-runtime")
    lease = { version: 2, manager: SandboxManager, references: 0, available: false, backgrounds: new Set(), policy: undefined, resetting: Promise.resolve() }
    globalThis[runtimeLease] = lease
  }
  if (lease.references && lease.signature !== signature) throw failure("in-use-policy")
  if (!lease.references) {
    const previousPolicy = lease.policy
    const reset = lease.resetting
    lease.signature = signature
    lease.available = false
    lease.ready = (async () => {
      try { await reset } catch { throw failure("runtime-reset") }
      const current = SandboxManager.getConfig()
      if (current !== undefined && current !== previousPolicy) throw failure("unowned-runtime")
      // Mark provenance before initialization, including partial failure, so
      // cleanup may release only the resources this plugin actually attempted.
      lease.policy = policy
      await SandboxManager.initialize(policy)
      if (SandboxManager.getConfig() !== policy) throw failure("runtime-owner-mismatch")
      lease.available = true
    })()
  }
  lease.references++
  return lease
}

async function release(lease) {
  if (--lease.references) return
  lease.available = false
  const ready = lease.ready
  lease.resetting = (async () => {
    await ready.catch(() => {})
    const current = SandboxManager.getConfig()
    if (current !== undefined && current !== lease.policy) throw failure("runtime-owner-mismatch")
    await SandboxManager.reset()
    // SDK 0.0.76 intentionally retains getConfig() after reset. Keep this
    // idle ownership record, not a fake active lease or an unmarked reset race.
  })()
  await lease.resetting
}

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
    if (["1", "true"].includes(process.env.OPENCODE_DISABLE_SANDBOX)) return
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
    let unavailable = "runtime-init"
    let warned = false
    const signature = JSON.stringify(policy)
    let lease
    let ownsLease = false
    try {
      lease = reserve(policy, signature)
      ownsLease = true
      await lease.ready
      available = lease.available
    } catch (error) {
      unavailable = category(error, "runtime-init")
      console.error("Host sandbox unavailable [" + unavailable + "]; " + (permissive ? "explicit permissive policy allows unsandboxed shell" : "shell commands will be blocked"))
    }
    const commands = new Map()
    const backgrounds = new Set()
    const exited = new Set()
    const subscription = new AbortController()
    const cleanup = () => {
      if (!commands.size && !backgrounds.size && !globalThis[originalCommands]?.size && !lease?.backgrounds?.size) SandboxManager.cleanupAfterCommand()
    }
    globalThis[originalCommands] ??= new Map()
    if (ctx.event?.subscribe) {
      void (async () => {
        try {
          for await (const event of ctx.event.subscribe({ signal: subscription.signal })) {
            if (!["shell.exited", "shell.deleted"].includes(event.type)) continue
            const id = event.data.id
            if (backgrounds.delete(id)) { lease.backgrounds.delete(id); cleanup() }
            else {
              if (exited.size >= 256) exited.delete(exited.values().next().value)
              exited.add(id)
            }
          }
        } catch {
          if (!subscription.signal.aborted) {
            available = false
            unavailable = "event-stream"
            console.error("Host sandbox lifecycle subscription failed [event-stream]; new shell commands blocked in enforce mode")
          }
        }
      })()
    }
    let closed = false
    const close = async () => {
      if (closed) return
      closed = true
      subscription.abort()
      for (const id of commands.keys()) globalThis[originalCommands].delete(id)
      commands.clear()
      for (const id of backgrounds) lease.backgrounds.delete(id)
      backgrounds.clear()
      if (ownsLease) {
        try { await release(lease) }
        catch (error) { console.error("Host sandbox cleanup failed [" + category(error, "runtime-reset") + "]; future initialization remains blocked") }
      }
    }
    try {
      await ctx.tool.hook("execute.before", async event => {
        if (event.tool !== "shell") return
        if (typeof event.input?.command !== "string") throw new Error("Host sandbox: unsupported shell input; command blocked")
        if (closed || !available || !lease?.available || lease.signature !== signature || SandboxManager.getConfig() !== lease.policy) {
          if (!permissive) throw new Error("Host sandbox unavailable in enforce mode [" + unavailable + "]; command blocked")
          return
        }
        const command = event.input.command
        try {
          const wrapped = await SandboxManager.wrapWithSandbox(command, undefined, undefined, undefined, { commandId: event.id, commandText: command })
          commands.set(event.id, { command, input: event.input })
          globalThis[originalCommands].set(event.id, { command, cancel: () => { commands.delete(event.id); globalThis[originalCommands].delete(event.id); cleanup() } })
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
        globalThis[originalCommands].delete(event.id)
        if (event.status === "completed" && event.result.metadata?.status === "running" && event.result.metadata.shellID) {
          const id = event.result.metadata.shellID
          if (!exited.delete(id)) { backgrounds.add(id); lease.backgrounds.add(id) }
        }
        cleanup()
      })
    } catch (error) {
      await close()
      throw error
    }
    return close
  },
}
