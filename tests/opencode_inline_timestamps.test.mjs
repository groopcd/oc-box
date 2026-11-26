import assert from "node:assert/strict"
import { test } from "node:test"
import { localTimestamp } from "../src/oc_box/activity_timeline_events.mjs"
import { createInlineTimestamps, transcriptTimes } from "../src/oc_box/inline_timestamps.mjs"

const prompt = { id: "msg_prompt", type: "user", time: { created: 1 } }
const tool = { id: "call_one", type: "tool", time: { created: 2, ran: 3, completed: 8 } }
const reply = { id: "msg_reply", type: "assistant", content: [tool] }
const toolRow = "session-part:msg_reply:call_one"

class Node {
  constructor(id, children = []) {
    this.id = id
    this.visible = true
    this.children = []
    for (const child of children) this.add(child)
  }
  add(child) { this.children.push(child); child.parent = this }
  getChildren() { return this.children.slice() }
  getFlexDirection() { return 0 }
  destroy() {
    this.isDestroyed = true
    if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this)
    this.parent = undefined
  }
}

function frame(rows, unrelated = []) {
  const scroll = new Node("native-scroll", rows)
  scroll.scrollBy = () => {}
  return new Node("root", [new Node("session-pane", [scroll]), ...unrelated])
}

function decorator() {
  return createInlineTimestamps({
    column: 0,
    makeLabel(id, content, fg) { return Object.assign(new Node(id), { content, fg }) },
  })
}

function row(id) {
  return new Node(id, [new Node("native-body")])
}

test("exact metadata never matches an enclosing assistant, completion, or ambiguous IDs", () => {
  const unexpected = () => { throw new Error("read message content or tool input") }
  const times = transcriptTimes([
    { ...prompt, get text() { return unexpected() } },
    { ...reply, content: [
      { ...tool, get state() { return unexpected() } },
      { ...tool, id: "call_unknown", time: { created: 4, completed: 9 } },
      { ...tool, id: "call_no_time", time: { completed: 10 } },
      { ...tool, id: "text:0" },
      { ...tool, id: "duplicate" },
      { ...tool, id: "duplicate" },
    ] },
  ])
  assert.deepEqual([...times], [
    ["msg_prompt", { time: 1, unknown: false }],
    [toolRow, { time: 3, unknown: false }],
    ["session-part:msg_reply:call_unknown", { time: 4, unknown: true }],
  ])
  assert.equal(transcriptTimes([prompt, prompt, reply, reply]).size, 0)
})

test("only appends to exact visible column rows, and owns its annotations across updates", () => {
  const first = row("msg_prompt")
  const visibleTool = row(toolRow)
  const bare = row("msg_reply")
  const wrongLayout = row("msg_other")
  wrongLayout.getFlexDirection = () => 2
  const side = row("msg_prompt")
  const other = new Node("sidebar", [side])
  const root = frame([first, visibleTool, bare, wrongLayout], [other])
  const adapter = decorator()
  const input = {
    root, sessionID: "ses_example", messages: [prompt, reply, { ...prompt, id: "msg_other" }], color: "first",
  }
  adapter.update(input)
  const promptLabel = first.children[1]
  const firstTool = visibleTool.children[1]
  assert.equal(promptLabel.content, localTimestamp(1))
  assert.equal(firstTool.content, localTimestamp(3))
  adapter.update(input)
  assert.equal(first.children[1], promptLabel)
  assert.equal(visibleTool.children[1], firstTool)
  assert.deepEqual([bare.children.length, wrongLayout.children.length, side.children.length], [1, 1, 1])

  const later = { ...reply, content: [{ ...tool, time: { ran: 5 } }] }
  adapter.update({ ...input, messages: [prompt, later], color: "second" })
  assert.equal(firstTool.content, localTimestamp(5))
  assert.equal(firstTool.fg, "second")
  const added = new Node("late-native-body")
  first.add(added)
  adapter.update(input)
  assert.ok(promptLabel.isDestroyed)
  assert.equal(first.children[1], added)
  assert.equal(first.children[2].content, localTimestamp(1))
  adapter.update({ ...input, sessionID: "ses_other", messages: [] })
  assert.equal(first.children.length, 2)
  assert.equal(visibleTool.children.length, 1)
  assert.equal(added.isDestroyed, undefined)
})

test("an ambiguous layout or duplicate rendered ID never gets a label", () => {
  const first = row("msg_prompt")
  const duplicate = row("msg_prompt")
  const root = frame([first])
  const adapter = decorator()
  const input = { root, sessionID: "ses_example", messages: [prompt], color: "muted" }
  adapter.update(input)
  assert.equal(first.children.length, 2)
  first.parent.add(duplicate)
  adapter.update(input)
  assert.deepEqual([first.children.length, duplicate.children.length], [1, 1])
  duplicate.destroy()
  adapter.update(input)
  root.add(frame([row("msg_prompt")]).children[0])
  adapter.update(input)
  assert.equal(first.children.length, 1)
  adapter.clear()
})

test("a failed detach hides only the owned label", () => {
  const promptRow = row("msg_prompt")
  const adapter = decorator()
  adapter.update({ root: frame([promptRow]), sessionID: "ses_example", messages: [prompt] })
  const [body, stamp] = promptRow.children
  stamp.destroy = () => { throw new Error("synthetic scene detach failure") }
  adapter.clear()
  assert.equal(stamp.visible, false)
  assert.equal(body.visible, true)
  assert.equal(promptRow.visible, true)
})

test("nested pane or transcript matches are ambiguous, not a preferred parent", () => {
  for (const nestedPane of [true, false]) {
    const first = row("msg_prompt")
    const duplicate = row("msg_prompt")
    const root = frame([first])
    const nested = frame([duplicate]).children[0]
    first.add(nestedPane ? nested : nested.children[0])
    decorator().update({ root, sessionID: "ses_example", messages: [prompt] })
    assert.equal(first.children.length, 2)
    assert.equal(duplicate.children.length, 1)
  }
})

test("a silent scene refusal cannot keep a stale annotation or request new ones every frame", () => {
  const promptRow = row("msg_prompt")
  const input = { root: frame([promptRow]), sessionID: "ses_example", messages: [prompt] }
  const adapter = decorator()
  adapter.update(input)
  const stamp = promptRow.children[1]
  stamp.destroy = () => {}
  assert.throws(() => adapter.update({ ...input, messages: [] }), /did not detach/)
  assert.equal(stamp.visible, false)
  assert.equal(promptRow.children[0].visible, true)

  const refusingRow = row("msg_prompt")
  refusingRow.add = () => {}
  const refusing = decorator()
  assert.throws(() => refusing.update({ ...input, root: frame([refusingRow]) }), /did not attach/)
  refusing.clear()
  assert.equal(refusingRow.children.length, 1)
})
