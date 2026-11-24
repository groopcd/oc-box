"""Read-only selection of a fresh Studio workspace registration on the host.

The result is a snapshot, not a lifecycle lease, mount authority, or proof
that a fence is installed. No launcher entrypoint uses this module yet.
"""

import json
import os
import re
import stat
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from . import paths, vault, workspace_identity
else:
    import paths
    import vault
    import workspace_identity


NAMESPACE = "studio-workspaces"
RECORD_NAME = "registration.json"
MARKER_NAME = ".oc-box-id"
MARKER_PREFIX = "studio-v1:"
ROLE = "studio"
_STABLE = workspace_identity.STABLE_IDENTITY_KEY
_ID = re.compile(r"[0-9a-f]{32}")
_MARKER = re.compile(re.escape(MARKER_PREFIX.encode("ascii")) + rb"([0-9a-f]{32})\n?")
_DIRECTORY = os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
_MAX_RECORD = 32 * 1024
_MAX_ENTRIES = 256  # per active namespace; excess must not hide an overlap
_UNVERIFIED = "Studio workspace registration could not be verified."
_OVERLAP = "Studio workspace overlaps protected host paths or another registration."


class RegistryError(ValueError):
    """Fixed refusal text; do not disclose registry contents or host paths."""


@dataclass(frozen=True)
class StudioWorkspace:
    id: str
    project: Path
    project_identity: tuple[int, int]
    role: str = field(default=ROLE, init=False)


def _require(condition, message=_UNVERIFIED):
    if not condition:
        raise RegistryError(message)


def _path(value):
    text = os.fspath(value)
    path = Path(text)
    _require(
        path.anchor == "/"
        and str(path) == text
        and ".." not in path.parts
        and "\0" not in text
        and len(os.fsencode(text)) <= 4096
    )
    return path


def _inode(info):
    return info.st_dev, info.st_ino


def _stamp(info):
    return (
        *_inode(info),
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _owned(info, *, private=None):
    _require(info.st_uid == os.geteuid() and not info.st_mode & 0o022)
    if private is not None:
        _require(stat.S_IMODE(info.st_mode) == private)


@contextmanager
def _absolute_directory(path):
    """Pin each component, including intermediate directories, without links."""
    fd = os.open("/", _DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _directory(parent, name, *, private=None, optional=False):
    try:
        fd = os.open(name, _DIRECTORY, dir_fd=parent)
    except FileNotFoundError:
        if not optional:
            raise
        yield None
        return
    try:
        _owned(os.fstat(fd), private=private)
        yield fd
    finally:
        os.close(fd)


def _entry(parent, name):
    return _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))


def _read(parent, name, limit, *, private=None):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _owned(before, private=private)
    _require(
        stat.S_ISREG(before.st_mode)
        and before.st_nlink == 1
        and 0 < before.st_size <= limit
    )
    stamp = _stamp(before)
    fd = os.open(name, _READ, dir_fd=parent)
    try:
        _require(_stamp(os.fstat(fd)) == stamp)
        raw = bytearray()
        while len(raw) <= limit:
            part = os.read(fd, limit + 1 - len(raw))
            if not part:
                break
            raw.extend(part)
        _require(len(raw) == before.st_size and _stamp(os.fstat(fd)) == stamp)
        _require(_entry(parent, name) == stamp)
        return bytes(raw), stamp
    finally:
        os.close(fd)


def _json_object(pairs):
    result = dict(pairs)
    _require(len(result) == len(pairs))
    return result


def _invalid_constant(_):
    raise RegistryError(_UNVERIFIED)


def _valid_identity(value):
    return (
        type(value) is list
        and len(value) == 2
        and all(type(part) is int and 0 <= part < 2**64 for part in value)
        and value[1] > 0
    )


def _valid_stable(value):
    if (
        type(value) is not dict
        or set(value) != {"method", "fsid", "handle"}
        or value["method"] != "linux-file-handle-v1"
        or not isinstance(value["fsid"], str)
        or not re.fullmatch(r"[1-9a-f][0-9a-f]{0,15}", value["fsid"])
    ):
        return False
    handle = value["handle"]
    return (
        type(handle) is dict
        and set(handle) == {"type", "bytes"}
        and type(handle["type"]) is int
        and -(2**31) <= handle["type"] < 2**31
        and isinstance(handle["bytes"], str)
        and re.fullmatch(r"(?:[0-9a-f]{2}){1,4096}", handle["bytes"]) is not None
    )


def _record(raw, ident, *, studio):
    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_json_object,
        parse_constant=_invalid_constant,
    )
    _require(type(value) is dict and value.get("id") == ident)
    if studio:
        keys = {"version", "role", "id", "project", "project_identity"}
        _require(set(value) in (keys, keys | {_STABLE}))
        _require(
            type(value["version"]) is int
            and value["version"] == 1
            and value["role"] == ROLE
        )
    _require(type(value.get("project")) is str)
    project = _path(value["project"])
    if studio or "project_identity" in value:
        _require(_valid_identity(value.get("project_identity")))
    if _STABLE in value:
        _require("project_identity" in value and _valid_stable(value[_STABLE]))
    return project, {
        key: value[key] for key in ("project_identity", _STABLE) if key in value
    }


def _scan(state, namespace, *, studio):
    """Bound active peers, including stale paths. Never consult retired history."""
    with _directory(
        state, namespace, private=0o700 if studio else None, optional=not studio
    ) as root:
        if root is None:
            return None, {}
        root_stamp = _stamp(os.fstat(root))
        records = {}
        with os.scandir(root) as entries:
            for entry in entries:
                ident = entry.name
                _require(len(records) < _MAX_ENTRIES and _ID.fullmatch(ident))
                with _directory(
                    root, ident, private=0o700 if studio else None
                ) as folder:
                    folder_stamp = _stamp(os.fstat(folder))
                    raw, file_stamp = _read(
                        folder,
                        RECORD_NAME if studio else "workspace.json",
                        _MAX_RECORD,
                        private=0o600 if studio else None,
                    )
                    _require(_entry(root, ident) == folder_stamp)
                    project, identity = _record(raw, ident, studio=studio)
                    records[ident] = project, identity, folder_stamp, file_stamp
        _require(_entry(state, namespace) == root_stamp == _stamp(os.fstat(root)))
        return root_stamp, records


def _overlaps(a, b):
    return a.is_relative_to(b) or b.is_relative_to(a)


def _protect(project, state):
    home = Path.home()
    _require(project not in (Path("/"), Path("/home"), home, home.resolve()), _OVERLAP)
    _require(not any(char in str(project) for char in ":,\n\r"))
    private = [
        state,
        paths.source_root(Path(__file__).parent),
        paths.web_tls_home(),
        vault.vault_path().parent,
        home / ".ssh",
        home / ".gnupg",
        *(Path(name) for name in ("/dev", "/proc", "/sys", "/run")),
    ]
    for kind, default in (
        ("CONFIG", ".config"),
        ("DATA", ".local/share"),
        ("STATE", ".local/state"),
        ("CACHE", ".cache"),
    ):
        bases = (
            home / default,
            Path(os.environ.get(f"XDG_{kind}_HOME") or home / default),
        )
        private.extend(base / app for base in bases for app in ("oc-box", "opencode"))
    for root in private:
        for protected in (root.expanduser().absolute(), root.expanduser().resolve()):
            _require(not _overlaps(project, protected), _OVERLAP)


def _marker(project_fd):
    raw, stamp = _read(project_fd, MARKER_NAME, len(MARKER_PREFIX) + 32 + 1)
    match = _MARKER.fullmatch(raw)
    _require(match is not None)
    return match[1].decode("ascii"), stamp


def select(state_root, *, project=None, workspace_id=None):
    """Require one absolute no-link project path or raw 32-hex Studio ID.

    Both paths bind the same marker, role, private record and VFS identity.
    No lookup can fall back to a legacy workspace or to unregistered state.
    """
    _require(
        os.environ.get("OC_BOX_CONTAINER") != "1",
        "Studio registry selection is host-only.",
    )
    _require((project is None) != (workspace_id is None))
    try:
        state = _path(state_root)
        with ExitStack() as stack:
            state_fd = stack.enter_context(_absolute_directory(state))
            _owned(os.fstat(state_fd))
            if project is not None:
                project = _path(project)
                _protect(project, state)
                project_fd = stack.enter_context(_absolute_directory(project))
                workspace_id, marker_stamp = _marker(project_fd)
            else:
                _require(isinstance(workspace_id, str) and _ID.fullmatch(workspace_id))
            studio = _scan(state_fd, NAMESPACE, studio=True)
            _require(workspace_id in studio[1])
            recorded_project, recorded_identity, _, _ = studio[1][workspace_id]
            if project is None:
                project = recorded_project
                _protect(project, state)
                project_fd = stack.enter_context(_absolute_directory(project))
                marker_id, marker_stamp = _marker(project_fd)
                _require(marker_id == workspace_id)
            _require(project == recorded_project)
            identity, stable = workspace_identity.snapshot_fd(project_fd)
            _require(workspace_identity.matches(recorded_identity, identity, stable))

            legacy = _scan(state_fd, "workspaces", studio=False)
            for entries, own in ((legacy[1], None), (studio[1], workspace_id)):
                for ident, (peer, info, _, _) in entries.items():
                    if ident != own:
                        _require(
                            ident != workspace_id
                            and not _overlaps(peer, project)
                            and info.get("project_identity") != identity
                            and not workspace_identity.matches(info, identity, stable),
                            _OVERLAP,
                        )

            # Do not silently retry or accept an observed replacement.
            _require(studio == _scan(state_fd, NAMESPACE, studio=True))
            _require(legacy == _scan(state_fd, "workspaces", studio=False))
            _require(_entry(project_fd, MARKER_NAME) == marker_stamp)
            for path, fd in ((project, project_fd), (state, state_fd)):
                with _absolute_directory(path) as current:
                    _require(_inode(os.fstat(current)) == _inode(os.fstat(fd)))
            _owned(os.fstat(state_fd))
            return StudioWorkspace(workspace_id, project, tuple(identity))
    except RegistryError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise RegistryError(_UNVERIFIED) from None
