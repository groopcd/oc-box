import assert from "node:assert/strict"
import { test } from "node:test"
import { activityEvents, localTimestamp, MAX_EVENTS } from "../src/oc_box/activity_timeline_events.mjs"

test("reports creation and recorded invocation times, never finish times or message contents", () => {
  const unexpected = () => { throw new Error("read transcript content instead of metadata") }
  const tool = Object.freeze({
    type: "tool", name: "shell", time: { created: 3, ran: 5, completed: 9 },
    get state() { return unexpected() },
  })
  const user = Object.freeze({
    type: "user", time: { created: 1 }, get text() { return unexpected() },
  })
  const assistant = Object.freeze({
    type: "assistant", time: { created: 2, completed: 10 },
    content: Object.freeze([
      { type: "text", get text() { return unexpected() } },
      tool,
      { type: "tool", name: "read", time: { created: 4, completed: 8 } },
    ]),
  })
  assert.deepEqual(activityEvents(Object.freeze([user, assistant])), [
    { time: 5, label: "shell invoked" },
    { time: 4, label: "read call created (invocation time unknown)" },
    { time: 2, label: "Assistant response created" },
    { time: 1, label: "Prompt created" },
  ])
})

test("skips missing clocks and uses a fixed label for unusable tool names", () => {
  assert.deepEqual(activityEvents([
    null,
    { type: "synthetic", time: { created: 99 } },
    { type: "user", time: { created: Infinity } },
    { type: "assistant", time: { created: NaN }, content: [
      { type: "tool", name: "read", time: { completed: 12 } },
      { type: "tool", name: "bad\nname", time: { created: 3, ran: -1 } },
    ] },
  ]), [{ time: 3, label: "Tool call created (invocation time unknown)" }])
})

test("bounds the visible window and sorts across concurrent actions", () => {
  const messages = Array.from({ length: 240 }, (_, created) => ({ type: "user", time: { created } }))
  const visible = activityEvents(messages)
  assert.equal(visible.length, MAX_EVENTS)
  assert.equal(visible[0].time, 239)
  assert.equal(visible.at(-1).time, 40)
})

test("formats a full local date, time to seconds, and the offset at the event", () => {
  const previous = process.env.TZ
  process.env.TZ = "America/New_York"
  try {
    assert.equal(localTimestamp(Date.parse("2026-07-12T03:01:02Z")), "2026-07-11 23:01:02 -04:00")
    assert.equal(localTimestamp(Date.parse("2026-01-12T03:01:02Z")), "2026-01-11 22:01:02 -05:00")
    process.env.TZ = "Asia/Kolkata"
    assert.equal(localTimestamp(Date.parse("2026-11-26T09:02:03Z")), "2026-11-26 14:32:03 +05:30")
  } finally {
    if (previous === undefined) delete process.env.TZ
    else process.env.TZ = previous
  }
})
