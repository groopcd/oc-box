// Cooperative UI metadata only. No permissions, execution, files or host authority.
export const PROTOCOL = 1
export const TTL = 7000
export const MAX_CLIENTS = 16
export const MAX_TABS = 4096
export const nonce = (value) => typeof value === "string" && /^[0-9a-f]{32}$/.test(value)
export const sessionID = (value) =>
  typeof value === "string" && value.length <= 128 && /^ses[A-Za-z0-9_-]+$/.test(value)

const keys = [
  "generation", "client", "seq", "routeType", "selected", "tabsEnabled", "tabs", "activeRoot", "focus",
]

export function validReport(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).length !== keys.length || keys.some((key) => !Object.hasOwn(value, key)) ||
      !nonce(value.generation) || !nonce(value.client) ||
      !Number.isSafeInteger(value.seq) || value.seq < 1 ||
      !["home", "session", "plugin", "unknown"].includes(value.routeType) ||
      !["unknown", "focused", "blurred"].includes(value.focus) ||
      typeof value.tabsEnabled !== "boolean" ||
      !Array.isArray(value.tabs) || value.tabs.length > MAX_TABS ||
      !value.tabs.every(sessionID) || new Set(value.tabs).size !== value.tabs.length ||
      (value.selected !== null && !sessionID(value.selected)) ||
      (value.activeRoot !== null && !sessionID(value.activeRoot))) return false
  if ((value.routeType === "session") !== (value.selected !== null)) return false
  if (!value.tabsEnabled && (value.tabs.length || value.activeRoot !== null)) return false
  if (value.routeType !== "session" && value.activeRoot !== null) return false
  if (value.routeType === "session" && value.tabsEnabled &&
      (value.activeRoot === null || !value.tabs.includes(value.activeRoot))) return false
  return value.routeType !== "unknown" || (!value.tabsEnabled && !value.tabs.length)
}

export function createMemory({ clock = () => performance.now(), epoch = crypto.randomUUID() } = {}) {
  const entries = new Map()
  let generation
  let overflowUntil = -Infinity
  // Receipt freshness expires, but bounded sequence high-water/tombstones do
  // not. Forgetting them at TTL would admit a delayed old report as fresh.
  // New client IDs fail closed at capacity until this server location reloads.
  const missing = (reason) => ({ protocol: PROTOCOL, status: "missing", reason, epoch })
  return {
    report(value) {
      if (!validReport(value)) throw new Error("invalid-ui-report")
      const now = clock()
      if (generation !== undefined && generation !== value.generation) throw new Error("ui-generation-conflict")
      generation = value.generation
      const old = entries.get(value.client)
      if (old && value.seq <= old.value.seq) return { accepted: false }
      if (!old && entries.size >= MAX_CLIENTS) {
        overflowUntil = now + TTL
        return { accepted: false }
      }
      entries.set(value.client, {
        received: now,
        value: { ...value, tabs: [...value.tabs] },
        released: false,
      })
      return { accepted: true }
    },
    release(value) {
      if (!value || Object.keys(value).sort().join() !== "client,generation,seq" ||
          !nonce(value.client) || !nonce(value.generation) ||
          !Number.isSafeInteger(value.seq) || value.seq < 1) throw new Error("invalid-ui-release")
      const now = clock()
      const old = entries.get(value.client)
      if (generation !== value.generation || !old || value.seq <= old.value.seq) return { accepted: false }
      entries.set(value.client, {
        received: now, released: true, value: { ...old.value, seq: value.seq },
      })
      return { accepted: true }
    },
    snapshot(value) {
      if (!value || Object.keys(value).sort().join() !== "generation,ownerClient" ||
          !nonce(value.generation) || !nonce(value.ownerClient)) throw new Error("invalid-ui-query")
      const now = clock()
      if (now < overflowUntil) return missing("overflow")
      if (generation !== undefined && generation !== value.generation) return missing("generation")
      const fresh = [...entries.values()].filter(
        (entry) => !entry.released && now - entry.received < TTL,
      )
      const owner = fresh.find((entry) => entry.value.client === value.ownerClient)
      if (!owner && fresh.length > 1) return missing("ambiguous")
      const selected = owner ?? fresh[0]
      if (!selected) return missing("absent")
      if (selected.value.routeType === "unknown") return missing("reported-unknown")
      return {
        protocol: PROTOCOL, status: "valid", epoch,
        ...selected.value,
        tabs: [...selected.value.tabs],
        source: owner ? "owner-tui" : "single-tui",
      }
    },
    clear() {
      entries.clear()
      generation = undefined
      overflowUntil = -Infinity
    },
  }
}

// One in-flight request plus one latest pending snapshot. Heartbeats establish
// receipt freshness, not viewing recency. Three failures suspend until a public
// reconnect/config/plugin event; no native error or payload is logged.
export function createReporter(send, {
  client, generation, nextSequence,
  interval = 2000, timers = globalThis,
} = {}) {
  let desired, pending, last, running, stopped = false, failures = 0, controller, closing
  const pump = () => {
    if (running || stopped || failures >= 3 || !pending) return
    running = (async () => {
      while (pending && !stopped && failures < 3) {
        const value = pending
        pending = undefined
        controller = new AbortController()
        const timeout = timers.setTimeout(() => controller.abort(), 2000)
        try {
          await send("report", { generation, client, seq: nextSequence(), ...value }, controller.signal)
          failures = 0
        } catch {
          failures++
        } finally {
          timers.clearTimeout(timeout)
          controller = undefined
        }
      }
    })().finally(() => {
      running = undefined
      if (pending && !stopped && failures < 3) pump()
    })
  }
  const publish = (value, force = false) => {
    if (stopped) return
    desired = value
    const signature = JSON.stringify(value)
    if (!force && signature === last) return
    last = signature
    pending = value
    pump()
  }
  const timer = timers.setInterval(() => {
    if (desired && failures < 3) publish(desired, true)
  }, interval)
  return {
    publish,
    refresh() {
      failures = 0
      if (desired) publish(desired, true)
    },
    stop() {
      if (closing) return closing
      stopped = true
      pending = undefined
      timers.clearInterval(timer)
      controller?.abort()
      closing = (async () => {
        await running
        const abort = new AbortController()
        const timeout = timers.setTimeout(() => abort.abort(), 2000)
        try {
          await send("release", { generation, client, seq: nextSequence() }, abort.signal)
        } catch {
          // Expiry remains authoritative when release cannot be acknowledged.
        } finally {
          timers.clearTimeout(timeout)
        }
      })()
      return closing
    },
  }
}
