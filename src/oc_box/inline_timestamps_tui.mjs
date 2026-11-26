import { TextRenderable, Yoga } from "@opentui/core"
import { onCleanup } from "solid-js"
import { createInlineTimestamps } from "./inline_timestamps.mjs"

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

  return context.ui.slot({
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
}
