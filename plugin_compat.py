"""Generate private V2 entrypoints for copied V1 plugins; host files stay intact."""

import hashlib
import json
from pathlib import Path
import re


def bridge_file(relative):
    digest = hashlib.sha256(str(relative).encode()).hexdigest()[:16]
    original = relative.with_name(
        relative.stem + ".oc-box-v1" + (".mts" if relative.suffix == ".ts" else ".mjs")
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
