import type { Context, PanelInput } from "@opencode/plugin/tui/context"
import { createMemo, Index, Show } from "solid-js"
import { activityEvents, localTimestamp, MAX_EVENTS } from "./activity_timeline_events.mjs"

const PANEL = "oc-box.activity-timeline"

function ActivityPanel(props: { context: Context; panel: PanelInput }) {
  const { context } = props
  const events = createMemo(() => activityEvents(context.data.session.message.list(props.panel.sessionID) ?? []))
  context.keymap.layer(() => ({
    commands: [{ bind: "escape", run: () => { props.panel.close() } }],
  }))
  return (
    <box flexDirection="column" flexGrow={1} minHeight={0} paddingLeft={1} paddingRight={1}>
      <text fg={context.theme.text.base}>Activity timeline</text>
      <text fg={context.theme.text.muted} wrapMode="word">
        Loaded transcript · newest first · up to {MAX_EVENTS} events · Esc closes
      </text>
      <Show when={events().length > 0} fallback={<text fg={context.theme.text.muted}>No recorded activity.</text>}>
        <scrollbox flexGrow={1} minHeight={0}>
          <Index each={events()}>
            {(event) => (
              <box flexDirection="column" flexShrink={0} marginBottom={1}>
                <text fg={context.theme.text.muted}>{localTimestamp(event().time)}</text>
                <text fg={context.theme.text.base} wrapMode="word">{event().label}</text>
              </box>
            )}
          </Index>
        </scrollbox>
      </Show>
    </box>
  )
}

export function install(context: Context) {
  // Older V2 releases can load TUI plugins without providing session panels.
  if (typeof context.ui.panel?.open !== "function" || typeof context.data.session?.message?.list !== "function") {
    throw new Error("Session panels unavailable")
  }
  const stop = context.ui.slot({
    append: "session.panel",
    render: (panel) => (
      <Show when={panel.name === PANEL}>
        <ActivityPanel context={context} panel={panel} />
      </Show>
    ),
  })
  context.keymap.layer(() => ({
    mode: "global",
    commands: [{
      id: PANEL,
      title: "Open activity timeline",
      group: "Session",
      palette: true,
      slash: { name: "activity-timeline" },
      enabled: () => context.ui.router.current().type === "session",
      run: () => { context.ui.panel.open(PANEL) },
    }],
  }))
  return () => {
    context.ui.panel.close()
    stop()
  }
}
