// Offline bridge contract tests against a mocked public V2 plugin context.
import assert from "node:assert/strict"
import fs from "node:fs/promises"
import vm from "node:vm"

const source = await fs.readFile(process.argv[2], "utf8")
let declaration = { providers: { fixture: { package: "aisdk:fixture", settings: { baseURL: "http://localhost/v1" } } } }
const environment = {}
const context = vm.createContext({ structuredClone, process: { env: environment }, URL, Buffer, Request, Headers })
const sdk = new vm.SyntheticModule(["Model"], function () {
  this.setExport("Model", { Info: { default: (providerID, id) => ({ id, modelID: id, providerID, name: id,
    capabilities: { tools: true, input: ["text"], output: ["text"] }, limit: {}, variants: [] }) } })
}, { context })
const files = new vm.SyntheticModule(["readFile"], function () {
  this.setExport("readFile", async () => JSON.stringify(declaration))
}, { context })
const effect = new vm.SyntheticModule(["Schema"], function () {
  this.setExport("Schema", { isSchema: input => !!input?.fixtureSchema,
    toJsonSchemaDocument: () => ({ schema: { type: "object", properties: { command: { type: "string" } } }, definitions: {} }) })
}, { context })
const legacy = new vm.SyntheticModule(["tool"], function () {
  this.setExport("tool", { schema: { object: input => input } })
}, { context })
await legacy.link(() => {})
await legacy.evaluate()
const bridge = new vm.SourceTextModule(source, {
  context, identifier: "file:///fixture/plugin_bridge.js",
  initializeImportMeta(meta) { meta.url = "file:///fixture/plugin_bridge.js" },
  importModuleDynamically: async () => legacy,
})
await bridge.link(specifier => specifier === "@opencode/plugin" ? sdk : specifier === "effect" ? effect : files)
await bridge.evaluate()
const adapt = bridge.namespace.adapt

function fixture() {
  const hooks = new Map()
  const ctx = {
    app: { version: "2.2.6" }, options: {}, location: { directory: "/fixture" },
    tool: {
      async hook(name, callback) { hooks.set(name, callback) },
      async transform(callback) { callback({ list: () => [], update() {} }) },
      async list() { return [] },
    },
    shell: { async hook(name, callback) { hooks.set(name, callback) } },
    session: { async hook(name, callback) { hooks.set("session." + name, callback) } },
    provider: { async list() { return { data: [] } }, async transform() {} },
  }
  return { ctx, hooks }
}

{
  const native = { id: "native", setup() {} }
  assert.equal(adapt({ default: native }, "unused"), native)
}
{
  const { ctx } = fixture()
  await assert.rejects(adapt({ default: async () => ({ "chat.message": async () => {} }) }, "unsupported").setup(ctx), /unsupported hooks chat.message/)
  await assert.rejects(adapt({ a: async () => ({}), b: async () => ({}) }, "multiple").setup(ctx), /one distinct/)
  await assert.rejects(adapt({ default: async () => ({ config: async value => { value.agent = {} } }) }, "invalid-config").setup(ctx), /outside provider discovery/)
}
{
  const { ctx, hooks } = fixture()
  const factory = async () => ({
    "tool.execute.before": async (input, value) => {
      assert.equal(input.tool, "bash")
      assert.equal(input.callID, "call_test")
      if (value.args.command === "refuse") throw new Error("refused")
      value.args = { command: "changed" }
    },
    "shell.env": async (_, value) => { value.env.REQUIRED = "present" },
  })
  await adapt({ default: factory, server: factory }, "before").setup(ctx)
  const invocation = { tool: "shell", input: { command: "original" }, id: "call_test" }
  await hooks.get("execute.before")(invocation)
  assert.equal(invocation.input.command, "changed")
  await assert.rejects(hooks.get("execute.before")({ ...invocation, input: { command: "refuse" } }), /refused/)
  const shell = { cwd: "/fixture", env: { EXISTING: "preserved" } }
  await hooks.get("create.before")(shell)
  assert.equal(shell.env.REQUIRED, "present")
  assert.equal(shell.env.EXISTING, "preserved")
}
{
  const { ctx, hooks } = fixture()
  const attachment = { type: "file", uri: "data:image/png;base64,fixture", mime: "image/png" }
  const structured = { bytes: [1, 2, 3], result: true }
  let calls = 0
  await adapt({ default: async () => ({ "tool.execute.after": async (input, value) => {
    calls++
    assert.equal(input.tool, "bash")
    assert.equal(value.output, "large output")
    value.output = "clamped output"
    value.title = "[clamped]"
    value.metadata.changed = true
  } }) }, "after").setup(ctx)
  const event = { tool: "shell", status: "completed", input: {}, result: {
    content: [{ type: "text", text: "large output" }, attachment], output: structured, metadata: { exit: 0 },
  } }
  await hooks.get("execute.after")(event)
  assert.equal(event.result.output, structured)
  assert.equal(event.result.content[1], attachment)
  assert.equal(event.result.content[0].text, "clamped output")
  assert.equal(event.result.metadata.exit, 0)
  assert.equal(event.result.metadata.changed, true)
  assert.equal(event.result.metadata.title, "[clamped]")
  await hooks.get("execute.after")({ status: "error" })
  assert.equal(calls, 1)
}
{
  const { ctx, hooks } = fixture()
  const schema = { fixtureSchema: true, cycle: null }
  schema.cycle = schema
  let measured
  ctx.tool.list = async () => [{ id: "shell", description: "Shell", input: schema }]
  await adapt({ default: async () => ({ "tool.definition": async (_, output) => { measured = JSON.stringify(output.parameters).length } }) }, "schema").setup(ctx)
  assert.ok(measured > 20)
  await hooks.get("session.context")({ tools: { fixture: { description: "MCP", input: { type: "object", properties: { query: { type: "string" } } } } }, messages: [] })
  assert.ok(measured > 20)
}
{
  const { ctx, hooks } = fixture()
  await adapt({ default: async () => ({
    "experimental.chat.messages.transform": async (_, output) => {
      const parts = output.messages[0].parts
      parts[0] = { ...parts[0], type: "text", text: "converted PDF" }
      parts[1].state.input.command = "clean command"
    },
    "chat.headers": async (input, output) => { output.headers.effort = input.message.model.variant },
  }) }, "chat").setup(ctx)
  const media = { type: "media", media: { mediaType: "application/pdf", source: { type: "base64", data: "fixture" } }, filename: "fixture.pdf" }
  const tool = { type: "tool-call", id: "call_fixture", name: "shell", input: { command: "wrapped" }, providerMetadata: { fixture: true } }
  const reasoning = { type: "reasoning", text: "opaque", encrypted: "retained" }
  const event = { sessionID: "ses_fixture", messages: [{ role: "assistant", content: [media, tool, reasoning] }], tools: {} }
  await hooks.get("session.context")(event)
  assert.equal(event.messages[0].content[0].text, "converted PDF")
  assert.equal(event.messages[0].content[1].input.command, "clean command")
  assert.equal(event.messages[0].content[1].providerMetadata, tool.providerMetadata)
  assert.equal(event.messages[0].content[2], reasoning)
  const request = { model: { providerID: "fixture", id: "gpt-6-fixture", variant: "high" }, headers: { retained: "yes" } }
  await hooks.get("session.model.request")(request)
  assert.equal(request.headers.effort, "high")
  assert.equal(request.headers.retained, "yes")
  const http = { request: new Request("https://api.example.test/responses", { method: "POST", headers: { "x-oc-copilot-effort": "high", "content-type": "application/json" }, body: JSON.stringify({ model: "gpt-6-fixture", include: ["existing"] }) }) }
  await hooks.get("session.http.request")(http)
  const body = await http.request.json()
  assert.equal(body.reasoning.effort, "high")
  assert.equal(body.reasoning.summary, "auto")
  assert.deepEqual(body.include, ["existing", "reasoning.encrypted_content"])
  assert.equal(http.request.headers.has("x-oc-copilot-effort"), false)
  const existing = { request: new Request("https://api.example.test/responses", { method: "POST", headers: { "x-oc-copilot-effort": "max" }, body: JSON.stringify({ model: "gpt-6-fixture", reasoning: { effort: "low" } }) }) }
  await hooks.get("session.http.request")(existing)
  assert.equal((await existing.request.json()).reasoning.effort, "low")
}
{
  const { ctx, hooks } = fixture()
  await adapt({ default: async () => ({ "experimental.chat.messages.transform": async (_, value) => { value.messages[0].parts.reverse() } }) }, "order").setup(ctx)
  await assert.rejects(hooks.get("session.context")({ messages: [{ role: "user", content: [{ type: "text", text: "one" }, { type: "text", text: "two" }] }], tools: {} }), /part order/)
}
{
  const { ctx } = fixture()
  let registered
  ctx.tool.transform = async callback => callback({ add(tool) { registered = tool } })
  await adapt({ default: async () => ({ tool: { attachment_probe: {
    description: "Fixture attachment", args: {}, execute: async () => ({ output: "text", metadata: { kept: true },
      attachments: [{ type: "file", url: "data:text/plain;base64,ZmFrZQ==", mime: "text/plain", filename: "fixture.txt" }] }),
  } } }) }, "attachment").setup(ctx)
  const result = await registered.execute({}, { progress: async () => {} })
  assert.equal(result.content[0].text, "text")
  assert.equal(result.content[1].uri, "data:text/plain;base64,ZmFrZQ==")
  assert.equal(result.content[1].mime, "text/plain")
  assert.equal(result.content[1].name, "fixture.txt")
  assert.equal(result.metadata.kept, true)
}
console.log("PASS: V1-to-V2 bridge refusal/input/env, native pass-through, unsupported hooks, attachment and structured-output preservation")

// Every expected value below is an independent literal/sentinel, not a call to
// the bridge's overlay/delta/projection implementation.
const failures = []
async function check(name, run) {
  try { await run(); console.log("PASS: " + name) }
  catch (error) { failures.push(name); console.error("FAIL: " + name + "\n" + error.stack) }
}
const plain = value => JSON.parse(JSON.stringify(value))
function providerFixture(settings = {}) {
  const { ctx } = fixture()
  const records = new Map([["fixture", { provider: { id: "fixture", name: "Fixture", package: "aisdk:fixture", settings: structuredClone(settings) }, models: new Map() }]])
  const edits = []
  let replay
  ctx.provider.list = async () => ({ data: [...records.values()].map(item => structuredClone(item.provider)) })
  ctx.provider.transform = async callback => {
    replay = () => callback({
      get: id => records.get(id),
      remove: id => { edits.push("remove"); records.delete(id) },
      add: ({ info, models }) => { edits.push("add"); records.set(info.id, { provider: info, models: new Map(models.map(model => [model.id, model])) }) },
      update: (id, update) => { edits.push("update"); update(records.get(id).provider) },
      models: { set: (id, models) => { edits.push("models"); records.get(id).models = new Map(models.map(model => [model.id, model])) } },
    })
    replay()
  }
  return { ctx, records, edits, replay: () => replay() }
}
const setupConfig = (ctx, hook) => adapt({ default: async () => ({ config: hook }) }, "settings-contract").setup(ctx)

if (!process.argv[3] || process.argv[3] === "settings") {
  await check("F5 reduced helper and launch overlay preserve effective settings after a successful edit", async () => {
    const secret = Buffer.from([115, 121, 110, 116, 104, 101, 116, 105, 99]).toString()
    const { ctx, records } = providerFixture({ baseURL: "http://effective/v1", apiKey: secret,
      headers: { authorization: secret, retained: "effective" }, nested: { retained: true }, timeout: 1 })
    declaration = { providers: { fixture: { package: "aisdk:fixture", settings: { baseURL: "http://discovered/v1", headers: { helper: "yes" } } } } }
    environment.OPENCODE_CONFIG_CONTENT = JSON.stringify({ providers: { fixture: { settings: { timeout: 42, headers: { launch: "yes" } } } } })
    await setupConfig(ctx, config => { config.provider.fixture.options.baseURL = "http://edited/v1" })
    assert.deepEqual(plain(records.get("fixture").provider.settings), { baseURL: "http://edited/v1", apiKey: secret,
      headers: { authorization: secret, retained: "effective" }, nested: { retained: true }, timeout: 1 })
    // Supplied discovery/launch values form the hook view, not an unrelated write
    // back to effective settings: only the hook's baseURL edit is committed.
  })
  delete environment.OPENCODE_CONFIG_CONTENT
  declaration = {}
  await check("F5 supplied nested discovery and launch keys are overlaid into the effective hook view", async () => {
    const { ctx, records } = providerFixture({ nested: { retained: 1 }, headers: { native: "kept" }, timeout: 1 })
    declaration = { providers: { fixture: { settings: { nested: { helper: 2 }, headers: { helper: "yes" } } } } }
    environment.OPENCODE_CONFIG_CONTENT = JSON.stringify({ providers: { fixture: { settings: { nested: { launch: 3 }, timeout: 42 } } } })
    await setupConfig(ctx, config => {
      assert.deepEqual(plain(config.provider.fixture.options), { nested: { retained: 1, helper: 2, launch: 3 }, headers: { native: "kept", helper: "yes" }, timeout: 42 })
      config.provider.fixture.options.timeout = 43
    })
    assert.deepEqual(plain(records.get("fixture").provider.settings), { nested: { retained: 1 }, headers: { native: "kept" }, timeout: 43 })
  })
  delete environment.OPENCODE_CONFIG_CONTENT
  declaration = {}
  await check("F5 recursive delta preserves unseen current keys and intentionally deletes visible keys", async () => {
    const { ctx, records, replay } = providerFixture({ headers: { keep: "yes", remove: "old", change: "old" }, nested: { keep: true, remove: 1 }, remove: true })
    await setupConfig(ctx, config => {
      delete config.provider.fixture.options.headers.remove
      config.provider.fixture.options.headers.change = "new"
      delete config.provider.fixture.options.nested.remove
      delete config.provider.fixture.options.remove
      records.get("fixture").provider.settings.headers.unseen = "later"
      records.get("fixture").provider.settings.unseen = { retained: true }
    })
    const expected = { headers: { keep: "yes", change: "new", unseen: "later" }, nested: { keep: true }, unseen: { retained: true } }
    assert.deepEqual(plain(records.get("fixture").provider.settings), expected)
    replay()
    assert.deepEqual(plain(records.get("fixture").provider.settings), expected)
  })
  await check("F5 arrays and scalar values replace atomically", async () => {
    const { ctx, records } = providerFixture({ array: [1, { retained: true }], scalar: "old", object: { old: 1 }, unchanged: [3] })
    await setupConfig(ctx, config => {
      config.provider.fixture.options.array = [2]
      config.provider.fixture.options.scalar = null
      config.provider.fixture.options.object = false
      config.provider.fixture.options.added = { value: 7 }
    })
    assert.deepEqual(plain(records.get("fixture").provider.settings), { array: [2], scalar: null, object: false, unchanged: [3], added: { value: 7 } })
  })
  await check("F5 newly supplied object keys recurse without replacing unseen current siblings", async () => {
    const { ctx, records } = providerFixture({ retained: true })
    await setupConfig(ctx, config => {
      config.provider.fixture.options.added = { edited: true }
      records.get("fixture").provider.settings.added = { unseen: true }
    })
    assert.deepEqual(plain(records.get("fixture").provider.settings), { retained: true, added: { unseen: true, edited: true } })
  })
  await check("F5 deleting the options property deletes its visible keys but retains unseen effective keys", async () => {
    const { ctx, records } = providerFixture({ remove: 1, headers: { visible: "remove" } })
    await setupConfig(ctx, config => {
      delete config.provider.fixture.options
      records.get("fixture").provider.settings.unseen = "retained"
    })
    assert.deepEqual(plain(records.get("fixture").provider.settings), { unseen: "retained" })
  })
  await check("F5 provider addition removal and model inventory edits remain supported", async () => {
    const { ctx, records } = providerFixture({ retained: 1 })
    records.set("remove", { provider: { id: "remove", name: "Remove", package: "aisdk:fixture", settings: {} }, models: new Map() })
    await setupConfig(ctx, config => {
      delete config.provider.remove
      config.provider.added = { name: "Added", npm: "fixture", options: { baseURL: "http://added/v1" }, models: { added: { name: "Added model", options: { temperature: 0.3 } } } }
      config.provider.fixture.models = { edited: { name: "Edited model", limit: { context: 1234 }, attachment: true } }
    })
    assert.equal(records.has("remove"), false)
    assert.equal(records.get("added").provider.package, "aisdk:fixture")
    assert.deepEqual(plain(records.get("added").provider.settings), { baseURL: "http://added/v1" })
    assert.equal(records.get("added").models.get("added").settings.temperature, 0.3)
    assert.equal(records.get("fixture").models.get("edited").limit.context, 1234)
    assert.deepEqual(plain(records.get("fixture").models.get("edited").capabilities.input), ["text", "image"])
  })
  for (const [name, mutate] of [
    ["null options", value => { value.options = null }],
    ["array options", value => { value.options = [] }],
    ["string options", value => { value.options = "invalid" }],
    ["non-JSON options", value => { value.options.invalid = undefined }],
    ["added metadata", value => { value.unsupported = true }],
    ["deleted metadata", value => { delete value.name }],
    ["changed metadata", value => { value.name = "unsupported" }],
  ]) await check("F5 refuses " + name + " before any editor operation", async () => {
    const { ctx, records, edits } = providerFixture({ retained: true })
    records.set("invalid", { provider: { id: "invalid", name: "Invalid", package: "aisdk:fixture", settings: {} }, models: new Map() })
    const before = plain([...records])
    await assert.rejects(setupConfig(ctx, config => {
      config.provider.fixture.options.valid = "must not partially apply"
      mutate(config.provider.invalid)
    }), /oc-box V1 adapter:/)
    assert.deepEqual(edits, [])
    assert.deepEqual(plain([...records]), before)
  })
}

if (!process.argv[3] || process.argv[3] === "history") {
  const historyEvent = () => ({ sessionID: "ses_fixture", tools: {}, messages: [
    { id: "msg_first", role: "user", content: [{ type: "text", text: "original", providerMetadata: { retained: true } }] },
    { id: "msg_second", role: "assistant", content: [
      { type: "tool-call", id: "call_fixture", name: "shell", input: { command: "original" }, providerMetadata: { retained: true } },
      { type: "media", filename: "fixture.pdf", media: { mediaType: "application/pdf", source: { type: "bytes", data: new Uint8Array([1, 2, 3]) } }, providerMetadata: { retained: true } },
      { type: "reasoning", text: "opaque", encrypted: "opaque-encrypted", providerMetadata: { retained: true } },
    ] },
  ] })
  async function historyHook(mutate) {
    const { ctx, hooks } = fixture()
    await adapt({ default: async () => ({ "experimental.chat.messages.transform": async (_, output) => mutate(output) }) }, "history-contract").setup(ctx)
    return hooks.get("session.context")
  }
  await check("F6 supported text and tool-input edits preserve original opaque native media and reasoning", async () => {
    const event = historyEvent()
    const [tool, media, reasoning] = event.messages[1].content
    const first = event.messages[0].content[0]
    const hook = await historyHook(output => {
      output.messages[0].parts[0].text = "edited"
      output.messages[1].parts[0].state.input = { command: "changed", nested: [{ sessionID: "opaque-input" }] }
    })
    await hook(event)
    assert.equal(event.messages[0].content[0].text, "edited")
    assert.equal(event.messages[0].content[0].providerMetadata, first.providerMetadata)
    assert.deepEqual(plain(event.messages[1].content[0].input), { command: "changed", nested: [{ sessionID: "opaque-input" }] })
    assert.equal(event.messages[1].content[0].providerMetadata, tool.providerMetadata)
    assert.equal(event.messages[1].content[1], media)
    assert.equal(event.messages[1].content[2], reasoning)
  })
  await check("F6 file-to-text conversion supports replacement without obsolete file properties", async () => {
    const event = historyEvent()
    const hook = await historyHook(output => {
      const { id, sessionID, messageID } = output.messages[1].parts[1]
      output.messages[1].parts[1] = { id, sessionID, messageID, type: "text", text: "PDF replacement" }
    })
    await hook(event)
    assert.deepEqual(plain(event.messages[1].content[1]), { type: "text", text: "PDF replacement" })
  })
  for (const [name, mutate] of [
    ["message session reference", output => { output.messages[1].info.sessionID = "ses_other" }],
    ["deleted message session reference", output => { delete output.messages[1].info.sessionID }],
    ["message metadata", output => { output.messages[1].info.metadata = { ignored: true } }],
    ["deleted message role", output => { delete output.messages[1].info.role }],
    ["message undefined extra info", output => { output.messages[1].info.extra = undefined }],
    ["message extra property", output => { output.messages[1].extra = true }],
    ["output extra property", output => { output.extra = true }],
    ["messages array extra property", output => { output.messages.extra = true }],
    ["parts array extra property", output => { output.messages[1].parts.extra = true }],
    ["parts array hole", output => { delete output.messages[1].parts[0] }],
    ["text metadata", output => { output.messages[0].parts[0].metadata = {} }],
    ["deleted text", output => { delete output.messages[0].parts[0].text }],
    ["part session reference", output => { output.messages[1].parts[0].sessionID = "ses_other" }],
    ["deleted part message reference", output => { delete output.messages[1].parts[0].messageID }],
    ["tool identity", output => { output.messages[1].parts[0].callID = "other" }],
    ["tool status", output => { output.messages[1].parts[0].state.status = "running" }],
    ["tool state metadata", output => { output.messages[1].parts[0].state.metadata = {} }],
    ["tool extra property", output => { output.messages[1].parts[0].extra = true }],
    ["tool undefined extra property", output => { output.messages[1].parts[0].extra = undefined }],
    ["deleted tool input", output => { delete output.messages[1].parts[0].state.input }],
    ["media filename", output => { output.messages[1].parts[1].filename = "changed.pdf" }],
    ["deleted media mime", output => { delete output.messages[1].parts[1].mime }],
    ["converted media extra metadata", output => { Object.assign(output.messages[1].parts[1], { type: "text", text: "converted", metadata: {} }) }],
    ["reasoning text", output => { output.messages[1].parts[2].text = "changed" }],
    ["part count", output => { output.messages[1].parts.pop() }],
    ["part order", output => { output.messages[1].parts.reverse() }],
    ["message count", output => { output.messages.pop() }],
    ["message order", output => { output.messages.reverse() }],
    ["deleted message info", output => { delete output.messages[1].info }],
    ["invalid messages shape", output => { output.messages = {} }],
  ]) await check("F6 refuses " + name + " without partially applying an earlier valid edit", async () => {
    const event = historyEvent()
    const before = structuredClone(event)
    const originalParts = event.messages.map(message => message.content)
    const hook = await historyHook(output => {
      output.messages[0].parts[0].text = "must not partially apply"
      mutate(output)
    })
    await assert.rejects(hook(event), /oc-box V1 adapter:/)
    assert.deepEqual(event, before)
    event.messages.forEach((message, index) => assert.equal(message.content, originalParts[index]))
  })
  await check("F6 original hook refusal propagates without applying valid edits", async () => {
    const event = historyEvent()
    const before = structuredClone(event)
    const refusal = new Error("fixture refusal")
    const hook = await historyHook(output => { output.messages[0].parts[0].text = "must not apply"; throw refusal })
    await assert.rejects(hook(event), error => error === refusal)
    assert.deepEqual(event, before)
  })
}
if (failures.length) throw new Error("Bridge contract failures: " + failures.join(", "))
