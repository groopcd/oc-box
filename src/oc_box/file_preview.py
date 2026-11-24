"""Mount attestation for Ctrl-click opening in a native workspace TUI."""

import json
import os
import posixpath
import re
import stat
import subprocess
import sys
import unicodedata

if __package__:
    from . import startup_output
    from .file_preview_host import InactiveMountSource
else:
    import startup_output
    from file_preview_host import InactiveMountSource

RELEASE = re.compile(r"2\.3\.(?:0|[1-9][0-9]*)")
ROOTS = "OC_BOX_FILE_PREVIEW_ROOTS"
NOTICE = (
    "oc-box: Experimental Ctrl-click host opening requires an ordinary "
    "native OpenCode 2.3.x workspace with verified mounts; host opening is off."
)


def supports(release):
    return isinstance(release, str) and RELEASE.fullmatch(release) is not None


def client_incompatibility(release=None):
    """Cheap terminal/version admission before any private store inspection."""
    if release is not None and not supports(release):
        return "native-version-unsupported"
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return "not-interactive"
    if __package__:
        from . import host_drop
    else:
        import host_drop
    try:
        profile = host_drop.preflight(release)
    except host_drop.DropError:
        return "terminal-profile-unverified"
    # Kitty's optional remote control can inject terminal input.
    return None if profile == "konsole" else "terminal-profile-unsupported"


def _root(path):
    return (
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and path != "/"
        and posixpath.normpath(path) == path
        and not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in path)
        and len(path.encode("utf-8")) < 4096
    )


def verify_roots(box, data, info):
    """Only the registered project bind and named workspace home are trusted."""
    project, home = box.container_project(data), data.get("home")
    if (
        not all(map(_root, (project, home)))
        or project == home
        or any(
            path.split("/")[1] in ("dev", "proc", "sys", "run")
            for path in (project, home)
        )
    ):
        return None
    container = info.get("Id")
    labels = info.get("Config", {}).get("Labels", {})
    state = info.get("State", {})
    mounts = info.get("Mounts")
    if (
        not isinstance(container, str)
        or not re.fullmatch("[0-9a-f]{64}", container)
        or not isinstance(labels, dict)
        or labels.get(box.LABEL) != data.get("id")
        or not isinstance(state, dict)
        or state.get("Running") is not True
        or not isinstance(mounts, list)
    ):
        return None
    by_destination = {}
    for mount in mounts:
        if not isinstance(mount, dict):
            return None
        dest = mount.get("Destination")
        # Leave custom data mounts entirely out of this experiment. Kernel
        # mount IDs also guard runtime-generated mounts not in inspect.
        if dest not in (project, home) or dest in by_destination:
            return None
        by_destination[dest] = mount
    bind, volume = by_destination.get(project, {}), by_destination.get(home, {})
    if (
        bind.get("Type") != "bind"
        or bind.get("Source") != data.get("project")
        or volume.get("Type") != "volume"
        or not isinstance(data.get("home_volume"), str)
        or volume.get("Name") != data["home_volume"]
        or not _root(volume.get("Source"))
    ):
        return None
    return container, (project, home), volume["Source"]


def _inspect(box, kind, name):
    result = box.podman(
        kind,
        "inspect",
        name,
        capture=True,
        check=False,
        private=True,
        timeout=5,
        env=box.maintenance_host_env(),
    )
    if result.returncode or len(result.stdout) > 1024 * 1024:
        return None
    entries = json.loads(result.stdout)
    if isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict):
        return entries[0]
    return None


def _mount_identity(path):
    # Compare the directory object without relying on rootless UID projection.
    if not _root(path) or os.path.realpath(path) != path:
        return None
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        return None
    return str(info.st_dev), str(info.st_ino)


def _volume_source(box, data, volume, source):
    if (
        volume.get("Name") != data["home_volume"]
        or volume.get("Driver") != "local"
        or volume.get("Scope") != "local"
        or volume.get("Mountpoint") != source
        or volume.get("Options") not in (None, {})
        or (volume.get("Labels") or {}).get(box.LABEL) != data["id"]
    ):
        return None
    project = _mount_identity(data["project"])
    home = _mount_identity(source)
    return (project, home) if project and home else None


def client_binding(box, data, connection):
    """Read-only host admission for one opted-in, already-owned TUI client."""
    binding = None
    release = data.get("opencode_version")
    if (
        supports(release)
        and connection.get("version", release) == release
        and connection.get("workspace") == data.get("id")
        and data.get("ready") is True
        and not data.get("podman_args")
        and not data.get("restore_incomplete")
    ):
        try:
            info = _inspect(box, "container", data["container"])
            verified = verify_roots(box, data, info) if info is not None else None
            if verified is not None:
                container, roots, source = verified
                volume = _inspect(box, "volume", data["home_volume"])
                if volume is not None:
                    identities = _volume_source(box, data, volume, source)
                    if identities is not None:
                        binding = (
                            container,
                            {
                                "roots": roots,
                                "identities": identities,
                            },
                        )
        except (
            OSError,
            ValueError,
            AttributeError,
            TypeError,
            KeyError,
            subprocess.SubprocessError,
            box.BoxError,
        ):
            pass
    if binding is None:
        startup_output.line(NOTICE, flush=True)
    return binding


def host_export_mounts(box, data, container, *, refusals=None):
    """Inspect the active local engine for host mounts that could see the spool."""

    def refuse(reason):
        if refusals is not None:
            refusals.append(reason)
        return None

    if not _root(data.get("project")):
        return refuse("workspace-project-unverified")
    # `ps` and inspect share one sanitized environment. Never silently use a
    # remote server's paths as if they were local paths.
    env = box.maintenance_host_env()
    try:

        def query(*args):
            return box.podman(
                "--remote=false",
                *args,
                capture=True,
                check=False,
                private=True,
                timeout=4,
                env=env,
            )

        phase = "container-list-unverified"
        listing = query("ps", "--all", "--quiet", "--no-trunc")
        ids = listing.stdout.splitlines()
        if (
            listing.returncode
            or container not in ids
            or len(ids) > 128
            or len(set(ids)) != len(ids)
            or not all(re.fullmatch(r"[0-9a-f]{64}", ident) for ident in ids)
        ):
            return refuse(phase)
        phase = "container-inspect-unverified"
        result = query("container", "inspect", *ids)
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            return refuse(phase)
        entries = json.loads(result.stdout)
        if not isinstance(entries, list) or len(entries) != len(ids):
            return refuse(phase)
        remaining = set(ids)
        sources = {data["project"]}
        inactive_sources = set()
        volumes = {}
        phase = "workspace-registry-unverified"
        registry = box.workspace_paths()
        if len(registry) > 512:
            return refuse(phase)
        for path in registry:
            if len(sources) > 512:
                return refuse(phase)
            project = box.read_json(path).get("project")
            if not _root(project):
                return refuse(phase)
            if os.path.lexists(project):
                sources.add(project)
        phase = "container-mounts-unverified"
        for item in entries:
            ident = item.get("Id")
            if ident not in remaining:
                return refuse(phase)
            remaining.remove(ident)
            if ident == container and verify_roots(box, data, item) is None:
                return refuse("workspace-mounts-changed")
            state = item.get("State")
            # Only positively known inactive states get the absent-source
            # exception. Paused, restarting and unknown states stay strict.
            inactive = (
                ident != container
                and isinstance(state, dict)
                and state.get("Status")
                in ("configured", "created", "exited", "stopped")
                and state.get("Running") is False
                and state.get("Paused") is False
                and state.get("Restarting", False) is False
            )
            mounts = item.get("Mounts")
            if not isinstance(mounts, list):
                return refuse(phase)
            for mount in mounts:
                source = mount.get("Source")
                kind = mount.get("Type")
                if kind == "tmpfs" and not source:
                    continue
                if kind not in ("bind", "volume") or not _root(source):
                    return refuse("container-mount-layout-unsupported")
                if kind == "volume":
                    name = mount.get("Name")
                    if not isinstance(name, str) or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name
                    ):
                        return refuse(phase)
                    if volumes.setdefault(name, source) != source:
                        return refuse(phase)
                (inactive_sources if inactive else sources).add(source)
        if remaining or len(sources | inactive_sources) > 512 or len(volumes) > 128:
            return refuse(phase)
        if volumes:
            phase = "volume-inspect-unverified"
            result = query("volume", "inspect", *sorted(volumes))
            if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
                return refuse(phase)
            entries = json.loads(result.stdout)
            if not isinstance(entries, list) or len(entries) != len(volumes):
                return refuse(phase)
            for volume in entries:
                source = volumes.pop(volume.get("Name"))
                if (
                    volume.get("Driver") != "local"
                    or volume.get("Scope") != "local"
                    or volume.get("Mountpoint") != source
                    or volume.get("Options") not in (None, {})
                ):
                    return refuse("volume-backing-unverified")
        phase = "container-list-changed"
        listed_again = query("ps", "--all", "--quiet", "--no-trunc")
        if listed_again.returncode or sorted(ids) != sorted(
            listed_again.stdout.splitlines()
        ):
            return refuse(phase)
        # An active or registered source wins if both inventories name it.
        return (
            *sorted(sources),
            *(InactiveMountSource(path) for path in sorted(inactive_sources - sources)),
        )
    except (
        OSError,
        ValueError,
        AttributeError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
        box.BoxError,
    ):
        return refuse(phase)
