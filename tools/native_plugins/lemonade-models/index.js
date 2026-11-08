// GPL-3.0-or-later. Native V2 live Lemonade discovery and explicit control.
import { Model } from "@opencode/plugin"
const nonChat = new Set(["embeddings", "reranking", "image", "edit", "tts", "transcription"])
export function contextFor(model) {
  const values = [model.max_context_window, model.recipe_options?.ctx_size].filter(value => typeof value === "number" && Number.isFinite(value) && value > 0)
  return values.length ? Math.min(...values) : 8192
}
async function request(base, endpoint, value) {
  const response = await fetch(base + "/" + endpoint, { method: value === undefined ? "GET" : "POST", headers: { accept: "application/json", ...(value === undefined ? {} : { "content-type": "application/json" }) }, body: value === undefined ? undefined : JSON.stringify(value), signal: AbortSignal.timeout(value === undefined ? 2000 : 300000) })
  if (!response.ok) throw Error("Lemonade request failed")
  return response.json()
}
export default {
  id: "lemonade-models",
  async setup(ctx) {
    const options = ctx.options || {}
    const base = String(options.baseURL || process.env.LEMONADE_BASE_URL || "http://localhost:13305/api/v1").replace(/\/+$/, "")
    let report = "No Lemonade provider declared; discovery idle"
    async function discover() {
      const [catalog, health] = await Promise.allSettled([request(base, "models"), request(base, "health")])
      if (catalog.status === "rejected") throw catalog.reason
      return { models: (catalog.value.data || []).filter(model => typeof model.id === "string" && !(model.labels || []).some(label => nonChat.has(label))), health: health.status === "fulfilled" ? health.value : {} }
    }
    if (options.configured) {
      let catalog
      try { catalog = await discover() } catch { report = "Lemonade stopped/unreachable; provider removed for this startup" }
      const models = catalog?.models || []
      if (models.length) {
        const inventory = models.map(model => {
          const info = Model.Info.default("lemonade", model.id)
          info.name = model.id
          info.capabilities.tools = (model.labels || []).includes("tool-calling")
          info.capabilities.input = (model.labels || []).includes("vision") ? ["text", "image"] : ["text"]
          info.limit.context = contextFor(model)
          info.limit.output = Math.min(8192, info.limit.context)
          return info
        })
        await ctx.provider.transform(editor => {
          if (editor.get("lemonade")) editor.models.set("lemonade", inventory)
          else editor.add({ info: { id: "lemonade", name: "Lemonade", package: options.package || "aisdk:@ai-sdk/openai-compatible", activation: "enabled", settings: { baseURL: base } }, models: inventory })
        })
        report = `Discovered ${models.length} chat-capable models at startup`
      } else {
        await ctx.provider.transform(editor => editor.remove("lemonade"))
      }
    }
    await ctx.tool.transform(editor => editor.add({
      name: "lemonade", description: "Query the local Lemonade server. list/status are read-only; load/unload explicitly control a selected downloaded model. Startup picker inventory is a snapshot.",
      input: { type: "object", properties: { action: { type: "string", enum: ["list", "status", "load", "unload"] }, model: { type: "string" } }, required: ["action"], additionalProperties: false },
      options: { codemode: false },
      async execute(input) {
        if (input.action === "load" && !input.model) return { content: "load requires a model; use list first" }
        try {
          if (["load", "unload"].includes(input.action)) return { content: JSON.stringify(await request(base, input.action, input.model ? { model_name: input.model } : {})) }
          const { models, health } = await discover()
          const loaded = new Set((health.all_models_loaded || []).map(item => item.model_name))
          if (health.model_loaded) loaded.add(health.model_loaded)
          return { content: input.action === "status" ? `${report}\nLoaded: ${[...loaded].join(", ") || "none"}` : models.map(model => `${loaded.has(model.id) ? "*" : " "} ${model.id} ctx=${contextFor(model)} labels=${(model.labels || []).join(",")}`).join("\n") }
        } catch { return { content: "Lemonade stopped/unreachable, or request rejected. No real model was automatically loaded." } }
      },
    }))
  },
}
