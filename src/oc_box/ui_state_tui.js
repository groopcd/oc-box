import { Plugin } from "@opencode/plugin/tui"
import { createEffect, createSignal, onCleanup, untrack } from "solid-js"
import { UIState } from "./ui_state_rpc.js"
import { createReporter, MAX_TABS, nonce, sessionID } from "./ui_state_memory.js"

export default Plugin.define({
  id: "oc-box.ui-state.tui",
  setup(context) {
    const stopReporter = setupReporter(context)
    const stopActivity = process.env.OC_BOX_ACTIVITY_TIMELINE === "1" ? setupActivity(context) : undefined
    if (!process.env.OC_BOX_FILE_PREVIEW_ROOTS) {
      return stopActivity ? async () => {
        stopActivity()
        await stopReporter?.()
      } : stopReporter
    }
    let stopped = false
    let stopPreview
    const report = status => { process.stdout.write(`\x1b]778;oc-box-open:v1:${status}\x07`) }
    void import("./file_preview_tui.tsx").then(({ install }) => {
      if (stopped) return
      stopPreview = install(context)
      report("ready")
    }).catch(() => {
      if (stopped) return
      stopPreview?.()
      stopPreview = undefined
      try {
        context.ui.toast.show({
          message: "Host file opening is off: this client does not provide a compatible native TUI hook.",
          variant: "warning",
        })
        report("unavailable-notified")
      } catch {
        // The launcher reports an unconfirmed hook after restoring the TTY.
      }
    })
    return async () => {
      stopped = true
      stopPreview?.()
      stopActivity?.()
      await stopReporter?.()
    }
  },
})

function setupActivity(context) {
  let stopped = false
  let dispose
  const unavailable = () => {
    if (stopped) return
    try {
      context.ui.toast.show({
        message: "Activity timeline could not load; continuing without it.",
        variant: "warning",
      })
    } catch {
      // Older SDKs may not provide compatible notifications either.
    }
  }
  void import("./activity_timeline_tui.tsx").then(({ install }) => {
    if (!stopped) dispose = install(context, unavailable)
  }).catch(unavailable)
  return () => {
    stopped = true
    try {
      dispose?.()
    } catch {
      // Optional panel teardown must not interrupt reporter/preview cleanup.
    }
  }
}

function setupReporter(context) {
  // The launcher supplies NONSECRET per-process labels only to native TUI
  // clients. No global CLI plugin-list override, provider values or host reads.
  const client = process.env.OC_BOX_UI_STATE_CLIENT
  const generation = process.env.OC_BOX_UI_STATE_GENERATION
  if (!nonce(client) || !nonce(generation)) return
  const controlLocation = context.data.location.default()
  const [sequence, updateSequence] = context.storage.memory("sequence", { initial: { value: 0 } })
  let reporter
  const unregister = context.ui.slot({
    append: "app",
    render: () => {
      const [focus, setFocus] = createSignal("unknown")
      const rpc = context.client.rpc(UIState)
      reporter = createReporter(
        (method, input, signal) => rpc[method](input, { location: controlLocation, signal }),
        {
          client, generation,
          nextSequence() {
            return untrack(() => {
              let result
              updateSequence((draft) => { result = ++draft.value })
              return result
            })
          },
        },
      )
      const focused = () => setFocus("focused")
      const blurred = () => setFocus("blurred")
      context.renderer.on("focus", focused)
      context.renderer.on("blur", blurred)
      const stops = [
        context.data.on("server.connected", () => reporter.refresh()),
        context.data.on("plugin.updated", () => reporter.refresh()),
        context.data.on("config.updated", () => reporter.refresh()),
      ]
      createEffect(() => {
        // All reactive properties are read synchronously; never defer reading
        // the live route proxy into the asynchronous RPC request.
        const route = context.ui.router.current()
        const routeType = route.type
        const selected = routeType === "session" ? route.sessionID : null
        const tabsEnabled = context.ui.tabs.enabled()
        const entries = tabsEnabled ? context.ui.tabs.list() : []
        const tabs = entries.map((entry) => entry.sessionID)
        const active = entries.filter((entry) => entry.active).map((entry) => entry.sessionID)
        const common = { focus: focus() }
        if (tabs.length > MAX_TABS || !tabs.every(sessionID) ||
            (selected !== null && !sessionID(selected)) ||
            (tabsEnabled && routeType === "session" && active.length !== 1)) {
          reporter.publish({
            ...common, routeType: "unknown", selected: null,
            tabsEnabled: false, tabs: [], activeRoot: null,
          })
          return
        }
        reporter.publish({
          ...common, routeType, selected, tabsEnabled, tabs,
          activeRoot: tabsEnabled && routeType === "session" ? active[0] : null,
        })
      })
      onCleanup(() => {
        context.renderer.off("focus", focused)
        context.renderer.off("blur", blurred)
        stops.forEach((stop) => stop())
        void reporter.stop()
      })
      return null
    },
  })
  return async () => {
    unregister()
    await reporter?.stop()
  }
}
