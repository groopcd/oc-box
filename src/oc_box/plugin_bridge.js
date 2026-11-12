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
const owns = (value, key) => Object.prototype.hasOwnProperty.call(value, key)
const object = value => {
  if (!value || Object.prototype.toString.call(value) !== "[object Object]") return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === null || Object.getPrototypeOf(prototype) === null
}
// Compare the complete view, including deleted/undefined extra properties.
// JSON.stringify alone would silently hide some unsupported hook mutations.
function same(left, right) {
  if (Object.is(left, right)) return true
  if (!left || !right || typeof left !== "object" || typeof right !== "object" || Array.isArray(left) !== Array.isArray(right)) return false
  const keys = Reflect.ownKeys(left)
  return keys.length === Reflect.ownKeys(right).length && keys.every(key => owns(right, key) && same(left[key], right[key]))
}
function unchangedExcept(before, after, allowed) {
  return object(after) && [...new Set([...Reflect.ownKeys(before), ...Reflect.ownKeys(after)])]
    .every(key => allowed.includes(key) || (owns(before, key) === owns(after, key) && same(before[key], after[key])))
}
const sequence = value => Array.isArray(value) && Reflect.ownKeys(value).length === value.length + 1 &&
  Array.from({ length: value.length }, (_, index) => owns(value, index)).every(Boolean)
function json(value, seen = new Set()) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return true
  if (typeof value === "number") return Number.isFinite(value)
  if ((!object(value) && !Array.isArray(value)) || seen.has(value)) return false
  seen.add(value)
  const valid = Reflect.ownKeys(value).every(key => typeof key === "string" &&
    (Array.isArray(value) && key === "length" || Object.prototype.propertyIsEnumerable.call(value, key) && json(value[key], seen))) &&
    (!Array.isArray(value) || sequence(value))
  seen.delete(value)
  return valid
}
// Discovery overlays are in-memory only and omissions have no deletion meaning.
// This is the current source fold, not final effective native configuration.
function overlay(current, supplied) {
  return Object.fromEntries([...new Set([...Object.keys(current), ...Object.keys(supplied)])].map(key => [key,
    !owns(supplied, key) ? clone(current[key]) : object(current[key]) && object(supplied[key])
      ? overlay(current[key], supplied[key]) : clone(supplied[key])]))
}
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
      originals.set(part.id, { native, before: clone(part) })
      return part
    }),
  }))
  const output = { messages: view }
  const before = clone(output)
  await hook({}, output)
  if (!unchangedExcept(before, output, ["messages"])) throw new Error("oc-box V1 adapter: unsupported history output mutation")
  if (!sequence(output.messages) || output.messages.length !== event.messages.length) throw new Error("oc-box V1 adapter: adding/removing whole history messages requires a native V2 port")
  const contents = []
  for (let index = 0; index < output.messages.length; index++) {
    const message = output.messages[index]
    if (!unchangedExcept(before.messages[index], message, ["parts"])) {
      throw new Error("oc-box V1 adapter: changing history message order/identity/role requires a native V2 port")
    }
    const parts = message.parts
    if (!sequence(parts) || parts.length !== event.messages[index].content.length) {
      throw new Error("oc-box V1 adapter: changing history part count requires a native V2 port")
    }
    contents.push(parts.map((part, partIndex) => {
      if (!object(part) || part.id !== "prt_view_" + index + "_" + partIndex) {
        throw new Error("oc-box V1 adapter: changing history part order/identity requires a native V2 port")
      }
      const previous = originals.get(part.id)
      if (!previous) throw new Error("oc-box V1 adapter: invented legacy history parts require a native V2 port")
      if (same(part, previous.before)) return previous.native
      const before = previous.before
      if (["sessionID", "messageID"].some(key => owns(part, key) !== owns(before, key) || !same(part[key], before[key]))) {
        throw new Error("oc-box V1 adapter: changing history references requires a native V2 port")
      }
      if (part.type === "text" && typeof part.text === "string" && ["text", "media"].includes(previous.native.type)) {
        const obsolete = previous.native.type === "media" ? ["mime", "filename", "url"].filter(key => !owns(part, key)) : []
        if (unchangedExcept(before, part, ["text", ...(previous.native.type === "media" ? ["type", ...obsolete] : [])])) {
          return previous.native.type === "text" ? { ...previous.native, text: part.text } : { type: "text", text: part.text }
        }
      }
      if (part.type === "tool" && previous.native.type === "tool-call" &&
          unchangedExcept(before, part, ["state"]) && unchangedExcept(before.state, part.state, ["input"]) && owns(part.state, "input") && json(part.state.input)) {
        return { ...previous.native, input: clone(part.state.input) }
      }
      throw new Error("oc-box V1 adapter: unsupported legacy history part mutation")
    }))
  }
  // Validate every message/part first; a later unsupported edit must not leave
  // an earlier supported edit applied to the outgoing native event.
  contents.forEach((content, index) => { event.messages[index].content = content })
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
  // Only source model inventory edits are bridged. In 2.2.6, native provider
  // config runs AFTER external plugins. Neither setup reads nor this editor's
  // position is the final configured settings view, including after reload.
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
      const options = owns(provider, "settings") ? provider.settings : owns(provider, "options") ? provider.options : {}
      if (!object(options) || !json(options)) throw new Error("oc-box V1 adapter: invalid provider options")
      const { package: nativePackage, settings, options: legacyOptions, ...metadata } = provider
      config.provider[id] = {
        ...config.provider[id], ...metadata,
        npm: (nativePackage || provider.npm || config.provider[id]?.npm || "").replace(/^aisdk:/, ""),
        options: overlay(config.provider[id]?.options || {}, options),
        models: owns(provider, "models") ? provider.models : config.provider[id]?.models || {},
      }
    }
  }
  const before = clone(config)
  await hook(config)
  if (!unchangedExcept(before, config, ["provider"])) {
    throw new Error("oc-box V1 adapter: config edits outside provider discovery require a native V2 port")
  }
  if (!object(config.provider)) {
    throw new Error("oc-box V1 adapter: invalid provider config edit")
  }
  // Preflight the complete edit before registering a replayable native transform.
  // In particular, deletions of metadata are just as unsupported as additions.
  const edits = []
  for (const [id, provider] of Object.entries(config.provider)) {
    const previous = owns(before.provider, id) ? before.provider[id] : undefined
    if (!object(provider)) throw new Error("oc-box V1 adapter: invalid provider config edit")
    if (previous ? !unchangedExcept(previous, provider, ["options", "models"])
      : Reflect.ownKeys(provider).some(key => !["name", "npm", "options", "models"].includes(key))) {
      throw new Error("oc-box V1 adapter: provider metadata edits require a native V2 port")
    }
    const options = owns(provider, "options") ? provider.options : {}
    if (!object(options) || !json(options)) throw new Error("oc-box V1 adapter: invalid provider options")
    if (!previous && (typeof provider.npm !== "string" || !provider.npm || owns(provider, "name") && typeof provider.name !== "string")) {
      throw new Error("oc-box V1 adapter: invalid new provider metadata")
    }
    if (same(previous, provider)) continue
    if (owns(provider, "models") && (!object(provider.models) || !json(provider.models))) {
      throw new Error("oc-box V1 adapter: invalid provider model inventory")
    }
    const inventory = Object.entries(provider.models || {}).map(([name, model]) => {
      if (!object(model)) throw new Error("oc-box V1 adapter: invalid provider model")
      return toModel(id, name, model)
    })
    edits.push({ id, provider: clone(provider), previous, options: clone(options), inventory })
  }
  const removed = Object.keys(before.provider).filter(id => !owns(config.provider, id))
  // There is no supported late-provider/config API on the pinned plugin Context.
  // Refuse the whole plan before registration, even for apparently unconfigured
  // providers: a later project/managed declaration is not visible in this fold
  // or guaranteed to be in the minimal discovery helper. Request hooks cannot
  // implement provider-list settings, removal, or configured-key deletion.
  if (removed.length || edits.some(({ previous, options }) => !previous || !same(previous.options || {}, options))) {
    throw new Error("oc-box V1 adapter: provider settings/addition/removal edits require a native V2 port; native config applies after plugin setup")
  }
  if (!edits.length && !removed.length) return
  await ctx.provider.transform(editor => {
    for (const { id, provider, previous, inventory } of edits) {
      if (!same(previous.models, provider.models)) editor.models.set(id, inventory)
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
