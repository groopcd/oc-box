// Exercise the actual private-source rewrite, not just text replacement markers.
import assert from "node:assert/strict"
import { stripTypeScriptTypes } from "node:module"
import vm from "node:vm"

let input = ""
for await (const chunk of process.stdin) input += chunk
const source = JSON.parse(input).source
const context = vm.createContext({ Buffer })
const module = new vm.SourceTextModule(stripTypeScriptTypes(source) + "\nexport { headSlice, tailSlice, belowThreshold }", { context })
await module.link(() => {})
await module.evaluate()
const { headSlice, tailSlice, belowThreshold } = module.namespace
for (const value of ["中".repeat(3000), "🙂".repeat(3000), "é".repeat(3000), "x中🙂".repeat(2000)]) {
  const bytes = Buffer.byteLength(value)
  assert.equal(belowThreshold(value, 8192), bytes <= 8192)
  const head = headSlice(value, 5000)
  const tail = tailSlice(value, 2000)
  assert.ok(Buffer.byteLength(head) <= 5000)
  assert.ok(Buffer.byteLength(tail) <= 2000)
  assert.ok(value.startsWith(head) && value.endsWith(tail))
  assert.ok(!/[\uD800-\uDBFF]$/.test(head) && !/^[\uDC00-\uDFFF]/.test(tail))
  assert.ok(!head.includes("�") && !tail.includes("�"))
}
console.log("PASS: copied output clamp checks UTF-8 thresholds and retains whole Unicode within byte budgets")
