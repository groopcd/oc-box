import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import { createEffect, createSignal, onCleanup, Show } from "solid-js"

const PANEL = "oc-box.file-preview"
const MAX_BYTES = 256 * 1024
const MAX_FRAME = 17 * 1024
const TEXT_CONTROLS = /[\p{Cc}\p{Cf}\p{Cs}]/u
const helper = fileURLToPath(new URL("./file_preview_io.py", import.meta.url))

function cleanPath(path) {
  return typeof path === "string" && path.startsWith("/") &&
    Buffer.byteLength(path) < 4096 && !TEXT_CONTROLS.test(path) &&
    path.slice(1).split("/").every((part) => part && part !== "." && part !== "..")
}

function targetPath(target, directory, mounts) {
  if (typeof target !== "string" || !target || TEXT_CONTROLS.test(target) ||
      Buffer.byteLength(target) > 12 * 1024) return
  // Parse the raw path rather than using URL pathname: normalized URLs can
  // hide dot segments before the confined reader gets a chance to refuse them.
  const file = /^file:\/\/(?:localhost)?(\/[^?#]*)$/i.exec(target)
  if (!file && (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith("//"))) return
  let path = decodeURIComponent(file ? file[1] : target.split(/[?#]/, 1)[0])
  if (!path || path.includes("\\") ||
      (!file && !target.startsWith("/") &&
        (path.startsWith("/") || /^[a-z][a-z0-9+.-]*:/i.test(path)))) return
  if (!path.startsWith("/")) {
    // An ordinary leading ./ is not a traversal; other dot components are
    // refused. Never resolve relative links against the launcher cwd.
    if (path.startsWith("./")) path = path.slice(2)
    if (!cleanPath(directory)) return
    path = `${directory}/${path}`
  }
  if (!cleanPath(path) || /^\/(?:proc|sys|dev|run)(?:\/|$)/.test(path)) return
  // The reader independently verifies the mounts; this is only input shaping.
  if (Buffer.byteLength(JSON.stringify({ path, ...mounts })) > MAX_FRAME) return
  return path
}

function snapshot(path, mounts, signal) {
  return new Promise<string>((resolve, reject) => {
    const fail = () => reject(new Error("snapshot-refused"))
    if (signal.aborted) return fail()
    const chunks: Buffer[] = []
    let size = 0
    let failed = false
    let timer: ReturnType<typeof setTimeout>
    const child = spawn("/usr/bin/python3", ["-I", "-S", "-B", helper], {
      cwd: "/",
      env: { PATH: "/usr/bin:/bin", LANG: "C.UTF-8" },
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
    timer = setTimeout(stop, 4500)
    child.on("error", () => { failed = true })
    child.stdin.on("error", stop)
    child.stdout.on("error", stop)
    child.stdout.on("data", (chunk: Buffer) => {
      if (failed) return
      size += chunk.length
      if (size > MAX_BYTES) return stop()
      chunks.push(chunk)
    })
    child.once("close", (code) => {
      clearTimeout(timer)
      signal.removeEventListener("abort", stop)
      if (failed || code !== 0 || signal.aborted) return fail()
      try {
        const text = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks))
        if (TEXT_CONTROLS.test(text.replace(/[\t\r\n]/g, ""))) return fail()
        // Only literal text reaches the panel, never a Markdown renderer,
        // clipboard, command, prompt, session history or external viewer.
        resolve(text.replace(/\r\n?/g, "\n").replace(/\t/g, "    "))
      } catch {
        fail()
      }
    })
    child.stdin.end(JSON.stringify({ path, ...mounts }))
  })
}

function messageID(target) {
  // Native transcript ancestry is private and version-gated. Require the
  // message box under session-pane instead of activating links in dialogs,
  // toasts, plugin widgets or terminal tool output.
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

export function install(context) {
  if (!/^2\.3\.(?:0|[1-9][0-9]*)$/.test(context.app.version)) throw new Error("native-version")
  const { roots, identities } = JSON.parse(process.env.OC_BOX_FILE_PREVIEW_ROOTS ?? "{}")
  if (!Array.isArray(roots) || roots.length !== 2 || roots[0] === roots[1] ||
      !roots.every(cleanPath) || !Array.isArray(identities) || identities.length !== 2 ||
      !identities.every((pair) => Array.isArray(pair) && pair.length === 2 &&
        pair.every((value) => typeof value === "string" && /^[0-9]{1,20}$/.test(value)))) {
    throw new Error("preview-roots")
  }
  const mounts = { roots, identities }
  const { renderer } = context
  const root = renderer?.root
  const original = root?.processMouseEvent
  if (typeof original !== "function" || typeof renderer.getLinkAt !== "function" ||
      typeof context.ui.panel?.open !== "function") throw new Error("preview-hook")
  const [shown, setShown] = createSignal<{ session: string, path: string, text: string }>()
  let pressed
  let pending: AbortController | undefined
  let closed = false
  let routeGeneration = 0
  const unregister = context.ui.slot({
    append: "session.panel",
    render: (panel) => (
      <Show when={panel.name === PANEL && shown()?.session === panel.sessionID && shown()}>
        {(file) => (
          <box flexDirection="column" height="100%">
            <box flexDirection="row">
              <text flexGrow={1} fg={context.theme.text.base}>File preview (read-only)</text>
              <text selectable={false} onMouseUp={(event) => {
                if (event.button === 0 && !event.isDragging) {
                  event.stopPropagation()
                  setShown(undefined)
                  panel.close()
                }
              }}>[Close preview]</text>
            </box>
            <text selectable={false} wrapMode="word">{file().path}</text>
            <scrollbox flexGrow={1}>
              <text selectable={false} wrapMode="word">{file().text || "(empty file)"}</text>
            </scrollbox>
          </box>
        )}
      </Show>
    ),
  })
  const activeSession = () => {
    const route = context.ui.router.current()
    return route.type === "session" ? route.sessionID : undefined
  }
  const unwatch = context.ui.slot({
    append: "app",
    render: () => {
      createEffect(() => {
        const session = activeSession()
        if (session) context.data.session.get(session)?.location?.directory
        invalidate()
      })
      createEffect(() => {
        const active = activeSession()
        const file = shown()
        if (file && (file.session !== active || context.ui.panel.current()?.name !== PANEL)) {
          setShown(undefined)
        }
      })
      onCleanup(() => pending?.abort())
      return null
    },
  })
  const warn = () => context.ui.toast.show({
    variant: "warning",
    message: "File preview unavailable: the file is not readable as a bounded, safe text snapshot.",
  })
  function invalidate() {
    routeGeneration++
    pressed = undefined
    pending?.abort()
  }
  renderer.on("blur", invalidate)
  const open = async (session, path, generation) => {
    if (pending) return
    const controller = pending = new AbortController()
    try {
      const text = await snapshot(path, mounts, controller.signal)
      if (closed || controller.signal.aborted || generation !== routeGeneration ||
          session !== activeSession()) return
      if (context.ui.panel.open(PANEL)) setShown({ session, path, text })
    } catch {
      if (!closed && !controller.signal.aborted && generation === routeGeneration &&
          session === activeSession()) warn()
    } finally {
      if (pending === controller) pending = undefined
    }
  }
  function handle(event) {
    if (["drag", "drag-end", "scroll"].includes(event.type) ||
        (event.type === "move" && pressed &&
          (pressed.x !== event.x || pressed.y !== event.y))) pressed = undefined
    if (!["up", "down"].includes(event.type) || event.button !== 0) return
    const previous = pressed
    pressed = undefined
    if (event.defaultPrevented || event.isDragging || !event.modifiers.ctrl ||
        event.modifiers.shift || event.modifiers.alt) return
    const session = activeSession()
    const id = messageID(event.target)
    if (!session || !id) return
    const target = renderer.getLinkAt(event.x, event.y)
    if (!target) return
    const message = context.data.session.message.get(session, id)
    if (!message || message.type !== "assistant") return
    const info = context.data.session.get(session)
    const directory = info?.location?.directory
    let path
    try { path = targetPath(target, directory, mounts) } catch { return }
    if (!path) return
    if (event.type === "down") {
      pressed = { session, id, path, target, node: event.target, generation: routeGeneration,
        x: event.x, y: event.y }
      return
    }
    if (previous?.session !== session || previous.id !== id || previous.path !== path ||
        previous.target !== target || previous.node !== event.target || previous.generation !== routeGeneration ||
        previous.x !== event.x || previous.y !== event.y) return
    // Let native keyboard/paste and normal mouse actions run unchanged.
    void open(session, path, routeGeneration)
  }
  const wrapped = function (event) {
    if (!closed) {
      try { handle(event) } catch { pressed = undefined }
    }
    return original.call(this, event)
  }
  root.processMouseEvent = wrapped
  return () => {
    closed = true
    pending?.abort()
    pressed = undefined
    setShown(undefined)
    unwatch()
    unregister()
    renderer.off("blur", invalidate)
    if (root.processMouseEvent === wrapped) root.processMouseEvent = original
  }
}
