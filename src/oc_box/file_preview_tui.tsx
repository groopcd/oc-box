import { spawn } from "node:child_process"
import { randomBytes } from "node:crypto"
import { fileURLToPath } from "node:url"
import { For, createEffect, createMemo, createSignal, onCleanup, Show } from "solid-js"

const PANEL = "oc-box.file-preview"
const MAX_BYTES = 256 * 1024
const MAX_PNG_BYTES = 5 * 1024 * 1024
const MAX_RASTER = 16 * 1024
const PNG_RESPONSE = Buffer.from("OCBOXPNG1\n")
const PNG_SIGNATURE = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])
type Snapshot = { kind: "text", text: string } | { kind: "png", bytes: Buffer, rows: string[] }
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
  const png = /\.png$/i.test(path)
  return new Promise<Snapshot>((resolve, reject) => {
    const fail = () => reject(new Error("snapshot-refused"))
    if (signal.aborted) return fail()
    const chunks: Buffer[] = []
    let size = 0
    let failed = false
    let timer: ReturnType<typeof setTimeout>
    const child = spawn("/usr/bin/python3", ["-I", "-S", "-B", helper, ...(png ? ["--png"] : [])], {
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
    timer = setTimeout(stop, png ? 14000 : 4500)
    child.on("error", () => { failed = true })
    child.stdin.on("error", stop)
    child.stdout.on("error", stop)
    child.stdout.on("data", (chunk: Buffer) => {
      if (failed) return
      size += chunk.length
      if (size > (png ? MAX_PNG_BYTES + MAX_RASTER : MAX_BYTES)) return stop()
      chunks.push(chunk)
    })
    child.once("close", (code) => {
      clearTimeout(timer)
      signal.removeEventListener("abort", stop)
      if (failed || code !== 0 || signal.aborted) return fail()
      try {
        const data = Buffer.concat(chunks)
        if (png) {
          const stop = data.indexOf(10, PNG_RESPONSE.length)
          if (!data.subarray(0, PNG_RESPONSE.length).equals(PNG_RESPONSE) ||
              stop < 0 || stop >= MAX_RASTER) return fail()
          const rows = JSON.parse(data.subarray(PNG_RESPONSE.length, stop).toString("ascii"))
          const bytes = data.subarray(stop + 1)
          if (!Array.isArray(rows) || !rows.length || rows.length > 32 ||
              !rows.every((row) => typeof row === "string" &&
                row.length === rows[0].length && /^(?:[0-9a-f]{6}){1,60}$/.test(row)) ||
              bytes.length > MAX_PNG_BYTES || !bytes.subarray(0, 8).equals(PNG_SIGNATURE)) return fail()
          return resolve({ kind: "png", bytes, rows })
        }
        const text = new TextDecoder("utf-8", { fatal: true }).decode(data)
        if (TEXT_CONTROLS.test(text.replace(/[\t\r\n]/g, ""))) return fail()
        // Only literal text reaches the panel, never a Markdown renderer,
        // clipboard, command, prompt, session history or external viewer.
        resolve({ kind: "text", text: text.replace(/\r\n?/g, "\n").replace(/\t/g, "    ") })
      } catch {
        fail()
      }
    })
    child.stdin.end(JSON.stringify({ path, ...mounts }))
  })
}

function Raster(props: { rows: string[], width: number, height: number }) {
  const cells = createMemo(() => {
    const sourceWidth = props.rows[0].length / 6
    const sourceHeight = props.rows.length
    const width = Math.min(sourceWidth, Math.max(1, props.width - 2))
    const height = Math.min(sourceHeight, Math.max(1, props.height - 6) * 2)
    const scale = Math.min(width / sourceWidth, height / sourceHeight)
    const cols = Math.max(1, Math.floor(sourceWidth * scale))
    const pixels = Math.max(1, Math.floor(sourceHeight * scale))
    const color = (col, row) => {
      const y = Math.min(sourceHeight - 1, Math.floor(row * sourceHeight / pixels))
      const x = Math.floor(col * sourceWidth / cols) * 6
      return "#" + props.rows[y].slice(x, x + 6)
    }
    return Array.from({ length: Math.ceil(pixels / 2) }, (_, line) =>
      Array.from({ length: cols }, (_, col) => ({
        top: color(col, line * 2), bottom: color(col, Math.min(line * 2 + 1, pixels - 1)),
      })))
  })
  // Colored half-block cells work in Yakuake without a graphics protocol,
  // remote images, terminal file reads or a new dependency.
  return <box flexDirection="column"><For each={cells()}>{(line) =>
    <text selectable={false} wrapMode="none"><For each={line}>{(cell) =>
      <span fg={cell.top} bg={cell.bottom}>▀</span>
    }</For></text>
  }</For></box>
}

async function propose(bytes: Buffer, active: () => boolean) {
  const id = randomBytes(16).toString("hex")
  const send = (frame: string) => new Promise<void>((resolve, reject) =>
    process.stdout.write(`\x1b]778;oc-box-png:v1:${id}:${frame}\x07`, (error) =>
      error ? reject(error) : resolve()))
  await send(`begin:${bytes.length}`)
  for (let i = 0; i < bytes.length; i += 4096) {
    if (!active()) return
    await send(`data:${bytes.subarray(i, i + 4096).toString("base64")}`)
  }
  if (active()) await send("end")
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
  const [shown, setShown] = createSignal<{ session: string, path: string, content: Snapshot }>()
  let pressed
  let pending: AbortController | undefined
  let closed = false
  let routeGeneration = 0
  let exporting = false
  const hostOpen = process.env.OC_BOX_FILE_PREVIEW_HOST_OPEN === "1"
  async function openOnHost(file) {
    const active = () => !closed && shown() === file && activeSession() === file.session
    if (!hostOpen || exporting || !active() || file.content.kind !== "png") return
    exporting = true
    try {
      await propose(file.content.bytes, active)
      if (active()) context.ui.toast.show({
        message: "Press F8 to review this PNG in the host confirmation.",
        variant: "info",
      })
    } catch {
      if (active()) context.ui.toast.show({message: "Could not propose the PNG to the host.", variant: "warning"})
    } finally {
      exporting = false
    }
  }
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
            <Show when={hostOpen && file().content.kind === "png"}>
              <text selectable={false} onMouseUp={(event) => {
                if (event.button === 0 && !event.isDragging) {
                  event.stopPropagation()
                  void openOnHost(file())
                }
              }}>[Open in host app]</text>
            </Show>
            <scrollbox flexGrow={1}>
              <Show when={file().content.kind === "png"} fallback={
                <text selectable={false} wrapMode="word">{file().content.text || "(empty file)"}</text>
              }>
                <Raster rows={file().content.rows} width={panel.width} height={renderer.height} />
              </Show>
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
      const content = await snapshot(path, mounts, controller.signal)
      if (closed || controller.signal.aborted || generation !== routeGeneration ||
          session !== activeSession()) return
      if (context.ui.panel.open(PANEL)) setShown({ session, path, content })
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
