// Public-hook contract tests. The runtime is mocked: NOT host isolation proof.
import assert from "node:assert/strict"
import fs from "node:fs/promises"
import vm from "node:vm"

const source = await fs.readFile(process.argv[2], "utf8")
async function load(user = {}, env = {}, broken = false) {
  const hooks = new Map(), calls = []
  const runtime = {
    async initialize(policy) { calls.push(["policy", policy]); if (broken) throw Error("private failure") },
    async wrapWithSandbox(command) { return "WRAPPED " + command },
    cleanupAfterCommand() { calls.push(["cleanup"]) },
    async reset() { calls.push(["reset"]) },
  }
  const context = vm.createContext({ process: { env }, console: { error() {} } })
  const module = new vm.SourceTextModule(source, { context })
  const exports = {
    "@anthropic-ai/sandbox-runtime": { SandboxManager: runtime },
    "node:fs/promises": { default: { async readFile() { return JSON.stringify(user) } } },
    "node:os": { default: { homedir: () => "/home/fixture", tmpdir: () => "/tmp/fixture" } },
    "node:path": { default: (await import("node:path")).default },
  }
  await module.link(name => new vm.SyntheticModule(Object.keys(exports[name]), function () {
    for (const [key, value] of Object.entries(exports[name])) this.setExport(key, value)
  }, { context }))
  await module.evaluate()
  const cleanup = await module.namespace.default.setup({ location: { directory: "/project", project: { worktree: "/project" } }, tool: { async hook(name, hook) { hooks.set(name, hook) } } })
  return { hooks, calls, cleanup }
}
{
  const { hooks, calls, cleanup } = await load({ mode: "enforce", network: { allowedDomains: ["fixture.test"] } })
  assert.deepEqual(Array.from(calls[0][1].network.allowedDomains), ["fixture.test"])
  assert.ok(calls[0][1].filesystem.denyRead.includes("/home/fixture/.ssh"))
  const original = { command: "plain command" }
  const before = { id: "call_fixture", tool: "shell", input: original }
  await hooks.get("execute.before")(before)
  assert.equal(before.input.command, "WRAPPED plain command")
  assert.equal(original.command, "plain command")
  await hooks.get("execute.after")({ ...before, status: "error" })
  assert.equal(before.input.command, "plain command")
  assert.ok(calls.some(item => item[0] === "cleanup"))
  await cleanup()
  assert.equal(calls.at(-1)[0], "reset")
}
{
  const { hooks } = await load({}, {}, true)
  await assert.rejects(hooks.get("execute.before")({ tool: "shell", input: { command: "blocked" } }), /command blocked/)
  const { hooks: permissive } = await load({ mode: "permissive" }, {}, true)
  await permissive.get("execute.before")({ tool: "shell", input: { command: "explicitly permitted" } })
}
{
  const { hooks } = await load({ mode: "unknown" })
  assert.throws(() => hooks.get("execute.before")({ tool: "shell" }), /policy invalid; command blocked/)
}
{
  for (const env of [{ OC_BOX_CONTAINER: "1" }, { OPENCODE_DISABLE_SANDBOX: "1" }]) {
    const { hooks, calls } = await load({}, env)
    assert.equal(hooks.size, 0)
    assert.equal(calls.length, 0)
  }
}
console.log("PASS: native host sandbox hook policy, fail-closed setup, success/error cleanup and unchanged original input; mocked runtime, not isolation validation")
