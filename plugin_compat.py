"""Generate private V2 entrypoints for copied V1 plugins; host files stay intact."""

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse


def local_source(target, source):
    """Configured local files under the copied config root, never npm specs."""
    if not isinstance(target, str):
        return None
    target = target.removeprefix("-")
    if target.startswith("file://"):
        parsed = urlparse(target)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            return None
        candidate = Path(unquote(parsed.path))
    elif target.startswith(("/", "./", "../", "~/")) or (source / target).is_file():
        candidate = Path(target).expanduser()
        if not candidate.is_absolute():
            candidate = source / candidate
    else:
        return None
    try:
        relative = candidate.resolve().relative_to(source.resolve())
    except ValueError:
        return None
    return (
        source / relative
        if candidate.is_file() and candidate.suffix in (".ts", ".js", ".mts", ".mjs")
        else None
    )


def copied_source(relative, content):
    """Repair the known legacy byte-slicing bug only in private V2 copies."""
    if relative.name == "tool-definition-audit.ts":
        text = content.decode()
        old = '''"schemaChars is NOT: parameters arrives as the internal Zod object, measuring ~6.8x " +
        "the emitted JSON Schema. MCP tool definitions never reach this hook at all - only " +
        "built-in and plugin-registered tools appear here."'''
        new = '''"schemaChars measures the JSON Schema supplied by the oc-box V2 request bridge, " +
        "including advertised MCP/Code Mode definitions. Activation-only entries may differ " +
        "from a request's normalized schema; request hooks refresh each advertised definition."'''
        return text.replace(old, new).encode()
    if relative.name != "output-clamp.ts":
        return content
    text = content.decode()
    expected = (
        "const HEAD_BYTES = 5000",
        "const TAIL_BYTES = 2000",
        "const CLAMP_THRESHOLDS",
        'const bytes = Buffer.byteLength(original, "utf8")',
    )
    if not all(marker in text for marker in expected):
        return content
    # Match exact legacy expressions. Refuse to alter unfamiliar implementations.
    fixes = {
        "if (original.length <= threshold) return": 'if (Buffer.byteLength(original, "utf8") <= threshold) return',
        "const raw = s.slice(0, budget)": "const raw = utf8Slice(s, budget, false)",
        "const raw = s.slice(-budget)": "const raw = utf8Slice(s, budget, true)",
        "nl > budget * 0.8": 'nl >= 0 && Buffer.byteLength(raw.slice(0, nl), "utf8") > budget * 0.8',
        "nl >= 0 && nl < budget * 0.2": 'nl >= 0 && Buffer.byteLength(raw.slice(0, nl + 1), "utf8") < budget * 0.2',
    }
    if not all(text.count(old) == 1 for old in fixes):
        return content
    for old, new in fixes.items():
        text = text.replace(old, new)
    helper = """
// oc-box copied-V2 repair: slice whole Unicode code points within UTF-8 budgets.
function utf8Slice(text: string, budget: number, tail: boolean): string {
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
"""
    return (text + helper).encode()


def bridge_file(relative):
    digest = hashlib.sha256(str(relative).encode()).hexdigest()[:16]
    original = relative.with_name(
        relative.stem
        + ".oc-box-v1"
        + (".mts" if relative.suffix in (".ts", ".mts") else ".mjs")
    )
    directory = relative.parent / ("oc-box-compat-" + digest)
    prefix = "../" * (len(directory.parts))
    script = (
        "import { adapt } from "
        + json.dumps(prefix + "oc-box-v1-bridge.js")
        + "\n"
        + "import * as original from "
        + json.dumps("../" + original.name)
        + "\n"
        + "export default adapt(original, "
        + json.dumps("oc-box.v1." + digest)
        + ")\n"
    )
    return original, directory, script.encode()


def package_target(target):
    match = re.fullmatch(r"(opencode-sandbox)(?:@([^/\s]+))?", target)
    return (match[1], match[2] or "latest") if match else None


def rewrite_plugins(config, paths, sandbox_path):
    """Convert supported configured standalone sources to native directories."""
    for key in ("plugin", "plugins"):
        if key not in config:
            continue
        result = []
        for entry in config[key]:
            target = (
                entry
                if isinstance(entry, str)
                else entry[0]
                if isinstance(entry, list)
                else entry.get("package", "")
            )
            if target.startswith("-"):
                remove = target[1:]
                result.append(
                    "-"
                    + paths.get(
                        remove, sandbox_path if package_target(remove) else remove
                    )
                )
                continue
            replacement = paths.get(
                target, sandbox_path if package_target(target) else target
            )
            if isinstance(entry, str):
                result.append(replacement)
            elif isinstance(entry, list):
                result.append([replacement, *entry[1:]])
            else:
                result.append({**entry, "package": replacement})
        config[key] = result


def sandbox_entry():
    # Mainline Podman containment already opted this plugin out on V1. Invoke
    # that same maintained package's factory, rather than inventing enforcement.
    return b'import { adapt } from "../../oc-box-v1-bridge.js"\nimport * as original from "opencode-sandbox"\nconst implementation = adapt(original, "oc-box.v1.opencode-sandbox")\nexport default { id: implementation.id, async setup(ctx) {\n  if (process.env.OPENCODE_DISABLE_SANDBOX !== "1") throw new Error("oc-box sandbox adapter requires container sandbox opt-out")\n  return await implementation.setup(ctx)\n} }\n'
