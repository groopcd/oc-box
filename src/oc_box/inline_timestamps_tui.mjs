import { TextRenderable, Yoga } from "@opentui/core"
import { getOwner, onCleanup } from "solid-js"
import { createInlineTimestamps } from "./inline_timestamps.mjs"

function findSessionAnchors(owner, sessionID) {
  // The composer slot runs inside the native Session's Solid provider. Only
  // use the existing whole-message scroll anchors; never infer a notice's ID
  // from its rendered text or position. The provider layout is private.
  const state = owner?.context
  if (!state) return
  const keys = Reflect.ownKeys(state)
  if (keys.length > 64) return
  let match
  for (const key of keys) {
    const value = Object.getOwnPropertyDescriptor(state, key)?.value
    if (typeof value !== "object" || value === null ||
        Object.getOwnPropertyDescriptor(value, "sessionID")?.value !== sessionID ||
        typeof Object.getOwnPropertyDescriptor(value, "messageIndex")?.value !== "function" ||
        typeof Object.getOwnPropertyDescriptor(value, "pendingDelivery")?.value !== "function") continue
    const anchors = Object.getOwnPropertyDescriptor(value, "anchors")?.value
    if (typeof anchors?.get !== "function" || typeof anchors.forMessage !== "function" ||
        typeof anchors.messagePositions !== "function") continue
    if (match && match !== anchors) return
    match = anchors
  }
  return match
}

export function install(context, unavailable) {
  const renderer = context.renderer
  const column = Yoga?.FlexDirection?.Column
  if (
    typeof column !== "number" ||
    typeof renderer?.setFrameCallback !== "function" ||
    typeof renderer.removeFrameCallback !== "function" ||
    typeof renderer.requestRender !== "function" ||
    typeof context.ui?.router?.current !== "function" ||
    typeof context.data?.session?.message?.list !== "function"
  ) throw new Error("Native transcript renderer unavailable")

  const composers = new Set()
  function completionFor(sessionID) {
    if (!sessionID || composers.size > 32) return
    const matching = [...composers].filter((entry) => entry.sessionID === sessionID)
    if (matching.length !== 1) return
    try {
      return {
        anchors: matching[0].anchors,
        pending: context.data.session.pending?.list(sessionID),
      }
    } catch { /* No verified notice state; retain ordinary row-ID matching. */ }
  }
  const stopApp = context.ui.slot({
    append: "app",
    render: () => {
      const decorator = createInlineTimestamps({
        column,
        makeLabel: (id, content, fg) => new TextRenderable(renderer, {
          id, content, fg, flexShrink: 0, paddingLeft: 3, wrapMode: "word", selectable: false,
        }),
      })
      let stopped = false
      const stop = () => {
        if (stopped) return
        stopped = true
        try { renderer.removeFrameCallback(frame) } catch { /* Callback is already inert. */ }
        decorator.clear()
      }
      const frame = async () => {
        if (stopped) return
        try {
          const route = context.ui.router.current()
          const sessionID = route.type === "session" ? route.sessionID : undefined
          decorator.update({
            root: renderer.root,
            sessionID,
            messages: sessionID ? context.data.session.message.list(sessionID) : [],
            color: context.theme.text.muted,
            completion: completionFor(sessionID),
          })
        } catch {
          stop()
          unavailable()
        }
      }
      onCleanup(stop)
      try {
        renderer.setFrameCallback(frame)
        renderer.requestRender()
      } catch {
        stop()
        unavailable()
      }
      return null
    },
  })
  let stopComposer
  try {
    stopComposer = context.ui.slot({
      append: "session.composer.top",
      render: (input) => {
        try {
          const sessionID = input.sessionID
          const anchors = findSessionAnchors(getOwner(), sessionID)
          if (!anchors) return null
          const entry = { sessionID, anchors }
          onCleanup(() => composers.delete(entry))
          composers.add(entry)
        } catch { /* Older clients keep the existing row-ID path. */ }
        return null
      },
    })
  } catch { /* Clients without the slot can still timestamp ID-bearing rows. */ }
  return () => {
    composers.clear()
    try { stopComposer?.() } finally { stopApp() }
  }
}
