"""Container-only config refresh; retain installed dependencies when unchanged."""

import io
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile
import tempfile


LOCKFILES = ("package-lock.json", "bun.lock", "bun.lockb")
DEPENDENCY_FILES = ("package.json", *LOCKFILES)
PLUGIN_PACKAGE = "@opencode-ai/plugin"
V2_PLUGIN_PACKAGE = "@opencode/plugin"


def sdk_security_overrides(runtime_version):
    # Apply the advisories to the selected V2 SDK's dependency ranges, not to
    # a list of SDK releases or to unrelated top-level packages. Exact newer
    # fixed requirements do not match these selectors.
    # GHSA-3g3m-2fjr-6pw4 and GHSA-g3hv-75wg-m249.
    if isinstance(runtime_version, str) and re.fullmatch(
        r"2\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?", runtime_version
    ):
        return {
            f"@opencode/plugin@{runtime_version}": {
                "glob@>=9.0.0 <14.0.2": "14.0.2",
                "minimatch@>=7.0.0 <11.0.1": "11.0.1",
            }
        }
    return {}


def runtime_manifest(manifest, package, version, security_overrides):
    effective = json.loads(json.dumps(manifest)) if manifest is not None else {}
    effective.setdefault("dependencies", {})[package] = version
    if security_overrides:
        overrides = effective.get("overrides", {})
        # npm selects matching ancestors in insertion order. Put the exact SDK
        # selector ahead of broader rules.
        effective["overrides"] = {**security_overrides, **overrides}
        for selector, fixes in security_overrides.items():
            scoped = overrides.get(selector, {})
            if not isinstance(scoped, dict) or any(
                name in scoped and scoped[name] != fixed
                for name, fixed in fixes.items()
            ):
                raise ValueError("Conflicting OpenCode SDK security override.")
            effective["overrides"][selector] = {**scoped, **fixes}
    return effective


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def sdk_lock_packages(lockfile, version):
    """Verified lock inventory, or None for unknown/affected resolutions."""
    lock = read_json(lockfile)
    if not isinstance(lock, dict) or lock.get("lockfileVersion") not in (2, 3):
        return None
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        return None
    sdk = packages.get(f"node_modules/{V2_PLUGIN_PACKAGE}")
    if not isinstance(sdk, dict) or sdk.get("version") != version:
        return None
    for location, item in packages.items():
        path = PurePosixPath(location)
        if not isinstance(item, dict) or path.is_absolute() or ".." in path.parts:
            return None
        names = sdk_lock_names(location, item)
        if not names & {"glob", "minimatch"}:
            continue
        release = item.get("version")
        # Refuse unknown/prerelease identities rather than treating them as fixed.
        if not isinstance(release, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", release
        ):
            return None
        triplet = tuple(map(int, release.split(".")))
        if "glob" in names and (9, 0, 0) <= triplet < (14, 0, 2):
            return None
        if "minimatch" in names and (7, 0, 0) <= triplet < (11, 0, 1):
            return None
    return packages


def sdk_lock_names(location, item):
    names = {PurePosixPath(location).name}
    if isinstance(item.get("name"), str):
        names.add(item["name"])
    return names


def sdk_lock_safe(lockfile, version):
    return sdk_lock_packages(lockfile, version) is not None


def sdk_modules_match(config, version):
    """Check npm's installed inventory and the managed packages on disk."""
    manifest = sdk_lock_packages(config / "package-lock.json", version)
    installed = sdk_lock_packages(config / "node_modules/.package-lock.json", version)
    if manifest is None or installed is None:
        return False
    for location, item in installed.items():
        names = sdk_lock_names(location, item)
        if (
            not names & {"glob", "minimatch"}
            and location != f"node_modules/{V2_PLUGIN_PACKAGE}"
        ):
            continue
        path = config / location
        if not path.resolve().is_relative_to(config.resolve()):
            return False
        installed_file = read_json(path / "package.json")
        if (
            manifest.get(location, {}).get("version") != item.get("version")
            or not isinstance(installed_file, dict)
            or installed_file.get("version") != item.get("version")
        ):
            return False
    return True


def write_cache_state(state_file, state):
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(state))
    temporary.replace(state_file)


def mark_sdk_installed(home, version):
    config = Path(home) / ".config/opencode"
    state_file = config.parent / ".oc-box-dependencies.json"
    state = read_json(state_file)
    if (
        not isinstance(state, dict)
        or state.get("runtime_version") != version
        or not sdk_modules_match(config, version)
    ):
        return False
    write_cache_state(state_file, {**state, "sdk_installed": True})
    return True


def sdk_only_manifest(manifest, plugin_package=PLUGIN_PACKAGE):
    """True only for the dependency-only manifest generated by OpenCode."""
    return (
        isinstance(manifest, dict)
        and set(manifest) == {"dependencies"}
        and isinstance(manifest["dependencies"], dict)
        and set(manifest["dependencies"]) == {plugin_package}
    )


def native_cli_preferences(config):
    """Return only an owned regular preferences file, without reading its contents."""
    if config.is_symlink():
        return None
    preferences = config / "cli.json"
    try:
        info = preferences.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise ValueError("Unsafe native CLI preferences file")
    return preferences


def sync_config(home, payload, runtime_version=None):
    v2 = runtime_version is not None and runtime_version.startswith("2.")
    plugin_package = V2_PLUGIN_PACKAGE if v2 else PLUGIN_PACKAGE
    config = Path(home) / ".config/opencode"
    if v2 and config.parent.is_symlink():
        raise ValueError("Unsafe native CLI preferences directory")
    config.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or "node_modules" in name.parts
                or (
                    v2
                    and name.parts
                    and name.parts[0] == "cli.json"
                    and len(name.parts) != 1
                )
            ):
                raise ValueError("Unsafe config archive entry")
        # Native V2 writes the user's terminal settings here. Seed from the host
        # only when absent; subsequent refreshes must not reset explicit choices.
        # Retain the whole file opaquely, including unknown/future native settings.
        preferences = native_cli_preferences(config) if v2 else None
        dependencies = {
            member.name: archive.extractfile(member).read()
            for member in members
            if member.name in DEPENDENCY_FILES
        }
        manifest = (
            json.loads(dependencies["package.json"])
            if "package.json" in dependencies
            else None
        )
        previous = read_json(config / "package.json")
        security_overrides = sdk_security_overrides(runtime_version)
        effective = (
            runtime_manifest(
                manifest, plugin_package, runtime_version, security_overrides
            )
            if runtime_version is not None
            else None
        )
        # Compare with the last HOST input, not files that OpenCode legitimately
        # rewrites to match its running SDK. Otherwise a stale host SDK version
        # forces a reinstall on every launch (e.g. host 1.15.11 / runtime 1.19.5).
        source_hash = hashlib.sha256(
            json.dumps(
                {
                    "runtime_version": runtime_version,
                    **(
                        {"sdk_security_overrides": security_overrides}
                        if security_overrides
                        else {}
                    ),
                    "files": {
                        name: hashlib.sha256(value).hexdigest()
                        for name, value in dependencies.items()
                    },
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        state_file = config.parent / ".oc-box-dependencies.json"
        state = read_json(state_file)
        unchanged_source = (
            isinstance(state, dict) and state.get("source_hash") == source_hash
        )
        same_files = (
            not security_overrides and manifest is not None and previous == manifest
        )
        if same_files:
            for name in LOCKFILES:
                try:
                    existing = (
                        (config / name).read_bytes()
                        if (config / name).exists()
                        else None
                    )
                except OSError:
                    existing = None
                    same_files = False
                if existing != dependencies.get(name):
                    same_files = False
        # Upgrade an already bootstrapped workspace without one final reinstall.
        # This applies ONLY to OpenCode's SDK-only generated manifest; custom
        # dependency sets/locks must have recorded matching input or exact files.
        adopt_generated = (
            state is None
            and not security_overrides
            and runtime_version is not None
            and (manifest is None or sdk_only_manifest(manifest, plugin_package))
            and sdk_only_manifest(previous, plugin_package)
            and previous["dependencies"][plugin_package] == runtime_version
            and (
                read_json(config / "node_modules" / plugin_package / "package.json")
                or {}
            ).get("version")
            == runtime_version
        )
        with tempfile.TemporaryDirectory(
            prefix=".oc-box-config-", dir=config.parent
        ) as temp:
            cached = Path(temp) / "node_modules"
            modules = config / "node_modules"
            preserve = (
                (unchanged_source or (state is None and same_files) or adopt_generated)
                and modules.is_dir()
                and not modules.is_symlink()
                and not config.is_symlink()
                and (
                    not security_overrides
                    or (
                        unchanged_source
                        and state.get("sdk_installed") is True
                        and sdk_modules_match(config, runtime_version)
                    )
                )
            )
            generated = {
                name: (config / name).read_bytes()
                for name in DEPENDENCY_FILES
                if preserve and (config / name).is_file()
            }
            if preserve:
                modules.rename(cached)
            try:
                if preferences is not None:
                    # Leave preferences in place even if refresh fails or is
                    # interrupted. Only the rest of the copied profile expires.
                    config.chmod(0o700)
                    preferences.chmod(0o600)
                    for target in config.iterdir():
                        if target == preferences:
                            continue
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                else:
                    if config.is_symlink():
                        config.unlink()
                    elif config.exists():
                        shutil.rmtree(config)
                    config.mkdir(mode=0o700)
                for member in members:
                    if preserve and member.name in DEPENDENCY_FILES:
                        continue
                    is_preferences = v2 and PurePosixPath(member.name) == PurePosixPath(
                        "cli.json"
                    )
                    if preferences is not None and is_preferences:
                        continue
                    target = config / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.extractfile(member) as source,
                        target.open("wb") as destination,
                    ):
                        shutil.copyfileobj(source, destination)
                    target.chmod(0o600 if is_preferences else member.mode & 0o777)
                for name, content in generated.items():
                    (config / name).write_bytes(content)
                if not preserve and effective is not None:
                    # The host manifest may have been generated by an older
                    # OpenCode. Start at the current SDK immediately, retaining
                    # all user-declared dependencies. Mainline need not first
                    # reconcile a stale package-lock from the host installation.
                    (config / "package.json").write_text(
                        json.dumps(effective, indent=2) + "\n"
                    )
                    if (
                        manifest is None
                        or manifest.get("dependencies", {}).get(plugin_package)
                        != runtime_version
                        or effective.get("overrides") != manifest.get("overrides")
                    ):
                        for name in LOCKFILES:
                            (config / name).unlink(missing_ok=True)
            finally:
                if cached.exists():
                    config.mkdir(parents=True, exist_ok=True)
                    cached.rename(config / "node_modules")
        # Record only a successfully applied config. The sidecar remains inside
        # the private container volume and is never trusted for host operations.
        write_cache_state(
            state_file,
            {
                "source_hash": source_hash,
                "runtime_version": runtime_version,
                **({"sdk_installed": preserve} if security_overrides else {}),
            },
        )
        return preserve


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "check-sdk-lock":
        lockfile = Path(os.environ["HOME"]) / ".config/opencode/package-lock.json"
        sys.exit(0 if sdk_lock_safe(lockfile, sys.argv[2]) else 1)
    if len(sys.argv) == 3 and sys.argv[1] == "mark-sdk-installed":
        sys.exit(0 if mark_sdk_installed(os.environ["HOME"], sys.argv[2]) else 1)
    reused = sync_config(
        os.environ["HOME"],
        sys.stdin.buffer.read(),
        sys.argv[1] if len(sys.argv) > 1 else None,
    )
    print(
        "Reusing container plugin dependencies."
        if reused
        else "Plugin dependencies will be resolved by OpenCode if needed."
    )
