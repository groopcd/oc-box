import type { Context, PanelInput } from "@opencode/plugin/tui/context"
import { createMemo, ErrorBoundary, Index, Show } from "solid-js"
import { activityEvents, localTimestamp, MAX_EVENTS } from "./activity_timeline_events.mjs"

const PANEL = "oc-box.activity-timeline"

function ownsPanel(panel: { readonly name?: string } | null | undefined) {
  try { return panel?.name === PANEL } catch { return false }
}

function ActivityPanel(props: { context: Context; panel: PanelInput; unavailable: () => void }) {
  const { context } = props
  context.keymap.layer(() => ({
    commands: [{
      bind: "escape",
      run: () => {
        try { props.panel.close() } catch { props.unavailable() }
      },
    }],
  }))
  return (
    <ErrorBoundary fallback={<text>Activity timeline unavailable in this TUI. Esc closes.</text>}>
      <ActivityEntries context={context} sessionID={props.panel.sessionID} />
    </ErrorBoundary>
  )
}

function ActivityEntries(props: { context: Context; sessionID: string }) {
  const { context } = props
  const events = createMemo(() => activityEvents(context.data.session.message.list(props.sessionID) ?? []))
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

export function install(context: Context, unavailable: () => void) {
  // Older V2 releases can load TUI plugins without providing session panels.
  if (
    typeof context.ui?.panel?.open !== "function" ||
    typeof context.ui.panel.close !== "function" ||
    typeof context.ui.slot !== "function" ||
    typeof context.ui.router?.current !== "function" ||
    typeof context.keymap?.layer !== "function" ||
    typeof context.data?.session?.message?.list !== "function"
  ) {
    throw new Error("Session panels unavailable")
  }
  const stop = context.ui.slot({
    append: "session.panel",
    render: (panel) => (
      <Show when={ownsPanel(panel)}>
        <ErrorBoundary fallback={<text>Activity timeline unavailable in this TUI.</text>}>
          <ActivityPanel context={context} panel={panel} unavailable={unavailable} />
        </ErrorBoundary>
      </Show>
    ),
  })
  try {
    context.keymap.layer(() => ({
      mode: "global",
      commands: [{
        id: PANEL,
        title: "Open activity timeline",
        group: "Session",
        palette: true,
        slash: { name: "activity-timeline" },
        enabled: () => {
          try { return context.ui.router.current().type === "session" } catch { return false }
        },
        run: () => {
          try { context.ui.panel.open(PANEL) } catch { unavailable() }
        },
      }],
    }))
  } catch (error) {
    stop()
    throw error
  }
  return () => {
    try {
      if (ownsPanel(context.ui.panel.current?.())) context.ui.panel.close()
    } finally { stop() }
  }
}
