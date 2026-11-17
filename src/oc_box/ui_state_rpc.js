import { Rpc } from "@opencode/plugin/rpc"
import { nonce, validReport } from "./ui_state_memory.js"

// Standard Schema V1 is a public portable RPC schema, without a dependency or
// a transforming JSON-Schema decoder. Never strip or echo unrecognized fields.
const schema = (check) => ({
  "~standard": {
    version: 1, vendor: "oc-box.ui-state",
    validate(value) {
      return check(value) ? { value } : { issues: [{ message: "Invalid UI metadata." }] }
    },
  },
})
const object = (value, names) => value !== null && typeof value === "object" &&
  !Array.isArray(value) && Object.keys(value).sort().join() === [...names].sort().join()
const accepted = schema((value) => object(value, ["accepted"]) && typeof value.accepted === "boolean")
const snapshot = schema((value) => {
  if (!value || value.protocol !== 1 || typeof value.epoch !== "string" ||
      value.epoch.length < 1 || value.epoch.length > 64) return false
  if (value.status === "missing") return object(value, ["protocol", "epoch", "status", "reason"]) &&
    ["overflow", "generation", "ambiguous", "absent", "reported-unknown"].includes(value.reason)
  const { protocol, epoch, status, source, ...report } = value
  return status === "valid" && ["owner-tui", "single-tui"].includes(source) && validReport(report)
})
export const UIState = Rpc.define({
  id: "oc-box.ui-state",
  methods: {
    ping: {
      input: schema((value) => object(value, [])),
      output: schema((value) => object(value, ["protocol"]) && value.protocol === 1),
    },
    report: { input: schema(validReport), output: accepted },
    release: {
      input: schema((value) => object(value, ["generation", "client", "seq"]) &&
        nonce(value.generation) && nonce(value.client) && Number.isSafeInteger(value.seq) && value.seq > 0),
      output: accepted,
    },
    snapshot: {
      input: schema((value) => object(value, ["generation", "ownerClient"]) &&
        nonce(value.generation) && nonce(value.ownerClient)),
      output: snapshot,
    },
  },
  events: {},
})
