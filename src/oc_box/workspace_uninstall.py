"""Workspace review and gated engine-only execution for full uninstall.

The only filesystem inputs read here are the selected registry's directory
metadata and workspace.json records. No project, marker, lock, transcript,
native OpenCode, or recovery data is opened. There is no subprocess runner,
engine inspector, lock acquisition, or filesystem deletion here.
"""

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable


MAX_RECORD_BYTES = 1024 * 1024
MAX_WORKSPACES = 128
MAX_ENGINE_OBJECTS = 4096
MAX_MOUNTS = 128
MAX_ENGINE_REFERENCES = 16384
MAX_STORAGE_ALIASES = 8
EXECUTION_TIMEOUT = 300
PODMAN_COMMAND_TIMEOUT = 20
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_WORKSPACE_ID = re.compile(r"[a-f0-9]{32}")
_ENGINE_ID = re.compile(r"[a-f0-9]{64}")
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_WORKSPACE_CONTAINER = re.compile(r"oc-box-[a-f0-9]{32}")
_WORKSPACE_VOLUME = re.compile(r"oc-box-home-[a-f0-9]{32}")
_FSID = re.compile(r"[1-9a-f][0-9a-f]{0,15}")
_HANDLE = re.compile(r"(?:[a-f0-9]{2}){1,4096}")
_BROAD_STATE_ROOTS = frozenset(
    (
        "/",
        "/dev",
        "/etc",
        "/home",
        "/opt",
        "/proc",
        "/run",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
        "/var/tmp",
    )
)
_CREATED = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class WorkspacePlanError(ValueError):
    """Fixed diagnostic. Never interpolate inspect output or raw record data."""

    def __init__(self, reason, workspace_id=None):
        self.reason = reason
        self.workspace_id = workspace_id
        detail = f" for workspace {workspace_id}" if workspace_id else ""
        super().__init__(f"Full uninstall workspace review refused{detail} ({reason}).")


@dataclass(frozen=True)
class StoragePath:
    """Absolute canonical path and adapter-verified (device, inode, mount ID)."""

    path: str
    identity: tuple[int, int, int]


@dataclass(frozen=True)
class EngineScope:
    rootless: bool
    remote: bool
    graph_root: StoragePath
    run_root: StoragePath
    volume_root: StoragePath


@dataclass(frozen=True)
class ContainerState:
    status: str
    running: bool
    paused: bool
    restarting: bool
    pid: int
    conmon_pid: int
    mounted: bool


@dataclass(frozen=True)
class HostMount:
    # The adapter projects host-backed binds and volumes only; no implicit
    # exclusion of an unrecognized host-backed mount type is allowed.
    kind: str
    source: str
    destination: str
    name: str | None = None
    # Additional canonical backing paths for observable bind aliases/subpaths.
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContainerInspection:
    name: str
    ident: str
    # Exact values of io.oc-box.workspace and io.oc-box.managed.
    workspace_label: str | None
    managed_label: str | None
    state: ContainerState | None
    mounts: tuple[HostMount, ...]
    dependencies: tuple[str, ...]
    in_pod: bool
    # Owned graph storage only (e.g. UpperDir, not shared image LowerDirs).
    writable_paths: tuple[StoragePath, ...] = ()


@dataclass(frozen=True)
class VolumeInspection:
    name: str
    workspace_label: str | None
    driver: str
    scope: str
    anonymous: bool
    default_options: bool
    external_storage: bool
    created_at: str
    mountpoint: StoragePath | None
    mount_count: int


@dataclass(frozen=True)
class EngineSnapshot:
    """Closed projection from a trusted local store inspection adapter.

    ``complete`` covers present/absent names, all container consumers (also
    stopped and external storage users), dependency references and all volumes.
    If the adapter cannot account for storage users, path aliases, an unknown
    contract, or the absence of a reviewed object, it must not claim completeness.
    ``labeled_networks`` is informational only; networks are not targets.
    """

    scope: EngineScope
    containers: tuple[ContainerInspection, ...]
    volumes: tuple[VolumeInspection, ...]
    labeled_networks: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class Registration:
    workspace_id: str
    project: str
    container_project: str
    home: str
    container_name: str
    home_volume: str
    fingerprint: str
    record_stamp: tuple[int, ...]
    directory_stamp: tuple[int, ...]


@dataclass(frozen=True)
class WorkspaceTargets:
    registration: Registration
    container: ContainerInspection | None
    home_volume: VolumeInspection | None


@dataclass(frozen=True)
class WorkspacePlan:
    state_root: str
    registry_chain: tuple[tuple[int, ...], ...]
    registry_present: bool
    engine: EngineScope
    workspaces: tuple[WorkspaceTargets, ...]
    retained_networks: tuple[str, ...]
    protected_paths: tuple[str, ...]


@dataclass(frozen=True)
class ResourceRef:
    workspace_id: str
    kind: str
    name: str
    container_id: str | None = None


class RemovalAck(Enum):
    ACKNOWLEDGED = "acknowledged"
    NOT_DISPATCHED = "not-dispatched"
    UNCONFIRMED = "unconfirmed"


class ResourceState(str, Enum):
    CONFIRMED = "confirmed"
    RETAINED = "retained"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ResourceOutcome:
    resource: ResourceRef
    state: ResourceState


@dataclass(frozen=True)
class WorkspaceExecution:
    resources: tuple[ResourceOutcome, ...]
    reason: str | None
    lock_cleanup_unverified: bool = False

    @property
    def complete(self):
        return self.reason is None and not self.lock_cleanup_unverified


@dataclass(frozen=True)
class EngineActions:
    """Trusted, bounded actions against the same audited local store.

    ``inspect`` implements the review callback contract. ``remove`` must not
    retry and may return NOT_DISPATCHED only with positive proof no child
    started. ``absent`` independently checks the original name and, for a
    container, its full immutable ID. Only exact True establishes absence.
    """

    inspect: Callable[[tuple[tuple[str, str], ...]], EngineSnapshot]
    remove: Callable[[EngineScope, ResourceRef], RemovalAck]
    absent: Callable[[EngineScope, ResourceRef], bool]


def _fail(reason, workspace_id=None):
    raise WorkspacePlanError(reason, workspace_id) from None


def _matches(pattern, value):
    return type(value) is str and pattern.fullmatch(value) is not None


def _path(value):
    return (
        type(value) is str
        and 0 < len(value) < 4096
        and all(char.isprintable() for char in value)
        and len(value.encode("utf-8")) < 4096
        and Path(value).anchor == "/"
        and str(Path(value)) == value
        and ".." not in Path(value).parts
    )


def _overlap(first, second):
    return (
        first == second
        or first.startswith(second.rstrip("/") + "/")
        or second.startswith(first.rstrip("/") + "/")
    )


def _storage(value):
    return (
        type(value) is StoragePath
        and _path(value.path)
        and value.path != "/"
        and type(value.identity) is tuple
        and len(value.identity) == 3
        and all(type(part) is int and 0 <= part < 2**64 for part in value.identity)
        and value.identity[1] > 0
        and value.identity[2] > 0
    )


def _storage_overlap(first, second):
    return (
        _overlap(first.path, second.path) or first.identity[:2] == second.identity[:2]
    )


def _same_mount(first, second):
    return (
        first.identity[0] == second.identity[0]
        and first.identity[2] == second.identity[2]
    )


def _valid_created(value):
    match = _CREATED.fullmatch(value) if type(value) is str else None
    if match is None:
        return False
    try:
        # Check calendar/clock fields independently of fractional precision
        # (Python 3.10 doesn't accept all Podman subsecond stamps).
        datetime.fromisoformat(match[1])
        return True
    except ValueError:
        return False


def _mount_id(fd):
    # st_dev is not enough: a bind mount can alias the same filesystem.
    with open(f"/proc/self/fdinfo/{fd}", "rb") as stream:
        raw = stream.read(4097)
    ids = re.findall(rb"^mnt_id:\s*([0-9]{1,20})$", raw, re.MULTILINE)
    if len(raw) > 4096 or len(ids) != 1 or int(ids[0]) == 0:
        _fail("registry-mount-unverified")
    return int(ids[0])


def _verify_owner(info, *, owned):
    uid = os.geteuid()
    sticky_ancestor = not owned and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
    if info.st_uid not in ((uid,) if owned else (uid, 0)) or (
        info.st_mode & 0o022 and not sticky_ancestor
    ):
        _fail("unsafe-registry")


def _directory_stamp(fd, *, owned):
    info = os.fstat(fd)
    _verify_owner(info, owned=owned)
    return (info.st_dev, info.st_ino, _mount_id(fd), info.st_uid, info.st_mode)


def _file_stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_uid,
        info.st_mode,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _unique_pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            _fail("invalid-registration")
        data[key] = value
    return data


def _bad_constant(_):
    _fail("invalid-registration")


def _project_baseline(data):
    identity = data.get("project_identity")
    if (
        type(identity) is not list
        or len(identity) != 2
        or any(type(value) is not int or not 0 <= value < 2**64 for value in identity)
        or identity[1] == 0
    ):
        return False
    if "project_filesystem_identity" not in data:
        return True
    stable = data["project_filesystem_identity"]
    if (
        type(stable) is not dict
        or set(stable) != {"method", "fsid", "handle"}
        or stable["method"] != "linux-file-handle-v1"
        or not _matches(_FSID, stable["fsid"])
        or type(stable["handle"]) is not dict
        or set(stable["handle"]) != {"type", "bytes"}
    ):
        return False
    handle = stable["handle"]
    return (
        type(handle["type"]) is int
        and -(2**31) <= handle["type"] < 2**31
        and _matches(_HANDLE, handle["bytes"])
    )


@contextmanager
def _child_directory(parent, name, mount, *, owned):
    fd = os.open(name, _DIR, dir_fd=parent)
    try:
        stamp = _directory_stamp(fd, owned=owned)
        if mount is not None and stamp[2] != mount:
            _fail("registry-mount-boundary")
        yield fd, stamp
    finally:
        os.close(fd)


def _read_record(directory, ident, mount, state_root):
    try:
        with _child_directory(directory, ident, mount, owned=True) as (
            workspace,
            stamp,
        ):
            # Inspect the object before opening it for content. A substituted
            # link, FIFO, or device is never opened for I/O.
            pinned = os.open(
                "workspace.json",
                os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=workspace,
            )
            try:
                before = os.fstat(pinned)
                _verify_owner(before, owned=True)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or _mount_id(pinned) != mount
                ):
                    _fail("unsafe-registry")
                if before.st_size > MAX_RECORD_BYTES:
                    _fail("invalid-registration")
                expected = _file_stamp(before)
                with open(f"/proc/self/fd/{pinned}", "rb") as stream:
                    fd = stream.fileno()
                    if _file_stamp(os.fstat(fd)) != expected or _mount_id(fd) != mount:
                        _fail("registry-changed")
                    raw = stream.read(MAX_RECORD_BYTES + 1)
                    current = os.stat(
                        "workspace.json", dir_fd=workspace, follow_symlinks=False
                    )
                    if (
                        len(raw) > MAX_RECORD_BYTES
                        or expected != _file_stamp(os.fstat(fd))
                        or expected != _file_stamp(current)
                    ):
                        _fail("registry-changed")
            finally:
                os.close(pinned)
    except FileNotFoundError:
        _fail("missing-registration", ident)
    except OSError:
        _fail("unsafe-registry", ident)

    try:
        data = json.loads(
            raw, object_pairs_hook=_unique_pairs, parse_constant=_bad_constant
        )
    except (ValueError, RecursionError):
        _fail("invalid-registration", ident)
    if (
        type(data) is not dict
        or type(data.get("version")) is not int
        or data["version"] != 2
        or data.get("id") != ident
        or data.get("container") != "oc-box-" + ident
        or data.get("home_volume") != "oc-box-home-" + ident
    ):
        _fail("invalid-registration", ident)
    if (
        data.get("restore_incomplete", False) is not False
        or "restore_staging_project" in data
        or "restore_container_create_attempted" in data
        or "restore_volume_create_attempted" in data
    ):
        _fail("incomplete-restore", ident)
    if "entrypoint_conversion" in data or "entrypoint_retirement" in data:
        _fail("incomplete-container-replacement", ident)
    project, home = data.get("project"), data.get("home")
    container_project = data.get("container_project", project)
    if (
        not all(
            _path(value) and value != "/"
            for value in (project, home, container_project)
        )
        or any(char in project for char in (":", ","))
        or container_project == home
        or not _project_baseline(data)
    ):
        _fail("invalid-registration", ident)
    if _overlap(project, state_root):
        _fail("registry-project-overlap", ident)
    return Registration(
        ident,
        project,
        container_project,
        home,
        data["container"],
        data["home_volume"],
        hashlib.sha256(raw).hexdigest(),
        expected,
        stamp,
    )


def _registry(state_root):
    components = Path(state_root).parts[1:]
    fd = os.open("/", _DIR)
    try:
        chain = [_directory_stamp(fd, owned=False)]
        for index, name in enumerate(components, 1):
            leaf = index == len(components)
            try:
                child = os.open(name, _DIR, dir_fd=fd)
            except FileNotFoundError:
                return tuple(chain), False, ()
            os.close(fd)
            fd = child
            stamp = _directory_stamp(fd, owned=leaf)
            if leaf and stamp[2] != chain[-1][2]:
                _fail("registry-mount-boundary")
            chain.append(stamp)
        mount = chain[-1][2]
        try:
            directory = os.open("workspaces", _DIR, dir_fd=fd)
        except FileNotFoundError:
            return tuple(chain), True, ()
        try:
            directory_start = _file_stamp(os.fstat(directory))
            stamp = _directory_stamp(directory, owned=True)
            if stamp[2] != mount:
                _fail("registry-mount-boundary")
            chain.append(stamp)
            names = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    if len(names) >= MAX_WORKSPACES:
                        _fail("registry-limit")
                    if not _matches(_WORKSPACE_ID, entry.name):
                        _fail("unexpected-registry-entry")
                    names.append(entry.name)
            records = tuple(
                _read_record(directory, name, mount, state_root)
                for name in sorted(names)
            )
            # Directory mtime/ctime detect membership changes during this pass.
            if _file_stamp(os.fstat(directory)) != directory_start:
                _fail("registry-changed")
            return tuple(chain), True, records
        finally:
            os.close(directory)
    except OSError:
        _fail("unsafe-registry")
    finally:
        os.close(fd)


def _label(value):
    # Foreign label values are never retained in a plan or printed.
    return value is None or type(value) is str and len(value) <= 256


def _state_stopped(state):
    return (
        type(state) is ContainerState
        and state.status in ("configured", "created", "exited", "stopped")
        and all(
            flag is False
            for flag in (state.running, state.paused, state.restarting, state.mounted)
        )
        and type(state.pid) is int
        and state.pid == 0
        and type(state.conmon_pid) is int
        and state.conmon_pid == 0
    )


def _local_scope(scope):
    return (
        type(scope) is EngineScope and scope.rootless is True and scope.remote is False
    )


def _storage_layout(scope):
    graph, run, volume = scope.graph_root, scope.run_root, scope.volume_root
    return (
        all(map(_storage, (graph, run, volume)))
        and not _storage_overlap(graph, run)
        and not _storage_overlap(volume, run)
        and volume.identity[:2] != graph.identity[:2]
        and Path(volume.path).is_relative_to(graph.path)
        and volume.path != graph.path
        and _same_mount(volume, graph)
    )


def _inventory(snapshot, state_root, registry_stamps, protected_paths):
    if type(snapshot) is not EngineSnapshot or snapshot.complete is not True:
        _fail("engine-inventory-unverified")
    scope = snapshot.scope
    if not _local_scope(scope):
        _fail("engine-not-local-rootless")
    if not _storage_layout(scope):
        _fail("unsupported-engine-storage")
    graph, run, volume = scope.graph_root, scope.run_root, scope.volume_root
    if any(
        _overlap(storage.path, path)
        for storage in (graph, run, volume)
        for path in (state_root, *protected_paths)
    ) or any(
        storage.identity[:2] == entry[:2]
        for storage in (graph, run, volume)
        for entry in registry_stamps
    ):
        _fail("engine-storage-overlap")
    if not all(
        type(items) is tuple and len(items) <= MAX_ENGINE_OBJECTS
        for items in (snapshot.containers, snapshot.volumes, snapshot.labeled_networks)
    ):
        _fail("engine-inventory-unverified")
    containers, volumes, ids = {}, {}, set()
    references = 0
    for item in snapshot.containers:
        if (
            type(item) is not ContainerInspection
            or not _matches(_RESOURCE_NAME, item.name)
            or not _matches(_ENGINE_ID, item.ident)
            or item.name in containers
            or item.ident in ids
            or not _label(item.workspace_label)
            or not _label(item.managed_label)
            or type(item.in_pod) is not bool
            or type(item.mounts) is not tuple
            or len(item.mounts) > MAX_MOUNTS
            or type(item.dependencies) is not tuple
            or len(item.dependencies) > MAX_MOUNTS
            or type(item.writable_paths) is not tuple
            or len(item.writable_paths) > MAX_STORAGE_ALIASES
            or any(not _matches(_ENGINE_ID, dep) for dep in item.dependencies)
            or not all(map(_storage, item.writable_paths))
        ):
            _fail("engine-inventory-unverified")
        references += (
            len(item.mounts) + len(item.dependencies) + len(item.writable_paths)
        )
        for mount in item.mounts:
            if (
                type(mount) is not HostMount
                or not _path(mount.source)
                or not _path(mount.destination)
                or type(mount.aliases) is not tuple
                or len(mount.aliases) > MAX_STORAGE_ALIASES
                or not all(map(_path, mount.aliases))
                or (
                    not _matches(_RESOURCE_NAME, mount.name)
                    if mount.kind == "volume"
                    else mount.kind != "bind" or mount.name is not None
                )
            ):
                _fail("engine-inventory-unverified")
            references += len(mount.aliases)
        if references > MAX_ENGINE_REFERENCES:
            _fail("engine-inventory-unverified")
        containers[item.name] = item
        ids.add(item.ident)
    if any(
        dep not in ids for value in containers.values() for dep in value.dependencies
    ):
        _fail("engine-inventory-unverified")
    for item in snapshot.volumes:
        if (
            type(item) is not VolumeInspection
            or not _matches(_RESOURCE_NAME, item.name)
            or item.name in volumes
            or not _label(item.workspace_label)
            or (item.mountpoint is not None and not _storage(item.mountpoint))
        ):
            _fail("engine-inventory-unverified")
        volumes[item.name] = item
    if any(
        mount.kind == "volume" and mount.name not in volumes
        for container in containers.values()
        for mount in container.mounts
    ):
        _fail("engine-inventory-unverified")
    if not all(_matches(_RESOURCE_NAME, item) for item in snapshot.labeled_networks):
        _fail("engine-inventory-unverified")
    if len(set(snapshot.labeled_networks)) != len(snapshot.labeled_networks):
        _fail("engine-inventory-unverified")
    return containers, volumes


def _private_volume(record, item, scope):
    if item.workspace_label != record.workspace_id:
        _fail("volume-ownership", record.workspace_id)
    root = scope.volume_root
    if (
        item.driver != "local"
        or item.scope != "local"
        or item.anonymous is not False
        or item.default_options is not True
        or item.external_storage is not False
        or not _valid_created(item.created_at)
        or item.mountpoint is None
        or item.mountpoint.path != str(Path(root.path) / item.name / "_data")
        or not _same_mount(item.mountpoint, root)
        or item.mountpoint.identity[:2] == root.identity[:2]
    ):
        _fail("unsupported-volume-storage", record.workspace_id)
    if type(item.mount_count) is not int or item.mount_count != 0:
        _fail("volume-in-use", record.workspace_id)


def _owned_container(record, item, volume, scope):
    ident = record.workspace_id
    if item.workspace_label != ident or item.managed_label != "true":
        _fail("container-ownership", ident)
    if not _state_stopped(item.state):
        _fail("container-state-unverified", ident)
    if item.in_pod or item.dependencies:
        _fail("unsupported-container-dependencies", ident)
    if volume is None:
        _fail("missing-container-home", ident)
    if len(item.mounts) != 2 or {
        (mount.kind, mount.source, mount.destination, mount.name)
        for mount in item.mounts
    } != {
        ("bind", record.project, record.container_project, None),
        ("volume", volume.mountpoint.path, record.home, volume.name),
    }:
        _fail("unsupported-container-mounts", ident)
    graph = scope.graph_root
    if not item.writable_paths or any(
        not Path(path.path).is_relative_to(graph.path)
        or path.path == graph.path
        or _storage_overlap(path, scope.volume_root)
        or _storage_overlap(path, volume.mountpoint)
        or path.identity[:2] == graph.identity[:2]
        or not _same_mount(path, graph)
        for path in item.writable_paths
    ):
        _fail("unsupported-writable-storage", ident)
    if any(
        _overlap(alias, root.path)
        for mount in item.mounts
        if mount.kind == "bind"
        for alias in mount.aliases
        for root in (graph, scope.run_root, scope.volume_root)
    ) or any(
        alias != volume.mountpoint.path
        for mount in item.mounts
        if mount.kind == "volume"
        for alias in mount.aliases
    ):
        _fail("unsupported-container-mounts", ident)


def _plan_targets(records, snapshot, containers, volumes):
    targets = []
    scope = snapshot.scope
    storage_roots = (scope.graph_root, scope.run_root, scope.volume_root)
    for record in records:
        ident = record.workspace_id
        if any(_overlap(record.project, path.path) for path in storage_roots):
            _fail("engine-project-overlap", ident)
        container, home = (
            containers.get(record.container_name),
            volumes.get(record.home_volume),
        )
        if home is not None:
            _private_volume(record, home, scope)
        if container is not None:
            if container.ident in containers:
                # The full-ID command selector must not also name a foreign
                # container, regardless of engine name/ID lookup precedence.
                _fail("container-id-ambiguous", ident)
            _owned_container(record, container, home, scope)
        paths = (
            *((home.mountpoint,) if home is not None else ()),
            *(container.writable_paths if container is not None else ()),
        )
        for other in containers.values():
            if other is container:
                continue
            if container is not None and container.ident in other.dependencies:
                _fail("container-in-use", ident)
            if any(
                (home is not None and mount.name == home.name)
                or any(
                    _overlap(source, path.path)
                    for source in (mount.source, *mount.aliases)
                    for path in paths
                )
                for mount in other.mounts
            ) or any(
                _storage_overlap(path, other_path)
                for path in paths
                for other_path in other.writable_paths
            ):
                _fail("workspace-storage-in-use", ident)
        if any(
            _storage_overlap(path, other.mountpoint)
            for other in volumes.values()
            if other is not home and other.mountpoint is not None
            for path in paths
        ):
            _fail("workspace-storage-in-use", ident)
        targets.append(WorkspaceTargets(record, container, home))

    container_names = {record.container_name for record in records}
    volume_names = {record.home_volume for record in records}
    if any(
        name not in container_names
        and (
            item.workspace_label is not None
            or item.managed_label == "true"
            or _WORKSPACE_CONTAINER.fullmatch(name)
        )
        for name, item in containers.items()
    ) or any(
        name not in volume_names
        and (item.workspace_label is not None or _WORKSPACE_VOLUME.fullmatch(name))
        for name, item in volumes.items()
    ):
        _fail("orphaned-or-unexpected-resource")
    return tuple(targets)


def plan_workspaces(state_root, *, inspect_engine, protected_paths):
    """Return a review snapshot or refuse the whole workspace scope.

    Pass the raw absolute state-root selector, not a resolved path. The caller
    supplies canonical protected locations (including default/selected native
    roots and the installation); none of those locations are read here.
    ``inspect_engine(names)`` receives a tuple of (container name, home name)
    pairs for explicit presence checks. It must be trusted, bounded and
    read-only; this module launches no subprocess and has no default adapter.
    A plan is not a lock, an execution token, or permission to delete anything.
    """
    plan, _ = _plan_and_container_ids(state_root, inspect_engine, protected_paths)
    return plan


def _validate_selectors(state_root, protected_paths):
    try:
        state_root = os.fspath(state_root)
    except TypeError:
        _fail("unsafe-state-root")
    if (
        not _path(state_root)
        or state_root in _BROAD_STATE_ROOTS
        or any(_overlap(state_root, path) for path in ("/dev", "/proc", "/sys"))
        or type(protected_paths) is not tuple
        or len(protected_paths) > 128
        or not all(map(_path, protected_paths))
        or any(_overlap(state_root, path) for path in protected_paths)
    ):
        _fail("unsafe-state-root")
    return state_root


def _plan_and_container_ids(state_root, inspect_engine, protected_paths):
    state_root = _validate_selectors(state_root, protected_paths)
    before = _registry(state_root)
    chain, present, records = before
    names = tuple((record.container_name, record.home_volume) for record in records)
    try:
        snapshot = inspect_engine(names)
    except Exception:
        _fail("engine-inventory-unverified")
    if _registry(state_root) != before:
        _fail("registry-changed")
    stamps = (*chain, *(record.directory_stamp for record in records))
    containers, volumes = _inventory(snapshot, state_root, stamps, protected_paths)
    targets = _plan_targets(records, snapshot, containers, volumes)
    return (
        WorkspacePlan(
            state_root,
            chain,
            present,
            snapshot.scope,
            targets,
            tuple(sorted(snapshot.labeled_networks)),
            protected_paths,
        ),
        frozenset(item.ident for item in containers.values()),
    )


class _ExecutionRefusal(ValueError):
    pass


def _refuse(reason):
    raise _ExecutionRefusal(reason) from None


def _command_targets(resource):
    if (
        type(resource) is not ResourceRef
        or not _matches(_WORKSPACE_ID, resource.workspace_id)
        or type(resource.kind) is not str
        or not _matches(_RESOURCE_NAME, resource.name)
    ):
        return ()
    if (
        resource.kind == "container"
        and resource.name == "oc-box-" + resource.workspace_id
        and _matches(_ENGINE_ID, resource.container_id)
    ):
        return resource.container_id, resource.name
    if (
        resource.kind == "volume"
        and resource.name == "oc-box-home-" + resource.workspace_id
        and resource.container_id is None
    ):
        return (resource.name,)
    return ()


def _execution_steps(plan):
    if (
        type(plan) is not WorkspacePlan
        or type(plan.workspaces) is not tuple
        or len(plan.workspaces) > MAX_WORKSPACES
    ):
        _refuse("invalid-review")
    steps = []
    previous = ""
    for index, workspace in enumerate(plan.workspaces):
        if (
            type(workspace) is not WorkspaceTargets
            or type(workspace.registration) is not Registration
        ):
            _refuse("invalid-review")
        ident = workspace.registration.workspace_id
        if not _matches(_WORKSPACE_ID, ident) or ident <= previous:
            _refuse("invalid-review")
        previous = ident
        container, volume = workspace.container, workspace.home_volume
        if container is not None:
            if type(container) is not ContainerInspection:
                _refuse("invalid-review")
            steps.append(
                (
                    index,
                    ResourceRef(ident, "container", container.name, container.ident),
                )
            )
        if volume is not None:
            if type(volume) is not VolumeInspection:
                _refuse("invalid-review")
            steps.append((index, ResourceRef(ident, "volume", volume.name)))
    if any(not _command_targets(resource) for _, resource in steps):
        _refuse("invalid-review")
    return tuple(steps)


def _check_fence(check_locks, deadline):
    if time.monotonic() >= deadline:
        _refuse("execution-deadline")
    try:
        current = check_locks()
    except Exception:
        _refuse("lock-unverified")
    if current is not True:
        _refuse("lock-unverified")
    if time.monotonic() >= deadline:
        _refuse("execution-deadline")


def _revalidate(expected, engine, retired_ids, check):
    check()
    try:
        observed, container_ids = _plan_and_container_ids(
            expected.state_root, engine.inspect, expected.protected_paths
        )
    except Exception:
        _refuse("plan-unverified")
    check()
    if observed != expected or retired_ids.intersection(container_ids):
        _refuse("plan-changed")


def _without_resource(plan, index, kind):
    workspace = plan.workspaces[index]
    updated = (
        replace(workspace, container=None)
        if kind == "container"
        else replace(workspace, home_volume=None)
    )
    return replace(
        plan,
        workspaces=(*plan.workspaces[:index], updated, *plan.workspaces[index + 1 :]),
    )


def execute_workspace_plan(
    reviewed, *, state_root, protected_paths, engine, hold_locks
):
    """Execute only the in-process plan already authorized by the caller.

    The required ``hold_locks(reviewed)`` context holds the stable host gate,
    every workspace lifecycle lock and applicable V2 control admission for
    the entire pass. It yields a check callable that verifies this same fence,
    including certainty/stop intent, without initializing or clearing it.
    Only exact True passes. This module cannot attest a caller's lock
    implementation or grant confirmation; it never creates/removes a lock.

    No registry, project, launcher, or other filesystem mutation is performed
    on any outcome. RETAINED means no removal was dispatched by this pass,
    not a verified observation that the object is still present. CONFIRMED
    means acknowledged removal and checked absence during this pass. UNKNOWN
    is never retried, including when a timeout might have reached the engine.
    """
    deadline = time.monotonic() + EXECUTION_TIMEOUT
    try:
        steps = _execution_steps(reviewed)
    except _ExecutionRefusal as exc:
        return WorkspaceExecution((), exc.args[0])

    outcomes = [
        ResourceOutcome(resource, ResourceState.RETAINED) for _, resource in steps
    ]
    try:
        same_scope = (
            _validate_selectors(state_root, protected_paths) == reviewed.state_root
            and protected_paths == reviewed.protected_paths
        )
    except WorkspacePlanError:
        same_scope = False
    if (
        not same_scope
        or type(engine) is not EngineActions
        or not all(
            map(callable, (engine.inspect, engine.remove, engine.absent, hold_locks))
        )
    ):
        return WorkspaceExecution(tuple(outcomes), "invalid-review-scope")

    reason, entered, lock_cleanup_unverified = "lock-unavailable", False, False
    try:
        with hold_locks(reviewed) as check_locks:
            entered = True

            def check():
                _check_fence(check_locks, deadline)

            try:
                expected, retired = reviewed, set()
                _revalidate(expected, engine, retired, check)
                for position, (index, resource) in enumerate(steps):
                    check()
                    # A raised, interrupted, or malformed reply cannot prove
                    # the engine did not accept the request.
                    outcomes[position] = ResourceOutcome(
                        resource, ResourceState.UNKNOWN
                    )
                    try:
                        ack = engine.remove(expected.engine, resource)
                    except Exception:
                        _refuse("removal-unconfirmed")
                    if ack is RemovalAck.NOT_DISPATCHED:
                        outcomes[position] = ResourceOutcome(
                            resource, ResourceState.RETAINED
                        )
                        _refuse("removal-not-dispatched")
                    if ack is not RemovalAck.ACKNOWLEDGED:
                        _refuse("removal-unconfirmed")
                    check()
                    try:
                        absent = engine.absent(expected.engine, resource)
                    except Exception:
                        _refuse("absence-unverified")
                    if absent is not True:
                        _refuse("absence-unverified")
                    expected = _without_resource(expected, index, resource.kind)
                    if resource.container_id is not None:
                        retired.add(resource.container_id)
                    # Revalidate the whole scope, not only the next volume;
                    # only this acknowledged removal may differ from review.
                    _revalidate(expected, engine, retired, check)
                    outcomes[position] = ResourceOutcome(
                        resource, ResourceState.CONFIRMED
                    )
                reason = None
            except _ExecutionRefusal as exc:
                reason = exc.args[0]
            except KeyboardInterrupt:
                reason = "interrupted"
            except Exception:
                reason = "execution-unverified"
    except (Exception, KeyboardInterrupt):
        if entered:
            lock_cleanup_unverified = True
            if reason in (None, "lock-unavailable"):
                reason = "lock-exit-unverified"

    return WorkspaceExecution(tuple(outcomes), reason, lock_cleanup_unverified)


def podman_actions(inspect_engine, run_local):
    """Map reviewed actions to fixed Podman argv using injected transport only.

    ``run_local(scope, argv, timeout=...)`` must pin/reverify the same local
    executable, selector/environment and EngineScope on every dispatch,
    enforce the timeout and return an exact exit code without raw output.
    ``argv`` does not include an executable. No transport or inspection
    discovery is supplied here; no fallback store is authorized.
    """

    def call(scope, kind, verb, selector):
        return run_local(
            scope,
            ("--remote=false", kind, verb, "--", selector),
            timeout=PODMAN_COMMAND_TIMEOUT,
        )

    def remove(scope, resource):
        selectors = _command_targets(resource)
        if not selectors or not _local_scope(scope) or not _storage_layout(scope):
            return RemovalAck.NOT_DISPATCHED
        code = call(scope, resource.kind, "rm", selectors[0])
        if type(code) is int and code == 0:
            return RemovalAck.ACKNOWLEDGED
        return RemovalAck.UNCONFIRMED

    def absent(scope, resource):
        selectors = _command_targets(resource)
        if not selectors or not _local_scope(scope) or not _storage_layout(scope):
            return False
        for selector in selectors:
            code = call(scope, resource.kind, "exists", selector)
            if type(code) is not int or code != 1:
                return False
        return True

    return EngineActions(inspect_engine, remove, absent)
