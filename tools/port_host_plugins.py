#!/usr/bin/env python3
"""Audit and explicitly port known personal plugins in the normal HOST V2 config.

No V1 SDK/bridge/profile launcher is installed. Default is a read-only audit;
--apply installs native entrypoint directories and retires obsolete sources to
one private safety snapshot. It changes only plugin entries/dependencies and
recognized source files; no auth/DB/env/service operation runs. Unknown plugins
are left alone, not classified as safe to delete.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import oc_box as box, plugin_compat

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
# Exact previously-generated native sandbox sources, not arbitrary user code.
PREVIOUS_SANDBOX_HASHES = {
    "4659614bbc1048d385647937d617fa24bfaa1f7f0341a9a2041ef67eaa90bf7a",
    "60fc6c0d1f24abdf80ca03b0eecd01f7f6af3ade28e4a641a965828a6038ff62",
}
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


def name_for(value):
    stem = Path(value.removeprefix("-").removeprefix("file://")).name
    for ending in (".oc-box-v1.mts", ".ts", ".js", ".mts", ".mjs"):
        if stem.endswith(ending):
            stem = stem[: -len(ending)]
            break
    return (
        stem
        if stem in KEEP or stem in RETIRE
        else "opencode-sandbox"
        if plugin_compat.package_target(value.removeprefix("-"))
        else None
    )


def audit(source):
    safe_directory(source)
    if (source / "host-bridge.js").exists():
        raise ValueError(
            "This is the old alternate compatibility profile; use the original normal host config directory"
        )
    config, files, names, unknown = {}, {}, set(), []
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
                    target = spec(entry)
                    name = name_for(target)
                    package = plugin_compat.package_target(target.removeprefix("-"))
                    if package and package[1] not in ("latest", "0.6.0"):
                        raise ValueError(
                            "This sandbox package version needs review before replacing its implementation: "
                            + target
                        )
                    if name and not target.startswith("-"):
                        names.add(name)
                    local = plugin_compat.local_source(target, source)
                    if local:
                        files[local.relative_to(source)] = read(local)
                        if (
                            name_for(local.name) is None
                            and "@opencode-ai/plugin"
                            in files[local.relative_to(source)].decode()
                        ):
                            unknown.append(str(local.relative_to(source)))
                    elif name and target.startswith(("/", "./", "../", "~/", "file:")):
                        directory = Path(
                            target.removeprefix("-").removeprefix("file://")
                        ).expanduser()
                        if not directory.is_absolute():
                            directory = source / directory
                        if (
                            not directory.is_dir()
                            or not directory.resolve().is_relative_to(source)
                        ):
                            raise ValueError(
                                "Known plugin source is outside the reviewed config root or missing: "
                                + target
                            )
    for folder in ("plugins", "plugin", "custom-plugins"):
        directory = source / folder
        if directory.exists() or directory.is_symlink():
            safe_directory(directory)
        for file in (source / folder).glob("*"):
            if file.is_file() and file.suffix in (".ts", ".js", ".mts", ".mjs"):
                name = name_for(file.name)
                if name:
                    names.add(name)
                    files[file.relative_to(source)] = read(file)
                elif "@opencode-ai/plugin" in read(file).decode():
                    unknown.append(str(file.relative_to(source)))
    for relative, content in files.items():
        name = name_for(relative.name)
        if name in MARKERS and not all(
            marker in content.decode() for marker in MARKERS[name]
        ):
            raise ValueError(
                "Unrecognized or customized plugin implementation requires review before replacement: "
                + str(relative)
            )
        if name == "tool-definition-audit":
            # Retiring an audit with real edits would remove functionality, not
            # mere diagnostics. Refuse customized override tables for review.
            text = content.decode()
            table = text.split("DESCRIPTION_OVERRIDES", 1)[-1].split("])", 1)[0]
            table = "\n".join(
                line for line in table.splitlines() if not line.strip().startswith("//")
            )
            if '["' in table or "['" in table:
                raise ValueError(
                    "Tool audit has active description overrides; review before retiring it"
                )
    return config, files, names, unknown


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
        or receipt.get("no_v1_sdk_or_bridge") is not True
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


def apply(source):
    if (source / "native-plugin-migration.json").exists():
        raise ValueError(
            "Native migration receipt already exists; do not overwrite a completed migration"
        )
    configs, files, names, unknown = audit(source)
    if unknown:
        raise ValueError(
            "Unknown V1 sources require manual review before SDK removal: "
            + ", ".join(unknown)
        )
    if not configs:
        raise ValueError(
            "No global configuration found; refusing to invent a replacement"
        )
    kept = [name for name in KEEP if name in names]
    if not kept and not (names & set(RETIRE)):
        raise ValueError("No recognized plugins found; nothing to port")
    for name in kept:
        if (source / "plugins" / name).exists() or (
            source / "plugins" / name
        ).is_symlink():
            raise ValueError("Native plugin destination already exists: " + name)
    # Construct config changes before moving any sources. Preserve unrelated
    # settings and the relative order/options of unrelated plugin entries.
    merged = {}
    for _, config in configs.values():
        merged.update(config)
    local = {**merged.get("provider", {}), **merged.get("providers", {})}.get(
        "lemonade"
    )
    entries = []
    for name in kept:
        entry = {"package": str(source / "plugins" / name)}
        for _, document in configs.values():
            for item in document.get("plugins", document.get("plugin", [])):
                if name_for(spec(item)) == name and not spec(item).startswith("-"):
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
    for filename, (_, original) in configs.items():
        config = json.loads(json.dumps(original))
        remaining = [
            entry
            for entry in config.get("plugins", config.get("plugin", []))
            if name_for(spec(entry)) is None
        ]
        disabled = [
            "-" + str(source / "plugins" / name_for(spec(item)))
            for item in config.get("plugins", config.get("plugin", []))
            if spec(item).startswith("-") and name_for(spec(item)) in kept
        ]
        config.pop("plugin", None)
        config["plugins"] = (
            [*entries, *remaining, *disabled]
            if filename == list(configs)[-1]
            else [*remaining, *disabled]
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
    dependencies.pop("@opencode-ai/plugin", None)
    dependencies.pop("opencode-sandbox", None)
    dependencies["@opencode/plugin"] = "2.2.6"
    if "opencode-sandbox" in kept:
        dependencies["@anthropic-ai/sandbox-runtime"] = "0.0.76"
    replacements[Path("package.json")] = (json.dumps(value, indent=2) + "\n").encode()
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
        for relative in files:
            if name_for(relative.name):
                (source / relative).unlink()
        for relative, content in replacements.items():
            atomic(source / relative, content)
        receipt = {
            "native_plugins": kept,
            "retired": sorted(names & set(RETIRE)),
            "safety_backup": str(backup),
            "version": "2.2.6",
            "no_v1_sdk_or_bridge": True,
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
    args = parser.parse_args(argv)
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        parser.error(
            "Run this tool on the regular host, not the copied container profile"
        )
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
    _, _, names, unknown = audit(source)
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
    result = apply(source)
    print("Native V2 plugins installed; safety snapshot: " + result["safety_backup"])
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
