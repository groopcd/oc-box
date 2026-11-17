import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
const source = await readFile(new URL("../src/oc_box/ui_state_memory.js", import.meta.url))
const { createMemory, createReporter, TTL, MAX_CLIENTS, MAX_TABS, validReport } =
  await import("data:text/javascript;base64," + source.toString("base64"))

const generation = "1".repeat(32), ownerClient = "2".repeat(32), alternate = "3".repeat(32)
let now = 0
const memory = createMemory({ clock: () => now, epoch: "synthetic" })
const query = { generation, ownerClient }
const value = (changes = {}) => ({
  generation, client: ownerClient, seq: 1, routeType: "session", selected: "ses_child",
  tabsEnabled: true, tabs: ["ses_root", "ses_other"], activeRoot: "ses_root",
  focus: "blurred", ...changes,
})
assert.equal(memory.snapshot(query).reason, "absent")
assert.equal(memory.report(value()).accepted, true)
assert.equal(memory.snapshot(query).source, "owner-tui")
assert.equal(memory.snapshot(query).selected, "ses_child")
memory.report(value({ client: alternate, selected: "ses_other", activeRoot: "ses_other" }))
assert.equal(memory.snapshot(query).client, ownerClient)
now = TTL - 1
assert.equal(memory.report(value()).accepted, false)
now = TTL
assert.equal(memory.snapshot(query).reason, "absent") // Replay did not refresh receipt.
assert.equal(memory.report(value()).accepted, false)
memory.report(value({ client: alternate, seq: 2 }))
assert.equal(memory.snapshot(query).source, "single-tui")
memory.report(value({ client: "4".repeat(32) }))
assert.equal(memory.snapshot(query).reason, "ambiguous")
memory.report(value({ seq: 2 }))
assert.equal(memory.snapshot(query).source, "owner-tui")
memory.release({ generation, client: ownerClient, seq: 3 })
assert.equal(memory.report(value({ seq: 2 })).accepted, false)
assert.equal(memory.snapshot(query).reason, "ambiguous")
memory.clear()
assert.equal(memory.snapshot(query).reason, "absent")
const released = createMemory({ clock: () => now, epoch: "release-history" })
released.report(value({ seq: 10 }))
released.release({ generation, client: ownerClient, seq: 11 })
now += TTL + 1
assert.equal(released.report(value({ seq: 10 })).accepted, false)
assert.equal(released.report(value({ seq: 11 })).accepted, false)
assert.equal(released.snapshot(query).reason, "absent")
assert.equal(released.report(value({ seq: 12 })).accepted, true)
assert.equal(released.snapshot(query).source, "owner-tui")
assert.throws(() => released.report(value({ generation: "f".repeat(32), seq: 13 })), /generation/)
memory.report(value({ generation: "5".repeat(32) }))
assert.equal(memory.snapshot(query).reason, "generation")
assert.throws(() => memory.report(value()), /generation/)
memory.clear()
for (let index = 0; index < MAX_CLIENTS; index++)
  memory.report(value({ client: index.toString(16).padStart(32, "0") }))
memory.report(value({ client: "f".repeat(32) }))
assert.equal(memory.snapshot(query).reason, "overflow")
now += TTL
assert.equal(memory.snapshot(query).reason, "absent")
for (const changes of [
  { client: "private-path" }, { generation: "bad" }, { seq: 0 }, { seq: Infinity },
  { seq: true }, { tabs: ["ses_root", "ses_root"] }, { activeRoot: "ses_missing" },
  { selected: null }, { routeType: "home" }, { focus: "last-active" },
  { tabs: Array(MAX_TABS + 1).fill("ses_root") }, { extra: "private-sentinel" },
]) assert.equal(validReport(value(changes)), false)
assert.equal(validReport(value({ routeType: "home", selected: null, activeRoot: null })), true)
assert.equal(validReport(value({ tabsEnabled: false, tabs: [], activeRoot: null })), true)

let interval, timer = 0, sequence = 0
const timers = {
  setInterval(fn) { interval = fn; return 1 },
  clearInterval() { interval = undefined },
  setTimeout() { return ++timer },
  clearTimeout() {},
}
const calls = [], gates = []
const reporter = createReporter((method, input) => {
  calls.push({ method, input })
  if (method === "release") return Promise.resolve({})
  return new Promise((resolve) => gates.push(resolve))
}, { client: ownerClient, generation, nextSequence: () => ++sequence, timers })
const { client, generation: ignoredGeneration, seq, ...first } = value()
reporter.publish(first)
reporter.publish({ ...first, selected: "ses_other", activeRoot: "ses_other" })
reporter.publish({ ...first, selected: "ses_root" })
assert.equal(calls.length, 1)
gates.shift()({})
for (let i = 0; i < 5; i++) await Promise.resolve()
assert.equal(calls.length, 2)
assert.equal(calls[1].input.selected, "ses_root")
gates.shift()({})
for (let i = 0; i < 5; i++) await Promise.resolve()
interval()
assert.equal(calls.length, 3)
gates.shift()({})
for (let i = 0; i < 5; i++) await Promise.resolve()
await reporter.stop()
assert.equal(interval, undefined)
assert.equal(calls.at(-1).method, "release")
assert.deepEqual(calls.map((item) => item.input.seq), [1, 2, 3, 4])
await reporter.stop()
assert.equal(calls.length, 4)
let failures = 0
const paused = createReporter(async () => { failures++; throw new Error("synthetic-refusal") }, {
  client: ownerClient, generation, nextSequence: () => ++sequence, timers,
})
paused.publish(first)
for (let i = 0; i < 10; i++) await Promise.resolve()
for (let attempt = 0; attempt < 4; attempt++) {
  interval()
  for (let i = 0; i < 10; i++) await Promise.resolve()
}
assert.equal(failures, 3)
paused.refresh()
for (let i = 0; i < 10; i++) await Promise.resolve()
assert.equal(failures, 4)
await paused.stop()
assert.equal(interval, undefined)
console.log("UI_STATE_CONTRACT_OK")
