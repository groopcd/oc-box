// GPL-3.0-or-later. A bounded V1-hook adapter on native OpenCode 2.x APIs.
// This is a plugin, not a replacement for OpenCode tools or its service.
import { Model } from "@opencode/plugin"
import { readFile } from "node:fs/promises"
import { Schema } from "effect"

const supported = new Set([
  "config", "tool", "dispose", "shell.env", "tool.definition",
  "tool.execute.before", "tool.execute.after",
  "experimental.chat.messages.transform", "chat.headers",
])
const alias = name => name === "shell" ? "bash" : name
const clone = value => structuredClone(value)
const expand = value => typeof value === "string" ? value.replace(/\{env:([^}]+)\}/g, (_, name) => process.env[name] || "")
  : Array.isArray(value) ? value.map(expand)
  : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expand(item)])) : value

function inputSchema(input) {
  if (input?.["~standard"]?.jsonSchema) return input["~standard"].jsonSchema.input({ target: "draft-2020-12" })
  if (Schema.isSchema(input)) {
    const document = Schema.toJsonSchemaDocument(input)
    return Object.keys(document.definitions).length ? { ...document.schema, $defs: document.definitions } : document.schema
  }
  if (!input || typeof input !== "object") return {}
  // Do not pass cyclic validation implementations as claimed wire schemas.
  return JSON.parse(JSON.stringify(input))
}

async function transformMessages(hook, event) {
  const originals = new Map()
  const view = event.messages.map((message, index) => ({
    info: { id: message.id || "msg_view_" + index, sessionID: event.sessionID, role: message.role },
    parts: message.content.map((native, partIndex) => {
      const identity = { id: "prt_view_" + index + "_" + partIndex, sessionID: event.sessionID, messageID: message.id }
      let part
      if (native.type === "media") {
        const source = native.media.source
        const url = source.type === "base64" ? `data:${native.media.mediaType};base64,${source.data}`
          : source.type === "bytes" ? `data:${native.media.mediaType};base64,${Buffer.from(source.data).toString("base64")}`
          : source.type === "url" ? source.url : undefined
        part = { ...identity, type: "file", mime: native.media.mediaType, filename: native.filename, url }
      } else if (native.type === "tool-call") {
        part = { ...identity, type: "tool", callID: native.id, tool: alias(native.name), state: { status: "completed", input: clone(native.input) } }
      } else {
        part = { ...identity, type: native.type, ...(typeof native.text === "string" ? { text: native.text } : {}) }
      }
      originals.set(part.id, { native, before: JSON.stringify(part) })
      return part
    }),
  }))
  const output = { messages: view }
  await hook({}, output)
  if (output.messages.length !== event.messages.length) throw new Error("oc-box V1 adapter: adding/removing whole history messages requires a native V2 port")
  for (let index = 0; index < output.messages.length; index++) {
    if (output.messages[index].info.id !== (event.messages[index].id || "msg_view_" + index) ||
        output.messages[index].info.role !== event.messages[index].role) {
      throw new Error("oc-box V1 adapter: changing history message order/identity/role requires a native V2 port")
    }
    const parts = output.messages[index].parts
    if (!Array.isArray(parts) || parts.length !== event.messages[index].content.length) {
      throw new Error("oc-box V1 adapter: changing history part count requires a native V2 port")
    }
    event.messages[index].content = parts.map((part, partIndex) => {
      if (part.id !== "prt_view_" + index + "_" + partIndex) {
        throw new Error("oc-box V1 adapter: changing history part order/identity requires a native V2 port")
      }
      const previous = originals.get(part.id)
      if (!previous) throw new Error("oc-box V1 adapter: invented legacy history parts require a native V2 port")
      if (JSON.stringify(part) === previous.before) return previous.native
      const before = JSON.parse(previous.before)
      if (part.sessionID !== before.sessionID || part.messageID !== before.messageID) {
        throw new Error("oc-box V1 adapter: changing history references requires a native V2 port")
      }
      if (part.type === "text" && typeof part.text === "string" && ["text", "media"].includes(previous.native.type)) {
        return previous.native.type === "text" ? { ...previous.native, text: part.text } : { type: "text", text: part.text }
      }
      if (part.type === "tool" && previous.native.type === "tool-call" &&
          part.tool === before.tool && part.callID === before.callID && part.state?.status === before.state.status) {
        return { ...previous.native, input: part.state.input }
      }
      throw new Error("oc-box V1 adapter: unsupported legacy history part mutation")
    })
  }
}

function toolResult(value) {
  if (typeof value === "string") return { content: value }
  if (!value || typeof value !== "object" || typeof value.output !== "string") {
    throw new Error("oc-box V1 adapter: custom tools must return text or a text-output object")
  }
  const attachments = (value.attachments || []).map(file => {
    if (file.type !== "file" || typeof file.url !== "string" || typeof file.mime !== "string") {
      throw new Error("oc-box V1 adapter: unsupported custom tool attachment")
    }
    return { type: "file", uri: file.url, mime: file.mime, ...(file.filename ? { name: file.filename } : {}) }
  })
  return { content: attachments.length ? [{ type: "text", text: value.output }, ...attachments] : value.output,
    metadata: { ...value.metadata, title: value.title } }
}

function toModel(providerID, id, value) {
  const result = Model.Info.default(providerID, id)
  result.name = value.name || id
  result.modelID = value.id || id
  result.capabilities.tools = value.tool_call ?? true
  result.capabilities.input = value.attachment ? ["text", "image"] : ["text"]
  result.capabilities.temperature = value.temperature
  if (value.limit) Object.assign(result.limit, value.limit)
  if (value.options) result.settings = value.options
  return result
}

async function configure(ctx, hook) {
  // Only provider inventory/settings are bridged. General config hooks need
  // domain-specific ports; never pretend an ignored edit took effect.
  const listed = await ctx.provider.list()
  const providers = Array.isArray(listed) ? listed : listed.data
  const config = { provider: Object.fromEntries(providers.map(provider => [provider.id, {
    name: provider.name,
    npm: provider.package.replace(/^aisdk:/, ""),
    options: clone(provider.settings || {}),
    models: {},
  }])) }
  const declared = expand(JSON.parse(await readFile(new URL("./oc-box-v1-provider-config.json", import.meta.url), "utf8")))
  const launch = expand(JSON.parse(process.env.OPENCODE_CONFIG_CONTENT || "{}"))
  for (const document of [declared, launch]) {
    for (const [id, provider] of Object.entries(document.providers || document.provider || {})) {
      config.provider[id] = {
        ...config.provider[id], ...provider,
        npm: (provider.package || provider.npm || config.provider[id]?.npm || "").replace(/^aisdk:/, ""),
        options: provider.settings || provider.options || config.provider[id]?.options || {},
        models: provider.models || {},
      }
    }
  }
  const before = clone(config)
  await hook(config)
  if (Object.keys(config).some(key => key !== "provider")) {
    throw new Error("oc-box V1 adapter: config edits outside provider discovery require a native V2 port")
  }
  if (JSON.stringify(before) === JSON.stringify(config)) return
  if (!config.provider || typeof config.provider !== "object") {
    throw new Error("oc-box V1 adapter: invalid provider config edit")
  }
  await ctx.provider.transform(editor => {
    for (const id of Object.keys(before.provider)) {
      if (!(id in config.provider)) editor.remove(id)
    }
    for (const [id, provider] of Object.entries(config.provider)) {
      const previous = before.provider[id]
      if (JSON.stringify(previous) === JSON.stringify(provider)) continue
      const changed = Object.keys(provider).filter(key => JSON.stringify(provider[key]) !== JSON.stringify(previous?.[key]))
      if (previous && changed.some(key => !["options", "models"].includes(key))) {
        throw new Error("oc-box V1 adapter: provider metadata edits require a native V2 port")
      }
      const inventory = Object.entries(provider.models || {}).map(([name, model]) => toModel(id, name, model))
      const original = editor.get(id)
      if (!original) {
        editor.add({ info: { id, name: provider.name || id, package: "aisdk:" + provider.npm, activation: "enabled", settings: provider.options || {} }, models: inventory })
      } else {
        if (JSON.stringify(previous?.options) !== JSON.stringify(provider.options)) {
          editor.update(id, value => { value.settings = clone(provider.options || {}) })
        }
        if (JSON.stringify(previous?.models) !== JSON.stringify(provider.models)) editor.models.set(id, inventory)
      }
    }
  })
}

export function adapt(module, id) {
  const native = module.default
  if (native && typeof native === "object" && typeof native.id === "string" &&
      (typeof native.setup === "function" || typeof native.effect === "function")) return native
  const factories = [...new Set(Object.values(module).filter(value => typeof value === "function"))]
  return {
    id,
    async setup(ctx) {
      if (factories.length !== 1) throw new Error("oc-box V1 adapter: expected one distinct legacy plugin factory")
      const unavailable = new Proxy({}, { get() { throw new Error("oc-box V1 adapter: this legacy client API requires a native V2 port") } })
      const hooks = await factories[0]({
        directory: ctx.location.directory,
        worktree: ctx.location.directory,
        project: { id: "global", worktree: ctx.location.directory },
        client: { app: { log: async () => ({ data: true }) }, session: unavailable, part: unavailable, _client: unavailable },
        $: globalThis.Bun?.$,
        experimental_workspace: unavailable,
      }, ctx.options)
      if (!hooks || typeof hooks !== "object") throw new Error("oc-box V1 adapter: invalid hook object")
      const unsupported = Object.keys(hooks).filter(key => !supported.has(key))
      if (unsupported.length) throw new Error("oc-box V1 adapter: unsupported hooks " + unsupported.join(", "))
      for (const [name, value] of Object.entries(hooks)) {
        if (name !== "tool" && typeof value !== "function") throw new Error("oc-box V1 adapter: invalid callback " + name)
      }
      if (hooks.config) await configure(ctx, hooks.config)
      if (hooks.tool) {
        const { tool } = await import("@opencode-ai/plugin")
        const tools = Object.entries(hooks.tool).map(([name, definition]) => ({
          name,
          description: definition.description,
          input: tool.schema.object(definition.args || {}),
          options: { codemode: false },
          execute: async (input, context) => {
            const result = await definition.execute(input, {
              ...context, callID: context.id, directory: ctx.location.directory,
              worktree: ctx.location.directory, abort: context.signal,
              metadata: value => { void context.progress({ ...value.metadata, title: value.title }) },
              ask: async () => { throw new Error("oc-box V1 adapter: custom permission requests require a native V2 port") },
            })
            return toolResult(result)
          },
        }))
        await ctx.tool.transform(editor => { for (const tool of tools) editor.add(tool) })
      }
      if (hooks["tool.definition"]) {
        const definitions = new Map()
        for (const tool of await ctx.tool.list()) {
          let parameters
          try { parameters = inputSchema(tool.input) } catch { continue }
          const original = JSON.stringify(parameters)
          const value = { description: tool.description, parameters }
          await hooks["tool.definition"]({ toolID: alias(tool.id) }, value)
          definitions.set(tool.id, { ...value, original })
        }
        await ctx.tool.transform(editor => {
          for (const [id, value] of definitions) editor.update(id, tool => {
            tool.description = value.description
            if (JSON.stringify(value.parameters) !== value.original) tool.input = value.parameters
          })
        })
      }
      if (hooks["tool.definition"] || hooks["experimental.chat.messages.transform"]) {
        await ctx.session.hook("context", async event => {
          if (hooks["tool.definition"]) {
            for (const [name, definition] of Object.entries(event.tools)) {
              const value = { description: definition.description, parameters: clone(definition.input) }
              await hooks["tool.definition"]({ toolID: alias(name) }, value)
              definition.description = value.description
              definition.input = value.parameters
            }
          }
          if (hooks["experimental.chat.messages.transform"]) await transformMessages(hooks["experimental.chat.messages.transform"], event)
        })
      }
      if (hooks["chat.headers"]) {
        await ctx.session.hook("model.request", async event => {
          const output = { headers: event.headers }
          await hooks["chat.headers"]({ sessionID: event.sessionID,
            message: { model: { variant: event.model.variant } },
            model: { id: event.model.id, providerID: event.model.providerID, api: { id: event.model.id } },
            provider: { info: { id: event.model.providerID } } }, output)
          event.headers = output.headers
        })
        // The copied Copilot effort workaround wraps global fetch on V1.
        // V2's native request executor need not call that global, so apply its
        // private-header contract at the supported HTTP request boundary too.
        await ctx.session.hook("http.request", async event => {
          const effort = event.request.headers.get("x-oc-copilot-effort")
          if (!effort) return
          const headers = new Headers(event.request.headers)
          headers.delete("x-oc-copilot-effort")
          let body = await event.request.clone().text()
          if (["low", "medium", "high", "xhigh", "max"].includes(effort) && /\/responses(?:\?|$)/.test(event.request.url)) {
            const value = JSON.parse(body)
            if (typeof value.model === "string" && value.model.startsWith("gpt-6") && !value.reasoning) {
              value.reasoning = { effort, summary: "auto" }
              value.include = Array.isArray(value.include) ? value.include : []
              if (!value.include.includes("reasoning.encrypted_content")) value.include.push("reasoning.encrypted_content")
              body = JSON.stringify(value)
            }
          }
          headers.delete("content-length")
          event.request = new Request(event.request, { headers, body })
        })
      }
      if (hooks["shell.env"]) {
        await ctx.shell.hook("create.before", async event => {
          const value = { env: event.env }
          await hooks["shell.env"]({ cwd: event.cwd }, value)
          event.env = value.env
        })
      }
      if (hooks["tool.execute.before"]) {
        await ctx.tool.hook("execute.before", async event => {
          const value = { args: event.input }
          await hooks["tool.execute.before"]({ tool: alias(event.tool), sessionID: event.sessionID, callID: event.id }, value)
          event.input = value.args
        })
      }
      if (hooks["tool.execute.after"]) {
        await ctx.tool.hook("execute.after", async event => {
          if (event.status !== "completed") return
          const content = event.result.content
          const text = typeof content === "string" ? content : Array.isArray(content)
            ? content.filter(item => item.type === "text").map(item => item.text).join("\n") : ""
          const value = { title: event.result.metadata?.title || event.tool, output: text, metadata: { ...event.result.metadata } }
          await hooks["tool.execute.after"]({ tool: alias(event.tool), sessionID: event.sessionID, callID: event.id, args: event.input }, value)
          if (value.output !== text) event.result.content = typeof content === "string" ? value.output : [
            { type: "text", text: value.output }, ...(Array.isArray(content) ? content.filter(item => item.type !== "text") : []),
          ]
          event.result.metadata = { ...value.metadata, title: value.title }
        })
      }
      return hooks.dispose
    },
  }
}
