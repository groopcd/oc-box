import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import { createEffect, onCleanup } from "solid-js"

const MAX_TEXT = 256 * 1024
const MAX_PNG = 5 * 1024 * 1024
const MAX_FRAME = 17 * 1024
const CONTROLS = /[\p{Cc}\p{Cf}\p{Cs}]/u
const GRANT_PREFIX = "\x1b]778;oc-box-click:v1:"
const GRANT = /^\x1b\]778;oc-box-click:v1:([0-9a-f]{32}):([1-9][0-9]{0,4}):([1-9][0-9]{0,4})(?:\x07|\x1b\\)$/
const HELPER = fileURLToPath(new URL("./file_preview_io.py", import.meta.url))

function cleanPath(path) {
  return typeof path === "string" && path.startsWith("/") &&
    Buffer.byteLength(path) < 4096 && !CONTROLS.test(path) &&
    path.slice(1).split("/").every((part) => part && part !== "." && part !== "..")
}

function targetPath(target, directory, mounts) {
  if (typeof target !== "string" || !target || CONTROLS.test(target) ||
      Buffer.byteLength(target) > 12 * 1024) return
  const file = /^file:\/\/(?:localhost)?(\/[^?#]*)(?:#[^?]*)?$/i.exec(target)
  if (!file && (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith("//"))) return
  let path = decodeURIComponent(file ? file[1] : target.split(/[?#]/, 1)[0])
  if (!path || path.includes("\\") ||
      (!file && !target.startsWith("/") &&
        (path.startsWith("/") || /^[a-z][a-z0-9+.-]*:/i.test(path)))) return
  if (!path.startsWith("/")) {
    if (path.startsWith("./")) path = path.slice(2)
    if (!cleanPath(directory)) return
    path = `${directory}/${path}`
  }
  if (!cleanPath(path) || /^\/(?:proc|sys|dev|run)(?:\/|$)/.test(path)) return
  if (Buffer.byteLength(JSON.stringify({ path, ...mounts })) > MAX_FRAME) return
  const extension = /\.([a-z]{2,8})$/i.exec(path)?.[1].toLowerCase()
  const kind = extension === "markdown" ? "md" : extension === "htm" ? "html" : extension
  return ["png", "txt", "md", "html"].includes(kind) ? { path, kind } : undefined
}

function snapshot(path, kind, mounts, signal) {
  return new Promise<Buffer>((resolve, reject) => {
    if (signal.aborted) return reject(new Error("snapshot-cancelled"))
    const chunks: Buffer[] = []
    let size = 0
    let failed = false
    const png = kind === "png"
    const child = spawn("/usr/bin/python3", ["-I", "-S", "-B", HELPER, ...(png ? ["--png"] : [])], {
      cwd: "/", env: { PATH: "/usr/bin:/bin", LANG: "C.UTF-8" },
      stdio: ["pipe", "pipe", "ignore"],
    })
    const stop = () => {
      failed = true
      chunks.length = 0
      child.stdin.destroy()
      child.stdout.destroy()
      child.kill("SIGKILL")
    }
    signal.addEventListener("abort", stop, { once: true })
    const timer = setTimeout(stop, png ? 14000 : 4500)
    child.on("error", () => { failed = true })
    child.stdin.on("error", stop)
    child.stdout.on("error", stop)
    child.stdout.on("data", (chunk: Buffer) => {
      if (failed) return
      size += chunk.length
      if (size > (png ? MAX_PNG : MAX_TEXT)) return stop()
      chunks.push(chunk)
    })
    child.once("close", (code) => {
      clearTimeout(timer)
      signal.removeEventListener("abort", stop)
      if (code !== 0 || failed || signal.aborted || !size) return reject(new Error("snapshot-refused"))
      resolve(Buffer.concat(chunks))
    })
    child.stdin.end(JSON.stringify({ path, ...mounts }))
  })
}

function messageID(target) {
  let id
  let markdown = false
  for (let node = target, depth = 0; node && depth < 32; node = node.parent, depth++) {
    if (node.id === "session-pane") return markdown ? id : undefined
    if (typeof node.id !== "string") continue
    if (/^markdown-\d+$/.test(node.id)) markdown = true
    const message = /^(msg_[A-Za-z0-9_-]{1,124})$/.exec(node.id) ??
      /^session-part:(msg_[A-Za-z0-9_-]{1,124}):text:\d{1,10}$/.exec(node.id)
    if (message) {
      if (id) return
      id = message[1]
    }
  }
}

async function transfer(id, kind, bytes, current) {
  const send = (frame: string) => new Promise<void>((resolve, reject) =>
    process.stdout.write(`\x1b]778;oc-box-open:v1:${id}:${frame}\x07`, (error) =>
      error ? reject(error) : resolve()))
  if (!current()) return
  await send(`begin:${kind}:${bytes.length}`)
  for (let index = 0; index < bytes.length; index += 4096) {
    if (!current()) return
    await send(`data:${bytes.subarray(index, index + 4096).toString("base64")}`)
  }
  if (current()) await send("end")
}

export function install(context) {
  if (!/^2\.3\.(?:0|[1-9][0-9]*)$/.test(context.app.version)) throw new Error("native-version")
  const mounts = JSON.parse(process.env.OC_BOX_FILE_PREVIEW_ROOTS ?? "{}")
  const { roots, identities } = mounts
  if (!Array.isArray(roots) || roots.length !== 2 || roots[0] === roots[1] ||
      !roots.every(cleanPath) || !Array.isArray(identities) || identities.length !== 2 ||
      !identities.every((pair) => Array.isArray(pair) && pair.length === 2 &&
        pair.every((value) => typeof value === "string" && /^[0-9]{1,20}$/.test(value)))) {
    throw new Error("file-open-mounts")
  }
  const { renderer } = context
  const root = renderer?.root
  const original = root?.processMouseEvent
  if (typeof original !== "function" || typeof renderer.getLinkAt !== "function" ||
      typeof renderer.prependInputHandler !== "function" || typeof renderer.removeInputHandler !== "function") {
    throw new Error("file-open-hook")
  }
  let pressed
  let clicked
  let pending: AbortController | undefined
  let closed = false
  let generation = 0

  function activeSession() {
    const route = context.ui.router.current()
    return route.type === "session" ? route.sessionID : undefined
  }
  function invalidate() {
    generation++
    pressed = clicked = undefined
    pending?.abort()
  }
  const unwatch = context.ui.slot({
    append: "app",
    render: () => {
      createEffect(() => {
        const session = activeSession()
        if (session) context.data.session.get(session)?.location?.directory
        invalidate()
      })
      onCleanup(invalidate)
      return null
    },
  })
  renderer.on("blur", invalidate)

  async function open(id, item) {
    if (pending) return
    const controller = pending = new AbortController()
    const current = () => !closed && !controller.signal.aborted &&
      item.generation === generation && item.session === activeSession()
    try {
      const bytes = await snapshot(item.path, item.kind, mounts, controller.signal)
      await transfer(id, item.kind, bytes, current)
    } catch {
      if (current()) context.ui.toast.show({
        message: "Host file open refused: the workspace file is not a supported safe snapshot.",
        variant: "warning",
      })
    } finally {
      if (pending === controller) pending = undefined
    }
  }

  function handle(event) {
    clicked = undefined
    if (["drag", "drag-end", "scroll"].includes(event.type) ||
        (event.type === "move" && pressed && (pressed.x !== event.x || pressed.y !== event.y))) {
      pressed = undefined
    }
    if (!["up", "down"].includes(event.type) || event.button !== 0) return
    const before = pressed
    pressed = undefined
    if (event.defaultPrevented || event.isDragging || !event.modifiers.ctrl ||
        event.modifiers.shift || event.modifiers.alt) return
    const session = activeSession()
    const id = messageID(event.target)
    if (!session || !id || context.data.session.message.get(session, id)?.type !== "assistant") return
    const href = renderer.getLinkAt(event.x, event.y)
    const item = targetPath(href, context.data.session.get(session)?.location?.directory, mounts)
    if (!item) return
    if (event.type === "down") {
      pressed = { ...item, session, id, href, generation, node: event.target, x: event.x, y: event.y }
      return
    }
    if (before?.session === session && before.id === id && before.href === href &&
      before.node === event.target && before.generation === generation &&
      before.x === event.x && before.y === event.y && before.path === item.path) {
      clicked = before
    }
  }
  const wrapped = function (event) {
    if (!closed) {
      try { handle(event) } catch { pressed = clicked = undefined }
    }
    return original.call(this, event)
  }
  function input(sequence) {
    if (!sequence.startsWith(GRANT_PREFIX)) return false
    const match = GRANT.exec(sequence)
    const item = clicked
    clicked = undefined
    if (match && item && item.generation === generation &&
        Number(match[2]) === item.x + 1 && Number(match[3]) === item.y + 1) {
      void open(match[1], item)
    }
    return true
  }
  root.processMouseEvent = wrapped
  renderer.prependInputHandler(input)
  return () => {
    closed = true
    invalidate()
    unwatch()
    renderer.removeInputHandler(input)
    renderer.off("blur", invalidate)
    if (root.processMouseEvent === wrapped) root.processMouseEvent = original
  }
}
