#!/usr/bin/env python3
"""Audit and explicitly port known personal plugins in the normal HOST V2 config.

No V1 SDK/bridge/profile launcher is installed. Default is a read-only audit;
--apply installs native entrypoint directories and retires obsolete sources to
one private safety snapshot. It changes only plugin entries/dependencies and
selected source files; no auth/DB/env/service operation runs. Dormant/disabled
and unrelated sources are preserved. Unknown local implementations and their
support/manifest inventory require exact-byte review before any migration writes.
Legacy dependencies are retained conservatively whenever local code or
uninspected package references are preserved.

Canonical personal V1 sources are not distributed with this utility. Before a
local source can be replaced, review its full contents against the proposed
native implementation and pass --reviewed-source RELATIVE_PATH=SHA256 for those
exact bytes. This is explicit operator approval, not automatic compatibility
certification. No source fingerprint is approved merely by its name or markers.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import oc_box as box
from oc_box import plugin_compat

KEEP = {
    "opencode-sandbox": "Keep: host shell isolation; native V2 port, existing policy retained.",
    "python-isolation": "Keep: require isolated pip installs and writable private caches.",
    "output-clamp": "Keep: selective 8/12 KiB UTF-8 budgets are stricter than the built-in 50 KiB cap.",
    "lemonade-models": "Keep: live local-model inventory/capabilities and explicit load/unload control.",
    "no-nested-sandbox": "Keep: prevent hand-wrapped shell commands from nesting the host sandbox.",
    "pdf-to-text": "Keep conditionally: extract only when the selected model lacks native PDF input.",
}
RETIRE = {
    "sandbox-uncloak": "Retire: V1 wrapped-history workaround; the native sandbox preserves original V2 inputs.",
    "tool-definition-audit": "Retire from normal use: diagnostics only, with no active description overrides.",
    "copilot-gpt6-effort": "Retire: V2 Copilot builds reasoning variants and sends selected reasoningEffort natively.",
}
MAX_FILE = 8 * 1024 * 1024
MAX_INVENTORY_FILES = 2048
MAX_INVENTORY_BYTES = 32 * 1024 * 1024
MAX_INVENTORY_DEPTH = 16
CODE_SUFFIXES = {".ts", ".js", ".mts", ".mjs", ".cts", ".cjs", ".tsx", ".jsx"}
NATIVE_IDS = {name: name for name in KEEP}
NATIVE_IDS["opencode-sandbox"] = "oc-box.host-sandbox"
# Exact previously-generated native sandbox sources, not arbitrary user code.
PREVIOUS_SANDBOX_HASHES = {
    "4659614bbc1048d385647937d617fa24bfaa1f7f0341a9a2041ef67eaa90bf7a",
    "60fc6c0d1f24abdf80ca03b0eecd01f7f6af3ade28e4a641a965828a6038ff62",
}
# Legacy hints retained for older synthetic harnesses, never authorization to
# replace source code. Local replacements require explicit per-file review.
MARKERS = {
    "opencode-sandbox": ("SandboxPlugin", "wrapWithSandbox", "resolveConfig"),
    "python-isolation": ("shell.env", "PIP_REQUIRE_VIRTUALENV"),
    "output-clamp": ("tool.execute.after", "CLAMP_THRESHOLDS"),
    "lemonade-models": ("LemonadeModels", "config:", "lemonade:"),
    "no-nested-sandbox": ("tool.execute.before", "no-nested-sandbox:"),
    "pdf-to-text": ("experimental.chat.messages.transform", "pdftotext"),
    "sandbox-uncloak": ("experimental.chat.messages.transform", "unix-block.bpf"),
    "tool-definition-audit": ("tool.definition", "DESCRIPTION_OVERRIDES"),
    "copilot-gpt6-effort": ("chat.headers", "x-oc-copilot-effort"),
}


def read(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_size > MAX_FILE
    ):
        raise ValueError("Unsafe or oversized plugin/config file: " + str(path))
    return path.read_bytes()


def safe_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(
            "Plugin/config directory must be owned and not symlinked: " + str(path)
        )


def spec(entry):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], str):
        return entry[0]
    if isinstance(entry, dict) and isinstance(entry.get("package"), str):
        return entry["package"]
    raise ValueError("Unsupported plugin configuration entry")


def source_name(value):
    """Classify an already-local file/directory, not an npm package basename."""
    stem = Path(value).name
    for ending in (".oc-box-v1.mts", ".ts", ".js", ".mts", ".mjs"):
        if stem.endswith(ending):
            stem = stem[: -len(ending)]
            break
    return stem if stem in KEEP or stem in RETIRE else None


def nonlocal_spec(plain):
    return plain.startswith("@") or bool(
        re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", plain)
        and not plain.startswith("file://")
    )


def configured_local_source(value, source):
    """Do not reinterpret scoped packages or remote URIs as relative files."""
    plain = value.removeprefix("-")
    if nonlocal_spec(plain):
        return None
    return plugin_compat.local_source(plain, source)


def name_for(value, source):
    """Only the exact sandbox package or an actual/explicit local source."""
    plain = value.removeprefix("-")
    if plugin_compat.package_target(plain):
        return "opencode-sandbox"
    local = configured_local_source(plain, source)
    if local:
        return source_name(local.name)
    if nonlocal_spec(plain):
        return None
    if plain.startswith(("/", "./", "../", "~/", "file:")) or (source / plain).is_dir():
        # Retain missing/out-of-root source and existing-destination refusals.
        # A scoped/URL package is never recognized just by its final component.
        return source_name(Path(plain.removeprefix("file://")).name)
    return None


def validate_reviews(reviewed_sources):
    if reviewed_sources is None:
        return {}
    if not isinstance(reviewed_sources, dict):
        raise ValueError(
            "Invalid source review approvals; expected relative paths and SHA-256 values"
        )
    result = {}
    for value, digest in reviewed_sources.items():
        if not isinstance(value, (str, Path)):
            raise ValueError("Invalid source review approval path")
        relative = Path(value)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None
        ):
            raise ValueError(
                "Invalid source review approval; use a relative plugin source path and exact SHA-256"
            )
        if relative in result:
            raise ValueError("Duplicate source review approval")
        result[relative] = digest.lower()
    return result


def parse_reviews(arguments):
    result = {}
    for argument in arguments:
        relative, separator, digest = argument.rpartition("=")
        if not separator or relative in result:
            raise ValueError(
                "Invalid or duplicate --reviewed-source approval; use RELATIVE_PATH=SHA256 once per file"
            )
        result[relative] = digest
    return validate_reviews(result)


def require_empty_audit_overrides(content):
    """Accept only a literal empty Map and direct read-only table uses.

    This bounded check never evaluates JavaScript. Other table forms or uses
    require a manual port; a source-hash approval cannot retire real overrides.
    Strings/comments cannot stand in for the actual declaration.
    Escaped executable identifiers, interpolated templates and regex/division
    syntax are outside this grammar: reject rather than infer their meaning.
    """
    tokens = re.findall(
        r"""//[^\r\n\u2028\u2029]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]""",
        content.decode(),
    )
    tokens = [token for token in tokens if not token.startswith(("//", "/*"))]
    references = [
        index for index, token in enumerate(tokens) if token == "DESCRIPTION_OVERRIDES"
    ]
    declarations = [
        index
        for index in references
        if index and tokens[index - 1] in ("const", "let", "var")
    ]
    unsupported = any(
        token in ("\\", "/") or (token.startswith("`") and "${" in token)
        for token in tokens
    )
    valid = len(declarations) == 1 and not unsupported
    if valid:
        declaration = declarations[0]
        tail = tokens[declaration + 1 :]
        # Typed Map initializers are ordinary V1 TypeScript, not evaluated data.
        if tail[:3] == ["=", "new", "Map"]:
            tail = tail[3:]
            if tail[:5] == ["<", "string", ",", "string", ">"]:
                tail = tail[5:]
            if tail[:4] == ["(", "[", "]", ")"]:
                tail = tail[4:]
            elif tail[:2] == ["(", ")"]:
                tail = tail[2:]
            else:
                valid = False
            if tail and tail[0] not in (
                ";",
                "export",
                "const",
                "let",
                "var",
                "function",
                "async",
                "}",
            ):
                valid = False
        else:
            valid = False
        for index in references:
            if index == declaration:
                continue
            use = tokens[index + 1 : index + 4]
            if (
                use[:2] not in ([".", "get"], [".", "has"], [".", "size"])
                or (use[1] != "size" and use[2:] != ["("])
                or (use[1] == "size" and use[2:] in (["="], ["+"], ["-"]))
            ):
                valid = False
    if not valid:
        raise ValueError(
            "Tool audit has active description overrides or a nonliteral override table; review before retiring it"
        )


def local_path(value, source):
    """Resolve a configured local file/directory, never a package basename."""
    plain = value.removeprefix("-")
    if nonlocal_spec(plain):
        return None
    if plain.startswith("file://"):
        parsed = urlparse(plain)
        if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
            raise ValueError("Unsupported local plugin URL; manual port required")
        candidate = Path(unquote(parsed.path))
    elif plain.startswith(("/", "./", "../", "~/")) or (source / plain).exists():
        candidate = Path(plain).expanduser()
        if not candidate.is_absolute():
            candidate = source / candidate
    else:
        return None
    # Check the literal traversal as well as the normalized path: link/../file
    # must not hide a symlink in a configured path or an imported dependency.
    if any(parent.is_symlink() for parent in (candidate, *candidate.parents)):
        raise ValueError("Plugin source must be owned and not symlinked")
    # Normalize dot components without resolving away a symlink.
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(source)
    except ValueError:
        raise ValueError(
            "Local plugin is outside the reviewed config root; manual port required"
        ) from None
    if not relative.parts or any(
        part in ("node_modules", ".git") for part in relative.parts
    ):
        raise ValueError("Local plugin requires a bounded manual port")
    for parent in reversed(candidate.parents):
        if parent == source or source in parent.parents:
            safe_directory(parent)
    if candidate.is_symlink():
        raise ValueError("Plugin source must not be symlinked")
    if not candidate.exists():
        raise ValueError("Local plugin source is missing; manual port required")
    return candidate


def import_targets(content):
    """Conservative literal import inventory, NOT a JavaScript safety parser.

    No code is certified native or SDK-free by this scan. Every preserved code
    file needs byte-bound admission and keeps legacy dependencies, including
    computed imports that this bounded literal inventory cannot resolve.
    """
    tokens = re.findall(
        r"""//[^\r\n\u2028\u2029]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]""",
        content.decode(),
    )
    tokens = [token for token in tokens if not token.startswith(("//", "/*"))]
    for index, token in enumerate(tokens):
        rest = tokens[index + 1 : index + 3]
        literal = None
        if token in ("from", "import") and rest:
            literal = rest[0]
        if token in ("import", "require") and rest[:1] == ["("] and len(rest) > 1:
            literal = rest[1]
        if (
            literal
            and literal.startswith(("'", '"', "`"))
            and literal[-1:] == literal[:1]
        ):
            value = literal[1:-1]
            if value.startswith(("./", "../", "/", "file:")):
                if "\\" in value or "${" in value or "?" in value or "#" in value:
                    raise ValueError("Ambiguous local import requires a manual port")
                yield value


def inspect(source, *, reviewed_sources=None):
    safe_directory(source)
    if (source / "host-bridge.js").exists():
        raise ValueError(
            "This is the old alternate compatibility profile; use the original normal host config directory"
        )
    reviews = validate_reviews(reviewed_sources)
    config, files, unknown = {}, {}, []
    tree, imports, candidates, operations = {}, {}, {}, []
    active_sources, active_packages = set(), set()
    package_references = []
    total_bytes = 0

    def include(path, depth=0):
        nonlocal total_bytes
        relative = path.relative_to(source)
        if depth > MAX_INVENTORY_DEPTH or len(relative.parts) > MAX_INVENTORY_DEPTH:
            raise ValueError(
                "Plugin inventory depth limit exceeded; manual port required"
            )
        if relative in tree:
            return
        if len(tree) >= MAX_INVENTORY_FILES:
            raise ValueError(
                "Plugin inventory file limit exceeded; manual port required"
            )
        if path.is_dir() and not path.is_symlink():
            safe_directory(path)
            children = sorted(path.iterdir())
            tree[relative] = tuple(child.name for child in children)
            for child in children:
                if child.name in ("node_modules", ".git"):
                    raise ValueError(
                        "Nested dependency/checkouts require a manual plugin port"
                    )
                include(child, depth + 1)
            return
        if path.is_symlink():
            raise ValueError("Plugin inventory must be owned and not symlinked")
        tree[relative] = None
        if path.suffix not in CODE_SUFFIXES and path.name != "package.json":
            return
        content = read(path)
        total_bytes += len(content)
        if total_bytes > MAX_INVENTORY_BYTES:
            raise ValueError(
                "Plugin inventory byte limit exceeded; manual port required"
            )
        files[relative] = content
        imports[relative] = set()
        if path.suffix not in CODE_SUFFIXES:
            return
        for target in import_targets(content):
            plain = target.removeprefix("file://")
            candidate = Path(plain)
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            choices = [candidate]
            if not candidate.suffix:
                choices.extend(
                    candidate.with_suffix(suffix) for suffix in sorted(CODE_SUFFIXES)
                )
            dependency = next(
                (
                    choice
                    for choice in choices
                    if choice.exists() or choice.is_symlink()
                ),
                None,
            )
            if dependency is None:
                raise ValueError(
                    "Unresolved local plugin import requires a manual port"
                )
            dependency = local_path(str(dependency), source)
            include(dependency, depth + 1)
            owner = dependency.relative_to(source)
            imports[relative].update(
                item for item in files if item == owner or owner in item.parents
            )

    # Only plugin/ and plugins/ autoload. custom-plugins/ is inventory, not an
    # activation instruction. Nested directories are retained as complete units.
    for folder in ("plugin", "plugins", "custom-plugins"):
        directory = source / folder
        if directory.exists() or directory.is_symlink():
            include(directory)
            if folder != "custom-plugins":
                for child in sorted(directory.iterdir()):
                    name = source_name(child.name)
                    if child.is_file() and child.suffix in (".ts", ".js") and name:
                        candidates[child.relative_to(source)] = name
                        active_sources.add(child.relative_to(source))
    for filename in ("opencode.json", "opencode.jsonc", "config.json"):
        file = source / filename
        if file.is_file():
            content = read(file)
            value = box.parse_jsonc(content.decode())
            if not isinstance(value, dict):
                raise ValueError("Configuration must be an object")
            config[filename] = (content, value)
            for key in ("plugin", "plugins"):
                if not isinstance(value.get(key, []), list):
                    raise ValueError("Plugin configuration must be an array")
                for entry in value.get(key, []):
                    if spec(entry).startswith("-") and not isinstance(entry, str):
                        raise ValueError(
                            "Ambiguous plugin removal entry requires manual review; use a string selector"
                        )
                    # V2 2.2.6 loads opencode.json(c), not legacy config.json.
                    # Retain the latter's source/dependency inventory for V1,
                    # but never turn its dormant registrations into activation.
                    effective = filename != "config.json" and key == (
                        "plugins" if "plugins" in value else "plugin"
                    )
                    target = spec(entry)
                    name = name_for(target, source)
                    package = plugin_compat.package_target(target.removeprefix("-"))
                    if package and package[1] not in ("latest", "0.6.0"):
                        raise ValueError(
                            "This sandbox package version needs review before replacing its implementation: "
                            + target
                        )
                    local = local_path(target, source)
                    if local:
                        if local.is_dir() and name:
                            raise ValueError(
                                "Known configured directory requires a manual port, not a file approval: "
                                + target
                            )
                        include(local)
                        if (
                            local.is_file()
                            and name
                            and effective
                            and not target.startswith("-")
                        ):
                            candidates[local.relative_to(source)] = name
                    elif name and not package:
                        # A directory basename is not implementation identity.
                        # Only bounded standalone files enter files/reviews;
                        # never invent a directory approval or discard its entry.
                        raise ValueError(
                            "Known plugin source is outside the reviewed config root, missing, or a configured directory requiring a manual port: "
                            + target
                        )
                    if local is None:
                        package_references.append(
                            (target.removeprefix("-"), name, bool(package))
                        )
                    if effective:
                        operations.append(
                            (
                                target,
                                name,
                                local.relative_to(source) if local else None,
                                bool(package),
                            )
                        )
    for target, name, relative, package in operations:
        plain = target.removeprefix("-")
        if target.startswith("-"):
            if relative is not None:
                active_sources.discard(relative)
            elif package:
                active_packages.discard(name)
                if plain == name:
                    active_sources.difference_update(
                        path
                        for path in list(active_sources)
                        if candidates[path] == name
                    )
            else:
                active_sources.difference_update(
                    path
                    for path in list(active_sources)
                    if selector_matches(plain, candidates[path])
                )
                active_packages.difference_update(
                    candidate
                    for candidate in list(active_packages)
                    if selector_matches(plain, candidate)
                )
        elif name and relative is not None:
            active_sources.add(relative)
        elif name and package:
            active_packages.add(name)
        else:
            active_sources.update(
                path
                for path, candidate in candidates.items()
                if selector_matches(plain, candidate)
            )
    selected = active_sources
    names = {candidates[path] for path in selected} | active_packages
    preserved_packages = {
        target
        for target, name, package in package_references
        if (package and name not in active_packages)
        or (
            not package
            and target != "*"
            and not target.endswith(".*")
            and not target.startswith("opencode.")
            and not any(
                selector_matches(target, candidate)
                for candidate in set(candidates.values()) | active_packages
            )
        )
    }
    reviewed = set()
    for relative, content in files.items():
        digest = hashlib.sha256(content).hexdigest()
        if (
            relative in selected
            and source_name(relative.name) == "tool-definition-audit"
        ):
            require_empty_audit_overrides(content)
        if relative in selected and reviews.get(relative) != digest:
            raise ValueError(
                "Unrecognized or customized plugin implementation requires review before replacement: "
                + str(relative)
                + "; compare the full source with the native replacement, then pass --reviewed-source RELATIVE_PATH=SHA256 for the reviewed bytes"
            )
        if relative in reviews and reviews[relative] != digest:
            raise ValueError(
                "Source requires review of changed exact bytes: " + str(relative)
            )
        if reviews.get(relative) == digest:
            reviewed.add(relative)
        elif relative not in selected:
            unknown.append(str(relative))
    if set(reviews) - reviewed:
        raise ValueError(
            "Source review approval does not match an inventoried local source"
        )
    preserved = set(files) - selected
    for owner in preserved:
        if imports.get(owner, set()) & selected:
            raise ValueError(
                "Retained plugin code imports a source selected for replacement; manual port required"
            )
    return {
        "config": config,
        "files": files,
        "names": names,
        "unknown": unknown,
        "selected": selected,
        "preserved": preserved,
        "tree": tree,
        "packages": active_packages,
        "preserved_packages": preserved_packages,
    }


def selector_matches(selector, name):
    ids = {name, NATIVE_IDS.get(name, name)}
    return selector == "*" or any(
        selector == identity
        or (selector.endswith(".*") and identity.startswith(selector[:-1]))
        for identity in ids
    )


def audit(source, *, reviewed_sources=None):
    plan = inspect(source, reviewed_sources=reviewed_sources)
    return tuple(plan[key] for key in ("config", "files", "names", "unknown"))


def atomic(path, content):
    with tempfile.NamedTemporaryFile(
        prefix=".native-plugin-", dir=path.parent, delete=False
    ) as output:
        staged = Path(output.name)
        output.write(content)
    try:
        staged.chmod(0o600)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def update_sandbox(source, *, apply_change=False):
    """Update only our recognized native sandbox; refuse customized installs."""
    safe_directory(source)
    safe_directory(source / "plugins")
    safe_directory(source / "plugins/opencode-sandbox")
    receipt_file = source / "native-plugin-migration.json"
    receipt_bytes = read(receipt_file)
    receipt = json.loads(receipt_bytes)
    if (
        not isinstance(receipt, dict)
        or not (
            receipt.get("no_v1_sdk_or_bridge") is True
            or receipt.get("legacy_dependencies_retained") is True
        )
        or "opencode-sandbox" not in receipt.get("native_plugins", [])
    ):
        raise ValueError("A recognized completed native host migration is required")
    installed = source / "plugins/opencode-sandbox/index.js"
    previous = read(installed)
    current = (APP / "tools/host_sandbox.js").read_bytes()
    before, after = (
        hashlib.sha256(previous).hexdigest(),
        hashlib.sha256(current).hexdigest(),
    )
    if (
        before != after
        and before not in PREVIOUS_SANDBOX_HASHES
        and before != receipt.get("sandbox_source_sha256")
    ):
        raise ValueError(
            "Installed sandbox source is customized or unrecognized; no files were replaced"
        )
    manifest = json.loads(read(source / "package.json"))
    if (
        manifest.get("dependencies", {}).get("@anthropic-ai/sandbox-runtime")
        != "0.0.76"
    ):
        raise ValueError(
            "Sandbox runtime dependency changed; review before applying this 0.0.76 lifecycle fix"
        )
    if before == after:
        return {"status": "current", "path": str(installed)}
    if not apply_change:
        return {"status": "planned", "path": str(installed)}
    if read(installed) != previous or read(receipt_file) != receipt_bytes:
        raise ValueError(
            "Sandbox source/receipt changed during inspection; no replacement applied"
        )
    backup = source / (".native-sandbox-update-" + secrets.token_hex(8) + ".js")
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(previous)
    backup.chmod(0o600)
    atomic(installed, current)
    receipt.update(sandbox_source_sha256=after, sandbox_update_backup=str(backup))
    atomic(receipt_file, (json.dumps(receipt, indent=2) + "\n").encode())
    return {"status": "updated", "path": str(installed), "backup": str(backup)}


def apply(source, *, reviewed_sources=None):
    if (source / "native-plugin-migration.json").exists():
        raise ValueError(
            "Native migration receipt already exists; do not overwrite a completed migration"
        )
    plan = inspect(source, reviewed_sources=reviewed_sources)
    configs, files, names, unknown = (
        plan[key] for key in ("config", "files", "names", "unknown")
    )
    active_configs = {
        filename: value
        for filename, value in configs.items()
        if filename != "config.json"
    }
    if unknown:
        raise ValueError(
            "Unknown V1 sources or ambiguous retained code requires review before any migration writes: "
            + ", ".join(unknown)
        )
    if not active_configs:
        raise ValueError(
            "No global configuration found; refusing to invent a replacement"
        )
    kept = [name for name in KEEP if name in names]
    if not kept and not (names & set(RETIRE)):
        raise ValueError("No active recognized plugins found; nothing to port")
    for name in kept:
        if (source / "plugins" / name).exists() or (
            source / "plugins" / name
        ).is_symlink():
            raise ValueError("Native plugin destination already exists: " + name)

    def replacing(item):
        target = spec(item)
        local = local_path(target, source)
        return (
            local.relative_to(source) in plan["selected"]
            if local
            else bool(plugin_compat.package_target(target.removeprefix("-")))
            and "opencode-sandbox" in plan["packages"]
        )

    # Construct config changes before moving any sources. Preserve unrelated
    # settings and the relative order/options of unrelated plugin entries.
    merged = {}
    for _, config in active_configs.values():
        merged.update(config)
    local = {**merged.get("provider", {}), **merged.get("providers", {})}.get(
        "lemonade"
    )
    entries = []
    for name in kept:
        entry = {"package": str(source / "plugins" / name)}
        for _, document in active_configs.values():
            for item in document.get("plugins", document.get("plugin", [])):
                if (
                    replacing(item)
                    and name_for(spec(item), source) == name
                    and not spec(item).startswith("-")
                ):
                    if isinstance(item, list):
                        entry["options"] = item[1]
                    elif isinstance(item, dict) and item.get("options"):
                        entry["options"] = item["options"]
        if name == "lemonade-models":
            options = (local or {}).get("settings", (local or {}).get("options", {}))
            entry["options"] = {
                **entry.get("options", {}),
                "configured": local is not None,
                "baseURL": options.get("baseURL", "{env:LEMONADE_BASE_URL}"),
                "package": (local or {}).get(
                    "package",
                    "aisdk:" + (local or {}).get("npm", "@ai-sdk/openai-compatible"),
                ),
            }
        entries.append(entry)
    replacements = {}
    for filename, (_, original) in active_configs.items():
        config = json.loads(json.dumps(original))
        remaining = [
            entry
            for entry in config.get("plugins", config.get("plugin", []))
            if not replacing(entry)
        ]
        config.pop("plugin", None)
        # A final explicit add must follow wildcard/ID removals that were
        # superseded by a later selection. Disabled sources are never installed.
        removal_before_selection = any(
            spec(item).startswith("-")
            and any(selector_matches(spec(item)[1:], name) for name in kept)
            for item in remaining
        )
        config["plugins"] = (
            (
                [*remaining, *entries]
                if removal_before_selection
                else [*entries, *remaining]
            )
            if filename == list(active_configs)[-1]
            else remaining
        )
        replacements[Path(filename)] = (json.dumps(config, indent=2) + "\n").encode()
    manifest = source / "package.json"
    original_manifest = read(manifest) if manifest.exists() else None
    value = (
        box.parse_jsonc(original_manifest.decode())
        if original_manifest
        else {"private": True}
    )
    dependencies = value.setdefault("dependencies", {})
    # Even SDK-free factories and computed imports can need the V1 SDK. Do not
    # infer their safety from substring absence: preserve dependencies whenever
    # any admitted local code/support/manifest remains outside this replacement.
    if not plan["preserved"] and not plan["preserved_packages"]:
        dependencies.pop("@opencode-ai/plugin", None)
        dependencies.pop("opencode-sandbox", None)
    dependencies["@opencode/plugin"] = "2.2.6"
    if "opencode-sandbox" in kept:
        dependencies["@anthropic-ai/sandbox-runtime"] = "0.0.76"
    no_legacy_code = (
        not plan["preserved"]
        and not plan["preserved_packages"]
        and not any(
            "@opencode-ai/plugin" in value.get(section, {})
            for section in (
                "dependencies",
                "devDependencies",
                "optionalDependencies",
                "peerDependencies",
            )
        )
    )
    replacements[Path("package.json")] = (json.dumps(value, indent=2) + "\n").encode()
    # Bind the entire bounded inventory (including additions/removals), exact
    # config and dependency bytes before creating even the safety snapshot.
    if inspect(source, reviewed_sources=reviewed_sources) != plan:
        raise ValueError("Plugin inventory changed before migration; no writes applied")
    if (read(manifest) if manifest.exists() else None) != original_manifest:
        raise ValueError("Dependencies changed before migration; no writes applied")
    backup = source / (
        ".native-plugin-backup-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        + "-"
        + secrets.token_hex(4)
    )
    backup.mkdir(mode=0o700)
    for relative, content in {
        **files,
        **{Path(name): content for name, (content, _) in configs.items()},
        **({Path("package.json"): original_manifest} if original_manifest else {}),
    }.items():
        target = backup / relative
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o600)
    try:
        (source / "plugins").mkdir(mode=0o700, exist_ok=True)
        for name in kept:
            directory = source / "plugins" / name
            directory.mkdir(mode=0o700)
            native = APP / "tools/native_plugins" / name / "index.js"
            content = (
                (APP / "tools/host_sandbox.js").read_bytes()
                if name == "opencode-sandbox"
                else native.read_bytes()
            )
            (directory / "index.js").write_bytes(content)
            (directory / "index.js").chmod(0o600)
            (directory / "package.json").write_text(
                '{"type":"module","main":"index.js"}\n'
            )
            (directory / "package.json").chmod(0o600)
            for notice in ("LICENSE", "NOTICE"):
                (directory / notice).write_bytes((APP / notice).read_bytes())
                (directory / notice).chmod(0o600)
        # Detect edits since the audit instead of overwriting concurrent work.
        for relative, content in files.items():
            if read(source / relative) != content:
                raise ValueError("Source changed during migration: " + str(relative))
        for filename, (content, _) in configs.items():
            if read(source / filename) != content:
                raise ValueError("Config changed during migration: " + filename)
        if original_manifest is not None and read(manifest) != original_manifest:
            raise ValueError("Dependencies changed during migration")
        for relative in plan["selected"]:
            (source / relative).unlink()
        for relative, content in replacements.items():
            atomic(source / relative, content)
        receipt = {
            "native_plugins": kept,
            "retired": sorted(names & set(RETIRE)),
            "safety_backup": str(backup),
            "version": "2.2.6",
            "no_v1_sdk_or_bridge": no_legacy_code,
            "preserved_sources": sorted(
                str(relative) for relative in plan["preserved"]
            ),
            "preserved_packages": sorted(plan["preserved_packages"]),
            "legacy_dependencies_retained": not no_legacy_code,
            "reviewed_source_sha256": {
                str(relative): hashlib.sha256(content).hexdigest()
                for relative, content in files.items()
            },
        }
        if "opencode-sandbox" in kept:
            receipt["sandbox_source_sha256"] = hashlib.sha256(
                (source / "plugins/opencode-sandbox/index.js").read_bytes()
            ).hexdigest()
        atomic(
            source / "native-plugin-migration.json",
            (json.dumps(receipt, indent=2) + "\n").encode(),
        )
    except BaseException:
        print(
            "Migration did not complete; private safety snapshot: " + str(backup),
            file=sys.stderr,
        )
        # Do not revert possibly concurrent edits or recursively remove paths.
        raise
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "opencode",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="replace recognized host sources/config plugin entries with native V2 implementations",
    )
    parser.add_argument(
        "--update-sandbox",
        action="store_true",
        help="inspect/update only the recognized installed native sandbox lifecycle code; requires --apply to write",
    )
    parser.add_argument(
        "--reviewed-source",
        action="append",
        default=[],
        metavar="RELATIVE_PATH=SHA256",
        help="admit exact reviewed source/manifest bytes (RELATIVE_PATH=SHA256); selected known sources may be replaced, all other inventoried code stays untouched. Review never activates dormant code or certifies compatibility; repeat per file",
    )
    args = parser.parse_args(argv)
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        parser.error(
            "Run this tool on the regular host, not the copied container profile"
        )
    if args.update_sandbox and args.reviewed_source:
        parser.error(
            "--reviewed-source is only for the full source migration, not --update-sandbox"
        )
    try:
        reviews = parse_reviews(args.reviewed_source)
    except ValueError as error:
        parser.error(str(error))
    source = args.config.expanduser().resolve(strict=True)
    if args.update_sandbox:
        result = update_sandbox(source, apply_change=args.apply)
        print("Native sandbox update " + result["status"] + ": " + result["path"])
        if result["status"] == "planned":
            print(
                "No files changed. Use --update-sandbox --apply to install the lifecycle fix."
            )
        elif result["status"] == "updated":
            print("Previous source preserved: " + result["backup"])
            print(
                "Other plugins/config/dependencies are unchanged. Exit the old broken host service once and start a fresh V2 process, then retest /reload."
            )
        return 0
    _, _, names, unknown = audit(source, reviewed_sources=reviews)
    print("Normal host profile: " + str(source))
    for name in KEEP:
        if name in names:
            print(name + ": " + KEEP[name])
    for name in RETIRE:
        if name in names:
            print(name + ": " + RETIRE[name])
    if unknown:
        print("Manual V1 review required: " + ", ".join(unknown))
    if not args.apply:
        print("Audit only; nothing changed. --apply performs the V2-only replacement.")
        return 0
    result = apply(source, reviewed_sources=reviews)
    print("Native V2 plugins installed; safety snapshot: " + result["safety_backup"])
    if result["legacy_dependencies_retained"]:
        print(
            "Other local sources/dependencies were preserved; byte-bound review is not a V2 compatibility certification."
        )
    print(
        "Refresh host dependencies: npm install --prefix "
        + shlex.quote(str(source))
        + " --no-audit --no-fund"
    )
    print(
        "Use normal opencode/opencode2 --standalone after sourcing your env file. No alternate profile launcher is needed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
