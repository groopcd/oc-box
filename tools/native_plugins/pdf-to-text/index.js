// GPL-3.0-or-later. Convert PDFs only for models lacking native PDF support.
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { mkdtemp, writeFile, unlink, rmdir, readFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { createHash } from "node:crypto"
import { fileURLToPath } from "node:url"

const execute = promisify(execFile)
export async function pdfBytes(part) {
  const source = part.media.source
  if (source.type === "base64") return Buffer.from(source.data, "base64")
  if (source.type === "bytes") return Buffer.from(source.data)
  if (source.type === "url" && source.url.startsWith("file:")) return readFile(fileURLToPath(source.url))
  if (source.type === "url" && source.url.startsWith("data:")) {
    const comma = source.url.indexOf(",")
    if (comma < 0) throw Error("Invalid PDF data URL")
    return source.url.slice(0, comma).includes(";base64") ? Buffer.from(source.url.slice(comma + 1), "base64") : Buffer.from(decodeURIComponent(source.url.slice(comma + 1)))
  }
  throw Error("Remote PDF URLs require explicit download; no automatic network fetch")
}

export default {
  id: "pdf-to-text",
  async setup(ctx) {
    const cache = new Map()
    const convert = async event => {
      const catalog = await ctx.model.list()
      const info = (catalog.data ?? catalog).find(model => model.providerID === event.model.providerID && model.id === event.model.id)
      if (info?.capabilities?.input?.includes("pdf")) return
      for (const message of event.messages) {
        for (let index = 0; index < message.content.length; index++) {
          const part = message.content[index]
          if (part.type !== "media" || part.media.mediaType !== "application/pdf") continue
          try {
            const bytes = await pdfBytes(part)
            if (bytes.length > 32 * 1024 * 1024) throw Error("PDF exceeds the extraction size limit")
            const key = createHash("sha256").update(bytes).digest("hex")
            let text = cache.get(key)
            if (text === undefined) {
              const directory = await mkdtemp(path.join(os.tmpdir(), "opencode-pdf-"))
              const file = path.join(directory, "input.pdf")
              try {
                await writeFile(file, bytes, { mode: 0o600 })
                const result = await execute("pdftotext", ["-layout", file, "-"], { timeout: 30000, maxBuffer: 16 * 1024 * 1024 })
                text = result.stdout.trim()
                if (cache.size >= 16) cache.delete(cache.keys().next().value)
                cache.set(key, text)
              } finally {
                await unlink(file).catch(() => {})
                await rmdir(directory).catch(() => {})
              }
            }
            message.content[index] = { type: "text", text: `<attached-pdf filename=${JSON.stringify(part.filename || "document.pdf")}>\n${text}\n</attached-pdf>` }
          } catch {
            // Abort explicitly rather than silently replace an unread PDF with
            // invented text or send a format known to be unsupported.
            throw Error("pdf-to-text: extraction failed; ensure pdftotext is installed and use a local PDF within the size limit. No PDF content was logged.")
          }
        }
      }
    }
    for (const kind of ["context", "compaction", "generate", "title"]) await ctx.session.hook(kind, convert)
  },
}
