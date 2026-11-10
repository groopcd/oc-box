"""Container-only config refresh; retain installed dependencies when unchanged."""

import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile


LOCKFILES = ("package-lock.json", "bun.lock", "bun.lockb")
DEPENDENCY_FILES = ("package.json", *LOCKFILES)
PLUGIN_PACKAGE = "@opencode-ai/plugin"


def input_hash(dependencies, runtime_version):
    return hashlib.sha256(
        json.dumps(
            {
                "runtime_version": runtime_version,
                "files": {
                    name: hashlib.sha256(value).hexdigest()
                    for name, value in dependencies.items()
                },
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def installed_hash(config):
    files = {}
    for name in DEPENDENCY_FILES:
        path = config / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return None
        files[name] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        )
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def write_state(config, value):
    target = config.parent / ".oc-box-dependencies.json"
    fd, name = tempfile.mkstemp(prefix=".oc-box-dependencies-", dir=config.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def members_and_dependencies(archive):
    members = archive.getmembers()
    names = set()
    for member in members:
        name = PurePosixPath(member.name)
        if (
            not member.isfile()
            or name.is_absolute()
            or ".." in name.parts
            or "node_modules" in name.parts
            or str(name) != member.name
            or member.name in names
        ):
            raise ValueError("Unsafe config archive entry")
        names.add(member.name)
    return members, {
        member.name: archive.extractfile(member).read()
        for member in members
        if member.name in DEPENDENCY_FILES
    }


def complete_config(home, payload, runtime_version=None):
    """Acknowledge this exact input only after the caller's installer exits zero."""
    config = Path(home) / ".config/opencode"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        _, dependencies = members_and_dependencies(archive)
    source_hash = input_hash(dependencies, runtime_version)
    state = read_json(config.parent / ".oc-box-dependencies.json")
    modules = config / "node_modules"
    if (
        not isinstance(state, dict)
        or type(state.get("version")) is not int
        or state["version"] != 2
        or state.get("status") != "pending"
        or state.get("source_hash") != source_hash
        or state.get("runtime_version") != runtime_version
        or config.is_symlink()
        or not modules.is_dir()
        or modules.is_symlink()
    ):
        raise ValueError("Dependency completion does not match pending configuration")
    signature = installed_hash(config)
    if signature is None:
        raise ValueError("Invalid installed dependency files")
    write_state(
        config,
        {**state, "status": "ready", "proof": "install", "installed_hash": signature},
    )


def package_files_complete(package, modules, *, require_code=False):
    """Check bounded declared entrypoints, including transitive package code."""
    manifest = read_json(package / "package.json")
    if not isinstance(manifest, dict):
        return False
    default = "./index.js" if (package / "index.js").is_file() else "./**/*.d.ts"
    entries = [
        manifest[key]
        for key in ("main", "module", "types", "typings")
        if key in manifest
    ]
    exports = manifest.get("exports", entries or default)
    files = []

    def collect(value, depth=0):
        if depth > 8 or len(files) > 128:
            return False
        if isinstance(value, str):
            files.append(value)
            return True
        if value is None:
            return True  # Explicitly private/unexported subpaths.
        if isinstance(value, dict) and value:
            # Published packages can advertise unpublished source/dev branches.
            # Check native JS/typing conditions, not arbitrary custom conditions.
            active = {
                "node",
                "node-addons",
                "import",
                "require",
                "default",
                "types",
                "bun",
                "module-sync",
            }
            return all(
                collect(item, depth + 1)
                for key, item in value.items()
                if key.startswith(".") or key in active
            )
        if isinstance(value, list) and value:
            return all(collect(item, depth + 1) for item in value)
        return False

    if not collect(exports) or not files or len(files) > 128:
        return False
    executable = False
    try:
        for name in files:
            relative = PurePosixPath(name)
            if relative.is_absolute() or ".." in relative.parts:
                return False
            paths = package.glob(name) if "*" in name else iter([package / relative])
            count = 0
            for path in paths:
                count += 1
                if (
                    count > 2048
                    or not path.resolve().is_relative_to(modules.resolve())
                    or not path.is_file()
                ):
                    return False
                # Empty JS stubs are legitimate in type/native-only packages;
                # the SDK itself still needs at least one real code entrypoint.
                executable |= (
                    path.suffix in (".js", ".mjs", ".cjs") and path.stat().st_size > 0
                )
            if not count:
                return False
        return executable or not require_code
    except (OSError, ValueError, RuntimeError):
        return False


def sdk_complete(config, plugin_package, runtime_version):
    """Legacy-only structural proof; inspect files, never execute package code."""
    modules = config / "node_modules"
    package = modules / plugin_package
    manifest = read_json(package / "package.json")
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != plugin_package
        or manifest.get("version") != runtime_version
        or not package_files_complete(package, modules, require_code=True)
    ):
        return False
    try:
        result = subprocess.run(
            [
                "npm",
                "ls",
                "--omit=dev",
                "--all",
                "--long",
                "--offline",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--json",
                "--prefix",
                str(config),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(config.parent.parent),
                "npm_config_userconfig": os.devnull,
                "npm_config_update_notifier": "false",
            },
        )
        if result.returncode:
            return False
        tree = json.loads(result.stdout)
        seen = set()

        def complete(node, depth=0):
            if not isinstance(node, dict) or depth > 32 or len(seen) > 512:
                return False
            manifest = {}
            if depth:
                if not isinstance(node.get("path"), str):
                    return False
                path = Path(node["path"]).resolve()
                if not path.is_relative_to(modules.resolve()):
                    return False
                if path in seen:
                    return True
                if len(seen) >= 512:
                    return False
                seen.add(path)
                if not package_files_complete(path, modules):
                    return False
                manifest = read_json(path / "package.json")
                if not isinstance(manifest, dict):
                    return False
            dependencies = node.get("dependencies", {})
            optional = manifest.get("optionalDependencies", {})
            optional = set(optional) if isinstance(optional, dict) else set()
            peers = manifest.get("peerDependenciesMeta", {})
            if isinstance(peers, dict):
                optional.update(
                    name
                    for name, info in peers.items()
                    if isinstance(info, dict) and info.get("optional") is True
                )
            return isinstance(dependencies, dict) and all(
                (child == {} and name in optional) or complete(child, depth + 1)
                for name, child in dependencies.items()
            )

        return (
            isinstance(tree, dict)
            and isinstance(tree.get("dependencies"), dict)
            and plugin_package in tree["dependencies"]
            and complete(tree)
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def sdk_only_manifest(manifest, plugin_package=PLUGIN_PACKAGE):
    """True only for the dependency-only manifest generated by OpenCode."""
    return (
        isinstance(manifest, dict)
        and set(manifest) == {"dependencies"}
        and isinstance(manifest["dependencies"], dict)
        and set(manifest["dependencies"]) == {plugin_package}
    )


def sync_config(home, payload, runtime_version=None):
    plugin_package = (
        "@opencode/plugin"
        if runtime_version and runtime_version.startswith("2.")
        else PLUGIN_PACKAGE
    )
    config = Path(home) / ".config/opencode"
    config.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members, dependencies = members_and_dependencies(archive)
        manifest = (
            json.loads(dependencies["package.json"])
            if "package.json" in dependencies
            else None
        )
        previous = read_json(config / "package.json")
        # Compare with the last HOST input, not files that OpenCode legitimately
        # rewrites to match its running SDK. Otherwise a stale host SDK version
        # forces a reinstall on every launch (e.g. host 1.15.11 / runtime 1.19.5).
        source_hash = input_hash(dependencies, runtime_version)
        state_file = config.parent / ".oc-box-dependencies.json"
        state = read_json(state_file)
        signature = installed_hash(config)
        unchanged_source = (
            isinstance(state, dict)
            and type(state.get("version")) is int
            and state["version"] == 2
            and state.get("status") == "ready"
            and state.get("proof") in ("install", "sdk-only")
            and state.get("source_hash") == source_hash
            and state.get("runtime_version") == runtime_version
            and signature is not None
            and state.get("installed_hash") == signature
            and not state_file.is_symlink()
        )
        # Upgrade an already bootstrapped workspace without one final reinstall.
        # This applies ONLY to OpenCode's SDK-only generated manifest; custom
        # dependency sets require an actual completed-install receipt. Neither
        # matching files nor the old input-only state proves installation.
        legacy = not state_file.exists() or (
            isinstance(state, dict)
            and set(state) == {"source_hash", "runtime_version"}
            and isinstance(state["source_hash"], str)
            and len(state["source_hash"]) == 64
            and all(char in "0123456789abcdef" for char in state["source_hash"])
            and (
                state["runtime_version"] is None
                or isinstance(state["runtime_version"], str)
            )
        )
        adopt_generated = (
            legacy
            and not state_file.is_symlink()
            and not config.is_symlink()
            and runtime_version is not None
            and (manifest is None or sdk_only_manifest(manifest, plugin_package))
            and (manifest is not None or not dependencies)
            and sdk_only_manifest(previous, plugin_package)
            and previous["dependencies"][plugin_package] == runtime_version
            and (config / "node_modules").is_dir()
            and not (config / "node_modules").is_symlink()
            and sdk_complete(config, plugin_package, runtime_version)
        )
        with tempfile.TemporaryDirectory(
            prefix=".oc-box-config-", dir=config.parent
        ) as temp:
            cached = Path(temp) / "node_modules"
            modules = config / "node_modules"
            preserve = (
                (unchanged_source or adopt_generated)
                and modules.is_dir()
                and not modules.is_symlink()
                and not config.is_symlink()
            )
            generated = {
                name: (config / name).read_bytes()
                for name in DEPENDENCY_FILES
                if preserve and (config / name).is_file()
            }
            if preserve:
                modules.rename(cached)
            try:
                if config.is_symlink():
                    config.unlink()
                elif config.exists():
                    shutil.rmtree(config)
                config.mkdir(mode=0o700)
                for member in members:
                    if preserve and member.name in DEPENDENCY_FILES:
                        continue
                    target = config / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.extractfile(member) as source,
                        target.open("wb") as destination,
                    ):
                        shutil.copyfileobj(source, destination)
                    target.chmod(member.mode & 0o777)
                for name, content in generated.items():
                    (config / name).write_bytes(content)
                if not preserve and runtime_version is not None:
                    # The host manifest may have been generated by an older
                    # OpenCode. Start at the current SDK immediately, retaining
                    # all user-declared dependencies. Mainline need not first
                    # reconcile a stale package-lock from the host installation.
                    effective = (
                        json.loads(json.dumps(manifest)) if manifest is not None else {}
                    )
                    effective.setdefault("dependencies", {})[plugin_package] = (
                        runtime_version
                    )
                    (config / "package.json").write_text(
                        json.dumps(effective, indent=2) + "\n"
                    )
                    if (
                        manifest is None
                        or manifest.get("dependencies", {}).get(plugin_package)
                        != runtime_version
                    ):
                        for name in LOCKFILES:
                            (config / name).unlink(missing_ok=True)
            finally:
                if cached.exists():
                    config.mkdir(parents=True, exist_ok=True)
                    cached.rename(config / "node_modules")
        # A copied config is pending, not a successful dependency installation.
        value = {
            "version": 2,
            "source_hash": source_hash,
            "runtime_version": runtime_version,
            "status": "pending",
        }
        if preserve:
            value.update(
                status="ready",
                proof=state["proof"] if unchanged_source else "sdk-only",
                installed_hash=installed_hash(config),
            )
        write_state(config, value)
        return preserve


if __name__ == "__main__":
    action = (
        complete_config
        if len(sys.argv) > 2 and sys.argv[2] == "--complete"
        else sync_config
    )
    reused = action(
        os.environ["HOME"],
        sys.stdin.buffer.read(),
        sys.argv[1] if len(sys.argv) > 1 else None,
    )
    print(
        "Container plugin dependencies resolved."
        if action is complete_config
        else "Reusing container plugin dependencies."
        if reused
        else "Plugin dependencies will be resolved by OpenCode if needed."
    )
