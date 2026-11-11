// Public-hook contract tests. The runtime is mocked: NOT host isolation proof.
import assert from "node:assert/strict"
import fs from "node:fs/promises"
import vm from "node:vm"

const source = await fs.readFile(process.argv[2], "utf8")
async function load(user = {}, env = {}, broken = false, control = {}) {
  const hooks = new Map(), calls = []
  let retainedConfig = control.existingConfig
  const subscribers = new Set()
  async function* subscribe({ signal }) {
    const pending = []
    let waiting
    const send = event => { if (signal.aborted) return; if (waiting) { const resolve = waiting; waiting = undefined; resolve(event) } else pending.push(event) }
    const abort = () => waiting?.(undefined)
    signal.addEventListener("abort", abort, { once: true })
    subscribers.add(send)
    try {
      while (!signal.aborted) {
        const event = pending.shift() || await new Promise(resolve => { waiting = resolve })
        if (event?.streamError) throw Error("private subscription error")
        if (event) yield event
      }
    } finally { subscribers.delete(send); signal.removeEventListener("abort", abort) }
  }
  const runtime = {
    async initialize(policy) { retainedConfig = policy; calls.push(["policy", policy]); if (broken) throw Error("private failure") },
    getConfig() { return retainedConfig },
    async wrapWithSandbox(command) { calls.push(["wrap", command]); return control.wrapTask ? await control.wrapTask(command) : "WRAPPED " + command },
    cleanupAfterCommand() { calls.push(["cleanup"]); control.cleanupTask?.() },
    async reset() { calls.push(["reset"]); if (control.resetTask) await control.resetTask() },
  }
  const context = vm.createContext({ process: { env }, console: { error(message) { calls.push(["warning", message]) } }, AbortController })
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
  const globals = vm.runInContext("globalThis", context)
  const ctx = { location: { directory: "/project", project: { worktree: "/project" } }, tool: { async hook(name, hook) { if (control.failHook) { control.failHook = false; throw Error("registration failure") } hooks.set(name, hook) } }, event: { subscribe } }
  const cleanup = await module.namespace.default.setup(ctx)
  return { hooks, calls, cleanup, runtime, globals, get originals() { return globals[Symbol.for("opencode.host-sandbox.original-commands")] }, get lease() { return globals[Symbol.for("opencode.host-sandbox.runtime-lease")] }, again: directory => module.namespace.default.setup({ ...ctx, location: { directory: directory || "/project", project: { worktree: directory || "/project" } } }), emit(event) { for (const send of subscribers) send(event) } }
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
  const { hooks, calls, cleanup, emit } = await load()
  const event = { tool: "shell", id: "background-fixture", input: { command: "background command", background: true } }
  await hooks.get("execute.before")(event)
  await hooks.get("execute.after")({ ...event, status: "completed", result: { metadata: { status: "running", shellID: "sh_fixture" } } })
  assert.equal(calls.filter(item => item[0] === "cleanup").length, 0)
  emit({ type: "shell.exited", data: { id: "sh_fixture" } })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls.filter(item => item[0] === "cleanup").length, 1)
  await cleanup()
}
{
  const { calls, cleanup, again } = await load()
  const second = await again()
  assert.equal(calls.filter(item => item[0] === "policy").length, 1)
  await cleanup()
  assert.equal(calls.filter(item => item[0] === "reset").length, 0)
  await second()
  assert.equal(calls.filter(item => item[0] === "reset").length, 1)
}
{
  const { hooks, cleanup, again, calls } = await load()
  const different = await again("/different-project")
  await assert.rejects(hooks.get("execute.before")({ tool: "shell", input: { command: "different policy" } }), /command blocked/)
  assert.equal(calls.filter(item => item[0] === "policy").length, 1)
  await different()
  assert.equal(calls.filter(item => item[0] === "reset").length, 0)
  await cleanup()
}
{
  const { hooks, calls, cleanup, again } = await load()
  await cleanup()
  for (let index = 0; index < 3; index++) {
    const next = await again()
    const event = { tool: "shell", id: "after-reload-" + index, input: { command: "harmless project command" } }
    await hooks.get("execute.before")(event)
    await hooks.get("execute.after")({ ...event, status: "error" })
    await next()
    await next() // cleanup is idempotent; never decrement another generation.
  }
  assert.equal(calls.filter(item => item[0] === "policy").length, 4)
}
{
  let unblock
  const gate = new Promise(resolve => { unblock = resolve })
  const { hooks, calls, cleanup, again } = await load({}, {}, false, { resetTask: () => gate })
  const closing = cleanup()
  await new Promise(resolve => setImmediate(resolve))
  const reopening = again()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(calls.filter(item => item[0] === "policy").length, 1, "must not initialize while the old reset is pending")
  unblock()
  await closing
  const next = await reopening
  await hooks.get("execute.before")({ tool: "shell", id: "reset-race", input: { command: "plain" } })
  assert.equal(calls.filter(item => item[0] === "policy").length, 2)
  await next()
}
{
  const control = {}
  const { hooks, calls, cleanup, again } = await load({}, {}, false, control)
  await cleanup()
  control.failHook = true
  await assert.rejects(again(), /registration failure/)
  const next = await again()
  await hooks.get("execute.before")({ tool: "shell", id: "failed-setup-recovery", input: { command: "plain" } })
  await next()
  assert.equal(calls.filter(item => item[0] === "reset").length, 3)
}
{
  const { hooks, calls, cleanup } = await load({}, {}, false, { existingConfig: { private: "foreign policy" } })
  await assert.rejects(hooks.get("execute.before")({ tool: "shell", input: { command: "plain" } }), /unowned-runtime/)
  await cleanup()
  assert.equal(calls.filter(item => item[0] === "policy" || item[0] === "reset").length, 0)
}
{
  const { hooks } = await load({ mode: "unknown" })
  assert.throws(() => hooks.get("execute.before")({ tool: "shell" }), /policy invalid; command blocked/)
}
{
  for (const env of [{ OC_BOX_CONTAINER: "1", OPENCODE_DISABLE_SANDBOX: "1" }, { OPENCODE_DISABLE_SANDBOX: "1" }]) {
    const { hooks, calls } = await load({}, env)
    assert.equal(hooks.size, 0)
    assert.equal(calls.length, 0)
  }
}
{
  const { hooks, cleanup } = await load({}, { OC_BOX_CONTAINER: "1" })
  assert.ok(hooks.has("execute.before"))
  await cleanup()
}
// Every new case runs independently, including on the unfixed source, so a red
// result names all observed interleavings rather than stopping at the first one.
const failures = []
const tick = () => new Promise(resolve => setImmediate(resolve))
function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
async function bounded(promise) {
  let timer
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(Error("fixture deadline")), 1000) })]) }
  finally { clearTimeout(timer) }
}
async function named(name, test) {
  try { await test(); console.log("PASS: " + name) }
  catch (error) { failures.push(error); console.error("FAIL: " + name + "\n" + error.stack) }
}
const shell = id => ({ id, tool: "shell", input: { command: "plain " + id, description: "unchanged input" } })
const count = (fixture, name) => fixture.calls.filter(item => item[0] === name).length

for (const outcome of ["completed", "error", "cancel"]) {
  await named("pending-A-survives-B-" + outcome, async () => {
    const gate = deferred()
    const fixture = await load({}, {}, false, { wrapTask: command => command === "plain A" ? gate.promise : "WRAPPED " + command })
    const a = shell("A"), b = shell("B"), originalA = a.input, originalB = b.input
    const wrapping = fixture.hooks.get("execute.before")(a)
    await tick()
    try {
      await fixture.hooks.get("execute.before")(b)
      if (outcome === "cancel") fixture.originals.get("B").cancel()
      else await fixture.hooks.get("execute.after")({ ...b, status: outcome, result: { metadata: {} } })
      assert.equal(count(fixture, "cleanup"), 0, "B must not clean an unsettled A wrap")
    } finally { gate.resolve("WRAPPED plain A"); await bounded(wrapping) }
    assert.equal(a.input.command, "WRAPPED plain A")
    assert.equal(originalA.command, "plain A")
    assert.equal(originalB.command, "plain B")
    await fixture.hooks.get("execute.after")({ ...a, status: "error" })
    assert.equal(a.input.command, "plain A")
    assert.equal(count(fixture, "cleanup"), 1)
    assert.equal(fixture.originals.size, 0)
    assert.equal(fixture.lease.pending, 0, "all wrap paths must release pending ownership")
    await fixture.cleanup()
  })
}

for (const mode of ["enforce", "permissive"]) {
  for (const outcome of ["resolve", "reject"]) {
    await named("pending-wrap-" + outcome + "-after-close-" + mode, async () => {
      const gate = deferred(), fixture = await load({ mode }, {}, false, { wrapTask: () => gate.promise })
      const event = shell("closing"), original = event.input
      const wrapping = fixture.hooks.get("execute.before")(event).then(() => undefined, error => error)
      await tick()
      const closing = fixture.cleanup()
      await tick()
      let prematureReset
      try { prematureReset = count(fixture, "reset") }
      finally {
        if (outcome === "resolve") gate.resolve("WRAPPED stale")
        else gate.reject(Error("private wrap failure"))
      }
      const error = await bounded(wrapping)
      await bounded(closing)
      assert.equal(prematureReset, 0, "close must retain its lease until its pending wrap settles")
      assert.equal(event.input, original, "closed wrap must never publish input")
      assert.equal(event.input.command, "plain closing")
      assert.equal(fixture.originals.size, 0)
      assert.equal(count(fixture, "reset"), 1)
      assert.equal(fixture.lease.pending, 0)
      if (mode === "enforce") assert.match(error?.message || "", /command blocked/)
      else assert.equal(error, undefined)
      await fixture.cleanup()
      assert.equal(count(fixture, "reset"), 1)
    })
  }
}

await named("pending-old-wrap-reload-retains-new-owner", async () => {
  const gate = deferred(), fixture = await load({}, {}, false, { wrapTask: command => command === "plain old" ? gate.promise : "WRAPPED " + command })
  const oldBefore = fixture.hooks.get("execute.before"), oldAfter = fixture.hooks.get("execute.after")
  const old = shell("old"), fresh = shell("fresh")
  const wrapping = oldBefore(old).then(() => undefined, error => error)
  await tick()
  const closing = fixture.cleanup()
  const next = await bounded(fixture.again())
  await fixture.hooks.get("execute.before")(fresh)
  gate.resolve("WRAPPED obsolete")
  const error = await bounded(wrapping)
  await bounded(closing)
  assert.match(error?.message || "", /command blocked/)
  assert.equal(old.input.command, "plain old")
  assert.equal(fixture.originals.has("old"), false)
  assert.equal(fixture.originals.has("fresh"), true)
  assert.equal(count(fixture, "reset"), 0)
  assert.equal(count(fixture, "cleanup"), 0)
  await oldAfter({ ...old, status: "error" })
  assert.equal(count(fixture, "cleanup"), 0, "stale completion cannot clean the fresh command")
  await fixture.hooks.get("execute.after")({ ...fresh, status: "error" })
  assert.equal(fresh.input.command, "plain fresh")
  await next()
  assert.equal(count(fixture, "reset"), 1)
})

for (const change of ["generation", "lease", "signature", "config", "availability"]) {
  await named("pending-wrap-rechecks-" + change, async () => {
    const gate = deferred(), fixture = await load({}, {}, false, { wrapTask: () => gate.promise })
    const event = shell("ownership"), original = event.input
    const lease = fixture.lease
    const wrapping = fixture.hooks.get("execute.before")(event).then(() => undefined, error => error)
    await tick()
    if (change === "generation") lease.ready = Promise.resolve()
    if (change === "lease") fixture.globals[Symbol.for("opencode.host-sandbox.runtime-lease")] = { ...lease }
    if (change === "signature") lease.signature = "different policy"
    if (change === "config") fixture.runtime.getConfig = () => ({ foreign: true })
    if (change === "availability") lease.available = false
    gate.resolve("WRAPPED obsolete")
    const error = await bounded(wrapping)
    const observed = { error, input: event.input, originals: fixture.originals.size, cleanup: count(fixture, "cleanup") }
    await fixture.cleanup()
    assert.match(observed.error?.message || "", /command blocked/)
    assert.equal(observed.input, original)
    assert.equal(observed.originals, 0)
    assert.equal(observed.cleanup, 0, "stale wrap cannot clean a different runtime owner")
    assert.equal(count(fixture, "reset"), change === "availability" ? 1 : 0, "close cannot reset a different lease/generation/policy owner")
    assert.equal(lease.pending, 0)
  })
}

for (const eventType of ["shell.exited", "shell.deleted"]) {
  await named("pending-wrap-survives-background-" + eventType, async () => {
    const gate = deferred(), fixture = await load({}, {}, false, { wrapTask: command => command === "plain A" ? gate.promise : "WRAPPED " + command })
    const a = shell("A"), b = shell("B")
    await fixture.hooks.get("execute.before")(b)
    await fixture.hooks.get("execute.after")({ ...b, status: "completed", result: { metadata: { status: "running", shellID: "sh_pending" } } })
    const wrapping = fixture.hooks.get("execute.before")(a)
    await tick()
    fixture.emit({ type: eventType, data: { id: "sh_pending" } })
    await tick()
    const premature = count(fixture, "cleanup")
    gate.resolve("WRAPPED plain A"); await bounded(wrapping)
    await fixture.hooks.get("execute.after")({ ...a, status: "error" })
    await fixture.cleanup()
    assert.equal(premature, 0, "background exit must not clean an unsettled foreground wrap")
    assert.equal(count(fixture, "cleanup"), 1)
  })
}

await named("failed-wrap-cleans-once-and-preserves-primary-on-cleanup-error", async () => {
  const control = { wrapTask: () => { throw Error("private wrap error") }, cleanupTask: () => { throw Error("private cleanup error") } }
  const fixture = await load({}, {}, false, control), event = shell("failure")
  const error = await fixture.hooks.get("execute.before")(event).then(() => undefined, error => error)
  assert.match(error?.message || "", /could not wrap command in enforce mode; command blocked/)
  assert.equal(event.input.command, "plain failure")
  assert.equal(fixture.originals.size, 0)
  assert.equal(count(fixture, "cleanup"), 1, "failed wraps release temporary runtime resources")
  assert.ok(fixture.calls.some(item => item[0] === "warning" && item[1].includes("runtime-cleanup")))
  assert.ok(fixture.calls.every(item => !JSON.stringify(item).includes("private cleanup error")))
  control.wrapTask = undefined; control.cleanupTask = undefined
  await fixture.hooks.get("execute.before")(event)
  await fixture.hooks.get("execute.after")({ ...event, status: "error" })
  assert.equal(count(fixture, "cleanup"), 2)
  assert.equal(fixture.lease.pending, 0)
  await fixture.cleanup()
})

for (const outcome of ["completed", "error", "cancel"]) {
  await named("shared-lease-pending-A-survives-other-instance-B-" + outcome, async () => {
    const gate = deferred(), fixture = await load({}, {}, false, { wrapTask: command => command === "plain A" ? gate.promise : "WRAPPED " + command })
    const firstBefore = fixture.hooks.get("execute.before"), firstAfter = fixture.hooks.get("execute.after")
    const next = await fixture.again(), a = shell("A"), b = shell("B")
    const wrapping = firstBefore(a)
    await fixture.hooks.get("execute.before")(b)
    if (outcome === "cancel") fixture.originals.get("B").cancel()
    else await fixture.hooks.get("execute.after")({ ...b, status: outcome, result: { metadata: {} } })
    const premature = count(fixture, "cleanup")
    await next()
    gate.resolve("WRAPPED plain A"); await bounded(wrapping)
    assert.equal(premature, 0, "pending ownership must be shared, not just instance-local")
    assert.equal(count(fixture, "reset"), 0, "closing the idle instance cannot reset A's lease")
    await firstAfter({ ...a, status: "error" })
    assert.equal(count(fixture, "cleanup"), 1)
    assert.equal(fixture.lease.pending, 0)
    await fixture.cleanup()
    assert.equal(count(fixture, "reset"), 1)
  })
}

for (const mode of ["enforce", "permissive"]) {
  await named("two-pending-failures-release-exactly-once-" + mode, async () => {
    const gates = { "plain A": deferred(), "plain B": deferred() }
    const fixture = await load({ mode }, {}, false, { wrapTask: command => gates[command].promise })
    const a = shell("A"), b = shell("B")
    const wrappingA = fixture.hooks.get("execute.before")(a).then(() => undefined, error => error)
    const wrappingB = fixture.hooks.get("execute.before")(b).then(() => undefined, error => error)
    await tick()
    gates["plain A"].reject(Error("private A failure")); const errorA = await bounded(wrappingA)
    const premature = count(fixture, "cleanup"), remaining = fixture.lease.pending
    gates["plain B"].reject(Error("private B failure")); const errorB = await bounded(wrappingB)
    assert.equal(premature, 0)
    assert.equal(remaining, 1)
    assert.equal(fixture.lease.pending, 0)
    assert.equal(count(fixture, "cleanup"), 1)
    assert.equal(fixture.originals.size, 0)
    assert.equal(a.input.command, "plain A"); assert.equal(b.input.command, "plain B")
    for (const error of [errorA, errorB]) {
      if (mode === "enforce") assert.match(error?.message || "", /command blocked/)
      else assert.equal(error, undefined)
    }
    if (mode === "permissive") assert.equal(fixture.calls.filter(item => item[0] === "warning" && item[1].includes("wrapping failed")).length, 1)
    await fixture.cleanup()
    const next = await fixture.again()
    await next()
    assert.equal(count(fixture, "policy"), 2, "failed wraps cannot strand the idle lease")
  })
}

for (const mode of ["enforce", "permissive"]) {
  await named("pending-wrap-rechecks-event-stream-failure-" + mode, async () => {
    const gate = deferred(), fixture = await load({ mode }, {}, false, { wrapTask: () => gate.promise })
    const event = shell("stream"), original = event.input
    const wrapping = fixture.hooks.get("execute.before")(event).then(() => undefined, error => error)
    fixture.emit({ streamError: true }); await tick()
    gate.resolve("WRAPPED obsolete"); const error = await bounded(wrapping)
    assert.equal(event.input, original)
    assert.equal(fixture.originals.size, 0)
    assert.equal(fixture.lease.pending, 0)
    assert.equal(count(fixture, "cleanup"), 1)
    if (mode === "enforce") {
      assert.match(error?.message || "", /command blocked/)
      await assert.rejects(fixture.hooks.get("execute.before")(shell("later")), /event-stream/)
    } else {
      assert.equal(error, undefined)
      const later = shell("later"); await fixture.hooks.get("execute.before")(later)
      assert.equal(later.input.command, "plain later")
    }
    await fixture.cleanup()
  })
}

await named("failed-wrap-keeps-other-instance-background-until-exit", async () => {
  const gate = deferred(), fixture = await load({}, {}, false, { wrapTask: command => command === "plain A" ? gate.promise : "WRAPPED " + command })
  const firstBefore = fixture.hooks.get("execute.before"), next = await fixture.again(), a = shell("A"), b = shell("B")
  await fixture.hooks.get("execute.before")(b)
  await fixture.hooks.get("execute.after")({ ...b, status: "completed", result: { metadata: { status: "running", shellID: "sh_other" } } })
  const wrapping = firstBefore(a).then(() => undefined, error => error)
  gate.reject(Error("private failure")); assert.match((await bounded(wrapping))?.message || "", /command blocked/)
  assert.equal(count(fixture, "cleanup"), 0)
  await fixture.cleanup()
  assert.equal(count(fixture, "reset"), 0)
  assert.equal(fixture.lease.backgrounds.has("sh_other"), true)
  fixture.emit({ type: "shell.exited", data: { id: "sh_other" } }); await tick()
  assert.equal(count(fixture, "cleanup"), 1)
  assert.equal(fixture.lease.backgrounds.size, 0)
  await next()
  assert.equal(count(fixture, "reset"), 1)
})

await named("background-exit-before-after-does-not-strand-idle-lease", async () => {
  const fixture = await load(), event = shell("early-exit")
  await fixture.hooks.get("execute.before")(event)
  fixture.emit({ type: "shell.deleted", data: { id: "sh_early" } }); await tick()
  await fixture.hooks.get("execute.after")({ ...event, status: "completed", result: { metadata: { status: "running", shellID: "sh_early" } } })
  assert.equal(fixture.lease.backgrounds.size, 0)
  assert.equal(count(fixture, "cleanup"), 1)
  await fixture.cleanup()
})

await named("command-error-cleanup-failure-preserves-original-input-and-error", async () => {
  const fixture = await load({}, {}, false, { cleanupTask: () => { throw Error("private cleanup error") } })
  const event = shell("tool-error"), input = event.input, error = Error("private tool error")
  await fixture.hooks.get("execute.before")(event)
  const after = { ...event, status: "error", error }
  await fixture.hooks.get("execute.after")(after)
  assert.equal(after.error, error)
  assert.equal(after.input.command, "plain tool-error")
  assert.equal(input.command, "plain tool-error")
  assert.equal(fixture.originals.size, 0)
  assert.equal(count(fixture, "cleanup"), 1)
  assert.ok(fixture.calls.some(item => item[0] === "warning" && item[1].includes("runtime-cleanup")))
  assert.ok(fixture.calls.every(item => !JSON.stringify(item).includes("private cleanup error")))
  await fixture.cleanup()
})

await named("pending-failure-close-reset-failure-preserves-primary-and-blocks-reinit", async () => {
  const gate = deferred(), fixture = await load({}, {}, false, { wrapTask: () => gate.promise, resetTask: () => { throw Error("private reset error") } })
  const event = shell("reset-failure"), wrapping = fixture.hooks.get("execute.before")(event).then(() => undefined, error => error)
  const closing = fixture.cleanup()
  await tick(); const prematureReset = count(fixture, "reset")
  gate.reject(Error("private wrap error")); const error = await bounded(wrapping)
  await bounded(closing)
  assert.equal(prematureReset, 0)
  assert.match(error?.message || "", /could not wrap command in enforce mode; command blocked/)
  assert.equal(event.input.command, "plain reset-failure")
  assert.equal(fixture.lease.pending, 0)
  assert.equal(count(fixture, "reset"), 1)
  assert.ok(fixture.calls.some(item => item[0] === "warning" && item[1].includes("runtime-reset")))
  const next = await fixture.again()
  await assert.rejects(fixture.hooks.get("execute.before")(shell("later")), /runtime-reset/)
  assert.equal(count(fixture, "policy"), 1)
  assert.ok(fixture.calls.every(item => !JSON.stringify(item).includes("private reset error")))
  await next()
})

if (failures.length) throw new AggregateError(failures, failures.length + " named sandbox contract cases failed")
console.log("PASS: native host sandbox hook policy, fail-closed setup, success/error cleanup and unchanged original input; mocked runtime, not isolation validation")
