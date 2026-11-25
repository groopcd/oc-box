"""Verify ordinary saved accelerator/render bindings without opening a device."""

import contextlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


_PATH = re.compile(r"/dev/(?:accel/accel[0-9]{1,6}|dri/renderD[0-9]{1,6})")
_ID = re.compile(r"[a-f0-9]{64}")
_LIMIT = 1024 * 1024
_DIR = os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FD = os.O_CLOEXEC | os.O_NOFOLLOW
_MESSAGES = {
    "unsupported": (
        "Guarded startup supports saved plain accelerator and render-node paths "
        "only, not aliases, remaps, permission overrides, or CDI. Inspect "
        "'oc-box status --table'; preserve the existing workspace."
    ),
    "missing": (
        "A registered accelerator or render device is missing or is no longer "
        "a direct character device. Reconnect or enable the saved devices, "
        "then retry without changing the registered mappings."
    ),
    "engine": (
        "Guarded device verification requires a local rootless Podman overlay "
        "store. Select the workspace's original local engine and retry."
    ),
    "contract": (
        "Podman did not provide verifiable saved device metadata. Check that the "
        "local Podman supports 'podman generate spec'; keep the workspace and "
        "its device settings intact."
    ),
    "mismatch": (
        "The container's device bindings do not match the saved workspace "
        "mappings. Inspect 'oc-box status --table' and restore access to the "
        "registered nodes; do not reset or clear devices to bypass this check."
    ),
    "bundle": (
        "Podman's previously generated device bindings could not be verified "
        "for this container. Preserve the workspace and get help; no unsupported "
        "device binding was admitted."
    ),
    "changed": (
        "A registered device changed identity during guarded replacement. "
        "Keep both resources; retry only with the originally verified nodes. "
        "A re-enumerated device is not automatically substituted."
    ),
    "access": (
        "Could not verify access to the saved device nodes inside the container. "
        "Check host device permissions, groups, and SELinux policy, then retry with "
        "the same saved mappings."
    ),
    "override": (
        "Legacy replacement with devices cannot preserve an ambient "
        "CONTAINERS_CONF_OVERRIDE safely. Select the original local engine "
        "without an override only if that does not change its store or policy; "
        "otherwise keep the workspace and get help."
    ),
}


class DeviceError(ValueError):
    def __init__(self, reason):
        super().__init__(_MESSAGES[reason])


def paths(data):
    saved = data.get("devices", [])
    if (
        type(saved) is not list
        or len(saved) > 16
        or not all(type(path) is str and _PATH.fullmatch(path) for path in saved)
        or len(saved) != len(set(saved))
    ):
        raise DeviceError("unsupported")
    return tuple(saved)


def _node(path):
    # O_PATH checks identity only. It never opens the character device for I/O.
    prefix, name = path.rsplit("/", 1)
    with contextlib.ExitStack() as stack:
        dev = os.open("/dev", os.O_PATH | _DIR)
        stack.callback(os.close, dev)
        group = os.open(prefix.removeprefix("/dev/"), os.O_PATH | _DIR, dir_fd=dev)
        stack.callback(os.close, group)
        node = os.open(name, os.O_PATH | _FD, dir_fd=group)
        stack.callback(os.close, node)
        info = os.fstat(node)
        if not stat.S_ISCHR(info.st_mode):
            raise DeviceError("missing")
        return info.st_dev, info.st_ino, info.st_rdev


def _signature(saved):
    try:
        return tuple((path, *_node(path)) for path in sorted(saved))
    except OSError:
        raise DeviceError("missing") from None


def _json_command(box, env, reason, *args):
    try:
        result = box.podman(
            "--remote=false", *args, env=env, capture=True, private=True, timeout=15
        )
        raw = result.stdout
        if result.returncode or not 0 < len(raw.encode("utf-8")) <= _LIMIT:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (OSError, ValueError, UnicodeError, AttributeError, box.BoxError):
        raise DeviceError(reason) from None


def _storage(box, env):
    info = _json_command(box, env, "engine", "info", "--format=json")
    try:
        store, host = info["store"], info["host"]
        root = Path(store["graphRoot"])
        if (
            host["serviceIsRemote"] is not False
            or host["security"]["rootless"] is not True
            or store["graphDriverName"] != "overlay"
            or not root.is_absolute()
            or root == Path("/")
            or ".." in root.parts
        ):
            raise ValueError
        return root
    except (ValueError, KeyError, TypeError):
        raise DeviceError("engine") from None


def _read_bundle(path):
    uid = os.geteuid()
    with contextlib.ExitStack() as stack:
        directory = os.open("/", os.O_PATH | _DIR)
        stack.callback(os.close, directory)
        for component in path.parts[1:-1]:
            directory = os.open(component, os.O_PATH | _DIR, dir_fd=directory)
            stack.callback(os.close, directory)
            info = os.fstat(directory)
            if info.st_uid not in (0, uid) or info.st_mode & 0o022:
                raise ValueError
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NONBLOCK | _FD, dir_fd=directory
        )
        file = stack.enter_context(os.fdopen(descriptor, "rb"))
        info = os.fstat(file.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or not 0 < info.st_size <= _LIMIT
        ):
            raise ValueError
        raw = file.read(_LIMIT + 1)
        if len(raw) != info.st_size:
            raise ValueError
        return json.loads(raw)


def _effective(root, ident, info, saved, *, fresh):
    configured = info.get("OCIConfigPath")
    if fresh and configured in (None, ""):
        return
    bundle = root / "overlay-containers" / ident / "userdata"
    spec_path = bundle / "config.json"
    if configured != str(spec_path):
        raise DeviceError("bundle")
    try:
        mounts = _read_bundle(spec_path)["mounts"]
        if not isinstance(mounts, list) or not 0 < len(mounts) <= 128:
            raise ValueError
        seen = set()
        for item in mounts:
            if item["type"] != "bind":
                if item.get("destination") in saved:
                    raise DeviceError("mismatch")
                continue
            source, target = item["source"], item["destination"]
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError
            if target == "/dev/shm" and source == str(bundle / "shm"):
                continue  # Podman's standard per-container shared memory.
            if (
                source == "/dev"
                or source.startswith("/dev/")
                or target == "/dev"
                or target.startswith("/dev/")
            ):
                options = item["options"]
                if (
                    source != target
                    or source not in saved
                    or source in seen
                    or not isinstance(options, list)
                    or not {"rw", "nosuid", "noexec"} <= set(options)
                    or not {"bind", "rbind"}.intersection(options)
                    or not set(options)
                    <= {"rw", "bind", "rbind", "slave", "rslave", "nosuid", "noexec"}
                ):
                    raise DeviceError("mismatch")
                seen.add(source)
            else:
                # Device aliases outside /dev are not part of this contract.
                mode = os.stat(source).st_mode
                if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                    raise DeviceError("mismatch")
        if seen != set(saved):
            raise DeviceError("mismatch")
    except DeviceError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise DeviceError("bundle") from None


def verify(box, data, info, env, *, fresh=False):
    """Return a path and host-node signature for this exact inspected container."""
    saved = paths(data)
    if not saved:
        return ()
    try:
        ident, host = info["Id"], info["HostConfig"]
        create = info["Config"]["CreateCommand"]
        if (
            not _ID.fullmatch(ident)
            or host.get("Privileged") is not False
            or any(
                host.get(key)
                for key in ("Devices", "DeviceRequests", "DeviceCgroupRules")
            )
            or not isinstance(create, list)
            or not all(isinstance(item, str) for item in create)
            or [
                item
                for item in create
                if item == "--device" or item.startswith("--device=")
            ]
            != [f"--device={path}" for path in saved]
        ):
            raise ValueError
    except (TypeError, KeyError, ValueError, AttributeError):
        raise DeviceError("mismatch") from None
    nodes = _signature(saved)
    root = _storage(box, env)
    generated = _json_command(
        box, env, "contract", "generate", "spec", "--name=false", "--compact", ident
    )
    declared = generated.get("host_device_list")
    if declared is None:
        raise DeviceError("contract")
    if (
        not isinstance(declared, list)
        or len(declared) != len(saved)
        or not all(
            type(row) is dict
            and set(row) == {"path", "type", "major", "minor"}
            and row["path"] == path
            and row["type"] == ""
            and type(row["major"]) is int
            and row["major"] == 0
            and type(row["minor"]) is int
            and row["minor"] == 0
            for row, path in zip(declared, saved)
        )
    ):
        raise DeviceError("mismatch")
    _effective(root, ident, info, saved, fresh=fresh)
    if _signature(saved) != nodes:
        raise DeviceError("missing")
    return nodes


@contextlib.contextmanager
def creation_env(env, saved):
    """For a replacement only, prevent ambient default device additions."""
    if not saved:
        yield env
        return
    if "CONTAINERS_CONF_OVERRIDE" in env:
        raise DeviceError("override")
    with tempfile.TemporaryDirectory(prefix="oc-box-device-defaults-") as directory:
        override = Path(directory) / "devices.conf"
        with override.open("x", encoding="ascii") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write("[containers]\ndevices = []\n")
        yield {**env, "CONTAINERS_CONF_OVERRIDE": str(override)}
