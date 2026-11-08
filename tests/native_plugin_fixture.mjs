// Native-only plugin behavior tests; no legacy factories or bridge are imported.
import assert from "node:assert/strict"
import fs from "node:fs/promises"
import vm from "node:vm"
import path from "node:path"
import { createHash } from "node:crypto"

const root = process.argv[2]
async function load(name, overrides = {}) {
  const hooks = new Map(), writes = []
  const context = vm.createContext({ Buffer, URL, Map, AbortSignal, process: { env: {} }, fetch: overrides.fetch })
  const exports = {
    "node:fs/promises": { mkdir: async () => {}, writeFile: async (file, data, options) => writes.push({ file, data, options }),
      mkdtemp: async () => "/tmp/private-fixture", chmod: async () => {}, unlink: async () => {}, rmdir: async () => {}, readFile: async () => Buffer.from("fixture PDF") },
    "node:os": { default: { homedir: () => "/home/fixture", tmpdir: () => "/tmp" } },
    "node:path": { default: path },
    "node:crypto": { randomUUID: () => "fixture-id", createHash },
    "node:child_process": { execFile: (...args) => args.at(-1)(null, { stdout: "PDF_EXTRACTED" }) },
    "node:util": { promisify: () => async () => ({ stdout: "PDF_EXTRACTED" }) },
    "node:url": { fileURLToPath: value => new URL(value).pathname },
    "@opencode/plugin": { Model: { Info: { default: (providerID, id) => ({ id, providerID, capabilities: { tools: true, input: [], output: ["text"] }, limit: {} }) } } },
  }
  const module = new vm.SourceTextModule(await fs.readFile(path.join(root, name, "index.js"), "utf8"), { context })
  await module.link(specifier => new vm.SyntheticModule(Object.keys(exports[specifier]), function () {
    for (const [key, value] of Object.entries(exports[specifier])) this.setExport(key, value)
  }, { context }))
  await module.evaluate()
  const ctx = { options: {}, shell: { hook: async (name, fn) => hooks.set("shell." + name, fn) },
    session: { hook: async (name, fn) => hooks.set("session." + name, fn) },
    tool: { hook: async (name, fn) => hooks.set("tool." + name, fn), transform: async fn => fn({ add(tool) { hooks.set("custom." + tool.name, tool) } }) },
    model: { list: async () => ({ data: [{ id: "fixture", providerID: "fixture", capabilities: { input: ["text"] } }] }) },
    provider: { transform: async fn => fn({ get: () => undefined, add(record) { hooks.set("provider", record) }, remove() { hooks.set("provider-removed", true) } }) },
    ...overrides.ctx,
  }
  await module.namespace.default.setup(ctx)
  return { hooks, writes, ctx, context }
}
{
  const { hooks } = await load("python-isolation")
  const event = { env: { EXISTING: "retained" } }
  await hooks.get("shell.create.before")(event)
  assert.equal(event.env.PIP_REQUIRE_VIRTUALENV, "1")
  assert.equal(event.env.PYTHONDONTWRITEBYTECODE, "1")
  assert.equal(event.env.EXISTING, "retained")
}
{
  const { hooks, writes } = await load("output-clamp")
  for (const text of ["中".repeat(3000), "🙂".repeat(6000), "a".repeat(8192)]) {
    const file = { type: "file", uri: "data:image/png;base64,fixture", mime: "image/png" }
    const event = { tool: "shell", status: "completed", input: {}, result: { content: [{ type: "text", text }, file], output: { structured: true }, metadata: { kept: true } } }
    await hooks.get("tool.execute.after")(event)
    const result = event.result.content[0].text
    assert.ok(Buffer.byteLength(result) <= 8192)
    assert.ok(!/[\uD800-\uDBFF]$/.test(result) && !result.includes("�"))
    assert.equal(event.result.content[1], file)
    assert.equal(event.result.output.structured, true)
    assert.equal(event.result.metadata.kept, true)
    if (Buffer.byteLength(text) > 8192) assert.equal(writes.at(-1).data, text)
  }
  const exempt = { tool: "read", status: "completed", input: {}, result: { content: "long".repeat(5000) } }
  const before = exempt.result.content
  await hooks.get("tool.execute.after")(exempt)
  assert.equal(exempt.result.content, before)
  const optout = { ...exempt, tool: "shell", input: { command: "full-output-ok" } }
  await hooks.get("tool.execute.after")(optout)
  assert.equal(optout.result.content, before)
}
{
  const { hooks, context } = await load("no-nested-sandbox")
  const symbol = Symbol.for("opencode.host-sandbox.original-commands")
  let cancelled = false
  context.originalMapFixture = new Map([["call", { command: "plain", cancel() { cancelled = true } }]])
  vm.runInContext('globalThis[Symbol.for("opencode.host-sandbox.original-commands")] = originalMapFixture', context)
  await hooks.get("tool.execute.before")({ tool: "shell", id: "call", input: { command: "bwrap apply-seccomp socat TCP-LISTEN" } })
  context.originalMapFixture.get("call").command = "bwrap apply-seccomp socat TCP-LISTEN"
  assert.throws(() => hooks.get("tool.execute.before")({ tool: "shell", id: "call", input: { command: "wrapped twice" } }), /refusing a hand-written/)
  assert.equal(cancelled, true)
}
{
  const { hooks, ctx } = await load("pdf-to-text")
  const pdf = { type: "media", media: { mediaType: "application/pdf", source: { type: "base64", data: "Zml4dHVyZQ==" } }, filename: "fixture.pdf" }
  const event = { model: { id: "fixture", providerID: "fixture" }, messages: [{ role: "user", content: [pdf, { type: "text", text: "retained" }] }] }
  await hooks.get("session.context")(event)
  assert.match(event.messages[0].content[0].text, /PDF_EXTRACTED/)
  ctx.model.list = async () => ({ data: [{ id: "fixture", providerID: "fixture", capabilities: { input: ["text", "pdf"] } }] })
  const supported = { ...event, messages: [{ content: [pdf] }] }
  await hooks.get("session.context")(supported)
  assert.equal(supported.messages[0].content[0], pdf)
}
{
  const fetch = async url => ({ ok: true, json: async () => url.endsWith("/models") ? { data: [{ id: "fixture-model", labels: ["tool-calling"], max_context_window: 32000, recipe_options: { ctx_size: 16000 } }, { id: "image", labels: ["image"] }] } : { model_loaded: "fixture-model" } })
  const { hooks } = await load("lemonade-models", { fetch, ctx: { options: { configured: true } } })
  assert.equal(hooks.get("provider").models[0].limit.context, 16000)
  assert.equal(hooks.get("provider").models.length, 1)
  const listed = await hooks.get("custom.lemonade").execute({ action: "list" })
  assert.match(listed.content, /fixture-model ctx=16000/)
  const stopped = await load("lemonade-models", { fetch: async () => { throw Error("stopped") }, ctx: { options: { configured: true } } })
  assert.equal(stopped.hooks.get("provider-removed"), true)
  assert.match((await stopped.hooks.get("custom.lemonade").execute({ action: "status" })).content, /stopped\/unreachable/)
}
console.log("PASS: native V2 environment, selective Unicode byte limits, attachments, guard order, PDF capability fallback and Lemonade discovery without V1 SDK/bridge")
