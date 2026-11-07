// Offline bridge contract tests against a mocked public V2 plugin context.
import assert from "node:assert/strict"
import fs from "node:fs/promises"
import vm from "node:vm"

const source = await fs.readFile(process.argv[2], "utf8")
const context = vm.createContext({ structuredClone, process: { env: {} }, URL })
const sdk = new vm.SyntheticModule(["Model"], function () {
  this.setExport("Model", { Info: { default: (providerID, id) => ({ id, modelID: id, providerID, name: id,
    capabilities: { tools: true, input: ["text"], output: ["text"] }, limit: {}, variants: [] }) } })
}, { context })
const files = new vm.SyntheticModule(["readFile"], function () {
  this.setExport("readFile", async () => '{"providers":{"fixture":{"package":"aisdk:fixture","settings":{"baseURL":"http://localhost/v1"}}}}')
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
await bridge.link(specifier => specifier === "@opencode/plugin" ? sdk : files)
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
