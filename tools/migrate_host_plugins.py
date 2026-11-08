#!/usr/bin/env python3
"""Prepare a separate native OpenCode V2 host profile, without editing originals.

Default: print a names-only plan. --apply creates a private profile and launcher;
it never starts OpenCode, npm, models, services or sandbox processes. Supported
local V1 sources use the tested oc-box plugin bridge; host sandboxing uses a
dedicated native V2 port, never the container opt-out adapter. Original profile
and databases are untouched. Delete nothing to roll back: use ordinary opencode.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "src"))
from oc_box import oc_box as box, plugin_compat

MAX_FILE = 8 * 1024 * 1024
EXTENSIONS = (".ts", ".js", ".mts", ".mjs")


def bytes_at(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise ValueError("Expected bounded regular config/source file: " + str(path))
    return path.read_bytes()


def target(entry):
    if isinstance(entry, str):
        return entry
    if (
        isinstance(entry, list)
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], dict)
    ):
        return entry[0]
    if isinstance(entry, dict) and isinstance(entry.get("package"), str):
        return entry["package"]
    raise ValueError(
        "Unsupported plugin entry; inspect your config without exposing credential values"
    )


def discover(source):
    candidates, configs = set(), {}
    sandbox = False
    for name in ("opencode.json", "opencode.jsonc", "config.json"):
        file = source / name
        if not file.exists():
            continue
        config = box.parse_jsonc(bytes_at(file).decode())
        if not isinstance(config, dict):
            raise ValueError("Config must be an object: " + name)
        configs[name] = config
        for key in ("plugin", "plugins"):
            for entry in config.get(key, []):
                spec = target(entry)
                if plugin_compat.package_target(spec):
                    sandbox = True
                    continue
                file = plugin_compat.local_source(spec, source)
                if file:
                    candidates.add(file)
                elif (
                    spec.startswith(("/", "./", "../", "~/", "file:"))
                    and not (source / spec.removeprefix("-")).is_dir()
                ):
                    raise ValueError(
                        "Configured source is missing or outside the profile; cannot migrate safely: "
                        + spec
                    )
    for folder in ("plugin", "plugins"):
        candidates.update(
            file
            for file in (source / folder).glob("*")
            if file.is_file() and file.suffix in EXTENSIONS
        )
    # Handle the already-adapted copies visible inside an oc-box launch. On a
    # regular host, original .ts/.js sources are used instead.
    for folder in ("plugin", "plugins", "custom-plugins"):
        candidates.update(file for file in (source / folder).glob("*.oc-box-v1.mts"))
    sources = {}
    for file in sorted(candidates):
        text = bytes_at(file).decode()
        native = "@opencode/plugin" in text or bool(
            re.search(r"export\s+default\s*\{", text)
        )
        if not native and ('"event"' in text or re.search(r"\bevent\s*:", text)):
            raise ValueError(
                "This local plugin uses an unbridged event hook and needs a native port: "
                + str(file)
            )
        relative = file.relative_to(source)
        sources[relative] = bytes_at(file)
    return configs, sources, sandbox


def write_file(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        stream.write(content)
    path.chmod(mode)


def make_profile(source, output=None):
    configs, sources, sandbox = discover(source)
    files = {}
    excluded = {"node_modules", ".git", "package-lock.json", "bun.lock", "bun.lockb"}
    for file in source.rglob("*"):
        relative = file.relative_to(source)
        if (
            set(relative.parts) & excluded
            or relative in sources
            or relative.name.startswith("service")
            or relative.name in ("auth.json", "mcp-auth.json", ".env", ".npmrc")
            or (relative == Path("package.json"))
            or relative.name in configs
        ):
            continue
        if any(
            part == "oc-box-build-permissions" or part.startswith("oc-box-compat-")
            for part in relative.parts
        ) or relative.name in (
            "oc-box-profile.json",
            "oc-box-v1-bridge.js",
            "oc-box-v1-provider-config.json",
        ):
            continue
        if file.is_symlink():
            raise ValueError(
                "Symlinked configuration needs manual review: " + str(file)
            )
        if file.is_file():
            files[relative] = bytes_at(file)
    if output is None:
        root = Path(tempfile.mkdtemp(prefix="opencode-host-v2-", dir=source.parent))
    else:
        root = output.expanduser().resolve(strict=False)
        if root.is_relative_to(source) or source.is_relative_to(root):
            raise ValueError(
                "The V2 profile must be separate from the original config directory"
            )
        root.mkdir(mode=0o700)  # never replace an existing profile
    root.chmod(0o700)
    destination = root / "opencode"
    destination.mkdir(mode=0o700)
    replacements = {}
    saved_sources = set()
    for relative, content in sources.items():
        identity = hashlib.sha256(str(relative).encode()).hexdigest()[:16]
        directory = destination / "plugins" / ("host-compat-" + identity)
        name = relative.name.replace(".oc-box-v1.mts", ".ts")
        saved = (
            destination
            / relative.parent
            / (
                Path(name).stem
                + ".host-v1"
                + (".mjs" if relative.suffix in (".js", ".mjs") else ".mts")
            )
        )
        if saved.relative_to(destination) in files or saved in saved_sources:
            raise ValueError(
                "Generated plugin module conflicts with a supporting file; private profile retained: "
                + str(saved)
            )
        saved_sources.add(saved)
        corrected = plugin_compat.copied_source(relative.with_name(name), content)
        write_file(saved, corrected)
        original_uri = saved.as_uri()
        script = (
            'import { adapt } from "../../host-bridge.js"\nimport * as original from '
            + json.dumps(original_uri)
            + "\nexport default adapt(original, "
            + json.dumps("oc-box.host." + identity)
            + ")\n"
        )
        write_file(directory / "index.js", script.encode())
        write_file(directory / "package.json", b'{"type":"module","main":"index.js"}')
        for alias in (
            str(source / relative),
            (source / relative).as_uri(),
            "./" + str(relative),
            str(relative),
        ):
            replacements[alias] = str(directory)
    # Copy supporting config files, preserving source-relative layout. No
    # node_modules/cache, auth/database or private runtime service registration.
    for relative, content in files.items():
        executable = (source / relative).stat().st_mode & 0o111
        write_file(destination / relative, content, 0o700 if executable else 0o600)
    native_sandbox = str(destination / "plugins/host-sandbox")
    if sandbox:
        write_file(
            destination / "plugins/host-sandbox/index.js",
            (APP / "tools/host_sandbox.js").read_bytes(),
        )
        write_file(
            destination / "plugins/host-sandbox/package.json",
            b'{"type":"module","main":"index.js"}',
        )
    for name, original in configs.items():
        config = json.loads(json.dumps(original))
        # V2 shape only for plugin entries; all unrelated settings are retained.
        entries = config.get("plugins", config.get("plugin", []))
        config.pop("plugin", None)
        migrated = []
        for entry in entries:
            spec = target(entry)
            removed = spec.startswith("-")
            plain = spec.removeprefix("-")
            resolved = plugin_compat.local_source(plain, source)
            replacement = replacements.get(plain)
            if resolved is not None:
                replacement = replacements.get(str(resolved))
            if plugin_compat.package_target(plain):
                replacement = native_sandbox
            local_directory = Path(plain.removeprefix("file://")).expanduser()
            if not local_directory.is_absolute():
                local_directory = source / local_directory
            if replacement is None and local_directory.is_dir():
                try:
                    relative = local_directory.resolve().relative_to(source)
                    replacement = str(destination / relative)
                except ValueError:
                    pass
            if "oc-box-compat" in plain:
                # When invoked on a copied container profile, wrappers reference
                # non-autoloaded modules we repackaged above; remove old entries.
                continue
            replacement = replacement or plain
            if removed:
                migrated.append("-" + replacement)
            elif isinstance(entry, str):
                migrated.append(replacement)
            else:
                migrated.append(
                    {
                        "package": replacement,
                        "options": entry[1]
                        if isinstance(entry, list)
                        else entry.get("options", {}),
                    }
                )
        config["plugins"] = migrated
        write_file(destination / name, (json.dumps(config, indent=2) + "\n").encode())
    bridge = (box.PACKAGE / "plugin_bridge.js").read_bytes()
    bridge = bridge.replace(
        b"./oc-box-v1-provider-config.json", b"./host-providers.json"
    )
    write_file(destination / "host-bridge.js", bridge)
    # Discovery only needs endpoint/model inventory. Never duplicate provider
    # API keys or arbitrary request headers into this helper's snapshot.
    providers = {}
    for config in configs.values():
        for ident, provider in config.get(
            "providers", config.get("provider", {})
        ).items():
            options = provider.get("settings", provider.get("options", {}))
            providers[ident] = {
                key: provider[key]
                for key in ("name", "package", "npm", "models")
                if key in provider
            }
            providers[ident]["options"] = (
                {"baseURL": options["baseURL"]} if "baseURL" in options else {}
            )
    write_file(
        destination / "host-providers.json",
        json.dumps({"provider": providers}).encode(),
    )
    manifest = {
        "private": True,
        "type": "module",
        "dependencies": {"@opencode/plugin": "2.2.6", "@opencode-ai/plugin": "1.19.5"},
    }
    if (source / "package.json").is_file():
        old = box.parse_jsonc(bytes_at(source / "package.json").decode())
        manifest["dependencies"] = {
            **old.get("dependencies", {}),
            **manifest["dependencies"],
        }
    if sandbox:
        manifest["dependencies"].pop("opencode-sandbox", None)
        manifest["dependencies"]["@anthropic-ai/sandbox-runtime"] = "0.0.76"
    write_file(
        destination / "package.json", (json.dumps(manifest, indent=2) + "\n").encode()
    )
    # Other XDG consumers (including sandbox policy) use the same directories
    # as before. Only the OpenCode config directory is the independent snapshot.
    for sibling in source.parent.iterdir():
        if (
            sibling == source
            or sibling == root
            or sibling.name.startswith("opencode-host-v2-")
            or sibling.name in ("run-opencode", "migration.json")
        ):
            continue
        (root / sibling.name).symlink_to(
            sibling.resolve(), target_is_directory=sibling.is_dir()
        )
    launcher = (
        "#!"
        + sys.executable
        + "\nimport os,sys,subprocess,re\n"
        + "if os.environ.get('OC_BOX_CONTAINER')=='1': sys.exit('Run this launcher on the host, not in oc-box')\n"
        + "release=subprocess.check_output(['opencode','--version'],text=True,timeout=15).strip()\n"
        + "if not re.fullmatch(r'(?:opencode\\s+)?v?2\\.\\d+\\.\\d+(?:[-+][A-Za-z0-9._-]+)?',release): sys.exit('This separate profile requires OpenCode 2.x')\n"
        + 'os.environ["XDG_CONFIG_HOME"]='
        + repr(str(root))
        + "\n"
        + "if os.environ.get('OPENCODE_CONFIG'): sys.exit('Unset OPENCODE_CONFIG before using the separate host profile; preserve its file for manual review')\n"
        + 'os.execvp("opencode",["opencode","--standalone",*sys.argv[1:]])\n'
    )
    write_file(root / "run-opencode", launcher.encode(), 0o700)
    for name in ("LICENSE", "NOTICE"):
        write_file(root / name, (APP / name).read_bytes())
    receipt = {
        "source": str(source),
        "plugins": [str(relative) for relative in sources],
        "host_sandbox": sandbox,
        "limits": "Separate V2 config snapshot; originals/database untouched. Sandbox isolation needs host validation. No generated Build permission override.",
    }
    write_file(root / "migration.json", (json.dumps(receipt, indent=2) + "\n").encode())
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "opencode",
    )
    parser.add_argument(
        "--output", type=Path, help="new private V2 profile root; must not exist"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create the separate V2 profile; default reports names only",
    )
    args = parser.parse_args(argv)
    if os.environ.get("OC_BOX_CONTAINER") == "1":
        parser.error(
            "Run this script from the regular host terminal; do not migrate the copied container configuration"
        )
    source = args.config.expanduser().resolve(strict=True)
    _, sources, sandbox = discover(source)
    print("Source configuration (unchanged): " + str(source))
    for relative in sources:
        print("V2 adapter: " + str(relative))
    print(
        "Host sandbox: "
        + (
            "native V2 port; existing policy retained, default fail-closed"
            if sandbox
            else "no sandbox package configured"
        )
    )
    print(
        "Unrelated settings are copied, never rewritten; auth/databases and original files are untouched."
    )
    if not args.apply:
        print(
            "Dry run only. Use --apply to create a separate profile; originals stay in place."
        )
        return 0
    root = make_profile(source, args.output)
    print("Created independent V2 profile: " + str(root))
    print(
        "Install its isolated Node dependencies manually:\nnpm install --prefix "
        + shlex.quote(str(root / "opencode"))
        + " --no-audit --no-fund"
    )
    print(
        "Launch after sourcing your normal env file:\n"
        + shlex.quote(str(root / "run-opencode"))
    )
    print(
        "Rollback: exit this profile and run ordinary opencode; no original config was changed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
