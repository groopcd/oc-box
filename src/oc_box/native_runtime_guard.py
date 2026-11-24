"""Host-only write fence and rejection receipt for a workspace's native runtime.

The caller holds the workspace lock during publication and verifies private
resource identity, candidate admission and actual native readiness. This store
is not a compatibility check, rollback mechanism or authority to run a binary.
Descriptor and namespace checks reject observed substitution; they do not
isolate state from a malicious same-user process disregarding the lock.
The independent receipt remembers one confirmed rejection, never a permission
to run a different executable or weaken the positive fence.
"""

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import stat


_NAME = "native-runtime-guard.json"
_REJECTION_NAME = "native-runtime-rejection.json"
_REGISTRY = "workspace.json"
_MAX_RECORD = 1024
_MAX_REGISTRY = 1024 * 1024
_WORKSPACE_ID = re.compile(r"[0-9a-f]{32}")
_HEX_256 = re.compile(r"[0-9a-f]{64}")
_VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_VERSION = re.compile(r"2\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_FIELDS = ("container", "volume", "version", "digest", "ready")
_REJECTION_FIELDS = ("version", "host_digest", "probe_digest", "category")
_REJECTION_CATEGORIES = frozenset({"build-plugin-failed", "readiness-contract"})
_MESSAGES = {
    "invalid-path": "Native runtime guard requires a workspace registry path.",
    "invalid-registration": "Native runtime guard requires a valid workspace registration.",
    "unsafe-storage": "Native runtime guard storage is unsafe or unavailable.",
    "invalid-record": "Native runtime guard record is invalid or unsupported.",
    "conflict": "Native runtime guard state changed; retry preflight.",
    "invalid-transition": "Native runtime guard refuses this runtime state change.",
    "publish-unverified": "Native runtime guard save was not verified; rerun preflight before using the workspace.",
}


class GuardError(ValueError):
    """Fixed, secret-free message and category."""

    def __init__(self, category):
        self.category = (
            category
            if isinstance(category, str) and category in _MESSAGES
            else "unsafe-storage"
        )
        super().__init__(_MESSAGES[self.category])


@dataclass(frozen=True, slots=True)
class Record:
    container: str
    volume: str
    version: str
    digest: str
    ready: bool


@dataclass(frozen=True, slots=True)
class Rejection:
    version: str
    host_digest: str
    probe_digest: str
    category: str


def _match(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _validate_record(record):
    if type(record) is not Record or type(record.ready) is not bool:
        raise GuardError("invalid-record")
    for field, pattern in (
        (record.container, _HEX_256),
        (record.volume, _VOLUME),
        (record.version, _VERSION),
        (record.digest, _HEX_256),
    ):
        if not _match(field, pattern):
            raise GuardError("invalid-record")


def _validate_rejection(record):
    if (
        type(record) is not Rejection
        or not _match(record.version, _VERSION)
        or not _match(record.host_digest, _HEX_256)
        or not _match(record.probe_digest, _HEX_256)
        or type(record.category) is not str
        or record.category not in _REJECTION_CATEGORIES
    ):
        raise GuardError("invalid-record")


_SPECS = {
    Record: (_NAME, _FIELDS, _validate_record),
    Rejection: (_REJECTION_NAME, _REJECTION_FIELDS, _validate_rejection),
}


def _decode(raw, category):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (ValueError, RecursionError):
        raise GuardError(category) from None


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _file_info(info, *, private):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or (stat.S_IMODE(info.st_mode) != 0o600 if private else info.st_mode & 0o022)
    ):
        raise GuardError("unsafe-storage")


def _entry(directory, name):
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _require_entry(directory, name, stamp, category):
    current = _entry(directory, name)
    if current is not None:
        _file_info(current, private=name != _REGISTRY)
    if (None if current is None else _stamp(current)) != stamp:
        raise GuardError(category)


def _read(directory, name, *, maximum, category):
    observed = _entry(directory, name)
    if observed is None:
        return None, None
    _file_info(observed, private=name != _REGISTRY)
    if observed.st_size > maximum:
        raise GuardError(category)
    stamp = _stamp(observed)
    fd = os.open(name, _READ_FLAGS, dir_fd=directory)
    try:
        if _stamp(os.fstat(fd)) != stamp:
            raise GuardError("unsafe-storage")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > maximum or len(raw) != observed.st_size:
            raise GuardError(category)
        if _stamp(os.fstat(fd)) != stamp:
            raise GuardError("unsafe-storage")
        _require_entry(directory, name, stamp, "unsafe-storage")
        return raw, stamp
    finally:
        os.close(fd)


def _open_parent(path):
    # Public outer directories such as /home or /tmp are allowed. Do not follow
    # links at any component; the actual registry directory must be owner-writable
    # only (including the legitimate legacy 0755 mode).
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise GuardError("unsafe-storage")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _require_workspace(path, directory, registration):
    current = _open_parent(path)
    try:
        pinned, named = os.fstat(directory), os.fstat(current)
        if (pinned.st_dev, pinned.st_ino) != (named.st_dev, named.st_ino):
            raise GuardError("unsafe-storage")
        _require_entry(directory, _REGISTRY, registration, "unsafe-storage")
    finally:
        os.close(current)


@contextmanager
def _workspace(path):
    try:
        path = Path(path).absolute()
        if (
            path.anchor != "/"
            or path.name != _REGISTRY
            or not _WORKSPACE_ID.fullmatch(path.parent.name)
            or ".." in path.parts
            or "\0" in str(path)
            or len(os.fsencode(path)) > 4096
        ):
            raise GuardError("invalid-path")
    except GuardError:
        raise
    except (OSError, ValueError, TypeError):
        raise GuardError("invalid-path") from None

    try:
        directory = _open_parent(path)
        try:
            raw, registration = _read(
                directory,
                _REGISTRY,
                maximum=_MAX_REGISTRY,
                category="invalid-registration",
            )
            value = None if raw is None else _decode(raw, "invalid-registration")
            if not isinstance(value, dict) or value.get("id") != path.parent.name:
                raise GuardError("invalid-registration")
            yield path, directory, registration
        finally:
            os.close(directory)
    except OSError:
        raise GuardError("unsafe-storage") from None


def _load(directory, workspace, kind):
    name, fields, validate = _SPECS[kind]
    raw, stamp = _read(directory, name, maximum=_MAX_RECORD, category="invalid-record")
    if raw is None:
        return None, None
    value = _decode(raw, "invalid-record")
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "workspace", *fields}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["workspace"] != workspace
    ):
        raise GuardError("invalid-record")
    record = kind(*(value[field] for field in fields))
    validate(record)
    return record, stamp


def _read_sidecar(path, kind):
    name, _, _ = _SPECS[kind]
    with _workspace(path) as (path, directory, registration):
        record, stamp = _load(directory, path.parent.name, kind)
        _require_workspace(path, directory, registration)
        _require_entry(directory, name, stamp, "conflict")
        return record


def load(path: str | os.PathLike) -> Record | None:
    """Read the workspace's fence, or return None for an absent sidecar only."""
    return _read_sidecar(path, Record)


def rejected(path: str | os.PathLike) -> Rejection | None:
    """Read the last rejection receipt, or None for an absent receipt only."""
    return _read_sidecar(path, Rejection)


def _transition(previous, record):
    if previous is None:
        if record.ready:
            raise GuardError("invalid-transition")
        return
    version = tuple(map(int, record.version.split(".")))
    old_version = tuple(map(int, previous.version.split(".")))
    if (
        (record.container, record.volume) != (previous.container, previous.volume)
        or version < old_version
        or (version == old_version and record.digest != previous.digest)
        or (record.ready and (previous.ready or version != old_version))
    ):
        raise GuardError("invalid-transition")


def _write(path, directory, registration, stamp, record):
    name, fields, _ = _SPECS[type(record)]
    raw = (
        json.dumps(
            {
                "schema": 1,
                "workspace": path.parent.name,
                **{field: getattr(record, field) for field in fields},
            },
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    temporary = "." + name.removesuffix(".json") + "-" + secrets.token_hex(16) + ".tmp"
    fd = None
    renamed = False
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        os.fchmod(fd, 0o600)
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise GuardError("publish-unverified")
            remaining = remaining[written:]
        info = os.fstat(fd)
        _file_info(info, private=True)
        if len(raw) != info.st_size:
            raise GuardError("publish-unverified")
        staged = _stamp(info)
        os.fsync(fd)
        _require_workspace(path, directory, registration)
        _require_entry(directory, temporary, staged, "unsafe-storage")
        _require_entry(directory, name, stamp, "conflict")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        renamed = True
        os.fsync(directory)
        _require_workspace(path, directory, registration)
        saved = _stamp(os.fstat(fd))
        # Rename updates ctime. Every other property of the staged inode must
        # still match, then its complete new stamp must match the canonical name.
        if staged[:-1] != saved[:-1]:
            raise GuardError("publish-unverified")
        _require_entry(directory, name, saved, "publish-unverified")
    except OSError:
        raise GuardError("publish-unverified") from None
    except GuardError:
        if renamed:
            raise GuardError("publish-unverified") from None
        raise
    finally:
        if fd is not None:
            if not renamed:
                # Clean up only this still-named inode, not an abandoned writer
                # or a substituted name. The FD pins the inode through cleanup.
                with suppress(OSError):
                    named = _entry(directory, temporary)
                    own = os.fstat(fd)
                    if named is not None and (
                        named.st_dev,
                        named.st_ino,
                        named.st_nlink,
                    ) == (own.st_dev, own.st_ino, 1):
                        os.unlink(temporary, dir_fd=directory)
            os.close(fd)


def _publish(path, previous, record, kind):
    _, _, validate = _SPECS[kind]
    validate(record)
    if previous is not None:
        validate(previous)
    with _workspace(path) as (path, directory, registration):
        current, stamp = _load(directory, path.parent.name, kind)
        if current != previous:
            raise GuardError("conflict")
        if kind is Record:
            _transition(current, record)
        _write(path, directory, registration, stamp, record)


def publish(path: str | os.PathLike, previous: Record | None, record: Record) -> None:
    """Durably compare-and-set under the caller's existing workspace lock.

    Publish pending before live mutation, including retries of the same version
    and digest. Publish ready only from that exact pending record and only once
    real readiness is verified. Any exception means no authorization to proceed;
    a failed directory fsync can leave the new value visible but unconfirmed.
    """
    _publish(path, previous, record, Record)


def publish_rejection(
    path: str | os.PathLike, previous: Rejection | None, record: Rejection
) -> None:
    """Durably remember one confirmed artifact rejection under the workspace lock.

    Replacement has no monotonic version semantics: the receipt is only a cache
    of the last candidate result. It never writes, clears or requires a fence.
    """
    _publish(path, previous, record, Rejection)
