// GPL-3.0-or-later. Native V2 selective UTF-8 byte-budgeted output.
import { mkdir, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { randomUUID } from "node:crypto"

const limits = new Map([["shell", 8192], ["grep", 8192], ["webfetch", 12288],
  ["duckduckgo.fetch_content", 12288], ["duckduckgo_fetch_content", 12288],
  ["pdf-reader.read_pdf", 12288], ["pdf-reader_read_pdf", 12288]])

export function utf8Slice(text, budget, tail = false) {
  const bytes = Buffer.from(text, "utf8")
  if (bytes.length <= budget) return text
  let offset = tail ? bytes.length - budget : budget
  if (tail) {
    while (offset < bytes.length && (bytes[offset] & 0xc0) === 0x80) offset++
    return bytes.subarray(offset).toString("utf8")
  }
  while (offset > 0 && (bytes[offset] & 0xc0) === 0x80) offset--
  return bytes.subarray(0, offset).toString("utf8")
}

export default {
  id: "output-clamp",
  async setup(ctx) {
    const directory = process.env.OPENCODE_CLAMP_DIR || path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local/share"), "opencode/tool-output")
    await ctx.tool.hook("execute.after", async event => {
      const limit = limits.get(event.tool)
      if (event.status !== "completed" || limit === undefined) return
      const content = event.result.content
      const text = typeof content === "string" ? content : Array.isArray(content) ? content.filter(item => item.type === "text").map(item => item.text).join("\n") : ""
      let inputText = ""
      try { inputText = JSON.stringify(event.input || {}) } catch {}
      if (Buffer.byteLength(text, "utf8") <= limit || inputText.includes("full-output-ok")) return
      try {
        await mkdir(directory, { recursive: true, mode: 0o700 })
        const file = path.join(directory, "clamp_" + randomUUID() + ".txt")
        await writeFile(file, text, { encoding: "utf8", mode: 0o600, flag: "wx" })
        const head = utf8Slice(text, 5000)
        const tail = utf8Slice(text, 2000, true)
        const total = Buffer.byteLength(text)
        let shown = head + `\n\n[output-clamp] ${total} bytes; full output saved to ${file}\nUse grep/offset reads on the saved file, or full-output-ok to bypass.\n\n` + tail
        if (Buffer.byteLength(shown) > limit) {
          const excess = Buffer.byteLength(shown) - limit
          shown = utf8Slice(head, Math.max(0, 5000 - excess - 32)) + shown.slice(head.length)
        }
        if (Buffer.byteLength(shown) > limit) return // Never discard unrecoverable content.
        event.result.content = typeof content === "string" ? shown : [{ type: "text", text: shown }, ...content.filter(item => item.type !== "text")]
        event.result.metadata = { ...event.result.metadata, outputClamp: { bytes: total, spillPath: file } }
      } catch {
        // Failure to preserve the full output leaves the native result intact.
      }
    })
  },
}
