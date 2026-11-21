"""Host opt-in and mount attestation for the native in-container experiment."""

import json
import os
import posixpath
import re
import stat
import subprocess
import unicodedata

RELEASE = re.compile(r"2\.3\.(?:0|[1-9][0-9]*)")
FLAG = "OC_BOX_EXPERIMENTAL_FILE_PREVIEW"
ROOTS = "OC_BOX_FILE_PREVIEW_ROOTS"
NOTICE = (
    "oc-box: Experimental Ctrl-click file preview requires an ordinary "
    "native OpenCode 2.3.x workspace with verified mounts; preview is off."
)


def supports(release):
    return isinstance(release, str) and RELEASE.fullmatch(release) is not None


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
    """Read-only host admission for one already-owned native TUI client."""
    if os.environ.get(FLAG) != "1":
        return None
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
        print(NOTICE, flush=True)
    return binding
