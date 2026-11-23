import { Plugin } from "@opencode/plugin"
import { UIState } from "./ui_state_rpc.js"
import { createMemory, PROTOCOL } from "./ui_state_memory.js"

export default Plugin.define({
  id: "oc-box.ui-state",
  async setup(context) {
    // One instance per native control location. No session/storage mutations.
    const memory = createMemory()
    const registration = await context.rpc.register(UIState, {
      ping: async () => ({ protocol: PROTOCOL }),
      report: async (input) => memory.report(input),
      release: async (input) => memory.release(input),
      snapshot: async (input) => memory.snapshot(input),
    })
    return async () => {
      memory.clear()
      await registration.dispose()
    }
  },
})
