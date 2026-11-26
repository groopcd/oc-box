import { localTimestamp, validTime } from "./activity_timeline_events.mjs"

const MAX_MESSAGES = 200
const MAX_PARTS = 128
const MAX_NODES = 4096
const MAX_DEPTH = 32
const MAX_ROWS = 512
const MESSAGE_ID = /^msg[A-Za-z0-9_-]{1,125}$/
const TOOL_ID = /^[A-Za-z0-9_.-]{1,128}$/
const SESSION_ID = /^ses[A-Za-z0-9_-]{1,125}$/
const COMPLETION_SOURCES = new Set(["shell", "subagent"])

function isID(value, pattern) {
  return typeof value === "string" && pattern.test(value)
}

function toolTime(time) {
  if (validTime(time?.ran)) return { time: time.ran, unknown: false }
  if (validTime(time?.created)) return { time: time.created, unknown: true }
}

function format({ time, unknown, notice }) {
  const timestamp = localTimestamp(time)
  if (notice) return `${timestamp} · notice recorded`
  return timestamp + (unknown ? " · call created; invocation unknown" : "")
}

export function transcriptTimes(messages) {
  const times = new Map()
  const unique = new Map()
  for (const message of messages.slice(-MAX_MESSAGES)) {
    if (!isID(message?.id, MESSAGE_ID)) continue
    unique.set(message.id, unique.has(message.id) ? undefined : message)
  }
  for (const [id, message] of unique) {
    if (message?.type === "user" && validTime(message.time?.created)) {
      times.set(id, { time: message.time.created, unknown: false })
    }
    if (message?.type === "synthetic" && message.metadata?.state === "completed" &&
        COMPLETION_SOURCES.has(message.metadata.source) && validTime(message.time?.created)) {
      times.set(id, { time: message.time.created, unknown: false, notice: true })
    }
    if (message?.type !== "assistant" || !Array.isArray(message.content) || message.content.length > MAX_PARTS) continue
    const parts = new Map()
    for (const part of message.content) {
      if (part?.type !== "tool" || !isID(part.id, TOOL_ID)) continue
      const key = `session-part:${id}:${part.id}`
      parts.set(key, parts.has(key) ? undefined : toolTime(part.time))
    }
    for (const [key, value] of parts) {
      if (value) times.set(key, value)
    }
  }
  return times
}

function visible(node) {
  return node?.visible === true && !node.isDestroyed
}

function findOne(root, matches) {
  const pending = [[root, 0]]
  const seen = new Set()
  let match
  while (pending.length) {
    const [node, depth] = pending.pop()
    if (!visible(node) || seen.has(node)) continue
    seen.add(node)
    if (seen.size > MAX_NODES || depth > MAX_DEPTH) return
    if (matches(node)) {
      if (match) return
      match = node
      // Another matching pane or transcript can be nested inside this one.
      // A render tree is not a public layout contract; reject that ambiguity.
    }
    const children = node.getChildren?.() ?? []
    if (!Array.isArray(children) || children.length + pending.length > MAX_NODES) return
    for (const child of children) pending.push([child, depth + 1])
  }
  return match
}

function completionRows(times, anchors, pending) {
  const rows = new Map()
  if (!anchors || !Array.isArray(pending) || pending.length > MAX_ROWS) return rows
  const pendingIDs = new Set()
  for (const item of pending) {
    if (typeof item?.id !== "string") return rows
    pendingIDs.add(item.id)
  }
  for (const [id, stamp] of times) {
    // The projected creation timestamp can change when a queued notice is
    // delivered. Wait for the settled transcript record.
    if (!stamp.notice || pendingIDs.has(id)) continue
    const anchor = anchors.get({ type: "part", ref: { messageID: id, partID: "message" } })
    const ref = anchor?.target?.ref
    const row = anchor?.node
    if (anchor?.target?.type !== "part" || ref?.messageID !== id || ref.partID !== "message" ||
        !visible(row) || (row.id != null && row.id !== id)) continue
    rows.set(row, rows.has(row) ? undefined : id)
  }
  return rows
}

function placements(root, times, column, decorations, completion) {
  const result = new Map()
  if (!times.size) return result
  let notices
  try { notices = completionRows(times, completion?.anchors, completion?.pending) } catch { notices = new Map() }
  const rowID = (row) => {
    if (notices.has(row)) return notices.get(row)
    const stamp = times.get(row.id)
    if (stamp && !stamp.notice) return row.id
  }
  // This is a private layout, not a native plugin slot. Require one visible
  // session pane and a unique scrollbox with matching *direct* transcript rows.
  const pane = findOne(root, (node) => node.id === "session-pane")
  const scroll = pane && findOne(pane, (node) => {
    if (typeof node.scrollBy !== "function") return false
    const rows = node.getChildren?.()
    return Array.isArray(rows) && rows.length <= MAX_ROWS &&
      rows.some((row) => row?.parent === node && rowID(row))
  })
  if (!scroll) return result
  const rows = scroll.getChildren()
  if (!Array.isArray(rows) || rows.length > MAX_ROWS) return result
  const unique = new Map()
  for (const row of rows) {
    if (row?.parent !== scroll) continue
    const id = rowID(row)
    if (id) unique.set(id, unique.has(id) ? undefined : row)
  }
  for (const [id, row] of unique) {
    if (!visible(row) || typeof row.add !== "function" || row.getFlexDirection?.() !== column) continue
    const children = row.getChildren?.()
    const stamp = decorations.get(row)?.node
    if (!Array.isArray(children) || !children.some((child) => child !== stamp && visible(child))) continue
    result.set(row, { id, ...times.get(id) })
  }
  return result
}

export function createInlineTimestamps({ column, makeLabel }) {
  const decorations = new Map()
  let session
  let sequence = 0

  function remove(row, { node }) {
    try {
      if (!node.isDestroyed) node.destroy()
      if (node.parent != null || (!row.isDestroyed && row.getChildren().includes(node))) {
        throw new Error("Native scene did not detach the inline timestamp")
      }
    } catch (error) {
      // Do not keep displaying an old timestamp if this scene cannot detach it.
      try { node.visible = false } catch { /* No compatible visibility setter. */ }
      throw error
    } finally {
      decorations.delete(row)
    }
  }

  function clear() {
    for (const [row, decoration] of decorations) {
      try { remove(row, decoration) } catch { /* Leave native rows alone. */ }
    }
  }

  return {
    update({ root, sessionID, messages, color, completion }) {
      if (session !== sessionID) {
        clear()
        session = sessionID
      }
      const wanted = isID(session, SESSION_ID) && Array.isArray(messages)
        ? placements(root, transcriptTimes(messages), column, decorations, completion)
        : new Map()
      for (const [row, entry] of decorations) {
        if (wanted.get(row)?.id !== entry.id || entry.node.isDestroyed ||
            entry.node.parent !== row || row.getChildren().at(-1) !== entry.node) {
          remove(row, entry)
        }
      }
      for (const [row, stamp] of wanted) {
        const entry = decorations.get(row)
        if (entry) {
          if (entry.time !== stamp.time || entry.unknown !== stamp.unknown) {
            entry.node.content = format(stamp)
            entry.time = stamp.time
            entry.unknown = stamp.unknown
          }
          if (entry.color !== color) entry.node.fg = entry.color = color
          continue
        }
        const node = makeLabel(`oc-box-inline-timestamp-${++sequence}`, format(stamp), color)
        decorations.set(row, { ...stamp, node, color })
        row.add(node)
        if (node.parent !== row || !row.getChildren().includes(node)) {
          throw new Error("Native scene did not attach the inline timestamp")
        }
      }
    },
    clear,
  }
}
